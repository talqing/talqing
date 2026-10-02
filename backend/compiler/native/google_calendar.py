"""Google Calendar tools, implemented over the Calendar REST API v3.

The tool surface is Google's own (``google_calendar_schemas``); everything here
is the translation from it onto ``calendar/v3``. Nothing in this module knows
about MCP: the toolset it builds is a list of raw-schema function tools, which
is the same shape LiveKit's MCP client produces, so approval, namespacing and
dispatch downstream are unchanged.

Their arguments are a flattened, renamed abstraction over the Event resource
rather than a pass-through, and four of the renames hide semantics:

- `availability` is `transparency`, with different values.
- `guestPermissions.*` are three top-level booleans, one of them renamed.
- `notificationLevel` is the `sendUpdates` **query parameter**, whose REST
  default is `none` — which is how an agent books a meeting and tells nobody.
- `addedAttendees` / `removedAttendeeEmails` (and the attachment pair) promise a
  delta that `PATCH` has no way to express: it replaces an array wholesale.
  Verified live — patching one attendee onto a two-guest event deletes the
  other. Those, and `respond_to_event`, read the event first and send the whole
  merged array back.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import UTC, datetime, time, timedelta
from typing import Any
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from livekit.agents import llm
from livekit.agents.llm import ToolError, function_tool

from services.tools import exposed_tool_name

from .google_calendar_schemas import TOOL_SCHEMAS

logger = logging.getLogger("talqing.compiler.native.google_calendar")

CALENDAR_API = "https://www.googleapis.com/calendar/v3"

# Google's guidance for 429/5xx is truncated exponential backoff with jitter.
# The budget is deliberately small: this runs inside a turn a caller is waiting
# out, and one integration's retry storm is what would take the shared project
# quota down for every tenant at once.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_RETRY_ATTEMPTS = 3
_RETRY_CAP = 1.5

_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=5.0)

# `search_events` turns one tool call into one request per calendar. A heavy
# account has a handful; the cap is what keeps a subscribed-calendar collector
# from spending a per-user minute of quota on a single question.
_SEARCH_CALENDAR_LIMIT = 20
_SEARCH_PAGE_SIZE_DEFAULT = 25
_SEARCH_PAGE_SIZE_MAX = 50
_SEARCH_WINDOW_PAST = timedelta(days=30)
_SEARCH_WINDOW_FUTURE = timedelta(days=365)

# freebusy.query refuses more calendars than this in one request.
_FREEBUSY_CALENDAR_LIMIT = 50

_HOUR_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# Argument value → what Calendar REST calls it. Google's tool schemas are
# generated from protos and carry SCREAMING_SNAKE enum members; the REST
# parameters and body fields are lowerCamel, and passing one through as the
# other is a 400 the model cannot read.
_AVAILABILITY = {"AVAILABILITY_BUSY": "opaque", "AVAILABILITY_FREE": "transparent"}
_SEND_UPDATES = {"NONE": "none", "EXTERNAL_ONLY": "externalOnly", "ALL": "all"}
_EVENT_TYPE = {
    "DEFAULT": "default",
    "OUT_OF_OFFICE": "outOfOffice",
    "FOCUS_TIME": "focusTime",
    "WORKING_LOCATION": "workingLocation",
    "BIRTHDAY": "birthday",
    "FROM_GMAIL": "fromGmail",
}
_WORKING_LOCATION_TYPE = {"HOME_OFFICE": "homeOffice", "CUSTOM_LOCATION": "customLocation"}
_ORDER_BY = {"default": None, "startTime": "startTime", "lastModified": "updated"}

_WRITE_TOOLS = frozenset({"create_event", "update_event", "delete_event", "respond_to_event"})

# Where a REST error points, said in the caller's vocabulary. Only the renames
# are listed — a field we did not rename needs no translation.
_REST_FIELD_TO_ARGUMENT = {
    "transparency": "availability",
    "recurrence": "recurrenceData",
    "reminders": "overrideReminders",
    "guestsCanSeeOtherGuests": "guestPermissions.guestsCanSeeGuests",
    "eventTypes": "eventType",
    "sendUpdates": "notificationLevel",
    "maxResults": "pageSize",
    "timeMin": "startTime",
    "timeMax": "endTime",
    "q": "fullText",
    "start": "startTime",
    "end": "endTime",
}


class _CalendarClient:
    """One HTTP/2 connection pool to `calendar/v3`, bearing one tenant's token.

    ``auth`` mints the bearer per request and owns the 401 → refresh → retry →
    `needs_reconnect` path, so nothing below ever handles a token.
    """

    def __init__(self, auth: httpx.Auth) -> None:
        self._http = httpx.AsyncClient(
            base_url=CALENDAR_API,
            auth=auth,
            http2=True,
            timeout=_HTTP_TIMEOUT,
            follow_redirects=True,
        )
        # Only read when a tool needs a zone and the caller named none. One
        # lookup per compiled agent, not per call.
        self._primary_time_zone: str | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        tool: str,
        calendar_id: str = "primary",
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One Calendar API call, retried on 429/5xx, errors raised as ``ToolError``."""
        last: httpx.Response | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                response = await self._http.request(method, path, params=params, json=json)
            except httpx.HTTPError as exc:
                if attempt == _RETRY_ATTEMPTS - 1:
                    raise ToolError(f"Could not reach Google Calendar: {exc}") from exc
                await asyncio.sleep(_backoff(attempt, None))
                continue
            last = response
            if response.status_code not in _RETRY_STATUSES:
                break
            if attempt < _RETRY_ATTEMPTS - 1:
                await asyncio.sleep(_backoff(attempt, response.headers.get("Retry-After")))

        assert last is not None
        if last.status_code >= 400:
            _raise_calendar_error(last, tool=tool, calendar_id=calendar_id)
        if last.status_code == 204 or not last.content:
            return {}
        return last.json()

    async def primary_time_zone(self, *, tool: str) -> str:
        """The user's own time zone, read off their calendar list.

        From `calendarList.list` rather than the more obvious
        `calendars.get('primary')`, because that method takes none of our four
        scopes — it wants `calendar.calendars.readonly`, and asking for a fifth
        scope to read one string would be a consent change for every tenant.
        """
        if self._primary_time_zone is None:
            body = await self.request(
                "GET", "/users/me/calendarList", tool=tool, params={"maxResults": 250}
            )
            zone = next(
                (
                    item.get("timeZone")
                    for item in body.get("items") or []
                    if item.get("primary") and item.get("timeZone")
                ),
                None,
            )
            if not isinstance(zone, str) or not zone:
                raise ToolError(
                    "Could not read the user's time zone from Google Calendar. Pass `timeZone`."
                )
            self._primary_time_zone = zone
        return self._primary_time_zone


