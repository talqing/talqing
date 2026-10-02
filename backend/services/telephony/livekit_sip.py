"""LiveKit SIP admin helpers (self-hosted SIPService on livekit-server)."""

from __future__ import annotations

import json
import logging
import random
from typing import Any
from uuid import UUID

import aiohttp
from google.protobuf import duration_pb2
from livekit import api
from livekit.api import RoomAgentDispatch, RoomConfiguration

from settings import get_settings

logger = logging.getLogger("talqing.telephony.livekit_sip")


# One aiohttp session for every LiveKit call this process makes, so a dial pays
# for keep-alive rather than a fresh TCP + TLS handshake. Invisible at one call a
# minute; at batch rates it was the per-dial latency floor, twice over — a batch
# places two RPCs per recipient and each was opening its own connection.
#
# The `LiveKitAPI` wrapper stays per-call: passing `session=` makes it treat the
# session as the caller's, so its `aclose()` — and therefore every `async with
# lk_client()` below — leaves the shared session open. Constructing the wrapper
# is a handful of service objects and no I/O.
_session: aiohttp.ClientSession | None = None

# LiveKit's own default. Named here because it is now the deadline on a shared
# connection rather than a private one, and because the batch dispatcher's dial
# timeout has to stay above it to mean anything.
LIVEKIT_HTTP_TIMEOUT_SECONDS = 10


def lk_client() -> api.LiveKitAPI:
    """A LiveKit API client on this process's shared HTTP session.

    Safe inside `async with`: exiting closes the wrapper, not the session.
    """
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=LIVEKIT_HTTP_TIMEOUT_SECONDS)
        )
    s = get_settings()
    return api.LiveKitAPI(
        url=s.livekit.url,
        api_key=s.livekit.api_key,
        api_secret=s.livekit.api_secret,
        session=_session,
    )


