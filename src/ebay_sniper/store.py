"""SQLite persistence with the standard library ``sqlite3`` module.

Tables:

- ``listings``: every listing ever seen, keyed by the legacy item id, with its
  notification state. No seller data is stored.
- ``searches``: (query, marketplace) pairs that completed at least once. The
  first completion of a pair seeds the listings that were already online.
- ``runs``: one row per poll cycle, with Browse API usage and outcome.
- ``state``: small key-value records (dates of the daily messages, failure
  streak already alerted).

The schema version lives in ``PRAGMA user_version``; pending migrations are
applied in order when the database is opened.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Self

from ebay_sniper.models import Listing, Money, Verdict, VisionScore

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
    # M2: details from getItem and the verdict of the rules.
    """
    ALTER TABLE listings ADD COLUMN details_fetched INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE listings ADD COLUMN condition_description TEXT;
    ALTER TABLE listings ADD COLUMN import_charges TEXT;
    ALTER TABLE listings ADD COLUMN import_charges_currency TEXT;
    ALTER TABLE listings ADD COLUMN minimum_bid TEXT;
    ALTER TABLE listings ADD COLUMN minimum_bid_currency TEXT;
    ALTER TABLE listings ADD COLUMN reserve_met INTEGER;
    ALTER TABLE listings ADD COLUMN verdict TEXT;
    ALTER TABLE listings ADD COLUMN reasons TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE runs ADD COLUMN dropped INTEGER NOT NULL DEFAULT 0;
    """,
    # M3: image comparison with the reference sets.
    """
    ALTER TABLE listings ADD COLUMN vision_match REAL;
    ALTER TABLE listings ADD COLUMN vision_negative REAL;
    ALTER TABLE listings ADD COLUMN vision_colour REAL;
    ALTER TABLE listings ADD COLUMN vision_best_photo INTEGER;
    ALTER TABLE listings ADD COLUMN vision_photos INTEGER;
    ALTER TABLE listings ADD COLUMN vision_model TEXT;
    ALTER TABLE listings ADD COLUMN vision_error TEXT;
    ALTER TABLE runs ADD COLUMN below_threshold INTEGER NOT NULL DEFAULT 0;
    """,
    # M3: daily digest of the listings below the photo thresholds.
    """
    ALTER TABLE listings ADD COLUMN digested_at TEXT;
    CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
    # M3: spider-web dial probe (NULL for listings scored before it).
    """
    ALTER TABLE listings ADD COLUMN vision_web REAL;
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
    # Discarded by the rules; kept to review and tune them.
    DROPPED = "dropped"
    # Photos not close enough to the wanted variant: kept for the near-miss
    # digest and to recalibrate the thresholds.
    BELOW_THRESHOLD = "below_threshold"
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


@dataclass(frozen=True, slots=True)
class RunRecord:
    """A finished (or abandoned) poll cycle."""

    started_at: datetime
    status: RunStatus
    api_calls: int
    new_listings: int
    notified: int
    dropped: int
    below_threshold: int
    error: str | None


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
        return cls._connect(path, wal=True)

    @classmethod
    def in_memory(cls) -> Self:
        return cls._connect(":memory:", wal=False)

    @classmethod
    def _connect(cls, target: Path | str, *, wal: bool) -> Self:
        conn = sqlite3.connect(target, isolation_level=None)
        try:
            if wal:
                conn.execute("PRAGMA journal_mode = WAL")
            return cls(conn)
        except BaseException:
            # A failed migration must not leave the connection open.
            conn.close()
            raise

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
        dropped: int = 0,
        below_threshold: int = 0,
        error: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE runs
            SET finished_at = ?, status = ?, api_calls = ?, results = ?,
                new_listings = ?, notified = ?, dropped = ?, below_threshold = ?,
                error = ?
            WHERE run_id = ?
            """,
            (
                _to_iso(now),
                status,
                api_calls,
                results,
                new_listings,
                notified,
                dropped,
                below_threshold,
                error,
                run_id,
            ),
        )

    def recent_runs(self, limit: int) -> list[RunRecord]:
        """The latest runs that are no longer running, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE status != ? ORDER BY run_id DESC LIMIT ?",
            (RunStatus.RUNNING, limit),
        )
        return [_run_from_row(row) for row in rows]

    def runs_since(self, since: datetime) -> list[RunRecord]:
        """The runs started at or after ``since`` that are no longer running, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE status != ? AND started_at >= ? ORDER BY run_id DESC",
            (RunStatus.RUNNING, _to_iso(since)),
        )
        return [_run_from_row(row) for row in rows]

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
        columns = (*_DATA_COLUMNS, "first_seen_at", "last_seen_at", "status")
        with self._transaction() as conn:
            conn.executemany(
                f"INSERT INTO listings ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' * len(columns))})",
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

    def update_listing(self, listing: Listing, status: ListingStatus) -> None:
        """Store what changed after getItem and the rules, and the new status."""
        columns = [column for column in _DATA_COLUMNS if column != "listing_id"]
        values = _listing_values(listing)[1:]
        self._conn.execute(
            f"UPDATE listings SET {', '.join(f'{column} = ?' for column in columns)}, "
            "status = ? WHERE listing_id = ?",
            (*values, status, listing.listing_id),
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

    def near_misses(self) -> list[Listing]:
        """Listings kept out by the photo thresholds and not in a digest yet, closest first."""
        rows = self._conn.execute(
            """
            SELECT * FROM listings WHERE status = ? AND digested_at IS NULL
            ORDER BY vision_match DESC, listing_id
            """,
            (ListingStatus.BELOW_THRESHOLD,),
        )
        return [_listing_from_row(row) for row in rows]

    def mark_digested(self, listing_ids: Sequence[str], now: datetime) -> None:
        with self._transaction():
            self._conn.executemany(
                "UPDATE listings SET digested_at = ? WHERE listing_id = ?",
                ((_to_iso(now), listing_id) for listing_id in listing_ids),
            )

    def get_state(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_state(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def delete_state(self, key: str) -> None:
        self._conn.execute("DELETE FROM state WHERE key = ?", (key,))

    def recent_with_photos(self, limit: int) -> list[tuple[Listing, ListingStatus]]:
        """The most recently seen listings that have photos, with their status."""
        rows = self._conn.execute(
            """
            SELECT * FROM listings WHERE image_urls != '[]'
            ORDER BY first_seen_at DESC, origin_date DESC, listing_id LIMIT ?
            """,
            (limit,),
        )
        return [(_listing_from_row(row), ListingStatus(row["status"])) for row in rows]

    def update_vision(self, listing: Listing) -> None:
        """Store the photo score of a listing without changing its status (calibration)."""
        self._conn.execute(
            f"UPDATE listings SET {', '.join(f'{c} = ?' for c in _VISION_COLUMNS)} "
            "WHERE listing_id = ?",
            (*_vision_values(listing.vision), listing.vision_error, listing.listing_id),
        )


# Columns holding Listing data, in the order produced by _listing_values().
_BASE_COLUMNS = (
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
    "details_fetched",
    "condition_description",
    "import_charges",
    "import_charges_currency",
    "minimum_bid",
    "minimum_bid_currency",
    "reserve_met",
    "verdict",
    "reasons",
)
_VISION_COLUMNS = (
    "vision_match",
    "vision_negative",
    "vision_colour",
    "vision_best_photo",
    "vision_photos",
    "vision_model",
    "vision_web",
    "vision_error",
)
_DATA_COLUMNS = (*_BASE_COLUMNS, *_VISION_COLUMNS)


def _listing_values(listing: Listing) -> tuple[object, ...]:
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
        int(listing.details_fetched),
        listing.condition_description,
        *_money_values(listing.import_charges),
        *_money_values(listing.minimum_bid),
        None if listing.reserve_met is None else int(listing.reserve_met),
        listing.verdict,
        json.dumps(list(listing.reasons)),
        *_vision_values(listing.vision),
        listing.vision_error,
    )


def _vision_values(score: VisionScore | None) -> tuple[object, ...]:
    if score is None:
        return (None,) * 7
    return (
        score.match,
        score.negative,
        score.colour,
        score.best_photo,
        score.photos,
        score.model,
        score.web,
    )


def _vision_from_row(row: sqlite3.Row) -> VisionScore | None:
    if row["vision_match"] is None:
        return None
    return VisionScore(
        match=row["vision_match"],
        negative=row["vision_negative"],
        colour=row["vision_colour"],
        best_photo=row["vision_best_photo"],
        photos=row["vision_photos"],
        model=row["vision_model"],
        web=row["vision_web"],
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
        details_fetched=bool(row["details_fetched"]),
        condition_description=row["condition_description"],
        import_charges=_money(row["import_charges"], row["import_charges_currency"]),
        minimum_bid=_money(row["minimum_bid"], row["minimum_bid_currency"]),
        reserve_met=None if row["reserve_met"] is None else bool(row["reserve_met"]),
        verdict=Verdict(row["verdict"]) if row["verdict"] else None,
        reasons=tuple(json.loads(row["reasons"])),
        vision=_vision_from_row(row),
        vision_error=row["vision_error"],
    )


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        started_at=_from_iso(row["started_at"]),
        status=RunStatus(row["status"]),
        api_calls=row["api_calls"],
        new_listings=row["new_listings"],
        notified=row["notified"],
        dropped=row["dropped"],
        below_threshold=row["below_threshold"],
        error=row["error"],
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