def _backoff(attempt: int, retry_after: str | None) -> float:
    if retry_after and retry_after.isdigit():
        return min(float(retry_after), _RETRY_CAP)
    return min(0.3 * (2**attempt), _RETRY_CAP) * (0.5 + random.random())


def _raise_calendar_error(response: httpx.Response, *, tool: str, calendar_id: str) -> None:
    """Turn a Calendar API failure into something a model can act on.

    Google answers with `{"error": {"code", "message", "errors": [{"reason",
    "location"}]}}`, whose `location` is a REST parameter or body field. Naming
    the argument the model actually sent is the difference between a tool call
    it can correct and one it can only repeat.
    """
    body: Any = None
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    message = ""
    location = ""
    reason = ""
    if isinstance(error, dict):
        message = str(error.get("message") or "").strip()
        details = error.get("errors")
        if isinstance(details, list) and details and isinstance(details[0], dict):
            location = str(details[0].get("location") or "").strip()
            reason = str(details[0].get("reason") or "").strip()
    if not message:
        message = response.reason_phrase or f"HTTP {response.status_code}"

    status = response.status_code
    # Google answers a write to a calendar the account does not own with either
    # 403 or 404 depending on how much of it the account can see, and neither
    # body says which calendar or why. Both are the `calendar.events.owned`
    # scope, and it is the one limitation of this integration a model can route
    # around on its own — by picking a calendar `list_calendars` calls `owner`.
    ownership = (
        " Events can only be created, changed or deleted on calendars the connected Google "
        "account owns — `list_calendars` reports `accessRole` for each."
        if tool in _WRITE_TOOLS
        else ""
    )
    if status == 401:
        raise ToolError("Google Calendar is no longer connected. Reconnect it in Talqing.")
    if status == 403:
        raise ToolError(f"Google Calendar refused the request: {message}.{ownership}")
    if status == 404:
        raise ToolError(
            f"Google Calendar found nothing matching that on calendar "
            f"'{calendar_id}': {message}.{ownership}"
        )
    if status == 410:
        raise ToolError("That event has already been deleted.")
    if status == 429:
        raise ToolError("Google Calendar is rate-limiting this account. Try again in a moment.")

    argument = _REST_FIELD_TO_ARGUMENT.get(location.split(".")[0], location)
    if argument:
        raise ToolError(f"Google Calendar rejected `{argument}`: {message}")
    logger.warning("%s failed: %s %s (%s)", tool, status, message, reason or "no reason")
    raise ToolError(f"Google Calendar rejected the request: {message}")


# ── Argument normalisation ─────────────────────────────────────────────────


def _prune(value: Any) -> Any:
    """``value`` with every empty leaf removed, recursively.

    Models fill optional parameters with empty strings, and an empty string is
    not what any of these arguments mean. It is also not harmless: `timeMin=""`
    is a bare `400 Bad Request` from Calendar, and `pageToken=""` was an opaque
    `Precondition check failed.` from Google's own MCP server. Done once, here,
    rather than per tool.

    `false` and `0` survive — they are answers, not absences.
    """
    if isinstance(value, dict):
        return {k: v for k, v in ((k, _prune(v)) for k, v in value.items()) if not _is_empty(v)}
    if isinstance(value, list):
        return [v for v in (_prune(v) for v in value) if not _is_empty(v)]
    if isinstance(value, str):
        return value.strip()
    return value


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str | list | dict) and not value)


def _string(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError(f"`{key}` must be a string.")
    return value


def _required_string(args: dict[str, Any], key: str) -> str:
    value = _string(args, key)
    if not value:
        raise ToolError(f"`{key}` is required.")
    return value


def _bool(args: dict[str, Any], key: str) -> bool | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ToolError(f"`{key}` must be true or false.")
    return value


def _int(args: dict[str, Any], key: str, *, maximum: int | None = None) -> int | None:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"`{key}` must be a whole number.")
    if value < 1:
        raise ToolError(f"`{key}` must be greater than zero.")
    return min(value, maximum) if maximum is not None else value


def _list_of(args: dict[str, Any], key: str, kind: type) -> list[Any]:
    value = args.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ToolError(f"`{key}` must be a list.")
    for item in value:
        if not isinstance(item, kind):
            raise ToolError(
                f"`{key}` must be a list of {'objects' if kind is dict else 'strings'}."
            )
    return value


def _enum(args: dict[str, Any], key: str, mapping: dict[str, Any]) -> Any:
    value = _string(args, key)
    if value is None:
        return None
    if value not in mapping:
        allowed = ", ".join(f"`{v}`" for v in mapping)
        raise ToolError(f"`{key}` must be one of {allowed}.")
    return mapping[value]


