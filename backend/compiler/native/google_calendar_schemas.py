"""The nine Google Calendar tools, as the model is shown them.

Mirrors the tool surface of Google's own hosted Calendar MCP server
(`calendarmcp.googleapis.com`, captured from its unauthenticated `tools/list`)
so an agent written against theirs works against ours: same nine names, same
argument names, same meanings. The handlers in ``google_calendar`` translate
each of these into Calendar REST v3 — the arguments are a flattened, renamed
abstraction over the Event resource, not a pass-through.

Four deliberate departures from their schemas, each of which removes a trap
rather than a capability:

- **The `*_UNSPECIFIED` enum members are gone.** They are proto artifacts
  meaning "unset", and "unset" is what omitting an optional argument already
  says. A model spending a turn choosing `EVENT_TYPE_UNSPECIFIED` gains nothing.
- **Output-only attendee fields are gone from the input** (`id`, `organizer`,
  `self`, `comment`). Their schema marks them `readOnly` and then offers them as
  things to send.
- **`list_events.orderBy` lost `startTimeDesc`.** Calendar REST orders ascending
  only, and the honest implementations of "descending" are either a lie within
  one page or an unbounded number of requests. `default`, `startTime` and
  `lastModified` all map to something real.
- **`search_events` lost `pageToken`.** It fans out over every calendar (see its
  description), and one opaque cursor cannot page a merged result set.

Values that are prose in their schema (`responseStatus`, `visibility`,
`orderBy`, reminder `method`) are `enum` here: the set is closed either way, and
an enum is the half of it a model cannot get wrong.
"""

from __future__ import annotations

from typing import Any

# ── Shared object shapes ───────────────────────────────────────────────────
#
# Inlined at each use rather than hoisted into `$defs`. Google uses `$defs`
# because their schemas are generated from protos; ours reference each of these
# at most once per tool, where a `$ref` costs the same tokens as the object it
# points at and buys a level of indirection for the reader and for every model
# provider's schema compiler.


def _attendee(*, description: str) -> dict[str, Any]:
    return {
        "type": "array",
        "description": description,
        "items": {
            "type": "object",
            "properties": {
                "email": {"type": "string", "description": "Required. Attendee's email address."},
                "displayName": {"type": "string", "description": "Optional. Attendee's name."},
                "optionalAttendee": {
                    "type": "boolean",
                    "description": "Optional. Whether the attendee is optional. Default: `false`.",
                },
                "additionalGuests": {
                    "type": "integer",
                    "description": "Optional. Number of extra guests this attendee brings. Default: `0`.",
                },
                "resource": {
                    "type": "boolean",
                    "description": (
                        "Optional. Whether the attendee is a resource such as a meeting room. "
                        "Can only be set when the attendee is first added."
                    ),
                },
                "responseStatus": {
                    "type": "string",
                    "enum": ["needsAction", "declined", "tentative", "accepted"],
                    "description": (
                        "Optional. The attendee's response. Use `needsAction` when inviting "
                        "someone new."
                    ),
                },
            },
            "required": ["email"],
        },
    }


def _attachments(*, description: str) -> dict[str, Any]:
    return {
        "type": "array",
        "description": description,
        "items": {
            "type": "object",
            "properties": {
                "fileUrl": {
                    "type": "string",
                    "description": (
                        "Required. Link to the attachment. Must be a Google Drive file URL."
                    ),
                },
                "title": {"type": "string", "description": "Optional. Attachment title."},
            },
            "required": ["fileUrl"],
        },
    }


def _reminders(*, description: str) -> dict[str, Any]:
    return {
        "type": "array",
        "description": description,
        "items": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["email", "popup"],
                    "description": "Required. How the reminder is delivered.",
                },
                "minutes": {
                    "type": "integer",
                    "description": "Required. Minutes before the event that the reminder fires.",
                },
            },
            "required": ["method", "minutes"],
        },
    }


