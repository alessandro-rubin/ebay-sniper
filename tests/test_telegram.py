from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from pydantic import SecretStr

from ebay_sniper.config import TelegramConfig
from ebay_sniper.ebay.models import ItemDetails, SearchPage
from ebay_sniper.models import CurrencyConverter, Listing, Money, Verdict, VisionScore
from ebay_sniper.notify.telegram import (
    TelegramClient,
    TelegramError,
    TelegramNotifier,
    format_listing,
    format_remaining,
    upscale_ebay_image,
)
from factories import TELEGRAM_API, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, load_fixture

NOW = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)
ROME = ZoneInfo("Europe/Rome")
OK = {"ok": True, "result": {"message_id": 1}}


def fixture_listings() -> list[Listing]:
    page = SearchPage.model_validate(load_fixture("search_ebay_it.json"))
    return [
        Listing.from_summary(item, marketplace="EBAY_IT", query="futura (spider, ragno)")
        for item in page.item_summaries
    ]


@pytest.fixture
def sleeps() -> list[float]:
    return []


def make_notifier(sleeps: list[float], **config: object) -> TelegramNotifier:
    client = TelegramClient(httpx.Client(), SecretStr(TELEGRAM_BOT_TOKEN), sleep=sleeps.append)
    return TelegramNotifier(
        client, TELEGRAM_CHAT_ID, TelegramConfig.model_validate(config), clock=lambda: NOW
    )


def payload(route: respx.Route, index: int = -1) -> dict[str, object]:
    return json.loads(route.calls[index].request.content)


def test_format_auction() -> None:
    auction = fixture_listings()[0]
    text = format_listing(auction, tz=ROME, now=NOW)
    assert text.splitlines() == [
        "<b>Orologio Futura Quartz quadrante ragnatela madreperla vintage</b>",
        "Total: 26.40 EUR (current bid + shipping)",
        "Price: 19.90 EUR",
        "Current bid: 19.90 EUR, 2 bids",
        "Shipping: 6.50 EUR",
        "Format: Auction; ends Tue 29 Sep 21:30 CEST (in 2d 9h)",
        "Condition: Usato",
        "Found on EBAY_IT, item located in IT (search data only)",
        "Query: <i>futura (spider, ragno)</i>",
    ]


def test_format_auction_with_details_and_flags() -> None:
    details = ItemDetails.model_validate(load_fixture("item_110000000001.json"))
    auction = (
        fixture_listings()[0]
        .with_details(details)
        .with_verdict(Verdict.FLAG, ["negated 'vetro rotto' in condition notes"])
    )
    auction = replace(
        auction,
        reserve_met=False,
        condition_description="Funzionante <ok> & " + "molto bello " * 40,
    )
    lines = format_listing(auction, tz=ROME, now=NOW).splitlines()
    assert lines[1] == "<b>Check:</b> negated 'vetro rotto' in condition notes"
    assert "Current bid: 19.90 EUR, 2 bids; next bid from 20.40 EUR; reserve not met" in lines
    notes = next(line for line in lines if line.startswith("Condition notes:"))
    assert notes.startswith("Condition notes: <i>Funzionante &lt;ok&gt; &amp; molto bello")
    assert notes.endswith("...</i>")
    assert len(notes) < 360
    assert lines[-2] == "Found on EBAY_IT, item located in IT"


def test_format_escapes_html_and_converts_the_total() -> None:
    converted = fixture_listings()[1]
    usd = replace(converted, price=Money(Decimal("99.00"), "USD"), original_price=None)
    text = format_listing(
        usd, tz=ROME, now=NOW, converter=CurrencyConverter("EUR", {"USD": Decimal("0.86")})
    )
    assert "Japan Movt &lt;RARE&gt; &amp; Unique</b>" in text
    assert "Total: 99.00 USD (item), shipping unknown, about 85.14 EUR" in text
    assert "Shipping: unknown" in text
    assert "Format: Buy It Now, Best Offer" in text
    assert "Current bid" not in text
    assert "Check:" not in text


