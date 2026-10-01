"""Listing photos: eBay image URL sizes and a download cache on disk.

Photo URLs come from the Browse API; downloading them is not scraping, it is
what any client does to display a listing. Files are cached by URL so that a
recalibration does not download them again.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import timedelta
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_EBAY_IMAGE_SIZE = re.compile(r"/s-l\d+(\.(?:jpe?g|png|webp))(?=$|\?)", re.IGNORECASE)
# Content types accepted as images; eBay serves JPEG and WebP.
_IMAGE_TYPES = ("image/",)


def resize_ebay_image(url: str, size: str) -> str:
    """Ask eBay's image server for another rendition, for example s-l225 to s-l1600.

    URLs without a size suffix are returned unchanged.
    """
    return _EBAY_IMAGE_SIZE.sub(rf"/{size}\1", url, count=1)


class ImageFetchError(RuntimeError):
    """A photo could not be downloaded."""


class ImageCache:
    """Download photos once and keep them on disk for ``max_age``."""

    def __init__(
        self,
        http: httpx.Client,
        directory: Path,
        *,
        size: str | None = "s-l500",
        max_age: timedelta = timedelta(days=30),
    ) -> None:
        self._http = http
        self._dir = directory
        self._size = size
        self._max_age = max_age

    def fetch(self, url: str) -> bytes:
        """The photo at ``url`` in the configured size, falling back to ``url`` itself."""
        path = self._path(url)
        if path.exists():
            return path.read_bytes()
        candidates = [resize_ebay_image(url, self._size)] if self._size else []
        if url not in candidates:
            candidates.append(url)
        reason = "no candidate URL"
        for candidate in candidates:
            try:
                response = self._http.get(candidate)
            except httpx.HTTPError as exc:
                reason = type(exc).__name__
                continue
            content_type = response.headers.get("content-type", "")
            if response.status_code == 200 and content_type.startswith(_IMAGE_TYPES):
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(response.content)
                tmp.replace(path)
                return response.content
            reason = f"HTTP {response.status_code} {content_type or 'no content type'}"
        raise ImageFetchError(f"could not download {url}: {reason}")

    def prune(self) -> int:
        """Delete cached photos older than ``max_age``; return how many were removed."""
        if not self._dir.exists():
            return 0
        cutoff = time.time() - self._max_age.total_seconds()
        removed = 0
        for path in self._dir.glob("*/*.img"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        if removed:
            log.info("Removed %d cached photo(s) older than %s", removed, self._max_age)
        return removed

    def _path(self, url: str) -> Path:
        digest = hashlib.sha256(f"{self._size}|{url}".encode()).hexdigest()
        # Two-level layout keeps directories small.
        return self._dir / digest[:2] / f"{digest}.img"
