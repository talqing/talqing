"""Tenant secrets store — CRUD, runtime load, and secret-ref resolution.

Only this package may query the ``secrets`` table or encrypt/decrypt its values.
"""

from __future__ import annotations

from .materialize import (  # noqa: F401
    allocate_secret_name,
    format_secret_ref,
    is_secret_ref,
    materialize_secret_input,
    parse_secret_ref,
)
from .resolve import MissingSecretError, resolve_secretish  # noqa: F401
from .service import (  # noqa: F401
    CreateSecretRequest,
    SecretName,
    SecretRef,
    SecretResponse,
    create_secret,
    delete_secret,
    list_secret_names,
    list_secret_refs,
    list_secrets,
    load_secrets,
)
