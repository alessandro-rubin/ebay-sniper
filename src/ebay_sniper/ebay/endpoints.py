"""eBay API roots. A keyset works only in the environment it was created in."""

from __future__ import annotations

from typing import Literal

EbayEnvironment = Literal["production", "sandbox"]

API_ROOTS: dict[EbayEnvironment, str] = {
    "production": "https://api.ebay.com",
    "sandbox": "https://api.sandbox.ebay.com",
}

# eBay App IDs embed the environment of their keyset:
# <name>-<app>-PRD-<hex>-<hex> or <name>-<app>-SBX-<hex>-<hex>.
_KEYSET_MARKERS: dict[str, EbayEnvironment] = {"-PRD-": "production", "-SBX-": "sandbox"}


def keyset_environment(client_id: str) -> EbayEnvironment | None:
    """The environment a client id belongs to, or None if it does not say."""
    for marker, environment in _KEYSET_MARKERS.items():
        if marker in client_id:
            return environment
    return None