def test_format_shows_the_original_currency_and_import_charges() -> None:
    converted = replace(
        fixture_listings()[1],
        shipping=Money(Decimal("20.00"), "EUR"),
        import_charges=Money(Decimal("18.00"), "EUR"),
    )
    text = format_listing(converted, tz=ROME, now=NOW)
    assert "Total: 123.10 EUR (item + shipping + import charges)" in text
    assert "Price: 85.10 EUR (seller price 99.00 USD)" in text
    assert "Shipping: 20.00 EUR + import charges 18.00 EUR" in text


def test_format_remaining() -> None:
    assert format_remaining(timedelta(days=1, hours=2, minutes=3)) == "in 1d 2h"
    assert format_remaining(timedelta(hours=2, minutes=3)) == "in 2h 3m"
    assert format_remaining(timedelta(minutes=3, seconds=59)) == "in 3m"
    assert format_remaining(timedelta(seconds=-1)) == "ended"


def test_upscale_ebay_image() -> None:
    base = "https://i.ebayimg.com/images/g/AAAA/"
    assert upscale_ebay_image(base + "s-l225.jpg") == base + "s-l1600.jpg"
    assert upscale_ebay_image(base + "s-l500.webp?set_id=1") == base + "s-l1600.webp?set_id=1"
    assert upscale_ebay_image("https://example.com/photo.jpg") == "https://example.com/photo.jpg"


def test_single_photo_is_sent_with_caption_and_button(
    respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    route = respx_mock.post(f"{TELEGRAM_API}/sendPhoto").respond(json=OK)
    listing = fixture_listings()[1]
    make_notifier(sleeps).notify_listing(listing)
    body = payload(route)
    assert body["chat_id"] == TELEGRAM_CHAT_ID
    assert body["photo"] == "https://i.ebayimg.com/images/g/DDDDDDDDDDDDDDDD/s-l1600.jpg"
    assert body["parse_mode"] == "HTML"
    assert "Vintage Futura Quartz" in str(body["caption"])
    assert body["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "Open on eBay", "url": "https://www.ebay.it/itm/120000000002"}]
        ]
    }


