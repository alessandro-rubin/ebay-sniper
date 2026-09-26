from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ebay_sniper.ebay.models import SearchPage
from ebay_sniper.models import Listing
from ebay_sniper.store import MIGRATIONS, ListingStatus, RunStatus, Store
from factories import load_fixture, make_item, make_page

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def listings_from(page: SearchPage, marketplace: str = "EBAY_IT") -> list[Listing]:
    return [
        Listing.from_summary(item, marketplace=marketplace, query="q")
        for item in page.item_summaries
    ]


@pytest.fixture
def store() -> Store:
    return Store.in_memory()


def test_migrations_run_once(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "db.sqlite3"
    with Store.open(path) as store:
        assert store.schema_version == len(MIGRATIONS)
    with Store.open(path) as store:
        assert store.schema_version == len(MIGRATIONS)


def test_listing_round_trip(store: Store) -> None:
    listings = listings_from(SearchPage.model_validate(load_fixture("search_ebay_it.json")))
    store.save_cycle(NOW, new=[(listing, ListingStatus.PENDING) for listing in listings])
    assert store.known_ids(["110000000001", "999"]) == {"110000000001"}
    for listing in listings:
        assert store.get(listing.listing_id) == listing
    # Newest first.
    assert [listing.listing_id for listing in store.pending()] == [
        "110000000001",
        "120000000002",
        "130000000003",
    ]


def test_no_seller_data_is_stored(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    page = SearchPage.model_validate(load_fixture("search_ebay_it.json"))
    with Store.open(path) as store:
        store.save_cycle(NOW, new=[(item, ListingStatus.SEEDED) for item in listings_from(page)])
    # Closing checkpoints the write-ahead log into the main file.
    content = b"".join(file.read_bytes() for file in tmp_path.glob("db.sqlite3*"))
    assert b"example_seller" not in content
    assert b"351**" not in content
    assert b"Futura Quartz" in content


def test_known_ids_handles_many_ids(store: Store) -> None:
    items = [make_item(str(100000 + index)) for index in range(1200)]
    listings = listings_from(make_page(*items))
    store.save_cycle(NOW, new=[(listing, ListingStatus.SEEDED) for listing in listings])
    ids = [listing.listing_id for listing in listings] + ["missing"]
    assert len(store.known_ids(ids)) == 1200


def test_seen_listings_and_searches_are_updated(store: Store) -> None:
    listing = listings_from(make_page(make_item("1")))[0]
    store.save_cycle(NOW, new=[(listing, ListingStatus.SEEDED)], searches=[("q", "EBAY_IT", 5)])
    later = NOW + timedelta(hours=1)
    store.save_cycle(later, seen=["1"], searches=[("q", "EBAY_IT", 6), ("q", "EBAY_DE", 0)])
    assert store.established_searches() == {("q", "EBAY_IT"), ("q", "EBAY_DE")}
    row = store._conn.execute(
        "SELECT first_ok_at, last_ok_at, last_total FROM searches WHERE marketplace = 'EBAY_IT'"
    ).fetchone()
    assert tuple(row) == (NOW.isoformat(), later.isoformat(), 6)
    seen = store._conn.execute("SELECT last_seen_at FROM listings").fetchone()[0]
    assert seen == later.isoformat()


def test_notification_state(store: Store) -> None:
    first, second = listings_from(make_page(make_item("1"), make_item("2")))
    store.save_cycle(NOW, new=[(first, ListingStatus.PENDING), (second, ListingStatus.PENDING)])
    store.mark_notified("1", NOW)
    assert store.status_of("1") is ListingStatus.NOTIFIED
    assert store.mark_notify_failed("2", max_attempts=2) is ListingStatus.PENDING
    assert store.mark_notify_failed("2", max_attempts=2) is ListingStatus.FAILED
    assert store.pending() == []
    assert store.count_by_status() == {ListingStatus.NOTIFIED: 1, ListingStatus.FAILED: 1}
    store.set_status(["1"], ListingStatus.SUPPRESSED)
    assert store.status_of("1") is ListingStatus.SUPPRESSED
    assert store.status_of("missing") is None


def test_only_one_run_at_a_time(store: Store) -> None:
    run_id = store.begin_run(NOW, stale_after=timedelta(minutes=30))
    assert run_id is not None
    assert store.begin_run(NOW + timedelta(minutes=5), stale_after=timedelta(minutes=30)) is None
    store.finish_run(
        run_id, NOW, status=RunStatus.OK, api_calls=20, results=0, new_listings=0, notified=0
    )
    assert store.begin_run(NOW + timedelta(minutes=6), stale_after=timedelta(minutes=30))


def test_stale_run_is_abandoned(store: Store) -> None:
    stale = store.begin_run(NOW, stale_after=timedelta(minutes=30))
    fresh = store.begin_run(NOW + timedelta(minutes=31), stale_after=timedelta(minutes=30))
    assert fresh is not None and fresh != stale
    statuses = dict(store._conn.execute("SELECT run_id, status FROM runs").fetchall())
    assert statuses == {stale: RunStatus.ABANDONED, fresh: RunStatus.RUNNING}


def test_api_calls_since(store: Store) -> None:
    for hours_ago, calls in ((30, 100), (2, 20), (1, 20)):
        run_id = store.begin_run(NOW - timedelta(hours=hours_ago), stale_after=timedelta(0))
        assert run_id is not None
        store.finish_run(
            run_id,
            NOW - timedelta(hours=hours_ago),
            status=RunStatus.OK,
            api_calls=calls,
            results=0,
            new_listings=0,
            notified=0,
        )
    assert store.api_calls_since(NOW - timedelta(days=1)) == 40


def test_naive_datetimes_are_rejected(store: Store) -> None:
    with pytest.raises(ValueError, match="naive"):
        store.begin_run(datetime(2026, 1, 1), stale_after=timedelta(minutes=30))
