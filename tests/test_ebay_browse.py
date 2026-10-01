from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from ebay_sniper.ebay.browse import (
    BrowseClient,
    EbayApiError,
    build_filter,
    end_user_context,
    sanitize_response,
)
from ebay_sniper.ebay.endpoints import API_ROOTS
from ebay_sniper.models import Listing, Money
from factories import SANDBOX_SEARCH_URL, SEARCH_URL, load_fixture, make_page_data

OPTIONS = ("FIXED_PRICE", "AUCTION", "BEST_OFFER")


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def browse(sleeps: list[float]) -> BrowseClient:
    return BrowseClient(
        httpx.Client(), delivery_country="IT", postal_code="35100", sleep=sleeps.append
    )


def search(browse: BrowseClient, query: str = "futura (spider, ragno)", market: str = "EBAY_IT"):
    return browse.search(query, market, limit=100, buying_options=OPTIONS)


def test_filter_and_context_values() -> None:
    assert (
        build_filter(OPTIONS, "IT")
        == "buyingOptions:{FIXED_PRICE|AUCTION|BEST_OFFER},deliveryCountry:IT"
    )
    assert build_filter(["AUCTION"], None) == "buyingOptions:{AUCTION}"
    assert end_user_context("IT", "35100") == "contextualLocation=country%3DIT%2Czip%3D35100"
    assert end_user_context("IT", None) == "contextualLocation=country%3DIT"


def test_sandbox_search(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(SANDBOX_SEARCH_URL).respond(json=load_fixture("search_empty.json"))
    browse = BrowseClient(httpx.Client(), delivery_country="IT", api_root=API_ROOTS["sandbox"])
    assert search(browse).total == 0
    assert route.call_count == 1


def test_search_request(respx_mock: respx.MockRouter, browse: BrowseClient) -> None:
    route = respx_mock.get(SEARCH_URL).respond(json=load_fixture("search_empty.json"))
    search(browse)
    request = route.calls.last.request
    assert dict(request.url.params) == {
        "q": "futura (spider, ragno)",
        "filter": "buyingOptions:{FIXED_PRICE|AUCTION|BEST_OFFER},deliveryCountry:IT",
        "sort": "newlyListed",
        "limit": "100",
    }
    assert request.headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_IT"
    assert request.headers["X-EBAY-C-ENDUSERCTX"] == "contextualLocation=country%3DIT%2Czip%3D35100"
    assert browse.calls == 1


def test_search_parses_the_documented_fields(
    respx_mock: respx.MockRouter, browse: BrowseClient
) -> None:
    respx_mock.get(SEARCH_URL).respond(json=load_fixture("search_ebay_it.json"))
    page = search(browse)
    assert page.total == 3
    auction, converted, for_parts = (
        Listing.from_summary(item, marketplace="EBAY_IT", query="q") for item in page.item_summaries
    )

    assert auction.listing_id == "110000000001"
    assert auction.item_id == "v1|110000000001|0"
    assert auction.is_auction
    assert auction.current_bid == Money(Decimal("19.90"), "EUR")
    assert auction.bid_count == 2
    assert auction.shipping == Money(Decimal("6.50"), "EUR")
    assert auction.end_date == datetime(2026, 9, 29, 19, 30, tzinfo=UTC)
    assert auction.origin_date == datetime(2026, 9, 26, 8, 15, tzinfo=UTC)
    assert auction.location_country == "IT"
    assert auction.image_urls == (
        "https://i.ebayimg.com/images/g/AAAAAAAAAAAAAAAA/s-l225.jpg",
        "https://i.ebayimg.com/images/g/BBBBBBBBBBBBBBBB/s-l225.jpg",
        "https://i.ebayimg.com/images/g/CCCCCCCCCCCCCCCC/s-l225.jpg",
    )

    assert converted.price == Money(Decimal("85.10"), "EUR")
    assert converted.original_price == Money(Decimal("99.00"), "USD")
    assert converted.shipping is None  # calculated shipping without a cost
    assert converted.buying_options == ("FIXED_PRICE", "BEST_OFFER")
    assert not converted.is_auction

    assert for_parts.condition_id == "7000"
    assert for_parts.shipping == Money(Decimal("4.99"), "EUR")  # the cheapest option
    assert for_parts.original_price is None


def test_empty_search(respx_mock: respx.MockRouter, browse: BrowseClient) -> None:
    respx_mock.get(SEARCH_URL).respond(json=load_fixture("search_empty.json"))
    page = search(browse)
    assert page.total == 0
    assert page.item_summaries == ()


def test_legacy_id_falls_back_to_the_restful_id(
    respx_mock: respx.MockRouter, browse: BrowseClient
) -> None:
    item = {"itemId": "v1|555|0", "title": "t", "itemWebUrl": "https://www.ebay.it/itm/555"}
    respx_mock.get(SEARCH_URL).respond(json=make_page_data(item))
    assert search(browse).item_summaries[0].listing_id == "555"


def test_warnings_are_logged(
    respx_mock: respx.MockRouter, browse: BrowseClient, caplog: pytest.LogCaptureFixture
) -> None:
    body = load_fixture("search_empty.json") | {
        "warnings": [{"errorId": 12016, "message": "Invalid filter: deliveryCountry"}]
    }
    respx_mock.get(SEARCH_URL).respond(json=body)
    with caplog.at_level(logging.WARNING):
        search(browse)
    assert "12016: Invalid filter: deliveryCountry" in caplog.text


def test_rate_limit_is_retried_after_the_requested_delay(
    respx_mock: respx.MockRouter, browse: BrowseClient, sleeps: list[float]
) -> None:
    respx_mock.get(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(
                429, headers={"Retry-After": "3"}, json=load_fixture("error_rate_limit.json")
            ),
            httpx.Response(500),
            httpx.Response(200, json=load_fixture("search_empty.json")),
        ]
    )
    search(browse)
    assert sleeps[0] == 3.0
    assert len(sleeps) == 2
    assert browse.calls == 3


