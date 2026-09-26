"""SQLite persistence with the standard library ``sqlite3`` module.

Tables:

- ``listings``: every listing ever seen, keyed by the legacy item id, with its
  notification state. No seller data is stored.
- ``searches``: (query, marketplace) pairs that completed at least once. The
  first completion of a pair seeds the listings that were already online.
- ``runs``: one row per poll cycle, with Browse API usage and outcome.

The schema version lives in ``PRAGMA user_version``; pending migrations are
applied in order when the database is opened.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Self

from ebay_sniper.models import Listing, Money

MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE listings (
        listing_id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL,
        marketplace TEXT NOT NULL,
        query TEXT NOT NULL,
        title TEXT NOT NULL,
        url TEXT NOT NULL,
        price TEXT,
        price_currency TEXT,
        original_price TEXT,
        original_price_currency TEXT,
        current_bid TEXT,
        current_bid_currency TEXT,
        bid_count INTEGER,
        shipping TEXT,
        shipping_currency TEXT,
        buying_options TEXT NOT NULL,
        condition TEXT,
        condition_id TEXT,
        location_country TEXT,
        image_urls TEXT NOT NULL,
        origin_date TEXT,
        end_date TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        status TEXT NOT NULL,
        notify_attempts INTEGER NOT NULL DEFAULT 0,
        notified_at TEXT
    );
    CREATE INDEX listings_status ON listings (status);

    CREATE TABLE searches (
        query TEXT NOT NULL,
        marketplace TEXT NOT NULL,
        first_ok_at TEXT NOT NULL,
        last_ok_at TEXT NOT NULL,
        last_total INTEGER NOT NULL,
        PRIMARY KEY (query, marketplace)
    );

    CREATE TABLE runs (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,
        api_calls INTEGER NOT NULL DEFAULT 0,
        results INTEGER NOT NULL DEFAULT 0,
        new_listings INTEGER NOT NULL DEFAULT 0,
        notified INTEGER NOT NULL DEFAULT 0,
        error TEXT
    );
    """,
)

# SQLite limits the number of bound parameters per statement.
_CHUNK = 500


class ListingStatus(StrEnum):
    # New and waiting to be notified (also after a failed notification).
    PENDING = "pending"
    NOTIFIED = "notified"
    # Already online when its search ran for the first time: recorded only.
    SEEDED = "seeded"
    # Over the per-cycle notification cap: summarised, not notified.
    SUPPRESSED = "suppressed"
    # The notification kept failing.
    FAILED = "failed"


class RunStatus(StrEnum):
    RUNNING = "running"
    OK = "ok"
    # Some searches or notifications failed, the cycle still completed.
    PARTIAL = "partial"
    FAILED = "failed"
    # Found still running long after it started (killed process).
    ABANDONED = "abandoned"


