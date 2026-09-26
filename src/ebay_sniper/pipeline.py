"""One poll cycle: search, deduplicate, persist, notify.

The cycle is idempotent: listings are keyed by their legacy item id, so running
it twice in a row notifies nothing new the second time. A notification that
fails stays pending and is retried in the next cycle.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from ebay_sniper.config import AppConfig
from ebay_sniper.ebay.browse import EbayApiError
from ebay_sniper.ebay.models import SearchPage
from ebay_sniper.models import Listing
from ebay_sniper.notify.base import NotificationError, Notifier
from ebay_sniper.store import ListingStatus, RunStatus, Store

log = logging.getLogger(__name__)

# Cycles in which a listing notification may fail before it is given up.
MAX_NOTIFY_ATTEMPTS = 10
# Consecutive notification failures after which the channel is assumed down
# for the rest of the cycle.
MAX_CONSECUTIVE_NOTIFY_FAILURES = 2
# Entries listed in the message that summarises suppressed listings.
MAX_SUMMARY_ENTRIES = 25
# A run still marked as running after this long belongs to a dead process.
STALE_RUN_AFTER = timedelta(minutes=30)


class Searcher(Protocol):
    """What the pipeline needs from the Browse client."""

    calls: int

    def search(
        self, query: str, marketplace: str, *, limit: int, buying_options: Sequence[str]
    ) -> SearchPage: ...


class CycleError(RuntimeError):
    """The cycle could not do its job, for example because every search failed."""


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class CycleReport:
    searches_ok: int = 0
    searches_failed: int = 0
    new_searches: int = 0
    results: int = 0
    new: int = 0
    seeded: int = 0
    notified: int = 0
    notify_failed: int = 0
    suppressed: int = 0
    api_calls: int = 0
    # Another run was still active, nothing was done.
    skipped: bool = False

    @property
    def status(self) -> RunStatus:
        if self.searches_failed or self.notify_failed:
            return RunStatus.PARTIAL
        return RunStatus.OK

    def summary(self) -> str:
        if self.skipped:
            return "skipped: another run is active"
        searches = self.searches_ok + self.searches_failed
        return (
            f"{self.searches_ok}/{searches} searches ok, {self.results} results, "
            f"{self.new} new, {self.seeded} seeded, {self.notified} notified, "
            f"{self.notify_failed} notification failures, {self.suppressed} suppressed, "
            f"{self.api_calls} API calls"
        )


class Pipeline:
    def __init__(
        self,
        config: AppConfig,
        searcher: Searcher,
        store: Store,
        notifier: Notifier,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._config = config
        self._searcher = searcher
        self._store = store
        self._notifier = notifier
        self._clock = clock

    def run_cycle(self) -> CycleReport:
        report = CycleReport()
        run_id = self._store.begin_run(self._clock(), stale_after=STALE_RUN_AFTER)
        if run_id is None:
            log.warning("Another run is still active, skipping this cycle")
            report.skipped = True
            return report
        calls_before = self._searcher.calls
        status, error = RunStatus.FAILED, None
        try:
            self._search_phase(report)
            self._notify_phase(report)
            status = report.status
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            report.api_calls = self._searcher.calls - calls_before
            self._store.finish_run(
                run_id,
                self._clock(),
                status=status,
                api_calls=report.api_calls,
                results=report.results,
                new_listings=report.new,
                notified=report.notified,
                error=error,
            )
        log.info("Cycle done: %s", report.summary())
        return report

    def _search_phase(self, report: CycleReport) -> None:
        search = self._config.search
        seed = self._config.runtime.seed_new_searches
        established = self._store.established_searches()
        # First occurrence wins: marketplaces are searched in configuration
        # order, so the preferred marketplace provides URL and currency.
        found: dict[str, Listing] = {}
        # Listings returned by at least one search that ran before.
        notify_ids: set[str] = set()
        completed: list[tuple[str, str, int]] = []
        for marketplace in search.marketplaces:
            for query in search.queries:
                try:
                    page = self._searcher.search(
                        query,
                        marketplace,
                        limit=search.results_per_query,
                        buying_options=search.buying_options,
                    )
                except EbayApiError as exc:
                    report.searches_failed += 1
                    log.error("Search failed: %s", exc)
                    continue
                report.searches_ok += 1
                report.results += len(page.item_summaries)
                completed.append((query, marketplace, page.total))
                first_time = (query, marketplace) not in established
                if first_time:
                    report.new_searches += 1
                seeding = seed and first_time
                for item in page.item_summaries:
                    listing = Listing.from_summary(item, marketplace=marketplace, query=query)
                    found.setdefault(listing.listing_id, listing)
                    if not seeding:
                        notify_ids.add(listing.listing_id)
        if report.searches_ok == 0:
            raise CycleError(f"all {report.searches_failed} searches failed")

        known = self._store.known_ids(found)
        new = [
            (
                listing,
                ListingStatus.PENDING if listing_id in notify_ids else ListingStatus.SEEDED,
            )
            for listing_id, listing in found.items()
            if listing_id not in known
        ]
        report.new = sum(status is ListingStatus.PENDING for _, status in new)
        report.seeded = len(new) - report.new
        self._store.save_cycle(self._clock(), new=new, seen=known, searches=completed)
        if report.new_searches:
            log.info(
                "%d search(es) ran for the first time, %d listing(s) recorded without notification",
                report.new_searches,
                report.seeded,
            )

    def _notify_phase(self, report: CycleReport) -> None:
        if report.new_searches and self._config.runtime.seed_new_searches:
            self._send_text(
                f"Started watching {report.new_searches} new search(es). "
                f"{report.seeded} listing(s) already online were recorded without "
                "notification; from now on only new listings are notified."
            )
        pending = self._store.pending()
        cap = self._config.runtime.max_notifications_per_cycle
        to_send, overflow = pending[:cap], pending[cap:]
        consecutive_failures = 0
        for listing in to_send:
            try:
                self._notifier.notify_listing(listing)
            except NotificationError as exc:
                report.notify_failed += 1
                consecutive_failures += 1
                status = self._store.mark_notify_failed(
                    listing.listing_id, max_attempts=MAX_NOTIFY_ATTEMPTS
                )
                log.error(
                    "Notification failed for listing %s: %s%s",
                    listing.listing_id,
                    exc,
                    " (giving up)" if status is ListingStatus.FAILED else "",
                )
                if consecutive_failures >= MAX_CONSECUTIVE_NOTIFY_FAILURES:
                    log.error("Notification channel looks down, retrying in the next cycle")
                    return
                continue
            consecutive_failures = 0
            self._store.mark_notified(listing.listing_id, self._clock())
            report.notified += 1
        if overflow:
            self._store.set_status(
                [listing.listing_id for listing in overflow], ListingStatus.SUPPRESSED
            )
            report.suppressed = len(overflow)
            self._send_text(format_overflow(overflow, cap))

    def _send_text(self, text: str) -> None:
        """Best-effort service message: a failure is logged, not raised."""
        try:
            self._notifier.send_text(text)
        except NotificationError as exc:
            log.error("Could not send a service message: %s", exc)


def format_overflow(listings: Sequence[Listing], cap: int) -> str:
    """One message listing what exceeded the per-cycle notification cap."""
    lines = [
        f"{len(listings)} more new listing(s), not notified one by one (limit {cap} per cycle):"
    ]
    for listing in listings[:MAX_SUMMARY_ENTRIES]:
        title = html.escape(listing.title, quote=False)
        price = f" - {listing.price}" if listing.price is not None else ""
        lines.append(f'- <a href="{html.escape(listing.url)}">{title}</a>{price}')
    if len(listings) > MAX_SUMMARY_ENTRIES:
        lines.append(f"... and {len(listings) - MAX_SUMMARY_ENTRIES} more.")
    return "\n".join(lines)
