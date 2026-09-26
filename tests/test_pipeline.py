from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from ebay_sniper.config import AppConfig
from ebay_sniper.ebay.browse import EbayApiError
from ebay_sniper.ebay.models import SearchPage
from ebay_sniper.models import Listing
from ebay_sniper.notify.base import NotificationError
from ebay_sniper.pipeline import CycleError, Pipeline
from ebay_sniper.store import ListingStatus, RunStatus, Store
from factories import make_item, make_page

Q1 = "futura (spider, ragno)"
Q2 = "(spider, spiderweb) watch"
IT, DE = "EBAY_IT", "EBAY_DE"

type Pages = dict[tuple[str, str], SearchPage | Exception]


class FakeSearcher:
    def __init__(self) -> None:
        self.pages: Pages = {}
        self.calls = 0

    def search(
        self, query: str, marketplace: str, *, limit: int, buying_options: Sequence[str]
    ) -> SearchPage:
        self.calls += 1
        result = self.pages.get((query, marketplace), make_page())
        if isinstance(result, Exception):
            raise result
        return result


class FakeNotifier:
    def __init__(self) -> None:
        self.listings: list[Listing] = []
        self.texts: list[str] = []
        self.failing = False
        # Listings whose notification always fails (for example bad content).
        self.broken: set[str] = set()

    def notify_listing(self, listing: Listing) -> None:
        if self.failing:
            raise NotificationError("channel down")
        if listing.listing_id in self.broken:
            raise NotificationError("bad request")
        self.listings.append(listing)

    def send_text(self, text: str) -> None:
        if self.failing:
            raise NotificationError("channel down")
        self.texts.append(text)

    @property
    def ids(self) -> list[str]:
        return [listing.listing_id for listing in self.listings]


@pytest.fixture
def searcher() -> FakeSearcher:
    return FakeSearcher()


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def store() -> Store:
    return Store.in_memory()


def with_runtime(config: AppConfig, **changes: object) -> AppConfig:
    return config.model_copy(update={"runtime": config.runtime.model_copy(update=changes)})


def with_queries(config: AppConfig, *queries: str) -> AppConfig:
    search = config.search.model_copy(update={"queries": list(queries)})
    return config.model_copy(update={"search": search})


def pipeline(
    config: AppConfig, searcher: FakeSearcher, store: Store, notifier: FakeNotifier
) -> Pipeline:
    return Pipeline(config, searcher, store, notifier)


