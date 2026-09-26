"""One poll cycle: search, deduplicate, rules, details, notify.

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
from ebay_sniper.ebay.models import ItemDetails, SearchPage
from ebay_sniper.models import Listing, Verdict
from ebay_sniper.notify.base import NotificationError, Notifier
from ebay_sniper.rules import RuleEngine
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

    def get_item(self, item_id: str, marketplace: str) -> ItemDetails | None: ...


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
    # New listings from searches that ran before (candidates for notification).
    new: int = 0
    seeded: int = 0
    dropped: int = 0
    details: int = 0
    details_failed: int = 0
    details_skipped: int = 0
    # getItem answered 404: notified anyway with a warning.
    gone: int = 0
    notified: int = 0
    notify_failed: int = 0
    suppressed: int = 0
    api_calls: int = 0
    # Another run was still active, nothing was done.
    skipped: bool = False

    @property
    def status(self) -> RunStatus:
        if self.searches_failed or self.details_failed or self.notify_failed:
            return RunStatus.PARTIAL
        return RunStatus.OK

    def summary(self) -> str:
        if self.skipped:
            return "skipped: another run is active"
        searches = self.searches_ok + self.searches_failed
        return (
            f"{self.searches_ok}/{searches} searches ok, {self.results} results, "
            f"{self.new} new, {self.seeded} seeded, {self.dropped} dropped, "
            f"{self.details} details ({self.details_failed} failed, "
            f"{self.details_skipped} skipped, {self.gone} not found), "
            f"{self.notified} notified, {self.notify_failed} notification failures, "
            f"{self.suppressed} suppressed, {self.api_calls} API calls"
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
        self._rules = RuleEngine(config.rules, config.price)

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
            candidates = self._search_phase(report)
            self._details_phase(candidates, report)
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
                dropped=report.dropped,
                error=error,
            )
        log.info("Cycle done: %s", report.summary())
        return report

    def _search_phase(self, report: CycleReport) -> list[Listing]:
        """Search, deduplicate, apply the rules to the search data and persist.

        Returns the new listings that passed the rules, newest first.
        """
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
        new: list[tuple[Listing, ListingStatus]] = []
        candidates: list[Listing] = []
        for listing_id, found_listing in found.items():
            if listing_id in known:
                continue
            # The rules are cheap: seeded listings get a verdict too, for review.
            result = self._rules.evaluate(found_listing)
            listing = found_listing.with_verdict(result.verdict, result.reasons)
            if listing_id not in notify_ids:
                status = ListingStatus.SEEDED
                report.seeded += 1
            elif result.verdict is Verdict.DROP:
                status = ListingStatus.DROPPED
                report.new += 1
                report.dropped += 1
                log.info("Dropped %s %r: %s", listing_id, listing.title, "; ".join(result.reasons))
            else:
                status = ListingStatus.PENDING
                report.new += 1
                candidates.append(listing)
            new.append((listing, status))
        self._store.save_cycle(self._clock(), new=new, seen=known, searches=completed)
        if report.new_searches:
            log.info(
                "%d search(es) ran for the first time, %d listing(s) recorded without notification",
                report.new_searches,
                report.seeded,
            )
        return sorted(candidates, key=_newest_first)

    def _details_phase(self, candidates: Sequence[Listing], report: CycleReport) -> None:
        """getItem for new candidates: all photos, condition notes, precise shipping.

        Beyond the per-cycle cap, or when getItem fails, a listing is notified
        with its search data only: a late notification could cost the item.
        """
        limit = self._config.runtime.max_details_per_cycle
        if len(candidates) > limit:
            report.details_skipped = len(candidates) - limit
            log.warning(
                "%d new listing(s) exceed the details limit of %d per cycle and will be "
                "notified with search data only",
                report.details_skipped,
                limit,
            )
        for listing in candidates[:limit]:
            try:
                details = self._searcher.get_item(listing.item_id, listing.marketplace)
            except EbayApiError as exc:
                report.details_failed += 1
                log.error("Could not fetch the details of %s: %s", listing.listing_id, exc)
                continue
            report.details += 1
            if details is None:
                # Usually the listing ended in the meantime, but a new listing
                # can also be missing briefly: notify it anyway, with a warning.
                report.gone += 1
                log.warning("getItem found no listing %s, notifying search data", listing.url)
                reasons = (*listing.reasons, "details not found: the listing may have ended")
                self._store.update_listing(
                    listing.with_verdict(Verdict.FLAG, reasons), ListingStatus.PENDING
                )
                continue
            detailed = listing.with_details(details)
            result = self._rules.evaluate(detailed)
            detailed = detailed.with_verdict(result.verdict, result.reasons)
            if result.verdict is Verdict.DROP:
                report.dropped += 1
                log.info(
                    "Dropped %s %r after details: %s",
                    listing.listing_id,
                    detailed.title,
                    "; ".join(result.reasons),
                )
                self._store.update_listing(detailed, ListingStatus.DROPPED)
            else:
                self._store.update_listing(detailed, ListingStatus.PENDING)

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


def _newest_first(listing: Listing) -> float:
    return -listing.origin_date.timestamp() if listing.origin_date else 0.0


def format_overflow(listings: Sequence[Listing], cap: int) -> str:
    """One message listing what exceeded the per-cycle notification cap."""
    lines = [
        f"{len(listings)} more new listing(s), not notified one by one (limit {cap} per cycle):"
    ]
    for listing in listings[:MAX_SUMMARY_ENTRIES]:
        title = html.escape(listing.title, quote=False)
        price = f" - {listing.price}" if listing.price is not None else ""
        flag = " [check]" if listing.verdict is Verdict.FLAG else ""
        lines.append(f'- <a href="{html.escape(listing.url)}">{title}</a>{price}{flag}')
    if len(listings) > MAX_SUMMARY_ENTRIES:
        lines.append(f"... and {len(listings) - MAX_SUMMARY_ENTRIES} more.")
    return "\n".join(lines)
