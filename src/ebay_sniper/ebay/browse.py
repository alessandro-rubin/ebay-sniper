"""Browse API client: keyword search and item details.

Only the official Browse API is used (no Finding API, no HTML scraping), with
the application token from :mod:`ebay_sniper.ebay.auth`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ValidationError

from ebay_sniper.ebay.auth import EbayAuthError
from ebay_sniper.ebay.models import ApiMessage, ErrorResponse, ItemDetails, SearchPage
from ebay_sniper.retry import backoff_delay, retry_after_seconds

log = logging.getLogger(__name__)

BROWSE_URL = "https://api.ebay.com/buy/browse/v1"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class EbayApiError(RuntimeError):
    """A Browse API call failed after the retries allowed for it."""

    def __init__(
        self,
        operation: str,
        message: str,
        *,
        status_code: int | None = None,
        errors: Sequence[ApiMessage] = (),
    ) -> None:
        super().__init__(f"{operation}: {message}")
        self.operation = operation
        self.status_code = status_code
        self.errors = tuple(errors)


def build_filter(buying_options: Sequence[str], delivery_country: str | None) -> str:
    """The ``filter`` parameter.

    Without an explicit ``buyingOptions`` filter the Browse API returns only
    listings that have a fixed price, which would hide pure auctions.
    """
    parts = [f"buyingOptions:{{{'|'.join(buying_options)}}}"]
    if delivery_country:
        parts.append(f"deliveryCountry:{delivery_country}")
    return ",".join(parts)


def end_user_context(country: str, postal_code: str | None) -> str:
    """The ``X-EBAY-C-ENDUSERCTX`` value, for better shipping cost estimates."""
    location = f"country={country}"
    if postal_code:
        location += f",zip={postal_code}"
    return "contextualLocation=" + quote(location, safe="")


class BrowseClient:
    """Synchronous Browse API client with retries on rate limiting and server errors.

    ``calls`` counts every HTTP request sent, retries included, so that actual
    usage can be compared with the daily budget.
    """

    def __init__(
        self,
        http: httpx.Client,
        *,
        delivery_country: str,
        postal_code: str | None = None,
        max_attempts: int = 4,
        backoff_base_s: float = 2.0,
        max_retry_wait_s: float = 120.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http
        self._delivery_country = delivery_country
        self._end_user_context = end_user_context(delivery_country, postal_code)
        self._max_attempts = max(max_attempts, 1)
        self._backoff_base_s = backoff_base_s
        self._max_retry_wait_s = max_retry_wait_s
        self._sleep = sleep
        self.calls = 0

    def search(
        self,
        query: str,
        marketplace: str,
        *,
        limit: int,
        buying_options: Sequence[str],
    ) -> SearchPage:
        """The newest listings matching ``query`` on ``marketplace`` (one page)."""
        operation, response = self._search(query, marketplace, limit, buying_options)
        page = _parse(SearchPage, response, operation)
        for warning in page.warnings:
            log.warning("eBay warning for %s: %s", operation, warning.describe())
        return page

    def search_raw(
        self,
        query: str,
        marketplace: str,
        *,
        limit: int,
        buying_options: Sequence[str],
    ) -> Any:
        """The decoded search response without seller data, to save as a test fixture."""
        _, response = self._search(query, marketplace, limit, buying_options)
        return sanitize_response(response.json())

    def _search(
        self, query: str, marketplace: str, limit: int, buying_options: Sequence[str]
    ) -> tuple[str, httpx.Response]:
        operation = f"search {query!r} on {marketplace}"
        params = {
            "q": query,
            "filter": build_filter(buying_options, self._delivery_country),
            "sort": "newlyListed",
            "limit": str(limit),
        }
        return operation, self._get("/item_summary/search", operation, marketplace, params)

    def get_item(self, item_id: str, marketplace: str) -> ItemDetails | None:
        """Full details of a listing, or None when it no longer exists."""
        operation = f"getItem {item_id} on {marketplace}"
        path = f"/item/{quote(item_id, safe='')}"
        try:
            response = self._get(path, operation, marketplace, None)
        except EbayApiError as exc:
            if exc.status_code == 404:
                return None
            raise
        return _parse(ItemDetails, response, operation)

    def _get(
        self,
        path: str,
        operation: str,
        marketplace: str,
        params: dict[str, str] | None,
    ) -> httpx.Response:
        headers = {
            "X-EBAY-C-MARKETPLACE-ID": marketplace,
            "X-EBAY-C-ENDUSERCTX": self._end_user_context,
        }
        for attempt in range(1, self._max_attempts + 1):
            last_attempt = attempt == self._max_attempts
            self.calls += 1
            try:
                response = self._http.get(BROWSE_URL + path, params=params, headers=headers)
            except EbayAuthError:
                # The token request failed: the Browse request was never sent.
                self.calls -= 1
                raise
            except httpx.TransportError as exc:
                reason = f"{type(exc).__name__}: {exc}"
                if last_attempt or not self._wait(operation, attempt, reason, None):
                    raise EbayApiError(operation, reason) from exc
                continue
            except httpx.HTTPError as exc:
                raise EbayApiError(operation, f"{type(exc).__name__}: {exc}") from exc
            if response.status_code == 200:
                return response
            if (
                response.status_code in RETRY_STATUSES
                and not last_attempt
                and self._wait(
                    operation,
                    attempt,
                    f"HTTP {response.status_code}",
                    retry_after_seconds(response),
                )
            ):
                continue
            raise _error_from_response(operation, response)
        raise AssertionError("unreachable")

    def _wait(self, operation: str, attempt: int, reason: str, retry_after: float | None) -> bool:
        """Sleep before the next attempt; False when the server asks to wait too long."""
        delay = (
            retry_after
            if retry_after is not None
            else backoff_delay(attempt, base=self._backoff_base_s)
        )
        if delay > self._max_retry_wait_s:
            log.warning(
                "%s failed (%s), server asks to wait %.0f s: giving up", operation, reason, delay
            )
            return False
        log.warning("%s failed (%s), retrying in %.1f s", operation, reason, delay)
        self._sleep(delay)
        return True


_SELLER_KEYS = frozenset({"seller", "itemAffiliateWebUrl"})
_ADDRESS_KEYS = frozenset(
    {"addressLine1", "addressLine2", "city", "county", "postalCode", "stateOrProvince"}
)


def sanitize_response(data: Any) -> Any:
    """Remove seller data and every address detail except countries from a response.

    Addresses appear in ``itemLocation`` (the seller's) and in
    ``shipToLocationUsedForEstimate`` (the buyer's own postal code).
    """
    if isinstance(data, dict):
        return {
            key: sanitize_response(value)
            for key, value in data.items()
            if key not in _SELLER_KEYS and key not in _ADDRESS_KEYS
        }
    if isinstance(data, list):
        return [sanitize_response(value) for value in data]
    return data


def _parse[ModelT: BaseModel](
    model: type[ModelT], response: httpx.Response, operation: str
) -> ModelT:
    try:
        return model.model_validate_json(response.content)
    except ValidationError as exc:
        fields = ", ".join(
            ".".join(str(part) for part in error["loc"]) or error["type"]
            for error in exc.errors()[:5]
        )
        raise EbayApiError(operation, f"unexpected response format ({fields})") from None


def _error_from_response(operation: str, response: httpx.Response) -> EbayApiError:
    try:
        errors = ErrorResponse.model_validate_json(response.content).errors
    except ValidationError:
        errors = ()
    details = "; ".join(error.describe() for error in errors) or response.text[:200]
    return EbayApiError(
        operation,
        f"HTTP {response.status_code}: {details}",
        status_code=response.status_code,
        errors=errors,
    )