def test_several_photos_become_a_silent_album_followed_by_the_details(
    respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    album = respx_mock.post(f"{TELEGRAM_API}/sendMediaGroup").respond(json=OK)
    message = respx_mock.post(f"{TELEGRAM_API}/sendMessage").respond(json=OK)
    make_notifier(sleeps, max_photos=2).notify_listing(fixture_listings()[0])
    album_body = payload(album)
    assert album_body["disable_notification"] is True
    assert [item["media"] for item in album_body["media"]] == [
        "https://i.ebayimg.com/images/g/AAAAAAAAAAAAAAAA/s-l1600.jpg",
        "https://i.ebayimg.com/images/g/BBBBBBBBBBBBBBBB/s-l1600.jpg",
    ]
    message_body = payload(message)
    assert "Current bid: 19.90 EUR" in str(message_body["text"])
    assert "reply_markup" in message_body
    assert "disable_notification" not in message_body


def test_send_all_photos_false_sends_one_photo(
    respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    route = respx_mock.post(f"{TELEGRAM_API}/sendPhoto").respond(json=OK)
    make_notifier(sleeps, send_all_photos=False).notify_listing(fixture_listings()[0])
    assert route.call_count == 1


def test_photos_fall_back_to_original_size_then_to_text(
    respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    rejected = {"ok": False, "error_code": 400, "description": "Bad Request: wrong file"}
    photo = respx_mock.post(f"{TELEGRAM_API}/sendPhoto").mock(
        side_effect=[httpx.Response(400, json=rejected), httpx.Response(400, json=rejected)]
    )
    message = respx_mock.post(f"{TELEGRAM_API}/sendMessage").respond(json=OK)
    make_notifier(sleeps).notify_listing(fixture_listings()[1])
    assert [payload(photo, index)["photo"] for index in range(2)] == [
        "https://i.ebayimg.com/images/g/DDDDDDDDDDDDDDDD/s-l1600.jpg",
        "https://i.ebayimg.com/images/g/DDDDDDDDDDDDDDDD/s-l225.jpg",
    ]
    assert "reply_markup" in payload(message)


def test_listing_without_photos_is_a_text_message(
    respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    message = respx_mock.post(f"{TELEGRAM_API}/sendMessage").respond(json=OK)
    listing = Listing(
        listing_id="1",
        item_id="v1|1|0",
        title="t",
        url="https://www.ebay.it/itm/1",
        marketplace="EBAY_IT",
        query="q",
        price=Money(Decimal("1"), "EUR"),
    )
    make_notifier(sleeps).notify_listing(listing)
    assert message.call_count == 1
    assert payload(message)["link_preview_options"] == {"is_disabled": True}


def test_rate_limit_waits_for_retry_after(
    respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    limited = {"ok": False, "error_code": 429, "parameters": {"retry_after": 7}}
    respx_mock.post(f"{TELEGRAM_API}/sendMessage").mock(
        side_effect=[httpx.Response(429, json=limited), httpx.Response(200, json=OK)]
    )
    make_notifier(sleeps).send_text("hello")
    assert sleeps == [7.0]


def test_errors_never_contain_the_token(respx_mock: respx.MockRouter, sleeps: list[float]) -> None:
    respx_mock.post(f"{TELEGRAM_API}/sendMessage").mock(
        side_effect=httpx.ConnectError(f"cannot connect to {TELEGRAM_API}/sendMessage")
    )
    with pytest.raises(TelegramError) as exc_info:
        make_notifier(sleeps).send_text("hello")
    assert TELEGRAM_BOT_TOKEN not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__
    assert len(sleeps) == 2


def test_unauthorized_is_not_retried(respx_mock: respx.MockRouter, sleeps: list[float]) -> None:
    respx_mock.post(f"{TELEGRAM_API}/sendMessage").respond(
        401, json={"ok": False, "error_code": 401, "description": "Unauthorized"}
    )
    with pytest.raises(TelegramError, match="Unauthorized") as exc_info:
        make_notifier(sleeps).send_text("hello")
    assert exc_info.value.status_code == 401
    assert sleeps == []


def test_check_describes_bot_and_chat(respx_mock: respx.MockRouter, sleeps: list[float]) -> None:
    respx_mock.post(f"{TELEGRAM_API}/getMe").respond(
        json={"ok": True, "result": {"id": 1, "is_bot": True, "username": "spider_watch_bot"}}
    )
    chat = respx_mock.post(f"{TELEGRAM_API}/getChat").respond(
        json={"ok": True, "result": {"id": 4242, "type": "private", "first_name": "Ale"}}
    )
    assert make_notifier(sleeps).check() == "bot @spider_watch_bot, chat 'Ale' (private)"
    assert payload(chat) == {"chat_id": TELEGRAM_CHAT_ID}


def test_the_photo_closest_to_the_references_comes_first(
    respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    album = respx_mock.post(f"{TELEGRAM_API}/sendMediaGroup").respond(json=OK)
    message = respx_mock.post(f"{TELEGRAM_API}/sendMessage").respond(json=OK)
    score = VisionScore(
        match=0.871, negative=0.7, colour=-0.004, best_photo=1, photos=3, model="m", web=0.031
    )
    listing = fixture_listings()[0].with_vision(score)
    make_notifier(sleeps, max_photos=2).notify_listing(listing)
    assert [item["media"] for item in payload(album)["media"]] == [
        "https://i.ebayimg.com/images/g/BBBBBBBBBBBBBBBB/s-l1600.jpg",
        "https://i.ebayimg.com/images/g/AAAAAAAAAAAAAAAA/s-l1600.jpg",
    ]
    text = str(payload(message)["text"])
    assert "Photos: match 0.871, colour -0.004, web +0.031 (3 compared)" in text


def test_format_shows_why_the_photos_were_not_checked() -> None:
    listing = fixture_listings()[0].with_vision(None, "no photo could be downloaded <404>")
    text = format_listing(listing, tz=ROME, now=NOW)
    assert text.splitlines()[-1] == (
        "<b>Photos not checked:</b> no photo could be downloaded &lt;404&gt;"
    )