async def aclose() -> None:
    """Close the shared HTTP session. Call once, at process shutdown."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


def inbound_dispatch_metadata(
    *,
    tenant_id: UUID,
    agent_id: UUID,
    phone_number_id: UUID,
    telephony_account_id: UUID,
    did_e164: str,
) -> str:
    return json.dumps(
        {
            "kind": "sip_call",
            "direction": "inbound",
            "tenant": str(tenant_id),
            "agent": str(agent_id),
            "phone_number_id": str(phone_number_id),
            "telephony_account_id": str(telephony_account_id),
            "did_e164": did_e164,
        }
    )


def _max_call_duration() -> duration_pb2.Duration:
    """Wall-clock cap sent to LiveKit on every call, inbound and outbound."""
    return duration_pb2.Duration(seconds=get_settings().livekit.sip.max_call_duration_seconds)


def _ringing_timeout(override_seconds: float | None = None) -> duration_pb2.Duration:
    """Ring/answer deadline. Outbound: callee picks up. Inbound: agent joins.

    ``override_seconds`` is for a dial whose ring window is the caller's own
    choice rather than the platform's — a transfer's ``ringing_timeout``, which
    is a different knob with a different range from
    ``livekit.sip.ringing_timeout_seconds``.
    """
    if override_seconds is not None:
        return duration_pb2.Duration(seconds=int(override_seconds))
    return duration_pb2.Duration(seconds=get_settings().livekit.sip.ringing_timeout_seconds)


def sip_transport(name: str) -> api.SIPTransport:
    """One adapter's ``OutboundInlineConfig.transport`` as the LiveKit enum.

    TCP for anything unrecognised: it is what every trunk we provision is
    configured for, and it is the transport LiveKit's own examples assume.
    """
    return {
        "tcp": api.SIPTransport.SIP_TRANSPORT_TCP,
        "tls": api.SIPTransport.SIP_TRANSPORT_TLS,
        "udp": api.SIPTransport.SIP_TRANSPORT_UDP,
    }.get(name, api.SIPTransport.SIP_TRANSPORT_TCP)


async def ensure_inbound_trunk(*, name: str, e164: str) -> str:
    """The LiveKit inbound trunk for one DID, adopting one if it already exists.

    Adopt rather than create-and-hope, because LiveKit refuses a second trunk on
    the same number and there is no other way back from a half-finished
    provision. `provision_number` creates the trunk and *then* writes its id to
    `phone_numbers`; anything that stops the process in between — a killed
    container, a deploy, an OOM — leaves a trunk nothing references, and every
    later attempt on that DID fails with "Conflicting inbound SIP Trunks"
    forever. Adopting turns that permanent block into a no-op.

    A DID belongs to one workspace at a time, so a trunk already carrying this
    number is by definition the one this number should be using. The trunk is
    plumbing keyed by the number; *who answers* is the dispatch rule's business
    (`ensure_individual_dispatch_rule`), and that is repointed at the current
    tenant separately.

    The settings are re-applied on adoption: a trunk created by an older build,
    or before `livekit.sip.*` was retuned, would otherwise keep running on
    whatever it was made with and nothing would ever say so.

    Same shape as `TwilioAdapter._find_trunk` on the carrier side, and for the
    same reason.
    """
    info = api.SIPInboundTrunkInfo(
        name=name,
        numbers=[e164],
        max_call_duration=_max_call_duration(),
        ringing_timeout=_ringing_timeout(),
    )
    async with lk_client() as lk:
        # At most one row: LiveKit buckets inbound trunks by called number and
        # refuses a second one in the same bucket when `allowed_numbers` is
        # empty, which it always is for us
        # (`protocol/sip/sip.go::ValidateTrunksIter` → `validateTrunkInbound`).
        # That guarantee is what makes adoption unambiguous — there is never a
        # choice of trunk to make.
        #
        # The filter also returns the region's numberless default trunk (the
        # WhatsApp callee route, `ensure_whatsapp_route`), since a trunk with no
        # numbers matches every number. That one never conflicts — LiveKit only
        # falls back to it when no number-specific trunk matches — so it is not
        # a candidate for adoption.
        listed = await lk.sip.list_sip_inbound_trunk(api.ListSIPInboundTrunkRequest(numbers=[e164]))
        existing = [t for t in listed.items if e164 in t.numbers]
        if existing:
            adopted = existing[0]
            if list(adopted.numbers) != [e164]:
                # Not one of ours: we only ever create single-number trunks. The
                # update below replaces a trunk *entirely*, so adopting this one
                # would write `numbers=[e164]` over it and silently stop the
                # other DIDs on it from routing. Refuse instead — and refuse
                # here rather than falling through to a create, which LiveKit
                # would reject with a conflict error naming a trunk id that
                # means nothing to whoever is reading it.
                raise RuntimeError(
                    f"LiveKit trunk {adopted.sip_trunk_id} already serves {e164} together with "
                    f"{[n for n in adopted.numbers if n != e164]}. Talqing did not create it; "
                    "remove or split it in LiveKit before provisioning this number."
                )
            logger.info(
                "adopting existing livekit inbound trunk %s for %s", adopted.sip_trunk_id, e164
            )
            await lk.sip.update_sip_inbound_trunk(adopted.sip_trunk_id, info)
            return adopted.sip_trunk_id

        trunk = await lk.sip.create_sip_inbound_trunk(api.CreateSIPInboundTrunkRequest(trunk=info))
    trunk_id = trunk.sip_trunk_id
    if not trunk_id:
        raise RuntimeError("LiveKit CreateSIPInboundTrunk returned empty sip_trunk_id")
    logger.info("created livekit inbound trunk %s for %s", trunk_id, e164)
    return trunk_id


async def ensure_individual_dispatch_rule(
    *,
    name: str,
    trunk_id: str,
    agent_name: str,
    metadata: str,
    room_prefix: str = "sip-",
) -> str:
    """The dispatch rule for one trunk, adopting one if it already exists.

    ``trunk_ids`` must never be empty in multi-tenant deployments.

    Adoption is not optional here the way it is for the trunk: a trunk we
    adopted may already carry a rule, and *adding* a second one would dispatch
    two agents into the same call. This rewrites the rule instead — which is
    also what repoints an adopted trunk at whoever owns the DID now, since the
    tenant, agent and phone-number ids all live in the rule's metadata.
    """
    if not trunk_id.strip():
        raise ValueError("trunk_id is required for dispatch rule scoping")
    info = api.SIPDispatchRuleInfo(
        name=name,
        trunk_ids=[trunk_id],
        rule=api.SIPDispatchRule(
            dispatch_rule_individual=api.SIPDispatchRuleIndividual(room_prefix=room_prefix)
        ),
        room_config=RoomConfiguration(
            agents=[RoomAgentDispatch(agent_name=agent_name, metadata=metadata)]
        ),
    )
    async with lk_client() as lk:
        # Unlike trunks, LiveKit does not cap this at one — a trunk may carry
        # several rules (that is how pin routing works). We only ever make one,
        # so anything else here was made by something other than us, and both
        # ways out are wrong: rewriting a rule shared with other trunks
        # re-routes their calls, and adding a second rule beside an existing one
        # dispatches two agents into the same call. Refuse and say which.
        existing = list(
            (
                await lk.sip.list_sip_dispatch_rule(
                    api.ListSIPDispatchRuleRequest(trunk_ids=[trunk_id])
                )
            ).items
        )
        if len(existing) > 1:
            raise RuntimeError(
                f"LiveKit trunk {trunk_id} already has {len(existing)} dispatch rules "
                f"({', '.join(r.sip_dispatch_rule_id for r in existing)}). Talqing creates one; "
                "remove the extras in LiveKit before provisioning this number."
            )
        if existing:
            adopted = existing[0]
            if list(adopted.trunk_ids) != [trunk_id]:
                raise RuntimeError(
                    f"LiveKit dispatch rule {adopted.sip_dispatch_rule_id} serves trunk "
                    f"{trunk_id} together with "
                    f"{[t for t in adopted.trunk_ids if t != trunk_id]}. Talqing did not create "
                    "it; scope it to a single trunk in LiveKit before provisioning this number."
                )
            logger.info(
                "adopting existing livekit dispatch rule %s for trunk %s",
                adopted.sip_dispatch_rule_id,
                trunk_id,
            )
            await lk.sip.update_sip_dispatch_rule(adopted.sip_dispatch_rule_id, info)
            return adopted.sip_dispatch_rule_id

        rule = await lk.sip.create_sip_dispatch_rule(
            api.CreateSIPDispatchRuleRequest(dispatch_rule=info)
        )
    rule_id = rule.sip_dispatch_rule_id
    if not rule_id:
        raise RuntimeError("LiveKit CreateSIPDispatchRule returned empty id")
    logger.info("created livekit dispatch rule %s for trunk %s", rule_id, trunk_id)
    return rule_id


# The region's one trunk and rule for WhatsApp calls. Matched by name, because
# nothing else we store points at them.
WHATSAPP_ROUTE_NAME = "talqing-whatsapp-calls"


async def ensure_whatsapp_route() -> None:
    """The default inbound trunk and callee rule every WhatsApp call lands on.

    Our voice webhook creates the call's room and dispatches its agent, then
    tells Twilio to dial ``sip:<room>@<edge>``. All LiveKit has to do is drop
    that leg into the room its user part names. A trunk with no ``numbers`` is
    LiveKit's default trunk, consulted only when no number-specific trunk
    matches, so carrier DIDs keep their own trunks and rules. The callee rule
    carries no agents: the webhook already dispatched the only one.

    Region-wide and never deleted. Adopted on every trigger enable, which is also
    what re-applies a rotated ``livekit.sip.whatsapp`` credential.
    """
    credential = get_settings().livekit.sip.whatsapp
    info = api.SIPInboundTrunkInfo(
        name=WHATSAPP_ROUTE_NAME,
        numbers=[],
        auth_username=credential.username,
        auth_password=credential.password,
        max_call_duration=_max_call_duration(),
        ringing_timeout=_ringing_timeout(),
    )
    async with lk_client() as lk:
        trunks = (await lk.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())).items
        default = [t for t in trunks if not t.numbers]
        if any(t.name != WHATSAPP_ROUTE_NAME for t in default):
            # LiveKit allows one default trunk per project, and this one is not
            # ours to repurpose.
            raise RuntimeError(
                "LiveKit already has a default inbound trunk (one with no numbers) that "
                "Talqing did not create; remove it before turning on WhatsApp calls."
            )
        if default:
            trunk_id = default[0].sip_trunk_id
            await lk.sip.update_inbound_trunk(trunk_id, info)
        else:
            trunk = await lk.sip.create_inbound_trunk(api.CreateSIPInboundTrunkRequest(trunk=info))
            trunk_id = trunk.sip_trunk_id
            logger.info("created livekit whatsapp trunk %s", trunk_id)

        rule = api.SIPDispatchRuleInfo(
            name=WHATSAPP_ROUTE_NAME,
            trunk_ids=[trunk_id],
            rule=api.SIPDispatchRule(
                dispatch_rule_callee=api.SIPDispatchRuleCallee(room_prefix="")
            ),
        )
        rules = (
            await lk.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest(trunk_ids=[trunk_id]))
        ).items
        if len(rules) > 1:
            raise RuntimeError(
                f"LiveKit trunk {trunk_id} has {len(rules)} dispatch rules; Talqing creates "
                "one. Remove the extras before turning on WhatsApp calls."
            )
        if rules:
            await lk.sip.update_dispatch_rule(rules[0].sip_dispatch_rule_id, rule)
        else:
            created = await lk.sip.create_dispatch_rule(
                api.CreateSIPDispatchRuleRequest(dispatch_rule=rule)
            )
            logger.info("created livekit whatsapp dispatch rule %s", created.sip_dispatch_rule_id)


async def update_dispatch_rule_metadata(
    *,
    rule_id: str,
    trunk_id: str,
    name: str,
    agent_name: str,
    metadata: str,
    room_prefix: str = "sip-",
) -> None:
    """Replace dispatch rule agent metadata (e.g. agent reassignment)."""
    if not rule_id.strip():
        raise ValueError("rule_id is required")
    info = api.SIPDispatchRuleInfo(
        name=name,
        trunk_ids=[trunk_id],
        rule=api.SIPDispatchRule(
            dispatch_rule_individual=api.SIPDispatchRuleIndividual(
                room_prefix=room_prefix,
            )
        ),
        room_config=RoomConfiguration(
            agents=[
                RoomAgentDispatch(
                    agent_name=agent_name,
                    metadata=metadata,
                )
            ]
        ),
    )
    async with lk_client() as lk:
        await lk.sip.update_sip_dispatch_rule(rule_id, info)
    logger.info("updated livekit dispatch rule %s", rule_id)


async def delete_dispatch_rule(rule_id: str | None) -> None:
    if not rule_id:
        return
    async with lk_client() as lk:
        await lk.sip.delete_sip_dispatch_rule(
            api.DeleteSIPDispatchRuleRequest(sip_dispatch_rule_id=rule_id)
        )
    logger.info("deleted livekit dispatch rule %s", rule_id)


async def _delete_trunk(trunk_id: str | None) -> None:
    if not trunk_id:
        return
    async with lk_client() as lk:
        await lk.sip.delete_sip_trunk(api.DeleteSIPTrunkRequest(sip_trunk_id=trunk_id))
    logger.info("deleted livekit trunk %s", trunk_id)


async def create_sip_participant(
    *,
    room_name: str,
    sip_call_to: str,
    sip_number: str,
    hostname: str,
    auth_username: str,
    auth_password: str,
    participant_identity: str,
    transport: api.SIPTransport | None = None,
    wait_until_answered: bool = True,
    play_dialtone: bool = True,
    ringing_timeout_seconds: float | None = None,
) -> Any:
    """Place an outbound call via inline trunk config (BYO secrets).

    ``ringing_timeout_seconds`` overrides the platform-wide ring window for one
    dial. A call transfer sets it, because how long a builder is willing to let
    a colleague's desk phone ring is their decision about their own escalation,
    not a deployment setting.
    """
    trunk = api.SIPOutboundConfig(
        hostname=hostname,
        auth_username=auth_username,
        auth_password=auth_password,
    )
    if transport is not None:
        trunk.transport = transport
    req = api.CreateSIPParticipantRequest(
        room_name=room_name,
        sip_call_to=sip_call_to,
        sip_number=sip_number,
        participant_identity=participant_identity,
        wait_until_answered=wait_until_answered,
        play_dialtone=play_dialtone,
        max_call_duration=_max_call_duration(),
        ringing_timeout=_ringing_timeout(ringing_timeout_seconds),
        trunk=trunk,
    )
    async with lk_client() as lk:
        return await lk.sip.create_sip_participant(req)


async def transfer_sip_participant(
    *,
    room_name: str,
    participant_identity: str,
    transfer_to: str,
    ringing_timeout_seconds: float,
) -> Any:
    """Send a SIP REFER for one participant — the carrier takes the call from us.

    Called directly rather than through ``job_ctx.transfer_sip_participant()``:
    the job-context helper takes only ``(participant, transfer_to,
    play_dialtone)`` and therefore drops ``ringing_timeout``, which is the field
    that decides how long the caller listens to nothing before we can offer them
    something else.

    ``play_dialtone`` is left False. The carrier owns the media the moment the
    REFER is accepted, so there is nothing for us to play over — unlike the
    bridge path, where the caller sits in silence while we dial.

    Raises ``api.SipCallError`` when the carrier refuses, carrying the SIP status
    the caller must never hear and an operator always wants.
    """
    req = api.TransferSIPParticipantRequest(
        room_name=room_name,
        participant_identity=participant_identity,
        transfer_to=transfer_to,
        play_dialtone=False,
    )
    req.ringing_timeout.FromSeconds(int(ringing_timeout_seconds))
    async with lk_client() as lk:
        return await lk.sip.transfer_sip_participant(req)


# There is deliberately no `move_participant` helper here. Moving a participant
# between rooms is the documented way to merge a warm transfer, and it does not
# work on a self-hosted server: `livekit/pkg/service/roommanager.go` answers
# `MoveParticipant` with `errors.New("not implemented")` — a Cloud-only API,
# stubbed so the proto stays in sync. Warm transfer keeps both legs in one room
# and merges them with `UpdateSubscriptions` instead
# (`workers/voice/warm_transfer.py`).


def assign_sip_uri() -> str:
    """Pick this account's SIP edge, at random, from the configured entries.

    There is one entry per region and normally one region, so this degenerates
    to picking it. Call-level load balancing happens at the edge itself;
    what this chooses is which *edge* a carrier connects to,
    which only becomes a real decision when a second region exists. Persisting
    the choice is what makes re-pointing an account possible.
    """
    uris = get_settings().livekit.sip.uri
    if not uris:
        raise RuntimeError(
            "livekit.sip.uri is empty; set the public SIP host:port entries "
            "announced to Plivo/Exotel (e.g. sip.example.com:5060)"
        )
    return random.choice(uris)


def platform_sip_uri_for_provider(sip_uri: str) -> str:
    """Origination URI for one account's SIP edge (host:port;transport=…)."""
    s = get_settings()
    # An entry dropped from config would otherwise keep provisioning against a
    # hostname nothing serves, and the failure would surface as calls that never
    # arrive rather than as an error here. Re-pointing accounts onto a new name
    # leans on this: remove the old entries last, and anything missed fails
    # loudly at provision time.
    if sip_uri not in s.livekit.sip.uri:
        raise RuntimeError(
            f"account is pinned to SIP URI {sip_uri!r}, which is no longer in "
            f"livekit.sip.uri ({', '.join(s.livekit.sip.uri) or 'empty'}); restore "
            "the entry or re-point the account"
        )
    host = sip_uri.split(";")[0]
    if s.env != "local" and (
        host.startswith("localhost") or host.startswith("127.0.0.1") or host.startswith("0.0.0.0")
    ):
        raise RuntimeError(f"livekit.sip.uri must be public hosts outside local (got {sip_uri!r})")
    if ";transport=" in sip_uri:
        return sip_uri
    return f"{sip_uri};transport={s.livekit.sip.transport}"


async def teardown_number_livekit(
    *,
    dispatch_rule_id: str | None,
    inbound_trunk_id: str | None,
) -> None:
    """Delete LiveKit dispatch rule then inbound trunk. Raises on failure."""
    await delete_dispatch_rule(dispatch_rule_id)
    await _delete_trunk(inbound_trunk_id)
