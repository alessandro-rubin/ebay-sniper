"""A local HTML page with the stored listings, their photos and photo scores.

The page is written to disk and opened in the local browser: listing data
stays on this machine (see the hard constraints in CLAUDE.md). Photos are
loaded by the browser straight from eBay's image server.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from datetime import datetime

from ebay_sniper.config import VisionConfig
from ebay_sniper.images import resize_ebay_image
from ebay_sniper.models import Listing
from ebay_sniper.store import ListingStatus
from ebay_sniper.vision import below_threshold_reason

THUMBNAIL_SIZE = "s-l225"

_STYLE = """
:root { --bg: #f6f6f4; --card: #fff; --text: #1d1d1b; --muted: #6b6b66; --line: #ddd;
  --pass: #1f7a3a; --fail: #a23b2a; --bar: #3a6ea5; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #161615; --card: #22221f; --text: #ecece8; --muted: #a3a39c; --line: #3a3a36;
    --pass: #5cc27a; --fail: #e0806f; --bar: #7aa7d8; }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; background: var(--bg); color: var(--text);
  font: 14px/1.4 system-ui, sans-serif; }
h1 { font-size: 20px; margin: 0 0 4px; }
.meta { color: var(--muted); margin-bottom: 12px; }
.controls { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-bottom: 16px; }
input[type=search] { padding: 6px 8px; min-width: 240px; border: 1px solid var(--line);
  background: var(--card); color: var(--text); border-radius: 4px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 6px;
  padding: 12px; margin-bottom: 12px; }
.head { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: baseline; }
.rank { color: var(--muted); font-variant-numeric: tabular-nums; }
.title { font-weight: 600; flex: 1 1 320px; }
.title a { color: inherit; }
.scores { display: flex; flex-wrap: wrap; gap: 4px 16px; margin: 6px 0;
  font-variant-numeric: tabular-nums; }
.bar { display: inline-block; width: 120px; height: 8px; background: var(--line);
  border-radius: 4px; vertical-align: middle; overflow: hidden; }
.bar span { display: block; height: 100%; background: var(--bar); }
.verdict.pass { color: var(--pass); font-weight: 600; }
.verdict.fail { color: var(--fail); }
.details { color: var(--muted); }
.photos { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.photos img { width: 150px; height: 150px; object-fit: contain; background: var(--bg);
  border: 2px solid transparent; border-radius: 4px; }
.photos img.best { border-color: var(--bar); }
"""

_SCRIPT = """
const box = document.getElementById('filter');
const onlyPass = document.getElementById('only-pass');
function apply() {
  const q = box.value.toLowerCase();
  for (const card of document.querySelectorAll('.card')) {
    const text = card.dataset.text;
    const failing = onlyPass && onlyPass.checked && card.dataset.pass !== '1';
    const hide = (q && !text.includes(q)) || failing;
    card.style.display = hide ? 'none' : '';
  }
}
box.addEventListener('input', apply);
if (onlyPass) onlyPass.addEventListener('change', apply);
"""


def render_report(
    rows: Sequence[tuple[Listing, ListingStatus]],
    *,
    generated: datetime,
    thresholds: VisionConfig | None = None,
) -> str:
    """The page, listings sorted by ``match`` (unscored last).

    With ``thresholds`` (a configuration with the filter on), listings are
    marked as passing or not.
    """
    ranked = sorted(
        rows, key=lambda row: row[0].vision.match if row[0].vision else float("-inf"), reverse=True
    )
    scored = sum(1 for listing, _ in rows if listing.vision)
    passing = sum(1 for listing, _ in rows if _passes(listing, thresholds))
    meta = (
        f"{len(rows)} listings, {scored} with photo scores. Generated {generated:%Y-%m-%d %H:%M}."
    )
    if thresholds is not None:
        meta += (
            f" At match &ge; {thresholds.match_threshold:.3f}, "
            f"colour &ge; {thresholds.colour_threshold:+.3f} and "
            f"web &ge; {thresholds.web_threshold:+.3f}: {passing} would be notified."
        )
    controls = '<input type="search" id="filter" placeholder="Filter by title, status, query">'
    if thresholds is not None:
        controls += '<label><input type="checkbox" id="only-pass"> only passing</label>'
    cards = "\n".join(
        _card(rank, listing, status, thresholds)
        for rank, (listing, status) in enumerate(ranked, start=1)
    )
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Listing scores</title><style>{_STYLE}</style></head><body>"
        "<h1>Listing scores</h1>"
        f'<div class="meta">{meta} Blue border: the photo closest to the positive references. '
        "match: similarity to the wanted watch; "
        "colour: above zero silver-tone, below gold-tone; "
        "web: above zero the dial looks like a spider web.</div>"
        f'<div class="controls">{controls}</div>'
        f"{cards}<script>{_SCRIPT}</script></body></html>\n"
    )


def _passes(listing: Listing, thresholds: VisionConfig | None) -> bool:
    if thresholds is None or listing.vision is None:
        return False
    return below_threshold_reason(listing.vision, thresholds) is None


def _card(
    rank: int, listing: Listing, status: ListingStatus, thresholds: VisionConfig | None
) -> str:
    esc = html.escape
    score = listing.vision
    if score is not None:
        width = max(0.0, min(1.0, score.match)) * 100
        web = f"<span>web {score.web:+.3f}</span>" if score.web is not None else ""
        scores = (
            f'<span>match {score.match:.3f} <span class="bar"><span style="width:{width:.0f}%">'
            f"</span></span></span><span>colour {score.colour:+.3f}</span>{web}"
            f"<span>negative {score.negative:.3f}</span>"
            f"<span>{score.photos} photo(s) compared</span>"
        )
        if thresholds is not None:
            ok = _passes(listing, thresholds)
            scores += (
                f'<span class="verdict {"pass" if ok else "fail"}">'
                f"{'would be notified' if ok else 'below threshold'}</span>"
            )
    else:
        error = f": {esc(listing.vision_error)}" if listing.vision_error else ""
        scores = f"<span>not scored{error}</span>"
    best = score.best_photo if score else -1
    photos = "".join(
        f'<a href="{esc(url)}" target="_blank" rel="noreferrer">'
        f'<img loading="lazy" src="{esc(resize_ebay_image(url, THUMBNAIL_SIZE))}" alt=""'
        f"{' class=best' if index == best else ''}></a>"
        for index, url in enumerate(listing.image_urls)
    )
    price = listing.total or listing.price
    details = " &middot; ".join(
        part
        for part in (
            esc(str(status)),
            esc(listing.marketplace),
            esc(str(price)) if price else "",
            esc(listing.condition or ""),
            f"query <i>{esc(listing.query)}</i>",
        )
        if part
    )
    text = f"{listing.title} {status} {listing.query}".lower()
    passing = int(_passes(listing, thresholds))
    return (
        f'<div class="card" data-text="{esc(text)}" data-pass="{passing}">'
        f'<div class="head"><span class="rank">#{rank}</span>'
        f'<span class="title"><a href="{esc(listing.url)}" target="_blank" rel="noreferrer">'
        f"{esc(listing.title)}</a></span></div>"
        f'<div class="scores">{scores}</div><div class="details">{details}</div>'
        f'<div class="photos">{photos}</div></div>'
    )
