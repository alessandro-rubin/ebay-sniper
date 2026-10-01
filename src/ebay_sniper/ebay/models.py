"""Typed models for the Browse API responses.

Only the fields the app uses are declared; everything else is ignored. Field
names follow the Browse API OpenAPI contract (v1.20.x). The ``seller``
container is deliberately not modelled: seller data must never be stored.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class _ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_alias=True,
        validate_by_name=True,
        extra="ignore",
        frozen=True,
    )


class ApiAmount(_ApiModel):
    """``ConvertedAmount``: ``value``/``currency`` are in the marketplace currency.

    When the listing uses another currency, eBay converts it and reports the
    original amount in ``convertedFromValue``/``convertedFromCurrency``.
    """

    value: Decimal
    currency: str
    converted_from_value: Decimal | None = None
    converted_from_currency: str | None = None


class ApiImage(_ApiModel):
    image_url: str


class ApiLocation(_ApiModel):
    """Only the country: street, city and postal code are not needed."""

    country: str | None = None


class ApiShippingOption(_ApiModel):
    shipping_cost: ApiAmount | None = None
    # FIXED or CALCULATED.
    shipping_cost_type: str | None = None
    # Only returned by getItem, for items shipped through eBay's global programs.
    import_charges: ApiAmount | None = None


class ApiMessage(_ApiModel):
    """An entry of the ``errors`` or ``warnings`` arrays."""

    error_id: int | None = None
    domain: str | None = None
    category: str | None = None
    message: str | None = None
    long_message: str | None = None

    def describe(self) -> str:
        text = self.long_message or self.message or "no message"
        return f"{self.error_id}: {text}" if self.error_id is not None else text


class ItemSummary(_ApiModel):
    """An element of ``itemSummaries`` in a search response."""

    item_id: str
    legacy_item_id: str | None = None
    title: str
    item_web_url: str
    price: ApiAmount | None = None
    current_bid_price: ApiAmount | None = None
    bid_count: int | None = None
    buying_options: tuple[str, ...] = ()
    condition: str | None = None
    condition_id: str | None = None
    image: ApiImage | None = None
    additional_images: tuple[ApiImage, ...] = ()
    item_origin_date: datetime | None = None
    item_end_date: datetime | None = None
    shipping_options: tuple[ApiShippingOption, ...] = ()
    item_location: ApiLocation | None = None
    listing_marketplace_id: str | None = None

    @property
    def listing_id(self) -> str:
        """The legacy item id, stable across marketplaces and queries.

        RESTful ids look like ``v1|<legacy id>|<variation id>``; the legacy id
        is taken from there if the field is missing.
        """
        if self.legacy_item_id:
            return self.legacy_item_id
        parts = self.item_id.split("|")
        return parts[1] if len(parts) == 3 and parts[1] else self.item_id


class ItemDetails(ItemSummary):
    """The response of ``getItem``: a superset of the summary."""

    condition_description: str | None = None
    description: str | None = None
    short_description: str | None = None
    minimum_price_to_bid: ApiAmount | None = None
    reserve_price_met: bool | None = None
    unique_bidder_count: int | None = None


class SearchPage(_ApiModel):
    """A page of ``item_summary/search`` results."""

    total: int = 0
    item_summaries: tuple[ItemSummary, ...] = ()
    warnings: tuple[ApiMessage, ...] = ()


class ErrorResponse(_ApiModel):
    errors: tuple[ApiMessage, ...] = ()
