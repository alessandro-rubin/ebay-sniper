from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from ebay_sniper.ebay.models import ItemDetails, SearchPage
from ebay_sniper.models import CurrencyConverter, Listing, Money
from factories import load_fixture, make_item


def eur(value: str) -> Money:
    return Money(Decimal(value), "EUR")


def fixture_auction() -> Listing:
    page = SearchPage.model_validate(load_fixture("search_ebay_it.json"))
    return Listing.from_summary(page.item_summaries[0], marketplace="EBAY_IT", query="q")


def base_listing(**changes: object) -> Listing:
    listing = Listing(
        listing_id="1",
        item_id="v1|1|0",
        title="t",
        url="https://www.ebay.it/itm/1",
        marketplace="EBAY_IT",
        query="q",
        price=eur("30"),
        buying_options=("FIXED_PRICE",),
    )
    return replace(listing, **changes)


def test_total_adds_known_costs() -> None:
    assert base_listing().total == eur("30")
    assert base_listing(shipping=eur("5.50")).total == eur("35.50")
    assert base_listing(shipping=eur("5"), import_charges=eur("7")).total == eur("42")


def test_total_of_an_auction_uses_the_current_bid() -> None:
    auction = base_listing(buying_options=("AUCTION",), current_bid=eur("12"), shipping=eur("3"))
    assert auction.total == eur("15")
    no_bids = base_listing(buying_options=("AUCTION",), current_bid=None)
    assert no_bids.total == eur("30")


def test_total_is_unknown_without_price_or_with_mixed_currencies() -> None:
    assert base_listing(price=None).total is None
    assert base_listing(shipping=Money(Decimal("5"), "USD")).total is None


def test_currency_converter() -> None:
    converter = CurrencyConverter("EUR", {"USD": Decimal("0.86")})
    amount = eur("10")
    assert converter.convert(amount) is amount
    assert converter.convert(Money(Decimal("10.01"), "USD")) == eur("8.61")
    assert converter.convert(Money(Decimal("10"), "GBP")) is None


def test_with_details_merges_the_get_item_response() -> None:
    details = ItemDetails.model_validate(load_fixture("item_110000000001.json"))
    merged = fixture_auction().with_details(details)
    assert merged.details_fetched
    assert merged.condition_description == "Funzionante, vetro con piccoli graffi, batteria nuova."
    assert len(merged.image_urls) == 4
    assert merged.image_urls[0].endswith("AAAAAAAAAAAAAAAA/s-l1600.jpg")
    assert merged.minimum_bid == eur("20.40")
    assert merged.reserve_met is True
    assert merged.shipping == eur("6.50")
    assert merged.import_charges is None
    assert merged.total == eur("26.40")
    # Unchanged identity and origin.
    assert (merged.listing_id, merged.url, merged.query) == (
        "110000000001",
        fixture_auction().url,
        "q",
    )


def test_with_details_keeps_search_shipping_when_details_have_no_cost() -> None:
    listing = base_listing(shipping=eur("4"))
    details = ItemDetails.model_validate(
        make_item("1", shippingOptions=[{"shippingCostType": "CALCULATED"}])
    )
    assert listing.with_details(details).shipping == eur("4")


def test_with_details_takes_import_charges_of_the_cheapest_option() -> None:
    details = ItemDetails.model_validate(
        make_item(
            "1",
            currency="EUR",
            shippingOptions=[
                {
                    "shippingCostType": "FIXED",
                    "shippingCost": {"value": "30.00", "currency": "EUR"},
                    "importCharges": {"value": "1.00", "currency": "EUR"},
                },
                {
                    "shippingCostType": "FIXED",
                    "shippingCost": {"value": "12.00", "currency": "EUR"},
                    "importCharges": {"value": "6.00", "currency": "EUR"},
                },
            ],
        )
    )
    merged = base_listing().with_details(details)
    assert (merged.shipping, merged.import_charges) == (eur("12.00"), eur("6.00"))
    assert merged.total == eur("43.00")
