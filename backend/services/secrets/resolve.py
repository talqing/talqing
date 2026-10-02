"""Secret-only template resolution.

Tools owns the general ``{{args|userdata|secrets}}`` resolver used by operation
trees. This module only understands ``{{secrets.NAME}}`` so integrations and
other non-tool call sites can resolve credential refs without importing tools.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

# {{secrets.NAME}} — name matches SecretName (letter/underscore start).
_SECRET_TOKEN = re.compile(r"\{\{\s*secrets\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class MissingSecretError(ValueError):
    """Raised when a {{secrets.NAME}} template cannot be resolved.

    Fail loudly — never substitute an empty string for a missing credential.
    """

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"secret '{name}' does not exist")


def _lookup(name: str, secrets: Mapping[str, str]) -> str:
    if name not in secrets:
        raise MissingSecretError(name)
    return secrets[name]


def resolve_secretish(value: object, secrets: Mapping[str, str]) -> str | None:
    """Resolve a value that may contain ``{{secrets.NAME}}`` templates.

    Returns a stripped non-empty string, or None if the value is missing / empty
    after resolution. Used for integration credentials and webhook config fields.

    Only ``secrets`` tokens are substituted. Missing names raise
    ``MissingSecretError``. Dict/list values are not coerced to strings (returns
    None) — callers store plain string refs or templates.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None

    text = value
    # Exact single-token form: return the secret value as-is (no forced str()).
    m = _SECRET_TOKEN.fullmatch(text.strip())
    if m:
        resolved = _lookup(m.group(1), secrets)
    else:

        def _sub(mt: re.Match[str]) -> str:
            return _lookup(mt.group(1), secrets)

        resolved = _SECRET_TOKEN.sub(_sub, text)

    if not isinstance(resolved, str) or not resolved.strip():
        return None
    return resolved.strip()
