#!/usr/bin/env python3
"""Copy the exported OpenAPI document into the docs site, with servers attached.

`openapi/openapi.json` deliberately carries no `servers` block: the generated
SDKs must refuse to start without an explicit base URL, and a default in the
document would give them one. The docs site has the opposite need — Mintlify's
API playground has to know where to send a request — so the servers live here,
added on the way in.

Run after `PYTHONPATH=backend python openapi/export.py`:

    python documentation/sync-openapi.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "openapi" / "openapi.json"
TARGET = ROOT / "documentation" / "openapi.json"

SERVERS = [
    {"url": "https://api.in.talqing.com", "description": "India (in)"},
    {"url": "https://api.us.talqing.com", "description": "United States (us)"},
]

document = json.loads(SOURCE.read_text())
document["servers"] = SERVERS
document["security"] = [{"BearerAuth": []}]
TARGET.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
print(f"wrote {TARGET.relative_to(ROOT)} — {len(document['paths'])} paths")
