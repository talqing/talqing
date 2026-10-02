"""Telephony domain models and API request/response shapes."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from services.agents.plan import AgentPlanRequest

from .e164 import normalize_e164

TelephonyProvider = Literal["plivo", "exotel", "vobiz", "twilio"]
SetupFieldType = Literal["text", "secret_ref"]
SetupFieldTarget = Literal["account_info", "credentials"]

DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
# At-rest credential values are short ``{{secrets.NAME}}`` refs.
SecretRef = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
# Create/patch accept either a secret ref or plaintext (materialized server-side).
SecretInput = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16384)
]

AccountStatus = Literal["pending", "connected", "provisioning", "ready", "error", "disabled"]
NumberStatus = Literal["pending", "provisioning", "active", "error", "disabled"]

# What a number can actually do right now, as one value. `status` alone cannot
# answer that: an ``active`` number is dead for inbound without an agent, and an
# Exotel DID is dead for inbound until console work nobody can verify over an
# API. Computed here so the dashboard, CoPilot and the SDKs all read the same
# answer instead of each re-deriving it from four fields.
NumberReadiness = Literal[
    "live",  # answering inbound: active, inbound-capable, agent + dispatch rule
    "needs_agent",  # provisioned, nothing assigned to answer it
    "needs_carrier_setup",  # outbound works; inbound blocked in the carrier console
    "setting_up",  # pending | provisioning
    "error",
    "disabled",
]


class TelephonySetupField(BaseModel):
    """A form field shown when connecting a telephony carrier account."""

    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    type: SetupFieldType
    target: SetupFieldTarget
    required: bool = True
    placeholder: str | None = None
    hint: str | None = None
    # Prefilled into the input, not greyed out behind it — the tenant can edit or
    # clear it. Use this only for a value that is right for most accounts on this
    # carrier; a `placeholder` is the right home for a value they must supply
    # themselves and we can only illustrate.
    default: str | None = None


class TelephonySetupInstruction(BaseModel):
    """One numbered action inside a setup step.

    A step says *what* is missing; these say *how* to do it, in the order the
    tenant will do them. Structured rather than one prose blob so the dashboard
    can render a deep link as a button and a paste value next to a Copy button —
    the two things that turn "go configure your carrier" into something a tenant
    can follow without a second browser tab of documentation.
    """

    # frozen: these are registry rows, not request payloads.
    # json_schema_serialization_defaults_required: response-only, and every
    # field below is always sent, so a default must not publish it as
    # optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(frozen=True, json_schema_serialization_defaults_required=True)

    text: str
    # Deep link straight to the carrier console page this action happens on.
    # Adapters emit it only when they can build it without guessing; a dead link
    # into someone's carrier console is worse than no link.
    url: str | None = None
    url_label: str | None = None
    # A value to paste at this point (e.g. ``sip:<trunk_sid>``), with a line
    # saying what it is — a bare opaque identifier next to a Copy button tells
    # the tenant nothing about whether they are pasting the right thing.
    copy_value: str | None = None
    copy_hint: str | None = None


class TelephonySetupStep(BaseModel):
    """One step in getting a carrier account fully working.

    Adapters build these, because which steps exist and how to check them is
    carrier knowledge. Steps a tenant must perform in the carrier's own console
    are ``user_confirmable``: there is no API to read them back, so the tenant
    telling us is the only signal available.
    """

    # frozen: these are registry rows, not request payloads.
    # json_schema_serialization_defaults_required: response-only, and every
    # field below is always sent, so a default must not publish it as
    # optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(frozen=True, json_schema_serialization_defaults_required=True)

    key: str
    title: str
    detail: str | None = None
    instructions: list[TelephonySetupInstruction] = Field(default_factory=list)
    # The carrier's own page for this task, for a tenant who wants the long form.
    doc_url: str | None = None
    doc_label: str | None = None
    done: bool = False
    user_confirmable: bool = False


class TelephonyProviderSpec(BaseModel):
    """Public catalog entry for a telephony carrier provider.

    Registry rows in ``catalog.TELEPHONY_PROVIDERS`` use this same model so
    ``GET /v1/telephony/providers`` can return ``Page[TelephonyProviderSpec]``
    without a dict conversion layer.
    """

    # frozen: these are registry rows, not request payloads.
    # json_schema_serialization_defaults_required: response-only, and every
    # field below is always sent, so a default must not publish it as
    # optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(frozen=True, json_schema_serialization_defaults_required=True)

    provider: TelephonyProvider
    label: str
    description: str
    logo_url: str | None = None
    # Non-secret identity keys required in account_info.
    account_info_keys: list[str] = Field(default_factory=list)
    # Secret-ref keys required in credentials.
    credential_keys: list[str] = Field(default_factory=list)
    setup_fields: list[TelephonySetupField] = Field(default_factory=list)
    # Implemented adapter available?
    implemented: bool = False
    # Does this carrier honour SIP REFER on the trunks we provision? It decides
    # which transport a `transfer` operation uses — REFER, or a bridge dialled
    # into the caller's own room — and nothing else. One flag, per carrier: after
    # ``ensure_account_sip`` there is no per-account state that could differ.
    supports_refer: bool = False


TELEPHONY_ACCOUNT_COLUMNS = """
id, tenant_id, provider, display_name, account_info, credentials, provider_state,
sip_uri, status, status_message, last_synced_at, created_at, updated_at
"""

PHONE_NUMBER_COLUMNS = """
id, tenant_id, telephony_account_id, e164, provider, provider_number_id, label,
can_inbound, can_outbound, inbound_agent_id, livekit_inbound_trunk_id,
livekit_dispatch_rule_id, provider_state, status, status_message, last_synced_at,
created_at, updated_at
"""


def _json_object(value: object, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    raise TypeError(f"{label} must be a JSON object")


class TelephonyAccount(BaseModel):
    id: UUID
    tenant_id: UUID
    provider: TelephonyProvider
    display_name: str
    account_info: dict[str, Any]
    credentials: dict[str, Any]
    provider_state: dict[str, Any]
    # SIP edge this account's provider trunk points at.
    sip_uri: str
    status: AccountStatus
    status_message: str | None
    last_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> TelephonyAccount:
        return cls(
            id=row["id"],
            tenant_id=row["tenant_id"],
            provider=row["provider"],
            display_name=row["display_name"],
            account_info=_json_object(row["account_info"], "account_info"),
            credentials=_json_object(row["credentials"], "credentials"),
            provider_state=_json_object(row["provider_state"], "provider_state"),
            sip_uri=row["sip_uri"],
            status=row["status"],
            status_message=row["status_message"],
            last_synced_at=row["last_synced_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class PhoneNumber(BaseModel):
    id: UUID
    tenant_id: UUID
    telephony_account_id: UUID
    e164: str
    provider: TelephonyProvider
    provider_number_id: str | None
    label: str | None
    can_inbound: bool
    can_outbound: bool
    inbound_agent_id: UUID | None
    livekit_inbound_trunk_id: str | None
    livekit_dispatch_rule_id: str | None
    provider_state: dict[str, Any]
    status: NumberStatus
    status_message: str | None
    last_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> PhoneNumber:
        return cls(
            id=row["id"],
            tenant_id=row["tenant_id"],
            telephony_account_id=row["telephony_account_id"],
            e164=row["e164"],
            provider=row["provider"],
            provider_number_id=row["provider_number_id"],
            label=row["label"],
            can_inbound=row["can_inbound"],
            can_outbound=row["can_outbound"],
            inbound_agent_id=row["inbound_agent_id"],
            livekit_inbound_trunk_id=row["livekit_inbound_trunk_id"],
            livekit_dispatch_rule_id=row["livekit_dispatch_rule_id"],
            provider_state=_json_object(row["provider_state"], "provider_state"),
            status=row["status"],
            status_message=row["status_message"],
            last_synced_at=row["last_synced_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


# ── public API shapes ───────────────────────────────────────────────────────


class TelephonyAccountResponse(BaseModel):
    id: UUID
    provider: TelephonyProvider
    display_name: str
    account_info: dict[str, Any]
    # Never echo resolved secrets — only the ref map ({{secrets.X}} strings).
    credentials: dict[str, Any]
    provider_state: dict[str, Any]
    status: AccountStatus
    status_message: str | None
    # Remaining carrier-console work, already interpreted. Clients render these;
    # they must never re-derive setup state by reading ``provider_state``.
    setup_complete: bool
    setup_steps: list[TelephonySetupStep]
    # How a `transfer` operation reaches a human on this account: ``refer`` hands
    # the caller to the carrier, ``bridge`` dials the human into the caller's own
    # LiveKit room. Interpreted here for the same reason ``setup_steps`` is — a
    # raw capability flag would leave every client re-deriving this.
    #
    # A support and diagnostics field, not a builder-facing one: "which transport
    # ran?" is the first question when a transfer misbehaves, and the last one a
    # builder should have to care about.
    transfer_transport: Literal["refer", "bridge"]
    last_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_account(
        cls,
        account: TelephonyAccount,
        *,
        setup_steps: list[TelephonySetupStep],
        supports_refer: bool,
    ) -> TelephonyAccountResponse:
        return cls(
            id=account.id,
            provider=account.provider,
            display_name=account.display_name,
            account_info=account.account_info,
            credentials=account.credentials,
            provider_state=account.provider_state,
            status=account.status,
            status_message=account.status_message,
            setup_complete=all(step.done for step in setup_steps),
            setup_steps=setup_steps,
            transfer_transport="refer" if supports_refer else "bridge",
            last_synced_at=account.last_synced_at,
            created_at=account.created_at,
            updated_at=account.updated_at,
        )


def _readiness(number: PhoneNumber) -> NumberReadiness:
    """Collapse status × capability × routing into the one state a user cares about.

    ``livekit_dispatch_rule_id`` is checked, not just ``inbound_agent_id``: the
    rule is what actually dispatches an agent, and the two can disagree if a
    LiveKit call failed after the row was written.
    """
    if number.status == "disabled":
        return "disabled"
    if number.status == "error":
        return "error"
    if number.status in ("pending", "provisioning"):
        return "setting_up"
    if not number.can_inbound:
        return "needs_carrier_setup"
    if number.inbound_agent_id and number.livekit_dispatch_rule_id:
        return "live"
    return "needs_agent"


class PhoneNumberResponse(BaseModel):
    id: UUID
    telephony_account_id: UUID
    e164: str
    provider: TelephonyProvider
    provider_number_id: str | None
    label: str | None
    can_inbound: bool
    can_outbound: bool
    inbound_agent_id: UUID | None
    livekit_inbound_trunk_id: str | None
    livekit_dispatch_rule_id: str | None
    provider_state: dict[str, Any]
    status: NumberStatus
    status_message: str | None
    readiness: NumberReadiness
    last_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_number(cls, number: PhoneNumber) -> PhoneNumberResponse:
        return cls(
            id=number.id,
            telephony_account_id=number.telephony_account_id,
            e164=number.e164,
            provider=number.provider,
            provider_number_id=number.provider_number_id,
            label=number.label,
            can_inbound=number.can_inbound,
            can_outbound=number.can_outbound,
            inbound_agent_id=number.inbound_agent_id,
            livekit_inbound_trunk_id=number.livekit_inbound_trunk_id,
            livekit_dispatch_rule_id=number.livekit_dispatch_rule_id,
            provider_state=number.provider_state,
            status=number.status,
            status_message=number.status_message,
            readiness=_readiness(number),
            last_synced_at=number.last_synced_at,
            created_at=number.created_at,
            updated_at=number.updated_at,
        )


class CreateTelephonyAccountRequest(BaseModel):
    provider: TelephonyProvider
    display_name: DisplayName
    account_info: dict[str, Any] = Field(default_factory=dict)
    # Values may be plaintext or ``{{secrets.NAME}}``; stored as refs after materialize.
    credentials: dict[str, SecretInput] = Field(default_factory=dict)


class PatchTelephonyAccountRequest(BaseModel):
    display_name: DisplayName | None = None
    account_info: dict[str, Any] | None = None
    credentials: dict[str, SecretInput] | None = None
    status: Literal["disabled", "connected"] | None = None
    # The tenant confirming they have done the carrier-console work that no API
    # can check (Exotel App Bazaar). Gates inbound on this account's numbers.
    console_setup_confirmed: bool | None = None


class ImportPhoneNumbersRequest(BaseModel):
    telephony_account_id: UUID
    e164s: list[str] = Field(min_length=1)
    # Provisioning is what makes an imported number usable, so it is part of
    # importing rather than a second step. False imports the rows only.
    provision: bool = True

    @field_validator("e164s")
    @classmethod
    def _normalize_list(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in values:
            e164 = normalize_e164(raw)
            if e164 not in seen:
                seen.add(e164)
                out.append(e164)
        if not out:
            raise ValueError("at least one phone number is required")
        return out


class PatchPhoneNumberRequest(BaseModel):
    label: str | None = None
    can_inbound: bool | None = None
    can_outbound: bool | None = None
    # disabled: tear down LiveKit. pending: re-open a disabled number for re-provision.
    status: Literal["disabled", "pending"] | None = None


class AssignPhoneNumberRequest(BaseModel):
    agent_id: UUID


class RemoteNumber(BaseModel):
    e164: str
    provider_number_id: str | None = None
    label: str | None = None
    already_imported: bool = False


class ImportedNumberResult(BaseModel):
    """Outcome for one requested E.164 — success and failure share one shape.

    Import is per-number rather than all-or-nothing: one DID that is already
    registered, or that the carrier refuses to bind, must not discard the rest
    of the batch. ``phone_number`` is present whenever a row was written, so a
    number that imported but failed to provision still comes back (in ``error``)
    for the caller to show and retry.
    """

    e164: str
    ok: bool
    phone_number: PhoneNumberResponse | None = None
    error: str | None = None


class ImportPhoneNumbersResponse(BaseModel):
    """Batch import result — never a bare JSON array (see ``Page`` docs)."""

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    items: list[ImportedNumberResult]


class OutboundCallRequest(AgentPlanRequest):
    """One phone call: who to dial, from which of your numbers, and what runs.

    `vars` comes from `AgentPlanRequest` and lasts exactly as long as this call.
    """

    from_phone_number_id: UUID
    to: str
    # No contact_key: on a phone call the two numbers already name the person,
    # and inbound derives the same `sip:{your DID}:{their number}`. A key you
    # could pass here would only ever split one caller into two identities that
    # inbound could never find again.
    userdata: dict[str, object] | None = None

    # `to` is NOT normalized here, deliberately. A national-format number is
    # national relative to the DID it will be dialled FROM, and that DID is a
    # row this validator cannot read — so normalization happens in
    # `create_outbound_call`, once `from_phone_number_id` has been resolved.
    # Doing it here meant interpreting every bare number against a platform-wide
    # default, which stopped having an answer the day there were two regions.
    @field_validator("to")
    @classmethod
    def _to_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a destination number is required")
        return value.strip()


class OutboundCallResponse(BaseModel):
    """Which call this is, and what resolving it complained about."""

    # Response-only: `warnings` is always sent, so its default must not publish
    # it as optional.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    # The same two fields `CallTokenResponse` keeps beyond the join credential,
    # and for the same reason: nothing has been dialled yet, so there is no call
    # to describe. `GET /v1/calls/{id}` answers on this id from the moment it is
    # returned and carries the status, the conversation and the version;
    # `warnings` is not stored anywhere, so it can only be returned here.
    session_id: UUID
    # Resolved against state at CALL time, so a team member deleted since the
    # agent was published is a warning only this response can carry.
    warnings: list[str] = Field(default_factory=list)
