"""Retry helpers shared by the HTTP clients."""

from __future__ import annotations

import random

import httpx


def backoff_delay(attempt: int, *, base: float, cap: float = 60.0) -> float:
    """Exponential backoff with jitter for the given 1-based attempt number."""
    ceiling = min(cap, base * 2 ** (attempt - 1))
    return random.uniform(ceiling / 2, ceiling)


def retry_after_seconds(response: httpx.Response) -> float | None:
    """The Retry-After header in seconds, when present in that form."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        # The HTTP-date form is not worth supporting here.
        return None
