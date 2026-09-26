"""Builders for test data. All values are fake."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ebay_sniper.ebay.models import SearchPage

FIXTURES = Path(__file__).parent / "fixtures"

EBAY_CLIENT_ID = "test-client-id-0001"
EBAY_CLIENT_SECRET = "test-client-secret-0001"
TELEGRAM_BOT_TOKEN = "123456789:TEST-telegram-bot-token"
TELEGRAM_CHAT_ID = "4242"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
ITEM_URL = "https://api.ebay.com/buy/browse/v1/item"

ENV_FILE_CONTENT = (
    f"EBAY_CLIENT_ID={EBAY_CLIENT_ID}\n"
    f"EBAY_CLIENT_SECRET={EBAY_CLIENT_SECRET}\n"
    f"TELEGRAM_BOT_TOKEN={TELEGRAM_BOT_TOKEN}\n"
    f"TELEGRAM_CHAT_ID={TELEGRAM_CHAT_ID}\n"
)

CONFIG_TOML = """\
[search]
marketplaces = ["EBAY_IT", "EBAY_DE"]
delivery_country = "IT"
buyer_postal_code = "35100"
buying_options = ["FIXED_PRICE", "AUCTION", "BEST_OFFER"]
results_per_query = 100
queries = [
    "futura (spider, ragno)",
    "(spider, spiderweb) watch",
]

[price]
currency = "EUR"
max_total = 400
exchange_rates = { USD = 0.86 }

[rules]
drop_keywords = ["cracked crystal", "vetro rotto", "missing hand*"]
flag_keywords = ["needs battery", "defekt"]
flag_condition_ids = [7000]

[telegram]
send_all_photos = true
max_photos = 4
timezone = "Europe/Rome"

[runtime]
poll_interval_minutes = 20
database_path = "data/test.sqlite3"
image_cache_dir = "data/image_cache"
api_budget_share = 0.6
max_notifications_per_cycle = 10
seed_new_searches = true
"""


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_item(
    listing_id: str,
    *,
    title: str | None = None,
    price: str = "25.00",
    currency: str = "EUR",
    buying_options: tuple[str, ...] = ("FIXED_PRICE",),
    origin: str = "2026-09-26T08:00:00.000Z",
    domain: str = "it",
    images: int = 1,
    **extra: Any,
) -> dict[str, Any]:
    """An ``itemSummaries`` element in the Browse API JSON shape."""
    item: dict[str, Any] = {
        "itemId": f"v1|{listing_id}|0",
        "legacyItemId": listing_id,
        "title": title or f"Spider web watch {listing_id}",
        "price": {"value": price, "currency": currency},
        "buyingOptions": list(buying_options),
        "itemWebUrl": f"https://www.ebay.{domain}/itm/{listing_id}",
        "itemOriginDate": origin,
        "seller": {"username": f"seller_of_{listing_id}", "feedbackScore": 1},
    }
    if images:
        urls = [
            f"https://i.ebayimg.com/images/g/{listing_id}{index}/s-l225.jpg"
            for index in range(images)
        ]
        item["image"] = {"imageUrl": urls[0]}
        item["additionalImages"] = [{"imageUrl": url} for url in urls[1:]]
    item.update(extra)
    return item


def make_page_data(*items: dict[str, Any], total: int | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"total": len(items) if total is None else total}
    if items:
        data["itemSummaries"] = list(items)
    return data


def make_page(*items: dict[str, Any], total: int | None = None) -> SearchPage:
    return SearchPage.model_validate(make_page_data(*items, total=total))
