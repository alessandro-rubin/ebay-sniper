"""Interface between the pipeline and the notification channels."""

from __future__ import annotations

from typing import Protocol

from ebay_sniper.models import Listing


class NotificationError(RuntimeError):
    """A notification could not be delivered."""


class Notifier(Protocol):
    def notify_listing(self, listing: Listing) -> None:
        """Send one listing; raise NotificationError on failure."""
        ...

    def send_text(self, text: str) -> None:
        """Send a short HTML text; raise NotificationError on failure."""
        ...
