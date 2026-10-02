"""E.164 normalization for telephony DIDs and peers.

**A national-format number is national relative to the DID it was dialled on.**
That is the whole design here, and it is what replaced a platform-wide
``default_e164_region``: with one region there was no difference, and with two
there is no such thing as "the platform's country". `07911123456` is a UK mobile
*and* an Indian-national-format string, and only the DID it arrived on says
which. So callers pass the DID (or its region) and the platform default is the
last resort, for the paths that genuinely have neither.

**The dataset is `phonenumbers`, not our own rules.** The branch this replaced
handled India's trunk prefix by hand, with a `TODO(global)` warning that
generalising it by stripping a leading zero is wrong in three separate ways — the
prefix is `8` in Russia and `1` in the NANP, Italy folded its into the number in
1998 so stripping it silently corrupts every Italian landline, and Spain,
Portugal, Norway, Denmark, Singapore and Hong Kong have no trunk prefix at all.
None of that is logic anyone should hand-write; it is a table, maintained by
Google, shipped as a pure-Python package.

**Two strictnesses, because the two doors want different answers.** A DID we
import or a destination we dial is checked with ``is_valid_number``: there is
money at stake and a typo should be refused. Inbound caller ID is checked with
``is_possible_number``: it comes off a carrier's INVITE, and dropping a real
caller over a number plan our copy of the dataset has not caught up with is
worse than carrying a number we cannot fully verify.

What the old implementation let through, and this does not: its final branch was
string concatenation rather than validation, so ``"4155550101"`` became
``"+4155550101"`` — country code 4 does not exist — and passed both the regex and
the database CHECK. That is exactly the failure a US region turns on, because
`telephony.default_e164_region: US` made every Indian branch inert and dropped a
national-format US number straight through it.
"""

from __future__ import annotations

import phonenumbers


def _configured_default_region() -> str:
    """The last-resort region, from ``telephony.default_e164_region``.

    Used only where nothing better is known. Prefer :func:`region_of_did`.
    """
    from settings import get_settings

    return get_settings().telephony.default_e164_region


def region_of_did(e164: str | None) -> str | None:
    """The ISO 3166-1 alpha-2 region of a DID, for interpreting numbers dialled on it.

    ``None`` when the DID is missing or is not a number we can place, in which
    case the caller falls back to the configured default rather than guessing.
    """
    if not e164:
        return None
    try:
        return phonenumbers.region_code_for_number(phonenumbers.parse(e164, None))
    except phonenumbers.NumberParseException:
        return None


def normalize_e164(
    value: str,
    *,
    region: str | None = None,
    did: str | None = None,
    strict: bool = True,
) -> str:
    """Normalize a phone number to E.164 with a leading ``+``.

    ``+…`` and ``00…`` international forms carry their own country and need no
    region. A national-format number is interpreted against ``region``, or
    against the region of ``did`` — the number it was dialled on, which is the
    honest answer — or, failing both, against the configured default.

    ``strict=True`` (the default) demands a number that is actually assignable in
    its plan: for DIDs we import and destinations we dial, a typo must be refused
    rather than dialled. ``strict=False`` accepts anything of a plausible length
    for its country, which is what inbound caller ID gets.

    Raises ``ValueError`` when the result is not a usable E.164 number. It fails
    closed on an unknown country code: an unidentified caller is a correct
    outcome, and a guess is not.
    """
    if not isinstance(value, str):
        raise ValueError("phone number must be a string")
    raw = value.strip()
    if not raw:
        raise ValueError("phone number is required")

    # `00` is the international prefix in most of the world and `phonenumbers`
    # only recognises it with a region that uses it. Rewriting it here means the
    # number carries its own country and needs no region at all — which is what
    # it means.
    if raw.startswith("00"):
        raw = f"+{raw[2:]}"

    default_region = region or region_of_did(did) or _configured_default_region()
    try:
        parsed = phonenumbers.parse(raw, default_region)
    except phonenumbers.NumberParseException as exc:
        raise ValueError(f"invalid phone number: {value!r} ({exc})") from None

    usable = (
        phonenumbers.is_valid_number(parsed) if strict else phonenumbers.is_possible_number(parsed)
    )
    if not usable:
        raise ValueError(f"invalid E.164 phone number: {value!r}")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def plivo_number_key(e164: str) -> str:
    """Plivo Number API path segment: E.164 without leading ``+``."""
    return normalize_e164(e164)[1:]
