"""Loads the provider catalog from catalog.yaml.

Shared by: GET /catalog (dropdowns), the compiler (provider→factory),
and the billing pricer (pricing blocks). Adding a model = editing catalog.yaml.

Load path: ``yaml.safe_load`` → ``Catalog.model_validate``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from .languages import agent_language_options
from .models import Catalog

# backend/catalog.yaml — loader lives at services/catalog/loader.py
_CATALOG_PATH = Path(__file__).resolve().parents[2] / "catalog.yaml"


@lru_cache
def get_catalog() -> Catalog:
    # Loaded once per process and cached. catalog.yaml edits (incl. price changes)
    # therefore require a RESTART to take effect — and because the API prices
    # while the worker writes canonical provider/model names, BOTH the API and
    # the worker must be restarted together, or they'll disagree on the catalog.
    with _CATALOG_PATH.open() as f:
        raw = yaml.safe_load(f)
    catalog = Catalog.model_validate(raw)
    # Cross-entry language check — it needs the whole catalog, so it can't live
    # in Catalog's own validator alongside the per-entry rules. Called for the
    # raise: a contradictory label must break startup, not the first request.
    agent_language_options(catalog)
    return catalog
