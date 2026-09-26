"""Telegram notifications through the Bot API, with plain httpx.

The bot token is part of every request URL, so error messages built here never
include httpx exception texts or URLs.
"""

from __future__ import annotations

import html
import logging
import re
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import SecretStr

from ebay_sniper.config import TelegramConfig
from ebay_sniper.models import CurrencyConverter, Listing, Verdict
from ebay_sniper.notify.base import NotificationError
from ebay_sniper.retry import backoff_delay

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org"
MAX_CAPTION_LENGTH = 1024
LARGE_IMAGE_SIZE = "s-l1600"
# Characters of the seller's condition notes shown in a notification.
MAX_NOTES_LENGTH = 300

_EBAY_IMAGE_SIZE = re.compile(r"/s-l\d+(\.(?:jpe?g|png|webp))(?=$|\?)", re.IGNORECASE)
_FORMAT_NAMES = {
    "AUCTION": "Auction",
    "FIXED_PRICE": "Buy It Now",
    "BEST_OFFER": "Best Offer",
    "CLASSIFIED_AD": "Classified ad",
}


class TelegramError(NotificationError):
    """A Bot API call failed. The message never contains the bot token."""

    def __init__(self, method: str, description: str, *, status_code: int | None = None) -> None:
        super().__init__(f"Telegram {method}: {description}")
        self.method = method
        self.description = description
        self.status_code = status_code