class Store:
    """Thin repository over one SQLite connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._migrate()

    @classmethod
    def open(cls, path: Path) -> Self:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit mode: transactions are explicit, see _transaction().
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute("PRAGMA journal_mode = WAL")
        return cls(conn)

    @classmethod
    def in_memory(cls) -> Self:
        return cls(sqlite3.connect(":memory:", isolation_level=None))

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def _migrate(self) -> None:
        for version in range(self.schema_version, len(MIGRATIONS)):
            self._conn.executescript(
                f"BEGIN;\n{MIGRATIONS[version]}\nPRAGMA user_version = {version + 1};\nCOMMIT;"
            )

    @contextmanager
    def _transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # Runs

    def begin_run(self, now: datetime, *, stale_after: timedelta) -> int | None:
        """Register a new run, or return None if another one is still active.

        A run left in the running state for longer than ``stale_after`` belongs
        to a process that died; it is marked abandoned and does not block.
        """
        with self._transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT started_at FROM runs WHERE status = ? ORDER BY run_id DESC LIMIT 1",
                (RunStatus.RUNNING,),
            ).fetchone()
            if row is not None:
                if _from_iso(row["started_at"]) > now - stale_after:
                    return None
                conn.execute(
                    "UPDATE runs SET status = ? WHERE status = ?",
                    (RunStatus.ABANDONED, RunStatus.RUNNING),
                )
            cursor = conn.execute(
                "INSERT INTO runs (started_at, status) VALUES (?, ?)",
                (_to_iso(now), RunStatus.RUNNING),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def finish_run(
        self,
        run_id: int,
        now: datetime,
        *,
        status: RunStatus,
        api_calls: int,
        results: int,
        new_listings: int,
        notified: int,
        error: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE runs
            SET finished_at = ?, status = ?, api_calls = ?, results = ?,
                new_listings = ?, notified = ?, error = ?
            WHERE run_id = ?
            """,
            (_to_iso(now), status, api_calls, results, new_listings, notified, error, run_id),
        )

    def api_calls_since(self, since: datetime) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(api_calls), 0) FROM runs WHERE started_at >= ?",
            (_to_iso(since),),
        ).fetchone()
        return int(row[0])

    # Searches

    def established_searches(self) -> set[tuple[str, str]]:
        rows = self._conn.execute("SELECT query, marketplace FROM searches")
        return {(row["query"], row["marketplace"]) for row in rows}

    # Listings

    def known_ids(self, listing_ids: Iterable[str]) -> set[str]:
        ids = list(dict.fromkeys(listing_ids))
        known: set[str] = set()
        for start in range(0, len(ids), _CHUNK):
            chunk = ids[start : start + _CHUNK]
            placeholders = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT listing_id FROM listings WHERE listing_id IN ({placeholders})", chunk
            )
            known.update(row["listing_id"] for row in rows)
        return known

    def save_cycle(
        self,
        now: datetime,
        *,
        new: Iterable[tuple[Listing, ListingStatus]] = (),
        seen: Iterable[str] = (),
        searches: Iterable[tuple[str, str, int]] = (),
    ) -> None:
        """Persist the outcome of the search phase of a cycle in one transaction.

        ``new`` are listings never seen before with their initial status,
        ``seen`` the ids of known listings found again, and ``searches`` the
        (query, marketplace, total results) of every search that succeeded.
        """
        timestamp = _to_iso(now)
        with self._transaction() as conn:
            conn.executemany(
                f"INSERT INTO listings ({', '.join(_LISTING_COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(_LISTING_COLUMNS))})",
                (
                    (*_listing_values(listing), timestamp, timestamp, status)
                    for listing, status in new
                ),
            )
            conn.executemany(
                "UPDATE listings SET last_seen_at = ? WHERE listing_id = ?",
                ((timestamp, listing_id) for listing_id in seen),
            )
            conn.executemany(
                """
                INSERT INTO searches (query, marketplace, first_ok_at, last_ok_at, last_total)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (query, marketplace)
                DO UPDATE SET last_ok_at = excluded.last_ok_at, last_total = excluded.last_total
                """,
                (
                    (query, marketplace, timestamp, timestamp, total)
                    for query, marketplace, total in searches
                ),
            )

    def pending(self) -> list[Listing]:
        """Listings waiting for a notification, newest first.

        Listings whose notification already failed come last, so that one that
        always fails cannot hold back the others.
        """
        rows = self._conn.execute(
            """
            SELECT * FROM listings WHERE status = ?
            ORDER BY notify_attempts, origin_date DESC, first_seen_at DESC, listing_id
            """,
            (ListingStatus.PENDING,),
        )
        return [_listing_from_row(row) for row in rows]

    def mark_notified(self, listing_id: str, now: datetime) -> None:
        self._conn.execute(
            "UPDATE listings SET status = ?, notified_at = ? WHERE listing_id = ?",
            (ListingStatus.NOTIFIED, _to_iso(now), listing_id),
        )

    def mark_notify_failed(self, listing_id: str, *, max_attempts: int) -> ListingStatus:
        """Count a failed notification; give up after ``max_attempts``."""
        with self._transaction() as conn:
            conn.execute(
                "UPDATE listings SET notify_attempts = notify_attempts + 1 WHERE listing_id = ?",
                (listing_id,),
            )
            conn.execute(
                "UPDATE listings SET status = ? WHERE listing_id = ? AND notify_attempts >= ?",
                (ListingStatus.FAILED, listing_id, max_attempts),
            )
            row = conn.execute(
                "SELECT status FROM listings WHERE listing_id = ?", (listing_id,)
            ).fetchone()
        return ListingStatus(row["status"])

    def set_status(self, listing_ids: Sequence[str], status: ListingStatus) -> None:
        with self._transaction() as conn:
            conn.executemany(
                "UPDATE listings SET status = ? WHERE listing_id = ?",
                ((status, listing_id) for listing_id in listing_ids),
            )

    def status_of(self, listing_id: str) -> ListingStatus | None:
        row = self._conn.execute(
            "SELECT status FROM listings WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        return None if row is None else ListingStatus(row["status"])

    def get(self, listing_id: str) -> Listing | None:
        row = self._conn.execute(
            "SELECT * FROM listings WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        return None if row is None else _listing_from_row(row)

    def count_by_status(self) -> dict[ListingStatus, int]:
        rows = self._conn.execute("SELECT status, COUNT(*) FROM listings GROUP BY status")
        return {ListingStatus(row[0]): int(row[1]) for row in rows}


_LISTING_COLUMNS = (
    "listing_id",
    "item_id",
    "marketplace",
    "query",
    "title",
    "url",
    "price",
    "price_currency",
    "original_price",
    "original_price_currency",
    "current_bid",
    "current_bid_currency",
    "bid_count",
    "shipping",
    "shipping_currency",
    "buying_options",
    "condition",
    "condition_id",
    "location_country",
    "image_urls",
    "origin_date",
    "end_date",
    "first_seen_at",
    "last_seen_at",
    "status",
)


def _listing_values(listing: Listing) -> tuple[object, ...]:
    """Column values of a listing, in _LISTING_COLUMNS order up to end_date."""
    return (
        listing.listing_id,
        listing.item_id,
        listing.marketplace,
        listing.query,
        listing.title,
        listing.url,
        *_money_values(listing.price),
        *_money_values(listing.original_price),
        *_money_values(listing.current_bid),
        listing.bid_count,
        *_money_values(listing.shipping),
        json.dumps(list(listing.buying_options)),
        listing.condition,
        listing.condition_id,
        listing.location_country,
        json.dumps(list(listing.image_urls)),
        _to_iso(listing.origin_date) if listing.origin_date else None,
        _to_iso(listing.end_date) if listing.end_date else None,
    )


def _listing_from_row(row: sqlite3.Row) -> Listing:
    return Listing(
        listing_id=row["listing_id"],
        item_id=row["item_id"],
        title=row["title"],
        url=row["url"],
        marketplace=row["marketplace"],
        query=row["query"],
        price=_money(row["price"], row["price_currency"]),
        original_price=_money(row["original_price"], row["original_price_currency"]),
        current_bid=_money(row["current_bid"], row["current_bid_currency"]),
        bid_count=row["bid_count"],
        shipping=_money(row["shipping"], row["shipping_currency"]),
        buying_options=tuple(json.loads(row["buying_options"])),
        condition=row["condition"],
        condition_id=row["condition_id"],
        location_country=row["location_country"],
        image_urls=tuple(json.loads(row["image_urls"])),
        origin_date=_from_iso(row["origin_date"]) if row["origin_date"] else None,
        end_date=_from_iso(row["end_date"]) if row["end_date"] else None,
    )


def _money_values(money: Money | None) -> tuple[str | None, str | None]:
    return (None, None) if money is None else (str(money.value), money.currency)


def _money(value: str | None, currency: str | None) -> Money | None:
    return None if value is None or currency is None else Money(Decimal(value), currency)


def _to_iso(value: datetime) -> str:
    """UTC ISO 8601 with a fixed format, so that text order is time order."""
    if value.tzinfo is None:
        raise ValueError("naive datetimes are not stored")
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)