_GUEST_PERMISSIONS = {
    "type": "object",
    "description": "Optional. What attendees other than the organizer may do.",
    "properties": {
        "guestsCanInviteOthers": {
            "type": "boolean",
            "description": "Whether guests can invite others. Default: `true`.",
        },
        "guestsCanModify": {
            "type": "boolean",
            "description": "Whether guests can modify the event. Default: `false`.",
        },
        "guestsCanSeeGuests": {
            "type": "boolean",
            "description": "Whether guests can see the other guests. Default: `true`.",
        },
    },
}

_NOTIFICATION_LEVEL = {
    "type": "string",
    "enum": ["NONE", "EXTERNAL_ONLY", "ALL"],
    "description": (
        "Optional. Who is emailed about this change. Defaults to `ALL` when the event has "
        "attendees and `NONE` when it has none, so guests are told by default."
    ),
}

_EVENT_TYPES = ["DEFAULT", "OUT_OF_OFFICE", "FOCUS_TIME", "WORKING_LOCATION", "BIRTHDAY"]

_CALENDAR_ID = {
    "type": "string",
    "description": (
        "Optional. Calendar to act on, as its email-style id — resolve a name like "
        "'my family calendar' with `list_calendars`. Default: the user's primary calendar."
    ),
}

_TIME_ZONE = {
    "type": "string",
    "description": (
        "Optional. IANA time zone name, for example `America/Los_Angeles`. Overrides any UTC "
        "offset in `startTime` and `endTime`. Default: the user's primary time zone."
    ),
}

# Said on every tool that takes an `eventId`, because the id a model has in hand
# almost always came from a `singleEvents` expansion and therefore names one
# occurrence. Editing "the 3pm standup" should not silently move every standup.
_RECURRING_INSTANCE_NOTE = (
    "`list_events` and `search_events` return the individual occurrences of a recurring "
    "event, so an `eventId` taken from either of them addresses that one occurrence — not "
    "the whole series."
)


CREATE_EVENT = {
    "name": "create_event",
    "description": "Creates an event on the given calendar.",
    "parameters": {
        "type": "object",
        "properties": {
            "calendarId": _CALENDAR_ID,
            "summary": {"type": "string", "description": "Required. Event title."},
            "startTime": {
                "type": "string",
                "description": (
                    "Required. Start time as an ISO 8601 timestamp, for example "
                    "`2026-04-30T10:00:00+08:00`. A UTC offset is required unless `timeZone` "
                    "is set."
                ),
            },
            "endTime": {
                "type": "string",
                "description": (
                    "Required. End time as an ISO 8601 timestamp. Must be after `startTime`."
                ),
            },
            "timeZone": _TIME_ZONE,
            "allDay": {
                "type": "boolean",
                "description": (
                    "Optional. Whether the event spans whole days. Only the date part of "
                    "`startTime` and `endTime` is used, and the end date is exclusive — a "
                    "one-day event on 2026-04-30 ends on 2026-05-01."
                ),
            },
            "description": {"type": "string", "description": "Optional. Description, may be HTML."},
            "location": {"type": "string", "description": "Optional. Location."},
            "attendees": _attendee(
                description=(
                    "Optional. Guests to invite. On the user's primary calendar, the user is "
                    "added as an attendee automatically when there is at least one other guest."
                )
            ),
            "attendeeEmails": {
                "type": "array",
                "description": "Optional. Deprecated: use `attendees` instead.",
                "items": {"type": "string"},
            },
            "notificationLevel": _NOTIFICATION_LEVEL,
            "addGoogleMeetUrl": {
                "type": "boolean",
                "description": "Optional. Create a Google Meet link for the event.",
            },
            "googleMeetUrl": {
                "type": "string",
                "description": (
                    "Optional. Attach an existing Google Meet URL or meeting id. Overrides "
                    "`addGoogleMeetUrl`."
                ),
            },
            "availability": {
                "type": "string",
                "enum": ["AVAILABILITY_BUSY", "AVAILABILITY_FREE"],
                "description": (
                    "Optional. Whether the event blocks time on the calendar. Default: busy."
                ),
            },
            "visibility": {
                "type": "string",
                "enum": ["default", "public", "private"],
                "description": (
                    "Optional. Who can see the event's details. `default` follows the calendar."
                ),
            },
            "colorId": {
                "type": "string",
                "description": (
                    "Optional. Event colour, as an id from the Calendar colour palette (`1`–`11`)."
                ),
            },
            "guestPermissions": _GUEST_PERMISSIONS,
            "overrideReminders": _reminders(
                description=(
                    "Optional. Reminders for this event, replacing the calendar's defaults."
                )
            ),
            "recurrenceData": {
                "type": "array",
                "description": (
                    "Optional. Recurrence as RFC 5545 `RRULE`, `RDATE` or `EXDATE` lines, for "
                    "example `RRULE:FREQ=WEEKLY;BYDAY=MO`. `DTSTART`/`DTEND` are not allowed — "
                    "the first occurrence is `startTime`/`endTime`."
                ),
                "items": {"type": "string"},
            },
            "attachments": _attachments(description="Optional. Google Drive file attachments."),
            "eventType": {
                "type": "string",
                "enum": _EVENT_TYPES,
                "description": (
                    "Optional. Kind of event. Default: `DEFAULT`. `OUT_OF_OFFICE` and "
                    "`FOCUS_TIME` cannot be all-day, and the type cannot be changed afterwards."
                ),
            },
            "workingLocationProperties": {
                "type": "object",
                "description": (
                    "Optional. Where the user is working, and only that — leave it out of an "
                    "ordinary meeting. Valid only together with `eventType: WORKING_LOCATION`, "
                    "and each of the two is required by the other."
                ),
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["HOME_OFFICE", "CUSTOM_LOCATION"],
                        "description": "Working location type.",
                    },
                    "customLocationLabel": {
                        "type": "string",
                        "description": "Label for the location. Required when type is `CUSTOM_LOCATION`.",
                    },
                },
            },
        },
        "required": ["summary", "startTime", "endTime"],
    },
}


