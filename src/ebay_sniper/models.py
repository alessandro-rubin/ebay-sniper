"""Domain objects shared by the pipeline, the store and the notifier."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ebay_sniper.ebay.models import ApiAmount, ApiShippingOption, ItemSummary


@dataclass(frozen=True, slots=True)
class Money:
    value: Decimal
    currency: str

    def __str__(self) -> str:
        return f"{self.value:.2f} {self.currency}"

    @classmethod
    def from_api(cls, amount: ApiAmount | None) -> Money | None:
        return None if amount is None else cls(amount.value, amount.currency)

    @classmethod
    def original_from_api(cls, amount: ApiAmount | None) -> Money | None:
        """The amount before eBay's currency conversion, if one happened."""
        if amount is None or amount.converted_from_value is None:
            return None
        if amount.converted_from_currency is None:
            return None
        return cls(amount.converted_from_value, amount.converted_from_currency)


@dataclass(frozen=True, slots=True)
class Listing:
    """A listing as seen in the search results of one marketplace and query.

    It holds no seller data: the app must not store eBay user information.
    """

    listing_id: str
    item_id: str
    title: str
    url: str
    marketplace: str
    query: str
    # In the currency of `marketplace` (converted by eBay if needed).
    price: Money | None = None
    # The seller's own price, when eBay converted it.
    original_price: Money | None = None
    current_bid: Money | None = None
    bid_count: int | None = None
    # Cheapest shipping option to the configured delivery country; None if unknown.
    shipping: Money | None = None
    buying_options: tuple[str, ...] = ()
    condition: str | None = None
    condition_id: str | None = None
    location_country: str | None = None
    image_urls: tuple[str, ...] = ()
    origin_date: datetime | None = None
    end_date: datetime | None = None

    @property
    def is_auction(self) -> bool:
        return "AUCTION" in self.buying_options

    @classmethod
    def from_summary(cls, item: ItemSummary, *, marketplace: str, query: str) -> Listing:
        return cls(
            listing_id=item.listing_id,
            item_id=item.item_id,
            title=item.title,
            url=item.item_web_url,
            marketplace=marketplace,
            query=query,
            price=Money.from_api(item.price),
            original_price=Money.original_from_api(item.price),
            current_bid=Money.from_api(item.current_bid_price),
            bid_count=item.bid_count,
            shipping=cheapest_shipping(item.shipping_options),
            buying_options=item.buying_options,
            condition=item.condition,
            condition_id=item.condition_id,
            location_country=item.item_location.country if item.item_location else None,
            image_urls=_image_urls(item),
            origin_date=item.item_origin_date,
            end_date=item.item_end_date,
        )


def cheapest_shipping(options: tuple[ApiShippingOption, ...]) -> Money | None:
    costs = [
        money for option in options if (money := Money.from_api(option.shipping_cost)) is not None
    ]
    return min(costs, key=lambda money: money.value, default=None)


def _image_urls(item: ItemSummary) -> tuple[str, ...]:
    images = ([item.image] if item.image else []) + list(item.additional_images)
    return tuple(dict.fromkeys(image.image_url for image in images if image.image_url))
