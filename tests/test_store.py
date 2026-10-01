from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ebay_sniper.ebay.models import ItemDetails, SearchPage
from ebay_sniper.models import Listing, Money, Verdict, VisionScore
from ebay_sniper.store import MIGRATIONS, ListingStatus, RunStatus, Store
from factories import load_fixture, make_item, make_page

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def listings_from(page: SearchPage, marketplace: str = "EBAY_IT") -> list[Listing]:
    return [
        Listing.from_summary(item, marketplace=marketplace, query="q")
        for item in page.item_summaries
    ]


@pytest.fixture
def store() -> Iterator[Store]:
    with Store.in_memory() as store:
        yield store


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


def test_upgrade_from_the_m1_schema_keeps_the_data(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(f"BEGIN;\n{MIGRATIONS[0]}\nPRAGMA user_version = 1;\nCOMMIT;")
    conn.execute(
        "INSERT INTO listings (listing_id, item_id, marketplace, query, title, url, "
        "buying_options, image_urls, first_seen_at, last_seen_at, status) "
        "VALUES ('9', 'v1|9|0', 'EBAY_IT', 'q', 'Old', 'https://www.ebay.it/itm/9', "
        "'[\"FIXED_PRICE\"]', '[]', '2026-09-01T00:00:00+00:00', "
        "'2026-09-01T00:00:00+00:00', 'pending')"
    )
    conn.close()
    with Store.open(path) as store:
        assert store.schema_version == len(MIGRATIONS) == 3
        old = store.get("9")
        assert old is not None
        assert (old.title, old.verdict, old.reasons, old.details_fetched, old.vision) == (
            "Old",
            None,
            (),
            False,
            None,
        )
        assert [listing.listing_id for listing in store.pending()] == ["9"]


def test_update_listing_stores_details_and_verdict(store: Store) -> None:
    page = SearchPage.model_validate(load_fixture("search_ebay_it.json"))
    listing = listings_from(page)[0]
    store.save_cycle(NOW, new=[(listing, ListingStatus.PENDING)])
    details = ItemDetails.model_validate(load_fixture("item_110000000001.json"))
    detailed = listing.with_details(details).with_verdict(Verdict.FLAG, ["a", "b"])
    detailed = replace(detailed, import_charges=Money(Decimal("3.10"), "EUR"))
    store.update_listing(detailed, ListingStatus.PENDING)
    assert store.get(listing.listing_id) == detailed
    store.update_listing(detailed, ListingStatus.DROPPED)
    assert store.status_of(listing.listing_id) is ListingStatus.DROPPED


def test_vision_score_round_trip(store: Store) -> None:
    page = SearchPage.model_validate(load_fixture("search_ebay_it.json"))
    first, second = listings_from(page)[:2]
    store.save_cycle(NOW, new=[(first, ListingStatus.PENDING), (second, ListingStatus.PENDING)])
    score = VisionScore(
        match=0.84, negative=0.71, colour=0.012, best_photo=2, photos=4, model="m/p"
    )
    scored = first.with_vision(score)
    failed = second.with_vision(None, "could not download the photos")
    store.update_listing(scored, ListingStatus.BELOW_THRESHOLD)
    store.update_listing(failed, ListingStatus.PENDING)
    assert store.get(first.listing_id) == scored
    assert store.get(second.listing_id) == failed
    assert store.status_of(first.listing_id) is ListingStatus.BELOW_THRESHOLD