UPDATE_EVENT = {
    "name": "update_event",
    "description": (
        "Updates an event on the given calendar. Arguments that are not set are left "
        f"unchanged. {_RECURRING_INSTANCE_NOTE}"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "calendarId": _CALENDAR_ID,
            "eventId": {
                "type": "string",
                "description": (
                    "Required. Event id, from `list_events`, `search_events` or `create_event`."
                ),
            },
            "summary": {"type": "string", "description": "Optional. New title."},
            "description": {
                "type": "string",
                "description": "Optional. New description, may be HTML.",
            },
            "location": {"type": "string", "description": "Optional. New location."},
            "startTime": {
                "type": "string",
                "description": (
                    "Optional. New start time as an ISO 8601 timestamp. Set on its own, the "
                    "event keeps its current duration."
                ),
            },
            "endTime": {
                "type": "string",
                "description": "Optional. New end time as an ISO 8601 timestamp.",
            },
            "timeZone": _TIME_ZONE,
            "allDay": {
                "type": "boolean",
                "description": (
                    "Optional. Turn the event into (or out of) a whole-day event. "
                    "`startTime` and `endTime` must both be given with it."
                ),
            },
            "addedAttendees": _attendee(description="Optional. Guests to add to the event."),
            "addedAttendeeEmails": {
                "type": "array",
                "description": "Optional. Deprecated: use `addedAttendees` instead.",
                "items": {"type": "string"},
            },
            "removedAttendeeEmails": {
                "type": "array",
                "description": "Optional. Guests to remove, by email address.",
                "items": {"type": "string"},
            },
            "notificationLevel": _NOTIFICATION_LEVEL,
            "addGoogleMeetUrl": {
                "type": "boolean",
                "description": "Optional. Add a Google Meet link to the event.",
            },
            "googleMeetUrl": {
                "type": "string",
                "description": (
                    "Optional. Attach an existing Google Meet URL or meeting id. Overrides "
                    "`addGoogleMeetUrl`."
                ),
            },
            "availability": {
                "type": "string",
                "enum": ["AVAILABILITY_BUSY", "AVAILABILITY_FREE"],
                "description": "Optional. Whether the event blocks time on the calendar.",
            },
            "visibility": {
                "type": "string",
                "enum": ["default", "public", "private"],
                "description": "Optional. New visibility of the event.",
            },
            "colorId": {
                "type": "string",
                "description": "Optional. New event colour (`1`–`11`).",
            },
            "guestPermissions": _GUEST_PERMISSIONS,
            "overrideReminders": _reminders(
                description="Optional. Replaces every existing reminder on the event."
            ),
            "addedAttachments": _attachments(
                description="Optional. Google Drive files to attach to the event."
            ),
            "removedAttachmentFileUrls": {
                "type": "array",
                "description": "Optional. Attachments to remove, by their file URL.",
                "items": {"type": "string"},
            },
        },
        "required": ["eventId"],
    },
}


