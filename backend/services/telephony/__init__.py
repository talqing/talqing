"""Telephony domain: carrier accounts, phone numbers, LiveKit SIP.

Not integrations. HTTP routes import this package
(``from services import telephony as svc``), not ``service`` directly.
"""

from __future__ import annotations

from services.telephony.service import (
    assign_number,
    catalog,
    create_account,
    create_outbound_call,
    delete_account,
    get_account,
    get_number,
    import_numbers,
    list_accounts,
    list_numbers,
    list_remote_numbers,
    patch_account,
    patch_number,
    provision_account,
    provision_number,
    unassign_number,
)

__all__ = [
    "assign_number",
    "catalog",
    "create_account",
    "create_outbound_call",
    "delete_account",
    "get_account",
    "get_number",
    "import_numbers",
    "list_accounts",
    "list_numbers",
    "list_remote_numbers",
    "patch_account",
    "patch_number",
    "provision_account",
    "provision_number",
    "unassign_number",
]