def run_status(store: Store) -> tuple[str, str | None]:
    row = store._conn.execute(
        "SELECT status, error FROM runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    return row["status"], row["error"]


def test_first_cycle_seeds_listings_already_online(
    config: AppConfig, searcher: FakeSearcher, store: Store, notifier: FakeNotifier
) -> None:
    searcher.pages[(Q1, IT)] = make_page(make_item("1"), make_item("2"))
    report = pipeline(config, searcher, store, notifier).run_cycle()
    assert (report.new_searches, report.seeded, report.new, report.notified) == (4, 2, 0, 0)
    assert notifier.listings == []
    assert len(notifier.texts) == 1
    assert "Started watching 4 new search(es). 2 listing(s)" in notifier.texts[0]
    assert store.status_of("1") is ListingStatus.SEEDED
    assert run_status(store) == (RunStatus.OK, None)


def test_new_listings_are_notified_exactly_once(
    config: AppConfig, searcher: FakeSearcher, store: Store, notifier: FakeNotifier
) -> None:
    cycle = pipeline(config, searcher, store, notifier)
    searcher.pages[(Q1, IT)] = make_page(make_item("1"))
    cycle.run_cycle()
    searcher.pages[(Q1, IT)] = make_page(make_item("2"), make_item("1"))
    report = cycle.run_cycle()
    assert (report.new, report.notified, report.new_searches) == (1, 1, 0)
    assert notifier.ids == ["2"]
    cycle.run_cycle()
    assert notifier.ids == ["2"]
    assert len(notifier.texts) == 1
    assert store.status_of("2") is ListingStatus.NOTIFIED


def test_seeding_can_be_disabled(
    config: AppConfig, searcher: FakeSearcher, store: Store, notifier: FakeNotifier
) -> None:
    searcher.pages[(Q1, IT)] = make_page(make_item("1"))
    report = pipeline(
        with_runtime(config, seed_new_searches=False), searcher, store, notifier
    ).run_cycle()
    assert (report.seeded, report.notified) == (0, 1)
    assert notifier.texts == []


def test_duplicates_are_notified_once_from_the_preferred_marketplace(
    config: AppConfig, searcher: FakeSearcher, store: Store, notifier: FakeNotifier
) -> None:
    cycle = pipeline(config, searcher, store, notifier)
    cycle.run_cycle()
    searcher.pages[(Q2, DE)] = make_page(make_item("7", domain="de"))
    searcher.pages[(Q1, IT)] = make_page(make_item("7", domain="it"))
    searcher.pages[(Q2, IT)] = make_page(make_item("7", domain="it"))
    report = cycle.run_cycle()
    assert (report.results, report.new, report.notified) == (3, 1, 1)
    assert notifier.listings[0].url == "https://www.ebay.it/itm/7"
    assert notifier.listings[0].query == Q1


def test_a_new_query_is_seeded_while_old_ones_keep_notifying(
    config: AppConfig, searcher: FakeSearcher, store: Store, notifier: FakeNotifier
) -> None:
    pipeline(with_queries(config, Q1), searcher, store, notifier).run_cycle()
    searcher.pages[(Q1, IT)] = make_page(make_item("10"))
    searcher.pages[(Q2, IT)] = make_page(make_item("11"), make_item("10"))
    report = pipeline(config, searcher, store, notifier).run_cycle()
    assert (report.new_searches, report.new, report.seeded) == (2, 1, 1)
    assert notifier.ids == ["10"]
    assert store.status_of("11") is ListingStatus.SEEDED


def test_failed_searches_make_a_partial_cycle(
    config: AppConfig, searcher: FakeSearcher, store: Store, notifier: FakeNotifier
) -> None:
    searcher.pages[(Q1, IT)] = EbayApiError("search", "HTTP 500")
    report = pipeline(config, searcher, store, notifier).run_cycle()
    assert (report.searches_ok, report.searches_failed) == (3, 1)
    assert report.status is RunStatus.PARTIAL
    assert run_status(store)[0] == RunStatus.PARTIAL
    # The failed search is not established: it seeds when it first succeeds.
    assert (Q1, IT) not in store.established_searches()


def test_all_searches_failing_fails_the_cycle(
    config: AppConfig, searcher: FakeSearcher, store: Store, notifier: FakeNotifier
) -> None:
    for query in (Q1, Q2):
        for marketplace in (IT, DE):
            searcher.pages[(query, marketplace)] = EbayApiError("search", "HTTP 503")
    with pytest.raises(CycleError, match="all 4 searches failed"):
        pipeline(config, searcher, store, notifier).run_cycle()
    status, error = run_status(store)
    assert status == RunStatus.FAILED
    assert error is not None and "CycleError" in error


def test_failed_notifications_are_retried_in_the_next_cycle(
    config: AppConfig, searcher: FakeSearcher, store: Store, notifier: FakeNotifier
) -> None:
    cycle = pipeline(config, searcher, store, notifier)
    cycle.run_cycle()
    searcher.pages[(Q1, IT)] = make_page(make_item("5"))
    notifier.failing = True
    report = cycle.run_cycle()
    assert (report.notified, report.notify_failed) == (0, 1)
    assert store.status_of("5") is ListingStatus.PENDING
    notifier.failing = False
    report = cycle.run_cycle()
    assert (report.new, report.notified) == (0, 1)
    assert notifier.ids == ["5"]


def test_notifications_stop_when_the_channel_is_down(
    config: AppConfig, searcher: FakeSearcher, store: Store, notifier: FakeNotifier
) -> None:
    cycle = pipeline(config, searcher, store, notifier)
    cycle.run_cycle()
    searcher.pages[(Q1, IT)] = make_page(*(make_item(str(index)) for index in range(5)))
    notifier.failing = True
    report = cycle.run_cycle()
    assert report.notify_failed == 2
    assert report.suppressed == 0
    assert store.count_by_status()[ListingStatus.PENDING] == 5


def test_listings_that_keep_failing_do_not_block_the_others(
    config: AppConfig, searcher: FakeSearcher, store: Store, notifier: FakeNotifier
) -> None:
    cycle = pipeline(config, searcher, store, notifier)
    cycle.run_cycle()
    notifier.broken = {"20", "21"}
    searcher.pages[(Q1, IT)] = make_page(
        make_item("20", origin="2026-09-26T10:00:00.000Z"),
        make_item("21", origin="2026-09-26T09:00:00.000Z"),
    )
    cycle.run_cycle()
    # An older listing (a relist keeps its original date) shows up while the
    # broken ones are still pending: it sorts after them by date.
    searcher.pages[(Q1, IT)] = make_page(
        make_item("20", origin="2026-09-26T10:00:00.000Z"),
        make_item("21", origin="2026-09-26T09:00:00.000Z"),
        make_item("22", origin="2026-09-20T08:00:00.000Z"),
    )
    cycle.run_cycle()
    assert notifier.ids == ["22"]
    assert store.status_of("20") is ListingStatus.PENDING


def test_notification_cap_summarises_the_rest(
    config: AppConfig, searcher: FakeSearcher, store: Store, notifier: FakeNotifier
) -> None:
    cycle = pipeline(with_runtime(config, max_notifications_per_cycle=2), searcher, store, notifier)
    cycle.run_cycle()
    searcher.pages[(Q1, IT)] = make_page(
        *(make_item(str(day), origin=f"2026-09-{day:02d}T08:00:00.000Z") for day in range(10, 14))
    )
    report = cycle.run_cycle()
    assert (report.notified, report.suppressed) == (2, 2)
    assert notifier.ids == ["13", "12"]
    summary = notifier.texts[-1]
    assert summary.startswith("2 more new listing(s)")
    assert '<a href="https://www.ebay.it/itm/11">Spider web watch 11</a> - 25.00 EUR' in summary
    assert store.status_of("10") is ListingStatus.SUPPRESSED


def test_a_cycle_is_skipped_while_another_one_runs(
    config: AppConfig, searcher: FakeSearcher, store: Store, notifier: FakeNotifier
) -> None:
    store.begin_run(datetime.now(UTC), stale_after=timedelta(minutes=30))
    report = pipeline(config, searcher, store, notifier).run_cycle()
    assert report.skipped
    assert searcher.calls == 0


def test_api_calls_are_recorded(
    config: AppConfig, searcher: FakeSearcher, store: Store, notifier: FakeNotifier
) -> None:
    report = pipeline(config, searcher, store, notifier).run_cycle()
    assert report.api_calls == 4
    assert store.api_calls_since(datetime(2000, 1, 1, tzinfo=UTC)) == 4
