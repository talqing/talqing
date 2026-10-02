"""Turn user-supplied credential input into ``{{secrets.NAME}}`` refs.

Integrations and telephony accept either:
- an existing secret reference (``{{secrets.NAME}}``), or
- plaintext (bot token, API key, …).

Plaintext is written into the tenant secrets store under a preferred name
(or ``preferred_name_<hex>`` when that name is already taken). Callers always
persist the resulting ref — never plaintext — on integration/telephony rows.
"""

from __future__ import annotations

import re
import secrets as pysecrets

from fastapi import HTTPException

from services.user import Context

from .service import CreateSecretRequest, create_secret

# Exact single-token form used at rest for credentials.
_SECRET_REF_RE = re.compile(r"^\{\{\s*secrets\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")


def parse_secret_ref(value: str) -> str | None:
    """Return the secret name if ``value`` is exactly ``{{secrets.NAME}}``."""
    match = _SECRET_REF_RE.fullmatch(value.strip())
    return match.group(1) if match else None


def format_secret_ref(name: str) -> str:
    return f"{{{{secrets.{name}}}}}"


def is_secret_ref(value: object) -> bool:
    return isinstance(value, str) and parse_secret_ref(value) is not None


def allocate_secret_name(preferred: str, existing_names: set[str]) -> str:
    """Pick ``preferred`` or ``preferred_<hex>`` so we never overwrite an existing secret."""
    if preferred not in existing_names:
        return preferred
    for _ in range(32):
        candidate = f"{preferred}_{pysecrets.token_hex(2)}"
        if candidate not in existing_names:
            return candidate
    raise RuntimeError(f"could not allocate a free secret name for {preferred!r}")


async def materialize_secret_input(
    ctx: Context,
    value: str,
    *,
    preferred_name: str,
    existing_names: set[str],
) -> str:
    """Return a ``{{secrets.NAME}}`` ref, creating a secret when ``value`` is plaintext.

    - Existing ref → verify the name exists, return a normalized ref.
    - Plaintext → create under ``preferred_name`` (or a suffixed variant) and
      return the new ref. ``existing_names`` is updated in place.

    Never upserts an existing secret name when the preferred name is taken —
    a suffix is allocated so tool/shared secrets are not clobbered.
    """
    text = value.strip()
    if not text:
        raise HTTPException(status_code=400, detail="secret value must not be empty")

    name = parse_secret_ref(text)
    if name is not None:
        if name not in existing_names:
            raise HTTPException(
                status_code=400,
                detail=f"secret '{name}' does not exist",
            )
        return format_secret_ref(name)

    secret_name = allocate_secret_name(preferred_name, existing_names)
    await create_secret(CreateSecretRequest(name=secret_name, value=text), ctx)
    existing_names.add(secret_name)
    return format_secret_ref(secret_name)