class TelegramClient:
    """Minimal Bot API client: JSON calls with retries on 429 and server errors."""

    def __init__(
        self,
        http: httpx.Client,
        token: SecretStr,
        *,
        max_attempts: int = 3,
        backoff_base_s: float = 2.0,
        max_retry_wait_s: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http
        self._base_url = f"{API_URL}/bot{token.get_secret_value()}"
        self._max_attempts = max(max_attempts, 1)
        self._backoff_base_s = backoff_base_s
        self._max_retry_wait_s = max_retry_wait_s
        self._sleep = sleep

    def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        """Call a Bot API method and return its ``result``."""
        for attempt in range(1, self._max_attempts + 1):
            last_attempt = attempt == self._max_attempts
            try:
                response = self._http.post(f"{self._base_url}/{method}", json=payload or {})
            except httpx.HTTPError as exc:
                reason = f"request failed ({type(exc).__name__})"
                transient = isinstance(exc, httpx.TransportError)
                if last_attempt or not transient or not self._wait(method, attempt, reason, None):
                    raise TelegramError(method, reason) from None
                continue
            body = _json_body(response)
            if body.get("ok") is True:
                return body.get("result")
            description = str(body.get("description") or f"HTTP {response.status_code}")
            retryable = response.status_code == 429 or response.status_code >= 500
            if (
                retryable
                and not last_attempt
                and self._wait(method, attempt, description, _retry_after(body))
            ):
                continue
            raise TelegramError(method, description, status_code=response.status_code)
        raise AssertionError("unreachable")

    def _wait(self, method: str, attempt: int, reason: str, retry_after: float | None) -> bool:
        if retry_after is None:
            delay = backoff_delay(attempt, base=self._backoff_base_s)
        else:
            delay = retry_after
        if delay > self._max_retry_wait_s:
            log.warning(
                "Telegram %s failed (%s), asked to wait %.0f s: giving up", method, reason, delay
            )
            return False
        log.warning("Telegram %s failed (%s), retrying in %.1f s", method, reason, delay)
        self._sleep(delay)
        return True


def _json_body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _retry_after(body: dict[str, Any]) -> float | None:
    parameters = body.get("parameters")
    if isinstance(parameters, dict) and isinstance(parameters.get("retry_after"), int | float):
        return float(parameters["retry_after"])
    return None


class TelegramNotifier:
    """Formats listings and sends them to one chat."""

    def __init__(
        self,
        client: TelegramClient,
        chat_id: str,
        config: TelegramConfig,
        *,
        converter: CurrencyConverter | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._chat_id = chat_id
        self._max_photos = config.max_photos if config.send_all_photos else 1
        self._tz = ZoneInfo(config.timezone)
        self._converter = converter
        self._clock = clock

    def notify_listing(self, listing: Listing) -> None:
        """Photos, details and a button that opens the listing (the eBay app on a phone).

        A photo problem never loses the notification: it falls back to text.
        """
        text = format_listing(listing, tz=self._tz, now=self._clock(), converter=self._converter)
        markup = {"inline_keyboard": [[{"text": "Open on eBay", "url": listing.url}]]}
        photos = listing.image_urls[: self._max_photos]
        if len(photos) == 1 and len(text) <= MAX_CAPTION_LENGTH:
            if self._send_photos(photos, caption=text, markup=markup):
                return
        elif photos:
            self._send_photos(photos, caption=None, markup=None)
        self.send_text(text, markup=markup)

    def send_text(self, text: str, *, markup: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if markup is not None:
            payload["reply_markup"] = markup
        self._client.call("sendMessage", payload)

    def check(self) -> str:
        """Verify the token and the chat id; return a description of both."""
        bot = self._client.call("getMe")
        chat = self._client.call("getChat", {"chat_id": self._chat_id})
        chat_name = chat.get("title") or chat.get("username") or chat.get("first_name") or "?"
        return f"bot @{bot.get('username', '?')}, chat {chat_name!r} ({chat.get('type', '?')})"

    def _send_photos(
        self, photos: Sequence[str], *, caption: str | None, markup: dict[str, Any] | None
    ) -> bool:
        """Send the photos, first in large size; False if Telegram cannot fetch them."""
        for urls in _photo_variants(photos):
            try:
                if len(urls) == 1:
                    payload: dict[str, Any] = {"chat_id": self._chat_id, "photo": urls[0]}
                    if caption is not None:
                        payload |= {"caption": caption, "parse_mode": "HTML"}
                    else:
                        # The details message that follows is the one that rings.
                        payload["disable_notification"] = True
                    if markup is not None:
                        payload["reply_markup"] = markup
                    self._client.call("sendPhoto", payload)
                else:
                    media = [{"type": "photo", "media": url} for url in urls]
                    self._client.call(
                        "sendMediaGroup",
                        {"chat_id": self._chat_id, "media": media, "disable_notification": True},
                    )
            except TelegramError as exc:
                # 400 is what Telegram returns when it cannot fetch or use an image.
                if exc.status_code != 400:
                    raise
                log.warning("Telegram rejected %d photo(s): %s", len(urls), exc.description)
                continue
            return True
        return False


def upscale_ebay_image(url: str) -> str:
    """Ask eBay's image server for a larger rendition (for example s-l225 to s-l1600)."""
    return _EBAY_IMAGE_SIZE.sub(rf"/{LARGE_IMAGE_SIZE}\1", url, count=1)


def _photo_variants(photos: Sequence[str]) -> Iterator[tuple[str, ...]]:
    large = tuple(upscale_ebay_image(url) for url in photos)
    yield large
    if large != tuple(photos):
        yield tuple(photos)


def format_listing(
    listing: Listing,
    *,
    tz: ZoneInfo,
    now: datetime,
    converter: CurrencyConverter | None = None,
) -> str:
    """The notification text, in Telegram HTML."""
    lines = [f"<b>{_escape(listing.title)}</b>"]
    if listing.verdict is Verdict.FLAG and listing.reasons:
        lines.append(f"<b>Check:</b> {_escape('; '.join(listing.reasons))}")
    total = format_total(listing, converter)
    if total:
        lines.append(f"Total: {total}")
    price = str(listing.price) if listing.price is not None else "n/a"
    if listing.original_price is not None:
        price += f" (seller price {listing.original_price})"
    lines.append(f"Price: {price}")
    if listing.is_auction:
        lines.append(f"Current bid: {describe_bids(listing)}")
    shipping = str(listing.shipping) if listing.shipping is not None else "unknown"
    if listing.import_charges is not None:
        shipping += f" + import charges {listing.import_charges}"
    lines.append(f"Shipping: {shipping}")
    lines.append(f"Format: {describe_format(listing, tz=tz, now=now)}")
    if listing.condition:
        lines.append(f"Condition: {_escape(listing.condition)}")
    if listing.condition_description:
        notes = _shorten(listing.condition_description, MAX_NOTES_LENGTH)
        lines.append(f"Condition notes: <i>{_escape(notes)}</i>")
    found = f"Found on {listing.marketplace}"
    if listing.location_country:
        found += f", item located in {_escape(listing.location_country)}"
    if not listing.details_fetched:
        found += " (search data only)"
    lines.append(found)
    lines.append(f"Query: <i>{_escape(listing.query)}</i>")
    return "\n".join(lines)


def format_total(listing: Listing, converter: CurrencyConverter | None) -> str | None:
    """Item plus known costs, with what is still unknown and an approximate conversion."""
    total = listing.total
    if total is None:
        return None
    parts = ["current bid" if listing.is_auction and listing.current_bid else "item"]
    if listing.shipping is not None:
        parts.append("shipping")
    if listing.import_charges is not None:
        parts.append("import charges")
    text = f"{total} ({' + '.join(parts)})"
    if listing.shipping is None:
        text += ", shipping unknown"
    if converter is not None and total.currency != converter.target:
        converted = converter.convert(total)
        if converted is not None:
            text += f", about {converted}"
    return text


def describe_bids(listing: Listing) -> str:
    bid = listing.current_bid or listing.price
    count = listing.bid_count or 0
    text = f"{bid or 'n/a'}, {count} bid{'' if count == 1 else 's'}"
    if listing.minimum_bid is not None:
        text += f"; next bid from {listing.minimum_bid}"
    if listing.reserve_met is False:
        text += "; reserve not met"
    return text


def describe_format(listing: Listing, *, tz: ZoneInfo, now: datetime) -> str:
    text = ", ".join(_FORMAT_NAMES.get(option, option) for option in listing.buying_options)
    text = text or "unknown"
    if listing.is_auction and listing.end_date is not None:
        end = listing.end_date.astimezone(tz).strftime("%a %d %b %H:%M %Z")
        text += f"; ends {end} ({format_remaining(listing.end_date - now)})"
    return text


def format_remaining(delta: timedelta) -> str:
    if delta <= timedelta(0):
        return "ended"
    minutes = int(delta.total_seconds() // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"in {days}d {hours}h"
    if hours:
        return f"in {hours}h {minutes}m"
    return f"in {minutes}m"


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."