DELETE_EVENT = {
    "name": "delete_event",
    "description": f"Deletes an event on the given calendar. {_RECURRING_INSTANCE_NOTE}",
    "parameters": {
        "type": "object",
        "properties": {
            "calendarId": _CALENDAR_ID,
            "eventId": {"type": "string", "description": "Required. Id of the event to delete."},
            "notificationLevel": _NOTIFICATION_LEVEL,
        },
        "required": ["eventId"],
    },
}


RESPOND_TO_EVENT = {
    "name": "respond_to_event",
    "description": (
        "Sets the user's own RSVP on an event they were invited to. Leaves the other "
        f"guests untouched. {_RECURRING_INSTANCE_NOTE}"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "calendarId": _CALENDAR_ID,
            "eventId": {
                "type": "string",
                "description": "Required. Id of the event to respond to.",
            },
            "responseStatus": {
                "type": "string",
                "enum": ["accepted", "declined", "tentative"],
                "description": "Required. The user's answer to the invitation.",
            },
            "responseComment": {
                "type": "string",
                "description": "Optional. A note to attach to the response.",
            },
            "notificationLevel": _NOTIFICATION_LEVEL,
        },
        "required": ["eventId", "responseStatus"],
    },
}


GET_EVENT = {
    "name": "get_event",
    "description": "Returns a single event on the given calendar.",
    "parameters": {
        "type": "object",
        "properties": {
            "calendarId": _CALENDAR_ID,
            "eventId": {
                "type": "string",
                "description": (
                    "Required. Event id, from `list_events`, `search_events` or `create_event`."
                ),
            },
        },
        "required": ["eventId"],
    },
}


LIST_EVENTS = {
    "name": "list_events",
    "description": (
        "Returns events on one calendar matching all the given constraints. Do not set a "
        "time range unless the user asked for one. Recurring events are expanded into their "
        "individual occurrences. To search across every calendar the user has, use "
        "`search_events` instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "calendarId": _CALENDAR_ID,
            "startTime": {
                "type": "string",
                "description": (
                    "Optional. Lower bound of the time range, as an ISO 8601 timestamp. Set it "
                    "only when the user asked for a specific timeframe."
                ),
            },
            "endTime": {
                "type": "string",
                "description": (
                    "Optional. Upper bound of the time range, as an ISO 8601 timestamp. Must be "
                    "after `startTime`."
                ),
            },
            "timeZone": {
                "type": "string",
                "description": (
                    "Optional. IANA time zone name used to resolve times without an offset. "
                    "Default: the calendar's own time zone."
                ),
            },
            "fullText": {
                "type": "string",
                "description": (
                    "Optional. Case-insensitive free-text search over title, description, "
                    "location and attendees. Matches events containing every term."
                ),
            },
            "eventType": {
                "type": "array",
                "description": (
                    "Optional. Restrict to these kinds of event. Default: `DEFAULT`, "
                    "`OUT_OF_OFFICE`, `FOCUS_TIME` and events from Gmail."
                ),
                "items": {"type": "string", "enum": [*_EVENT_TYPES, "FROM_GMAIL"]},
            },
            "eventTypeFilter": {
                "type": "array",
                "description": "Optional. Deprecated: use `eventType` instead.",
                "items": {"type": "string"},
            },
            "orderBy": {
                "type": "string",
                "enum": ["default", "startTime", "lastModified"],
                "description": (
                    "Optional. Order of the returned events. `default` is unspecified but "
                    "stable; the other two are ascending."
                ),
            },
            "pageSize": {
                "type": "integer",
                "description": "Optional. Events per page (default `100`, max `250`). Prefer `10`.",
            },
            "pageToken": {
                "type": "string",
                "description": "Optional. `nextPageToken` from the previous page.",
            },
        },
    },
}


