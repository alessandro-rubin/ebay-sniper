from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest

from ebay_sniper.config import AppConfig
from ebay_sniper.ebay.browse import EbayApiError
from ebay_sniper.ebay.models import ItemDetails, ItemSummary, SearchPage
from ebay_sniper.models import Listing, Verdict
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
        # getItem answers by listing id; by default the search data is echoed.
        self.details: dict[str, ItemDetails | Exception | None] = {}
        self.detail_requests: list[str] = []
        self.calls = 0
        self._summaries: dict[str, ItemSummary] = {}

    def search(
        self, query: str, marketplace: str, *, limit: int, buying_options: Sequence[str]
    ) -> SearchPage:
        self.calls += 1
        result = self.pages.get((query, marketplace), make_page())
        if isinstance(result, Exception):
            raise result
        self._summaries.update((item.listing_id, item) for item in result.item_summaries)
        return result

    def get_item(self, item_id: str, marketplace: str) -> ItemDetails | None:
        self.calls += 1
        listing_id = item_id.split("|")[1]
        self.detail_requests.append(listing_id)
        if listing_id in self.details:
            result = self.details[listing_id]
            if isinstance(result, Exception):
                raise result
            return result
        return ItemDetails.model_validate(self._summaries[listing_id].model_dump())


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
def store() -> Iterator[Store]:
    with Store.in_memory() as store:
        yield store


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


def details(listing_id: str, **extra: object) -> ItemDetails:
    return ItemDetails.model_validate(make_item(listing_id, **extra))


def test_rules_drop_before_fetching_details(
    config: AppConfig, searcher: FakeSearcher, store: Store, notifier: FakeNotifier
) -> None:
    cycle = pipeline(config, searcher, store, notifier)
    cycle.run_cycle()
    searcher.pages[(Q1, IT)] = make_page(
        make_item("30", title="Spider watch, cracked crystal"),
        make_item("31"),
        make_item("32", price="401.00"),
    )
    report = cycle.run_cycle()
    assert (report.new, report.dropped, report.notified) == (3, 2, 1)
    assert searcher.detail_requests == ["31"]
    assert notifier.ids == ["31"]
    dropped = store.get("30")
    assert dropped is not None
    assert (dropped.verdict, dropped.reasons) == (Verdict.DROP, ("'cracked crystal' in title",))
    assert store.status_of("30") is ListingStatus.DROPPED
    expensive = store.get("32")
    assert expensive is not None
    assert expensive.reasons == ("total 401.00 EUR above the cap of 400.00 EUR",)
    runs = store._conn.execute("SELECT dropped FROM runs ORDER BY run_id DESC").fetchone()
    assert runs["dropped"] == 2


def test_condition_notes_drop_or_flag_after_details(
    config: AppConfig, searcher: FakeSearcher, store: Store, notifier: FakeNotifier
) -> None:
    cycle = pipeline(config, searcher, store, notifier)
    cycle.run_cycle()
    searcher.pages[(Q1, IT)] = make_page(make_item("40"), make_item("41"))
    searcher.details["40"] = details("40", conditionDescription="Vetro rotto sul lato.")
    searcher.details["41"] = details("41", images=3, conditionDescription="Nessun vetro rotto.")
    report = cycle.run_cycle()
    assert (report.details, report.dropped, report.notified) == (2, 1, 1)
    assert store.status_of("40") is ListingStatus.DROPPED
    sent = notifier.listings[0]
    assert sent.listing_id == "41"
    assert sent.details_fetched
    assert len(sent.image_urls) == 3
    assert (sent.verdict, sent.reasons) == (
        Verdict.FLAG,
        ("negated 'vetro rotto' in condition notes",),
    )
    # The details are stored with the listing, for retries and later review.
    stored = store.get("41")
    assert stored is not None
    assert stored.condition_description == "Nessun vetro rotto."


def test_listings_missing_at_details_time_are_notified_with_a_warning(
    config: AppConfig, searcher: FakeSearcher, store: Store, notifier: FakeNotifier
) -> None:
    cycle = pipeline(config, searcher, store, notifier)
    cycle.run_cycle()
    searcher.pages[(Q1, IT)] = make_page(make_item("50", title="Spider watch needs battery"))
    searcher.details["50"] = None
    report = cycle.run_cycle()
    assert (report.gone, report.notified) == (1, 1)
    sent = notifier.listings[0]
    assert not sent.details_fetched
    assert sent.verdict is Verdict.FLAG
    assert sent.reasons == (
        "'needs battery' in title",
        "details not found: the listing may have ended",
    )


def test_details_failures_and_cap_fall_back_to_search_data(
    config: AppConfig, searcher: FakeSearcher, store: Store, notifier: FakeNotifier
) -> None:
    cycle = pipeline(with_runtime(config, max_details_per_cycle=1), searcher, store, notifier)
    cycle.run_cycle()
    searcher.pages[(Q1, IT)] = make_page(
        make_item("61", origin="2026-09-26T08:00:00.000Z"),
        make_item("60", origin="2026-09-26T09:00:00.000Z"),
    )
    searcher.details["60"] = EbayApiError("getItem", "HTTP 500")
    report = cycle.run_cycle()
    # The newest listing gets the only details call; it fails.
    assert searcher.detail_requests == ["60"]
    assert (report.details_failed, report.details_skipped) == (1, 1)
    assert report.status is RunStatus.PARTIAL
    assert notifier.ids == ["60", "61"]
    assert not any(listing.details_fetched for listing in notifier.listings)


def test_seeded_listings_get_a_verdict_but_no_details(
    config: AppConfig, searcher: FakeSearcher, store: Store, notifier: FakeNotifier
) -> None:
    searcher.pages[(Q1, IT)] = make_page(make_item("70", title="Spider watch, cracked crystal"))
    pipeline(config, searcher, store, notifier).run_cycle()
    stored = store.get("70")
    assert stored is not None
    assert store.status_of("70") is ListingStatus.SEEDED
    assert stored.verdict is Verdict.DROP
    assert searcher.detail_requests == []
