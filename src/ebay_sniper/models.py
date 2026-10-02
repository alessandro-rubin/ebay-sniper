"""Domain objects shared by the pipeline, the rules, the store and the notifier."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from ebay_sniper.ebay.models import ApiAmount, ApiShippingOption, ItemDetails, ItemSummary

CENT = Decimal("0.01")


class Verdict(StrEnum):
    """Outcome of the rules: notify, notify with a warning, or discard."""

    PASS = "pass"
    FLAG = "flag"
    DROP = "drop"


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
class CurrencyConverter:
    """Approximate conversion to one currency with configured rates."""

    target: str
    # Units of `target` for one unit of each other currency.
    rates: Mapping[str, Decimal] = field(default_factory=dict)

    def convert(self, money: Money) -> Money | None:
        """The amount in the target currency, or None without a rate."""
        if money.currency == self.target:
            return money
        rate = self.rates.get(money.currency)
        if rate is None:
            return None
        return Money((money.value * rate).quantize(CENT), self.target)


@dataclass(frozen=True, slots=True)
class VisionScore:
    """How the photos of a listing compare with the reference images.

    All values are cosine similarities of image embeddings, or differences of
    them, so they depend on the model: thresholds are calibrated per model.
    """

    # Highest similarity of any photo to any positive reference ("is it this watch?").
    match: float
    # Highest similarity of any photo to any negative reference.
    negative: float
    # Similarity to a silver-tone description minus a gold-tone one, on the
    # photos closest to the positives: below zero leans gold-tone.
    colour: float
    # Index in Listing.image_urls of the photo closest to the positives.
    best_photo: int
    photos: int
    model: str
    # Similarity to a spider-web dial description minus a plain-dial one, on
    # the most web-looking photo: other watches of the brand stay near zero.
    # None for listings scored before this probe existed (2026-10-02).
    web: float | None = None

    def describe(self) -> str:
        """The scores as shown in notifications and command output."""
        text = f"match {self.match:.3f}, colour {self.colour:+.3f}"
        return text if self.web is None else f"{text}, web {self.web:+.3f}"


@dataclass(frozen=True, slots=True)
class Listing:
    """A listing as seen on one marketplace, optionally enriched by getItem.

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
    # From getItem.
    details_fetched: bool = False
    condition_description: str | None = None
    # Import charges of the cheapest shipping option (eBay international programs).
    import_charges: Money | None = None
    minimum_bid: Money | None = None
    reserve_met: bool | None = None
    # From the rules.
    verdict: Verdict | None = None
    reasons: tuple[str, ...] = ()
    # From the image comparison; the error is set when scoring failed.
    vision: VisionScore | None = None
    vision_error: str | None = None

    @property
    def is_auction(self) -> bool:
        return "AUCTION" in self.buying_options

    @property
    def total(self) -> Money | None:
        """Item price (current bid for auctions) plus shipping and import charges when known.

        None when the price is unknown or the parts use different currencies.
        """
        base = self.current_bid if self.is_auction and self.current_bid else self.price
        if base is None:
            return None
        extras = [money for money in (self.shipping, self.import_charges) if money is not None]
        if any(money.currency != base.currency for money in extras):
            return None
        return Money(base.value + sum((money.value for money in extras), Decimal(0)), base.currency)

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

    def with_details(self, details: ItemDetails) -> Listing:
        """Merge a getItem response: all photos, condition notes, precise shipping."""
        option = _cheapest_option(details.shipping_options)
        return replace(
            self,
            title=details.title,
            price=Money.from_api(details.price) or self.price,
            original_price=Money.original_from_api(details.price) or self.original_price,
            current_bid=Money.from_api(details.current_bid_price) or self.current_bid,
            bid_count=details.bid_count if details.bid_count is not None else self.bid_count,
            shipping=Money.from_api(option.shipping_cost) if option else self.shipping,
            import_charges=Money.from_api(option.import_charges) if option else None,
            condition=details.condition or self.condition,
            condition_id=details.condition_id or self.condition_id,
            image_urls=_image_urls(details) or self.image_urls,
            end_date=details.item_end_date or self.end_date,
            details_fetched=True,
            condition_description=details.condition_description,
            minimum_bid=Money.from_api(details.minimum_price_to_bid),
            reserve_met=details.reserve_price_met,
        )

    def with_verdict(self, verdict: Verdict, reasons: Sequence[str]) -> Listing:
        return replace(self, verdict=verdict, reasons=tuple(reasons))

    def with_vision(self, score: VisionScore | None, error: str | None = None) -> Listing:
        return replace(self, vision=score, vision_error=error)

    @property
    def photos_best_first(self) -> tuple[str, ...]:
        """The photos with the one closest to the references first."""
        best = self.vision.best_photo if self.vision else 0
        if not 0 < best < len(self.image_urls):
            return self.image_urls
        urls = self.image_urls
        return (urls[best], *urls[:best], *urls[best + 1 :])


def cheapest_shipping(options: Sequence[ApiShippingOption]) -> Money | None:
    option = _cheapest_option(options)
    return Money.from_api(option.shipping_cost) if option else None


def _cheapest_option(options: Sequence[ApiShippingOption]) -> ApiShippingOption | None:
    """The cheapest option with a known cost; None if no cost is known."""
    priced = [option for option in options if option.shipping_cost is not None]
    return min(
        priced,
        key=lambda option: option.shipping_cost.value if option.shipping_cost else Decimal(0),
        default=None,
    )


def _image_urls(item: ItemSummary) -> tuple[str, ...]:
    images = ([item.image] if item.image else []) + list(item.additional_images)
    return tuple(dict.fromkeys(image.image_url for image in images if image.image_url))