def test_rate_limit_with_a_long_wait_gives_up(
    respx_mock: respx.MockRouter, browse: BrowseClient, sleeps: list[float]
) -> None:
    respx_mock.get(SEARCH_URL).respond(
        429, headers={"Retry-After": "3600"}, json=load_fixture("error_rate_limit.json")
    )
    with pytest.raises(EbayApiError, match="2001: Too many requests") as exc_info:
        search(browse)
    assert exc_info.value.status_code == 429
    assert sleeps == []


def test_client_errors_are_not_retried(
    respx_mock: respx.MockRouter, browse: BrowseClient, sleeps: list[float]
) -> None:
    respx_mock.get(SEARCH_URL).respond(
        400,
        json={"errors": [{"errorId": 12001, "message": "The value of limit is invalid."}]},
    )
    with pytest.raises(EbayApiError, match="HTTP 400: 12001: The value of limit is invalid"):
        search(browse)
    assert sleeps == []
    assert browse.calls == 1


def test_transport_errors_are_retried_then_raised(
    respx_mock: respx.MockRouter, browse: BrowseClient, sleeps: list[float]
) -> None:
    respx_mock.get(SEARCH_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
    with pytest.raises(EbayApiError, match="ConnectTimeout"):
        search(browse)
    assert browse.calls == 4
    assert len(sleeps) == 3


def test_unexpected_payload_is_reported(respx_mock: respx.MockRouter, browse: BrowseClient) -> None:
    respx_mock.get(SEARCH_URL).respond(json={"itemSummaries": [{"title": "no id"}]})
    with pytest.raises(EbayApiError, match="unexpected response format"):
        search(browse)


def test_get_item_encodes_the_restful_id(
    respx_mock: respx.MockRouter, browse: BrowseClient
) -> None:
    route = respx_mock.get("https://api.ebay.com/buy/browse/v1/item/v1%7C110000000001%7C0").respond(
        json=load_fixture("item_110000000001.json")
    )
    details = browse.get_item("v1|110000000001|0", "EBAY_IT")
    assert route.called
    assert details is not None
    assert details.condition_description == "Funzionante, vetro con piccoli graffi, batteria nuova."
    assert len(details.additional_images) == 3
    assert details.minimum_price_to_bid is not None
    assert details.minimum_price_to_bid.value == Decimal("20.40")


def test_get_item_returns_none_when_the_listing_is_gone(
    respx_mock: respx.MockRouter, browse: BrowseClient
) -> None:
    respx_mock.get(url__startswith="https://api.ebay.com/buy/browse/v1/item/").respond(
        404, json={"errors": [{"errorId": 11001, "message": "Item not found."}]}
    )
    assert browse.get_item("v1|1|0", "EBAY_IT") is None


def test_sanitize_response_removes_seller_data() -> None:
    data = load_fixture("search_ebay_it.json") | {"extra": [load_fixture("item_110000000001.json")]}
    clean = sanitize_response(data)
    text = str(clean)
    assert "example_seller" not in text
    assert "itemAffiliateWebUrl" not in text
    assert "postalCode" not in text
    assert "35100" not in text
    assert "Padova" not in text
    assert clean["itemSummaries"][0]["itemLocation"] == {"country": "IT"}
    assert clean["itemSummaries"][0]["title"] == data["itemSummaries"][0]["title"]
    shipping = clean["extra"][0]["shippingOptions"][0]
    assert shipping["shipToLocationUsedForEstimate"] == {"country": "IT"}
