from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from ebay_sniper.cli import main
from ebay_sniper.models import Listing, VisionScore
from ebay_sniper.pipeline import utc_now
from ebay_sniper.report import render_report
from ebay_sniper.store import ListingStatus, Store
from factories import make_item, make_page

NOW = datetime(2026, 10, 1, 23, 0)


def listings() -> list[Listing]:
    page = make_page(
        make_item("1", title="Low <b>match</b>", images=2),
        make_item("2", title="High match", images=3),
        make_item("3", title="Not scored"),
    )
    low, high, unscored = (
        Listing.from_summary(item, marketplace="EBAY_IT", query="q") for item in page.item_summaries
    )

    def score(match: float, best: int) -> VisionScore:
        return VisionScore(
            match=match, negative=0.5, colour=0.02, best_photo=best, photos=2, model="m"
        )

    return [
        low.with_vision(score(0.40, 0)),
        high.with_vision(score(0.85, 2)),
        unscored.with_vision(None, "no photo could be downloaded"),
    ]


def test_report_ranks_by_match_and_marks_thresholds() -> None:
    rows = [(listing, ListingStatus.SEEDED) for listing in listings()]
    page = render_report(rows, generated=NOW, thresholds=(0.8, 0.0))
    assert (
        page.index("High match")
        < page.index("Low &lt;b&gt;match&lt;/b&gt;")
        < page.index("Not scored")
    )
    assert "<b>match</b>" not in page
    assert "3 listings, 2 with photo scores" in page
    assert "1 would be notified" in page
    assert page.count("would be notified</span>") == 1
    assert "not scored: no photo could be downloaded" in page
    # The best photo of the high match is its third one, shown as a thumbnail.
    assert 'src="https://i.ebayimg.com/images/g/22/s-l225.jpg" alt="" class=best' in page


def test_report_without_thresholds_marks_nothing() -> None:
    rows = [(listing, ListingStatus.SEEDED) for listing in listings()]
    page = render_report(rows, generated=NOW)
    assert "would be notified" not in page
    assert 'id="only-pass"' not in page


def test_report_command_writes_the_page(config_path: Path, capsys: pytest.CaptureFixture) -> None:
    db = config_path.parent / "data" / "test.sqlite3"
    assert main(["--config", str(config_path), "report", "--no-open"]) == 1
    with Store.open(db) as store:
        store.save_cycle(
            utc_now(),
            new=[(replace(listing, vision=None), ListingStatus.SEEDED) for listing in listings()],
        )
    out_file = config_path.parent / "page.html"
    code = main(
        ["--config", str(config_path), "report", "--no-open", "-o", str(out_file), "--match", "0.7"]
    )
    assert code == 0
    assert "Wrote 3 listings" in capsys.readouterr().out
    assert "match &ge; 0.700" in out_file.read_text(encoding="utf-8")