SEARCH_EVENTS = {
    "name": "search_events",
    "description": (
        "Searches for events by keyword across every calendar on the user's calendar list — "
        "including subscribed ones such as holidays — from 30 days ago to a year ahead, "
        "sorted by start time. Each result names the calendar it came from. For one specific "
        "calendar, or any other time range, use `list_events` with `fullText` instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Required. Case-insensitive search text, matched against title, "
                    "description, location and attendees."
                ),
            },
            "pageSize": {
                "type": "integer",
                "description": "Optional. Maximum number of events to return. Default `25`, max `50`.",
            },
        },
        "required": ["query"],
    },
}


LIST_CALENDARS = {
    "name": "list_calendars",
    "description": (
        "Returns the calendars this user has access to. Use it to resolve a calendar named "
        "in conversation ('my family calendar') into the `calendarId` the other tools take. "
        "`accessRole` says what the user may do: events can only be created, changed or "
        "deleted on a calendar they own."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pageSize": {
                "type": "integer",
                "description": "Optional. Calendars per page (default `100`, max `250`).",
            },
            "pageToken": {
                "type": "string",
                "description": "Optional. `nextPageToken` from the previous page.",
            },
        },
    },
}


SUGGEST_TIME = {
    "name": "suggest_time",
    "description": (
        "Finds the periods in a window when every listed attendee is free. Returns each free "
        "period whole rather than split into candidate slots, so a wide-open day comes back "
        "as one long period. Attendees whose calendars Google will not share — anyone outside "
        "the user's organisation, usually — are listed in `unreadableCalendars`; treat them as "
        "unknown, not as free."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "attendeeEmails": {
                "type": "array",
                "description": (
                    "Required. Email addresses to find free time for. Include the user's own "
                    "address to avoid double-booking them. At most 50."
                ),
                "items": {"type": "string"},
            },
            "startTime": {
                "type": "string",
                "description": "Required. Start of the window to search, as an ISO 8601 timestamp.",
            },
            "endTime": {
                "type": "string",
                "description": "Required. End of the window to search, as an ISO 8601 timestamp.",
            },
            "durationMinutes": {
                "type": "integer",
                "description": (
                    "Optional. Shortest free period worth returning, in minutes. Default `30`."
                ),
            },
            "timeZone": {
                "type": "string",
                "description": (
                    "Optional. IANA time zone name the preferred hours are read in, and the "
                    "one the answer is written in. Default: the user's primary time zone."
                ),
            },
            "preferences": {
                "type": "object",
                "description": "Optional. Narrows the window before free time is looked for.",
                "properties": {
                    "startHour": {
                        "type": "string",
                        "description": 'Earliest hour of the day to suggest, as "HH:mm".',
                    },
                    "endHour": {
                        "type": "string",
                        "description": 'Latest hour of the day to suggest, as "HH:mm".',
                    },
                    "excludeWeekends": {
                        "type": "boolean",
                        "description": "Skip Saturdays and Sundays.",
                    },
                    "pageSize": {
                        "type": "integer",
                        "description": "Maximum number of periods to return. Default `5`.",
                    },
                },
            },
        },
        "required": ["attendeeEmails", "startTime", "endTime"],
    },
}


# Order is what the tool-approval list and the model both see.
TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    LIST_CALENDARS,
    LIST_EVENTS,
    SEARCH_EVENTS,
    GET_EVENT,
    CREATE_EVENT,
    UPDATE_EVENT,
    DELETE_EVENT,
    RESPOND_TO_EVENT,
    SUGGEST_TIME,
)