def _rfc3339(value: str) -> datetime:
    """Parse a timestamp Google sent us. Malformed means our reading is wrong."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _timestamp(value: str, *, argument: str) -> datetime:
    """Parse one of the caller's ISO 8601 arguments, or say which one was wrong."""
    try:
        return _rfc3339(value)
    except ValueError as exc:
        raise ToolError(
            f"`{argument}` is not a valid ISO 8601 timestamp — expected something like "
            f"`2026-04-30T10:00:00+05:30`, got `{value}`."
        ) from exc


def _zone(name: str, *, argument: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ToolError(
            f"`{argument}` is not an IANA time zone name — expected something like "
            f"`Asia/Kolkata`, got `{name}`."
        ) from exc


def _event_time(
    value: str, *, argument: str, time_zone: str | None, all_day: bool
) -> dict[str, str]:
    """One `start` / `end` on the Event resource.

    Two rules of Google's that a caller cannot see: an all-day event carries a
    `date` and never a `dateTime`, and a `dateTime` without a UTC offset is
    rejected unless a `timeZone` rides with it — which is also what makes
    `timeZone` "override the offset", since we then send the wall clock alone.
    """
    parsed = _timestamp(value, argument=argument)
    if all_day:
        return {"date": parsed.date().isoformat()}
    if time_zone:
        return {"dateTime": parsed.replace(tzinfo=None).isoformat(), "timeZone": time_zone}
    if parsed.tzinfo is None:
        raise ToolError(
            f"`{argument}` needs a UTC offset (for example `2026-04-30T10:00:00+05:30`), or set "
            "`timeZone` to say which zone the time is in."
        )
    return {"dateTime": parsed.isoformat()}


def _attendee_body(entry: dict[str, Any], *, argument: str) -> dict[str, Any]:
    email = entry.get("email")
    if not isinstance(email, str) or not email.strip():
        raise ToolError(f"every entry in `{argument}` needs an `email`.")
    body: dict[str, Any] = {"email": email.strip()}
    for source, target in (
        ("displayName", "displayName"),
        ("optionalAttendee", "optional"),  # the one attendee field Google renamed
        ("additionalGuests", "additionalGuests"),
        ("resource", "resource"),
        ("responseStatus", "responseStatus"),
    ):
        if entry.get(source) is not None:
            body[target] = entry[source]
    return body


def _merged_attendees(
    args: dict[str, Any], *, objects_key: str, emails_key: str
) -> list[dict[str, Any]]:
    """The two attendee arguments as one list, in the order they were given.

    Google offers both a deprecated list of bare addresses and a list of objects
    and does not say which wins, so both are honoured and an address named twice
    keeps the richer entry.
    """
    merged: dict[str, dict[str, Any]] = {}
    for entry in _list_of(args, objects_key, dict):
        body = _attendee_body(entry, argument=objects_key)
        merged[body["email"].lower()] = body
    for email in _list_of(args, emails_key, str):
        merged.setdefault(email.strip().lower(), {"email": email.strip()})
    return list(merged.values())


def _conference_body(args: dict[str, Any]) -> dict[str, Any] | None:
    """`addGoogleMeetUrl` / `googleMeetUrl` as `conferenceData`.

    The explicit URL wins, as Google's own description says. A create request
    gets a fresh `requestId` every time: Google warns that reusing one can
    expose an existing meeting's details to people who were never in it.
    """
    existing = _string(args, "googleMeetUrl")
    if existing:
        return {
            "conferenceSolution": {"key": {"type": "hangoutsMeet"}},
            "entryPoints": [{"entryPointType": "video", "uri": existing}],
        }
    if _bool(args, "addGoogleMeetUrl"):
        return {
            "createRequest": {
                "requestId": uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
    return None


def _shared_event_body(args: dict[str, Any]) -> dict[str, Any]:
    """The Event fields `create_event` and `update_event` map identically."""
    body: dict[str, Any] = {}
    for key in ("summary", "description", "location", "colorId"):
        value = _string(args, key)
        if value is not None:
            body[key] = value
    visibility = _enum(
        args, "visibility", {"default": "default", "public": "public", "private": "private"}
    )
    if visibility is not None:
        body["visibility"] = visibility
    availability = _enum(args, "availability", _AVAILABILITY)
    if availability is not None:
        body["transparency"] = availability

    permissions = args.get("guestPermissions")
    if permissions is not None:
        if not isinstance(permissions, dict):
            raise ToolError("`guestPermissions` must be an object.")
        # The nested object is flattened away on the Event resource, and one of
        # the three is also renamed.
        for source, target in (
            ("guestsCanInviteOthers", "guestsCanInviteOthers"),
            ("guestsCanModify", "guestsCanModify"),
            ("guestsCanSeeGuests", "guestsCanSeeOtherGuests"),
        ):
            if permissions.get(source) is not None:
                body[target] = permissions[source]

    reminders = _list_of(args, "overrideReminders", dict)
    if reminders:
        # `useDefault` must be cleared explicitly, or the calendar's own
        # reminders stay in play alongside these.
        body["reminders"] = {"useDefault": False, "overrides": reminders}

    conference = _conference_body(args)
    if conference is not None:
        body["conferenceData"] = conference
    return body


def _time_fields(
    args: dict[str, Any], *, start: str | None, end: str | None, all_day: bool
) -> dict[str, Any]:
    time_zone = _string(args, "timeZone")
    body: dict[str, Any] = {}
    if start is not None:
        body["start"] = _event_time(
            start, argument="startTime", time_zone=time_zone, all_day=all_day
        )
    if end is not None:
        body["end"] = _event_time(end, argument="endTime", time_zone=time_zone, all_day=all_day)
    if (
        all_day
        and "start" in body
        and "end" in body
        and body["end"]["date"] <= body["start"]["date"]
    ):
        raise ToolError(
            "`endTime` must be after `startTime`. All-day end dates are exclusive, so a "
            f"one-day event on {body['start']['date']} ends the next day."
        )
    return body


def _send_updates(args: dict[str, Any], *, has_attendees: bool) -> str:
    """`notificationLevel` → the `sendUpdates` query parameter.

    REST defaults this to `none`, which is how an agent books a meeting and
    never tells the guests. When the model says nothing we say `all` for an
    event with guests — Google's own reference warns that `none` "can have
    significant adverse effects, including events not syncing to external
    calendars" — and `none` for one with nobody to notify.
    """
    chosen = _enum(args, "notificationLevel", _SEND_UPDATES)
    if chosen is not None:
        return str(chosen)
    return "all" if has_attendees else "none"


def _clean_event(event: dict[str, Any]) -> dict[str, Any]:
    """The Event as the model should read it: everything but the wire noise."""
    return {k: v for k, v in event.items() if k not in ("kind", "etag")}


# ── Tool handlers ──────────────────────────────────────────────────────────


async def _list_calendars(client: _CalendarClient, args: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {"maxResults": _int(args, "pageSize", maximum=250) or 100}
    page_token = _string(args, "pageToken")
    if page_token:
        params["pageToken"] = page_token
    body = await client.request(
        "GET", "/users/me/calendarList", tool="list_calendars", params=params
    )
    calendars = [
        {
            "id": item.get("id"),
            "summary": item.get("summaryOverride") or item.get("summary"),
            "description": item.get("description"),
            "timeZone": item.get("timeZone"),
            # What the user may do here. `calendar.events.owned` writes only to
            # calendars they own, so this is what stops the model attempting a
            # booking that can only come back 403.
            "accessRole": item.get("accessRole"),
            "primary": bool(item.get("primary")),
        }
        for item in body.get("items") or []
    ]
    result: dict[str, Any] = {"calendars": [_prune(c) for c in calendars]}
    if body.get("nextPageToken"):
        result["nextPageToken"] = body["nextPageToken"]
    return result


def _event_type_params(args: dict[str, Any]) -> list[str]:
    values = [*_list_of(args, "eventType", str), *_list_of(args, "eventTypeFilter", str)]
    mapped: list[str] = []
    for value in values:
        rest = _EVENT_TYPE.get(value)
        if rest is None:
            allowed = ", ".join(f"`{v}`" for v in _EVENT_TYPE)
            raise ToolError(f"`eventType` must contain only {allowed} — got `{value}`.")
        if rest not in mapped:
            mapped.append(rest)
    return mapped


async def _list_events(client: _CalendarClient, args: dict[str, Any]) -> dict[str, Any]:
    calendar_id = _string(args, "calendarId") or "primary"
    params: dict[str, Any] = {
        # Recurring series are expanded into occurrences. It is what a person
        # means by "my events on Tuesday", and `orderBy=startTime` is only legal
        # with it.
        "singleEvents": "true",
        "maxResults": _int(args, "pageSize", maximum=250) or 100,
    }
    for source, target in (
        ("startTime", "timeMin"),
        ("endTime", "timeMax"),
        ("timeZone", "timeZone"),
        ("fullText", "q"),
        ("pageToken", "pageToken"),
    ):
        value = _string(args, source)
        if value:
            if target in ("timeMin", "timeMax"):
                _timestamp(value, argument=source)
            params[target] = value
    order_by = _enum(args, "orderBy", _ORDER_BY)
    if order_by is not None:
        params["orderBy"] = order_by
    event_types = _event_type_params(args)
    if event_types:
        params["eventTypes"] = event_types

    body = await client.request(
        "GET",
        f"/calendars/{_path(calendar_id)}/events",
        tool="list_events",
        calendar_id=calendar_id,
        params=params,
    )
    result: dict[str, Any] = {
        "summary": body.get("summary"),
        "description": body.get("description"),
        "updated": body.get("updated"),
        "timeZone": body.get("timeZone"),
        "accessRole": body.get("accessRole"),
        "defaultReminders": body.get("defaultReminders"),
        "events": [_clean_event(e) for e in body.get("items") or []],
    }
    if body.get("nextPageToken"):
        result["nextPageToken"] = body["nextPageToken"]
    # `events` stays even when empty — an absent key and an empty list read the
    # same to a person and differently to a model.
    return {k: v for k, v in result.items() if v is not None or k == "events"}


async def _search_events(client: _CalendarClient, args: dict[str, Any]) -> dict[str, Any]:
    """Keyword search over every calendar the user has, merged by start time.

    Google's tool searches the primary calendar only, which makes it a slower
    `list_events` with `fullText`. Fanning out is what makes it a different
    tool: "when is my dentist appointment" finds the event wherever it lives.

    The cost is one request per calendar against a 600/minute/user quota, so the
    fan-out is capped and concurrent — in series a caller would hear every hop.
    """
    query = _required_string(args, "query")
    limit = _int(args, "pageSize", maximum=_SEARCH_PAGE_SIZE_MAX) or _SEARCH_PAGE_SIZE_DEFAULT
    now = datetime.now(UTC)
    window = {
        "timeMin": (now - _SEARCH_WINDOW_PAST).isoformat(),
        "timeMax": (now + _SEARCH_WINDOW_FUTURE).isoformat(),
    }

    calendars = await client.request(
        "GET",
        "/users/me/calendarList",
        tool="search_events",
        params={"maxResults": _SEARCH_CALENDAR_LIMIT},
    )
    entries = [
        (str(item["id"]), str(item.get("summaryOverride") or item.get("summary") or item["id"]))
        for item in calendars.get("items") or []
        if item.get("id")
    ]

    async def _search_one(calendar_id: str) -> list[dict[str, Any]]:
        body = await client.request(
            "GET",
            f"/calendars/{_path(calendar_id)}/events",
            tool="search_events",
            calendar_id=calendar_id,
            params={
                "q": query,
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": limit,
                **window,
            },
        )
        return list(body.get("items") or [])

    pages = await asyncio.gather(
        *(_search_one(calendar_id) for calendar_id, _ in entries), return_exceptions=True
    )

    found: list[dict[str, Any]] = []
    failures: list[BaseException] = []
    for (calendar_id, name), page in zip(entries, pages, strict=True):
        if isinstance(page, BaseException):
            # One unreadable calendar must not lose the hits from the others —
            # a subscribed calendar can disappear between the two calls.
            logger.warning("search_events: skipped calendar %s (%s)", calendar_id, page)
            failures.append(page)
            continue
        # `calendarId` rides along because every follow-up tool takes one and a
        # hit from a shared calendar is not on `primary`.
        found.extend(
            {**_clean_event(event), "calendarId": calendar_id, "calendarName": name}
            for event in page
        )

    if failures and len(failures) == len(entries):
        # Every calendar failed. Answering `{"events": []}` here would tell the
        # model the search ran and found nothing, which is a different fact.
        raise failures[0]

    # Upcoming first when there are more hits than fit. The window reaches 30
    # days back so "when did I last see the dentist" works, but the question
    # this tool is usually asked is "when is my next", and a plain ascending
    # truncation answers it with a month of history and drops the appointment.
    upcoming = sorted((e for e in found if _sort_key(e) >= now), key=_sort_key)
    past = sorted((e for e in found if _sort_key(e) < now), key=_sort_key, reverse=True)
    chosen = (upcoming + past)[:limit]
    # A per-calendar concatenation reads as random order to a model.
    chosen.sort(key=_sort_key)
    return {"events": chosen}


def _sort_key(event: dict[str, Any]) -> datetime:
    """When an event starts, as one comparable instant.

    A real timestamp rather than the raw string, because a merge across
    calendars is a merge across time zones and `"…T09:00:00+05:30"` sorts
    before `"…T20:00:00-07:00"` as text while starting nine hours after it. An
    all-day event has a `date` and no zone; midnight UTC is the reading Google's
    own tool output gives it.
    """
    start = event.get("start") or {}
    moment = start.get("dateTime")
    if isinstance(moment, str):
        parsed = _rfc3339(moment)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    day = start.get("date")
    if isinstance(day, str):
        return datetime.fromisoformat(day).replace(tzinfo=UTC)
    return datetime.min.replace(tzinfo=UTC)


async def _get_event(client: _CalendarClient, args: dict[str, Any]) -> dict[str, Any]:
    calendar_id = _string(args, "calendarId") or "primary"
    event_id = _required_string(args, "eventId")
    event = await client.request(
        "GET",
        f"/calendars/{_path(calendar_id)}/events/{_path(event_id)}",
        tool="get_event",
        calendar_id=calendar_id,
    )
    return _clean_event(event)


async def _create_event(client: _CalendarClient, args: dict[str, Any]) -> dict[str, Any]:
    calendar_id = _string(args, "calendarId") or "primary"
    all_day = bool(_bool(args, "allDay"))
    body = _shared_event_body(args)
    body.update(
        _time_fields(
            args,
            start=_required_string(args, "startTime"),
            end=_required_string(args, "endTime"),
            all_day=all_day,
        )
    )
    attendees = _merged_attendees(args, objects_key="attendees", emails_key="attendeeEmails")
    if attendees:
        body["attendees"] = attendees
    recurrence = _list_of(args, "recurrenceData", str)
    if recurrence:
        body["recurrence"] = recurrence
    attachments = _list_of(args, "attachments", dict)
    if attachments:
        body["attachments"] = attachments
    event_type = _enum(args, "eventType", _EVENT_TYPE)
    if event_type is not None:
        body["eventType"] = event_type
    working_location = args.get("workingLocationProperties")
    if working_location is not None and event_type != "workingLocation":
        # Dropped, not refused. Google rejects the pair — "workingLocationProperties
        # requires an event type of workingLocation, and vice versa" — and on an
        # ordinary meeting the field has no meaning to lose: there is nowhere on
        # a `default` event for it to go.
        #
        # Refusing was tried first and is worse. Measured live: a model asked to
        # book a 45-minute meeting attached this field unprompted, then read the
        # error as something to vary its arguments against — three failed
        # attempts, then it told the user the booking was impossible. Failing a
        # real booking over a field the user never mentioned is the wrong trade,
        # so the warning goes to the log, where it is loud in development,
        # instead of into a turn a person is waiting on.
        logger.warning(
            "create_event: dropped workingLocationProperties on a %s event",
            event_type or "default",
        )
        working_location = None
    elif event_type == "workingLocation" and working_location is None:
        # The other direction genuinely loses information — there is no default
        # for "where" — so this one is the caller's to fix.
        raise ToolError(
            "`eventType: WORKING_LOCATION` needs `workingLocationProperties` saying where the "
            "user is working."
        )
    if working_location is not None:
        body["workingLocationProperties"] = _working_location_body(working_location)

    return _clean_event(
        await client.request(
            "POST",
            f"/calendars/{_path(calendar_id)}/events",
            tool="create_event",
            calendar_id=calendar_id,
            params={
                "sendUpdates": _send_updates(args, has_attendees=bool(attendees)),
                # Both are silently ignored when absent: a Meet link is never
                # created, and attachments never attach.
                "conferenceDataVersion": 1,
                "supportsAttachments": "true",
            },
            json=body,
        )
    )


def _working_location_body(value: Any) -> dict[str, Any]:
    """`workingLocationProperties` as the Event resource spells it.

    Their argument is a flat `{type, customLocationLabel}`; the resource nests
    the label under a sub-object named after the type.
    """
    if not isinstance(value, dict):
        raise ToolError("`workingLocationProperties` must be an object.")
    kind = _WORKING_LOCATION_TYPE.get(str(value.get("type") or "HOME_OFFICE"))
    if kind is None:
        raise ToolError(
            "`workingLocationProperties.type` must be `HOME_OFFICE` or `CUSTOM_LOCATION`."
        )
    if kind == "homeOffice":
        return {"type": "homeOffice", "homeOffice": {}}
    label = value.get("customLocationLabel")
    if not isinstance(label, str) or not label.strip():
        raise ToolError(
            "`workingLocationProperties.customLocationLabel` is required for `CUSTOM_LOCATION`."
        )
    return {"type": "customLocation", "customLocation": {"label": label.strip()}}


def _needs_current_event(args: dict[str, Any]) -> bool:
    """Whether `update_event` has to read the event before it can patch it.

    Two reasons, both of them cases where `PATCH` alone would quietly destroy
    something: the four delta arguments describe a change to an array that PATCH
    replaces wholesale, and a lone `startTime` promises to keep the event's
    current duration, which is only knowable from the event.
    """
    deltas = (
        "addedAttendees",
        "addedAttendeeEmails",
        "removedAttendeeEmails",
        "addedAttachments",
        "removedAttachmentFileUrls",
    )
    if any(args.get(key) for key in deltas):
        return True
    return bool(args.get("startTime")) and not args.get("endTime")


async def _update_event(client: _CalendarClient, args: dict[str, Any]) -> dict[str, Any]:
    calendar_id = _string(args, "calendarId") or "primary"
    event_id = _required_string(args, "eventId")
    path = f"/calendars/{_path(calendar_id)}/events/{_path(event_id)}"

    current: dict[str, Any] = {}
    if _needs_current_event(args):
        current = await client.request("GET", path, tool="update_event", calendar_id=calendar_id)

    all_day = _bool(args, "allDay")
    start = _string(args, "startTime")
    end = _string(args, "endTime")
    if all_day is not None and (start is None or end is None):
        raise ToolError("`allDay` needs both `startTime` and `endTime`.")
    if start and not end and current:
        end = _shifted_end(current, start)

    body = _shared_event_body(args)
    body.update(_time_fields(args, start=start, end=end, all_day=bool(all_day)))

    attendees = _apply_attendee_delta(args, current)
    if attendees is not None:
        body["attendees"] = attendees
    attachments = _apply_attachment_delta(args, current)
    if attachments is not None:
        body["attachments"] = attachments

    if not body:
        raise ToolError("`update_event` needs at least one field to change.")

    if attendees is not None:
        has_attendees = bool(attendees)
    elif current:
        has_attendees = bool(current.get("attendees"))
    else:
        # An edit that needed no read — a new title, or a move with both ends
        # given — leaves us not knowing whether anyone is on the event. Assume
        # someone is: `sendUpdates=all` with no attendees emails nobody, where
        # `none` with attendees is a meeting that moved and never said so.
        has_attendees = True
    return _clean_event(
        await client.request(
            "PATCH",
            path,
            tool="update_event",
            calendar_id=calendar_id,
            params={
                "sendUpdates": _send_updates(args, has_attendees=has_attendees),
                "conferenceDataVersion": 1,
                "supportsAttachments": "true",
            },
            json=body,
        )
    )


def _shifted_end(current: dict[str, Any], start: str) -> str:
    """The new end that keeps the event's current duration, as Google promises."""
    old_start = (current.get("start") or {}).get("dateTime")
    old_end = (current.get("end") or {}).get("dateTime")
    if not old_start or not old_end:
        raise ToolError(
            "This is an all-day event — set `endTime` as well as `startTime` to move it."
        )
    duration = _timestamp(old_end, argument="endTime") - _timestamp(old_start, argument="startTime")
    return (_timestamp(start, argument="startTime") + duration).isoformat()


def _apply_attendee_delta(
    args: dict[str, Any], current: dict[str, Any]
) -> list[dict[str, Any]] | None:
    """The complete attendee list after the requested additions and removals.

    `PATCH` replaces the array rather than merging into it — verified live, one
    attendee patched onto a two-guest event leaves one guest — so a delta has to
    be computed against the event as it stands and sent back whole.
    """
    added = _merged_attendees(args, objects_key="addedAttendees", emails_key="addedAttendeeEmails")
    removed = {email.strip().lower() for email in _list_of(args, "removedAttendeeEmails", str)}
    if not added and not removed:
        return None
    merged: dict[str, dict[str, Any]] = {}
    for entry in current.get("attendees") or []:
        email = str(entry.get("email") or "").lower()
        if email and email not in removed:
            merged[email] = entry
    for entry in added:
        merged[entry["email"].lower()] = entry
    return list(merged.values())


def _apply_attachment_delta(
    args: dict[str, Any], current: dict[str, Any]
) -> list[dict[str, Any]] | None:
    added = _list_of(args, "addedAttachments", dict)
    removed = {url.strip() for url in _list_of(args, "removedAttachmentFileUrls", str)}
    if not added and not removed:
        return None
    merged = [
        entry
        for entry in current.get("attachments") or []
        if str(entry.get("fileUrl") or "") not in removed
    ]
    known = {str(entry.get("fileUrl") or "") for entry in merged}
    for entry in added:
        if not isinstance(entry.get("fileUrl"), str):
            raise ToolError("every entry in `addedAttachments` needs a `fileUrl`.")
        if entry["fileUrl"] not in known:
            merged.append(entry)
    return merged


async def _delete_event(client: _CalendarClient, args: dict[str, Any]) -> dict[str, Any]:
    """Read, then delete.

    `DELETE` answers `204` with no body, so the read is what lets the agent say
    *what* it cancelled — and what tells us whether there were guests to notify,
    which is the whole point of defaulting `sendUpdates` rather than taking
    REST's `none`.
    """
    calendar_id = _string(args, "calendarId") or "primary"
    event_id = _required_string(args, "eventId")
    path = f"/calendars/{_path(calendar_id)}/events/{_path(event_id)}"

    event = await client.request("GET", path, tool="delete_event", calendar_id=calendar_id)
    await client.request(
        "DELETE",
        path,
        tool="delete_event",
        calendar_id=calendar_id,
        params={"sendUpdates": _send_updates(args, has_attendees=bool(event.get("attendees")))},
    )
    return {"deleted": True, "event": _clean_event(event)}


async def _respond_to_event(client: _CalendarClient, args: dict[str, Any]) -> dict[str, Any]:
    """Set the user's own RSVP without disinviting everybody else.

    The response lives on the user's own entry inside the shared `attendees`
    array, and `PATCH` replaces that array, so the naive one-entry patch removes
    every other guest from the event for everyone.
    """
    calendar_id = _string(args, "calendarId") or "primary"
    event_id = _required_string(args, "eventId")
    status = _enum(
        args,
        "responseStatus",
        {"accepted": "accepted", "declined": "declined", "tentative": "tentative"},
    )
    if status is None:
        raise ToolError("`responseStatus` is required.")
    comment = _string(args, "responseComment")
    path = f"/calendars/{_path(calendar_id)}/events/{_path(event_id)}"

    event = await client.request("GET", path, tool="respond_to_event", calendar_id=calendar_id)
    attendees = [dict(entry) for entry in event.get("attendees") or []]
    mine = next((entry for entry in attendees if entry.get("self")), None)
    if mine is None:
        raise ToolError(
            "The user is not an attendee of that event, so there is nothing to respond to."
        )
    mine["responseStatus"] = status
    if comment:
        mine["comment"] = comment

    return _clean_event(
        await client.request(
            "PATCH",
            path,
            tool="respond_to_event",
            calendar_id=calendar_id,
            params={"sendUpdates": _send_updates(args, has_attendees=True)},
            json={"attendees": attendees},
        )
    )


async def _suggest_time(client: _CalendarClient, args: dict[str, Any]) -> dict[str, Any]:
    """Free periods common to every attendee, inside the caller's working hours.

    `freebusy` knows nothing about working hours or weekends, so steps 3 and 4
    are ours, and they are per-day in a real zone rather than arithmetic on a
    UTC offset — an hour range that is right in October is an hour out in
    November otherwise.
    """
    emails = [e.strip() for e in _list_of(args, "attendeeEmails", str) if e.strip()]
    if not emails:
        raise ToolError("`attendeeEmails` needs at least one email address.")
    if len(emails) > _FREEBUSY_CALENDAR_LIMIT:
        raise ToolError(
            f"`attendeeEmails` takes at most {_FREEBUSY_CALENDAR_LIMIT} addresses at a time."
        )
    window_start = _timestamp(_required_string(args, "startTime"), argument="startTime")
    window_end = _timestamp(_required_string(args, "endTime"), argument="endTime")
    if window_end <= window_start:
        raise ToolError("`endTime` must be after `startTime`.")
    minimum = timedelta(minutes=_int(args, "durationMinutes") or 30)

    preferences = args.get("preferences") or {}
    if not isinstance(preferences, dict):
        raise ToolError("`preferences` must be an object.")
    zone = await _suggest_zone(client, args)
    window_start, window_end = _aware(window_start, zone), _aware(window_end, zone)

    body = await client.request(
        "POST",
        "/freeBusy",
        tool="suggest_time",
        json={
            "timeMin": window_start.isoformat(),
            "timeMax": window_end.isoformat(),
            "items": [{"id": email} for email in emails],
        },
    )
    calendars = body.get("calendars") or {}
    busy: list[tuple[datetime, datetime]] = []
    unreadable: list[dict[str, str]] = []
    for email in emails:
        entry = calendars.get(email) or {}
        errors = entry.get("errors") or []
        if errors:
            # Not an empty calendar — a calendar Google would not show us, which
            # is what an address outside the organisation looks like. Reading it
            # as "free all week" is how an agent books over someone's day.
            unreadable.append(
                {"email": email, "reason": str(errors[0].get("reason") or "unavailable")}
            )
            continue
        for period in entry.get("busy") or []:
            busy.append((_rfc3339(period["start"]), _rfc3339(period["end"])))

    free = _invert(busy, window_start, window_end)
    free = list(_within_preferred_hours(free, preferences, zone))
    slots = [(s, e) for s, e in free if e - s >= minimum]
    limit = _int(preferences, "pageSize") or 5

    result: dict[str, Any] = {
        "timeSlots": [
            {
                "startTime": start.astimezone(zone).isoformat(),
                "endTime": end.astimezone(zone).isoformat(),
                "durationMinutes": int((end - start).total_seconds() // 60),
                "start": {"dateTime": start.astimezone(zone).isoformat(), "timeZone": str(zone)},
                "end": {"dateTime": end.astimezone(zone).isoformat(), "timeZone": str(zone)},
            }
            for start, end in slots[:limit]
        ]
    }
    if unreadable:
        result["unreadableCalendars"] = unreadable
    return result


async def _suggest_zone(client: _CalendarClient, args: dict[str, Any]) -> ZoneInfo:
    """The zone the window, the preferred hours and the answer are all read in.

    The caller's, or the user's primary calendar's — never the UTC offset on
    `startTime`, even though that is what Google's own tool falls back to. An
    offset is a moment, not a zone: "09:00 to 17:00, weekdays" spanning a DST
    change is an hour wrong for half the window if it is resolved by arithmetic
    on a fixed offset. The offset still decides *when* the window starts; it
    just does not get to decide what a working day is.
    """
    named = _string(args, "timeZone")
    if named:
        return _zone(named, argument="timeZone")
    return _zone(await client.primary_time_zone(tool="suggest_time"), argument="timeZone")


def _aware(value: datetime, zone: ZoneInfo) -> datetime:
    return value.replace(tzinfo=zone) if value.tzinfo is None else value


def _invert(
    busy: Sequence[tuple[datetime, datetime]], start: datetime, end: datetime
) -> list[tuple[datetime, datetime]]:
    """The gaps left in [start, end) once every attendee's busy time is removed."""
    merged: list[tuple[datetime, datetime]] = []
    for period_start, period_end in sorted(busy):
        if merged and period_start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], period_end))
        else:
            merged.append((period_start, period_end))

    free: list[tuple[datetime, datetime]] = []
    cursor = start
    for period_start, period_end in merged:
        if period_start > cursor:
            free.append((cursor, min(period_start, end)))
        cursor = max(cursor, period_end)
        if cursor >= end:
            break
    if cursor < end:
        free.append((cursor, end))
    return [(s, e) for s, e in free if e > s]


def _within_preferred_hours(
    free: Iterable[tuple[datetime, datetime]], preferences: dict[str, Any], zone: ZoneInfo
) -> Iterable[tuple[datetime, datetime]]:
    """Clip free periods to the preferred hours of each local day."""
    start_hour = _hour(preferences, "startHour")
    end_hour = _hour(preferences, "endHour")
    exclude_weekends = bool(preferences.get("excludeWeekends"))
    if start_hour is None and end_hour is None and not exclude_weekends:
        yield from free
        return
    day_start = start_hour or time(0, 0)
    day_end = end_hour or time(23, 59)
    if day_end <= day_start:
        raise ToolError("`preferences.endHour` must be later in the day than `startHour`.")

    for period_start, period_end in free:
        day = period_start.astimezone(zone).date()
        last = period_end.astimezone(zone).date()
        while day <= last:
            if not (exclude_weekends and day.weekday() >= 5):
                window_start = datetime.combine(day, day_start, tzinfo=zone)
                window_end = datetime.combine(day, day_end, tzinfo=zone)
                clipped = (max(period_start, window_start), min(period_end, window_end))
                if clipped[1] > clipped[0]:
                    yield clipped
            day += timedelta(days=1)


def _hour(preferences: dict[str, Any], key: str) -> time | None:
    value = preferences.get(key)
    if value is None or value == "":
        return None
    match = _HOUR_RE.match(str(value))
    if match is None:
        raise ToolError(f'`preferences.{key}` must look like "09:00" (24-hour), got `{value}`.')
    return time(int(match.group(1)), int(match.group(2)))


def _path(segment: str) -> str:
    """One path segment, percent-encoded.

    `safe=""` rather than the default, because a calendar id is an email address
    and a subscribed calendar's is `en.usa#holiday@group.v.calendar.google.com`
    — a `#` left raw truncates the URL at the fragment, and an `@` is only legal
    in an authority.
    """
    return quote(segment, safe="")


_HANDLERS: dict[str, Callable[[_CalendarClient, dict[str, Any]], Awaitable[Any]]] = {
    "list_calendars": _list_calendars,
    "list_events": _list_events,
    "search_events": _search_events,
    "get_event": _get_event,
    "create_event": _create_event,
    "update_event": _update_event,
    "delete_event": _delete_event,
    "respond_to_event": _respond_to_event,
    "suggest_time": _suggest_time,
}


class GoogleCalendarToolset(llm.Toolset):
    """The nine Calendar tools, sharing one connection pool for the session.

    ``allowed_tools`` and ``namespace`` are applied here because there is no MCP
    client to apply them: approval filters on the tool's own name and the prefix
    goes on afterwards, which is the same order `_MCPServerHTTP` uses and what
    keeps an approved list valid when the namespace changes.
    """

    def __init__(
        self,
        *,
        id: str,
        auth: httpx.Auth,
        allowed_tools: Sequence[str] | None,
        namespace: str,
    ) -> None:
        client = _CalendarClient(auth)
        approved = set(allowed_tools) if allowed_tools else None
        tools = [
            function_tool(
                _make_dispatcher(client, str(schema["name"])),
                raw_schema={**schema, "name": exposed_tool_name(namespace, str(schema["name"]))},
            )
            for schema in TOOL_SCHEMAS
            if approved is None or schema["name"] in approved
        ]
        super().__init__(id=id, tools=tools)
        self._client = client

    async def aclose(self) -> None:
        await self._client.aclose()
        await super().aclose()


def _make_dispatcher(
    client: _CalendarClient, name: str
) -> Callable[[dict[str, Any] | None], Awaitable[Any]]:
    handler = _HANDLERS[name]

    async def _dispatch(raw_arguments: dict[str, Any] | None) -> Any:
        args: dict[str, Any] = _prune(raw_arguments or {})
        return await handler(client, args)

    return _dispatch
