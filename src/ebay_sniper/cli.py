"""Command line entry point.

- ``run-once``: one poll cycle, for an external scheduler (cron, systemd timer,
  Windows Task Scheduler). Exit code 0 on success, 1 on failure.
- ``watch``: poll forever at the configured interval.
- ``check-config``: validate configuration and secrets and print the daily
  Browse API budget; ``--live`` also verifies the credentials.
- ``search``: run one query and print the results, to try out query syntax.
- ``notify-test``: send the newest result of a query to Telegram, to see a
  real notification end to end without touching the database.
- ``calibrate``: score the reference images and the stored listings with the
  image model and suggest the vision thresholds.
- ``report``: write a local HTML page with the stored listings, their photos
  and scores, and open it in the browser.
- ``digest``: send the near misses (below the photo thresholds) to Telegram
  now; the cycle also sends them once a day.

Results meant for the user go to stdout, logs go to stderr.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import signal
import sys
import time
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import FrameType
from zoneinfo import ZoneInfo

from ebay_sniper import __version__
from ebay_sniper.app import open_app, open_ebay, open_notifier, open_vision
from ebay_sniper.config import (
    DEFAULT_CONFIG_PATH,
    MAX_QUERY_LENGTH,
    MAX_RESULTS_PER_PAGE,
    SUPPORTED_MARKETPLACES,
    AppConfig,
    ConfigError,
    Secrets,
    VisionConfig,
    compute_budget,
    default_env_file,
    load_config,
    load_secrets,
    validate_query,
)
from ebay_sniper.ebay import EbayApiError, EbayAuthError, SearchPage
from ebay_sniper.logsetup import configure_logging, register_secrets
from ebay_sniper.models import Listing, Verdict
from ebay_sniper.notify import NotificationError
from ebay_sniper.pipeline import CycleError, format_digest, send_digest, utc_now
from ebay_sniper.report import render_report
from ebay_sniper.rules import RuleEngine
from ebay_sniper.store import ListingStatus, Store
from ebay_sniper.vision import (
    MATCH_MARGIN,
    VisionError,
    VisionScorer,
    below_threshold_reason,
    reference_files,
    score_references,
    suggest_thresholds,
    vision_available,
)

log = logging.getLogger(__name__)

Handler = Callable[[argparse.Namespace], int]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ebay-sniper",
        description="Watch eBay for a specific item and notify via Telegram. "
        "Global options go before the command.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="configuration file (default: %(default)s)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="secrets file (default: .env next to the configuration file); "
        "environment variables take precedence",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_once = subparsers.add_parser("run-once", help="Run a single poll cycle and exit.")
    run_once.set_defaults(handler=_cmd_run_once)

    watch = subparsers.add_parser("watch", help="Poll forever at the configured interval.")
    watch.set_defaults(handler=_cmd_watch)

    check = subparsers.add_parser(
        "check-config", help="Validate the configuration and print the daily API call budget."
    )
    check.add_argument(
        "--live",
        action="store_true",
        help="also request an eBay token and check the Telegram bot and chat "
        "(no message is sent, no Browse API call is made)",
    )
    check.set_defaults(handler=_cmd_check_config)

    search = subparsers.add_parser(
        "search",
        help="Run one query and print the results (one Browse API call per marketplace).",
    )
    search.add_argument("query", help="eBay keywords: space = AND, (a, b) = OR")
    search.add_argument(
        "-m",
        "--marketplace",
        action="append",
        help="marketplace to search, repeatable (default: the first configured one)",
    )
    search.add_argument(
        "-n",
        "--limit",
        type=_bounded_int(1, MAX_RESULTS_PER_PAGE),
        default=20,
        help="results to show (default: %(default)s)",
    )
    search.add_argument(
        "--save-json",
        type=Path,
        help="save the response without seller data, for example as a test fixture "
        "(one marketplace only)",
    )
    search.set_defaults(handler=_cmd_search)

    notify_test = subparsers.add_parser(
        "notify-test",
        help="Send the newest result of a query to Telegram as a test notification "
        "(two Browse API calls, nothing is stored).",
    )
    notify_test.add_argument(
        "query", nargs="?", help="eBay keywords (default: the first configured query)"
    )
    notify_test.add_argument(
        "-m", "--marketplace", help="marketplace to search (default: the first configured one)"
    )
    notify_test.set_defaults(handler=_cmd_notify_test)

    calibrate = subparsers.add_parser(
        "calibrate",
        help="Score the reference images and the stored listings with the image model "
        "and suggest the vision thresholds (no Browse API call).",
    )
    calibrate.add_argument(
        "-n",
        "--listings",
        type=_bounded_int(0, 10000),
        default=300,
        help="most recent stored listings to score (default: %(default)s, 0 = none)",
    )
    calibrate.add_argument(
        "--show",
        type=_bounded_int(0, 200),
        default=20,
        help="best-matching stored listings to print (default: %(default)s)",
    )
    calibrate.set_defaults(handler=_cmd_calibrate)

    report = subparsers.add_parser(
        "report",
        help="Write a local HTML page with the stored listings, their photos and scores, "
        "and open it in the browser (no Browse API call, no model needed).",
    )
    report.add_argument(
        "-o",
        "--output",
        type=Path,
        help="page to write (default: report.html next to the database)",
    )
    report.add_argument(
        "-n",
        "--listings",
        type=_bounded_int(1, 100000),
        default=2000,
        help="most recent stored listings to include (default: %(default)s)",
    )
    report.add_argument(
        "--match",
        type=float,
        help="mark listings at this match threshold (default: [vision] if the filter is on)",
    )
    report.add_argument(
        "--colour",
        type=float,
        help="mark listings at this colour threshold (default: [vision] if the filter is on)",
    )
    report.add_argument(
        "--web",
        type=float,
        help="mark listings at this web threshold (default: [vision] if the filter is on)",
    )
    report.add_argument("--no-open", action="store_true", help="do not open the browser")
    report.set_defaults(handler=_cmd_report)

    digest = subparsers.add_parser(
        "digest",
        help="Send the near misses not in a digest yet to Telegram now "
        "(the cycle also sends them once a day, see vision.digest_hour).",
    )
    digest.add_argument(
        "--dry-run", action="store_true", help="print the message instead of sending it"
    )
    digest.set_defaults(handler=_cmd_digest)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tolerate_unencodable_output()
    configure_logging(verbose=args.verbose)
    handler: Handler = args.handler
    try:
        return handler(args)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 1
    except KeyboardInterrupt:
        log.info("Interrupted")
        return 130
    except Exception:
        # Logged, not printed by the interpreter, so that secrets are redacted.
        log.exception("Unexpected error")
        return 1


def tolerate_unencodable_output() -> None:
    """Escape characters the output encoding cannot represent instead of crashing.

    Redirected output on Windows (a log file, a scheduled task) uses the ANSI
    code page, while eBay titles often contain emoji or other scripts.
    """
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="backslashreplace")


def _load(args: argparse.Namespace) -> tuple[AppConfig, Secrets]:
    config = load_config(args.config)
    secrets = load_secrets(_env_file(args))
    register_secrets(*secrets.redaction_values())
    return config, secrets


def _env_file(args: argparse.Namespace) -> Path:
    return args.env_file or default_env_file(args.config)


def _require_production(secrets: Secrets) -> None:
    """Keep sandbox test listings out of the database used in production.

    They would also mark the searches as seeded, so the first production run
    would notify every listing already online.
    """
    if secrets.ebay_environment != "production":
        raise ConfigError(
            f"EBAY_ENVIRONMENT is {secrets.ebay_environment}: run-once and watch need a "
            "production keyset (check-config --live and search work in the sandbox)"
        )


def _cmd_run_once(args: argparse.Namespace) -> int:
    config, secrets = _load(args)
    _require_production(secrets)
    with open_app(config, secrets) as app:
        try:
            app.pipeline.run_cycle()
        except (CycleError, EbayAuthError) as exc:
            log.error("Cycle failed: %s", exc)
            return 1
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    config, secrets = _load(args)
    _require_production(secrets)
    interval_s = config.runtime.poll_interval_minutes * 60
    signal.signal(signal.SIGTERM, _exit_on_sigterm)
    with open_app(config, secrets) as app:
        log.info("Watching every %d minutes", config.runtime.poll_interval_minutes)
        while True:
            started = time.monotonic()
            try:
                app.pipeline.run_cycle()
            except (CycleError, EbayAuthError, EbayApiError, NotificationError) as exc:
                log.error("Cycle failed: %s", exc)
            except Exception:
                log.exception("Cycle failed with an unexpected error")
            time.sleep(max(interval_s - (time.monotonic() - started), 0.0))


def _exit_on_sigterm(signum: int, frame: FrameType | None) -> None:
    log.info("Received SIGTERM, stopping")
    sys.exit(0)


def _cmd_check_config(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    search = config.search
    budget = compute_budget(config)
    ok = budget.within_budget

    print(f"Configuration: {args.config.resolve()}")
    print(f"Marketplaces ({len(search.marketplaces)}): {', '.join(search.marketplaces)}")
    print(
        f"Delivery country: {search.delivery_country}, "
        f"postal code: {search.buyer_postal_code or 'not set'}"
    )
    print(f"Buying options: {', '.join(search.buying_options)}")
    print(f"Queries ({len(search.queries)}), length out of {MAX_QUERY_LENGTH} characters:")
    for query in search.queries:
        print(f"  {len(query):3d}  {query}")
    print(
        f"Poll interval: {config.runtime.poll_interval_minutes} min, "
        f"{budget.cycles_per_day} cycles per day"
    )
    print(
        f"Browse API calls per day, worst case: {budget.search_calls_per_day} search + "
        f"{budget.detail_calls_per_day} item details = {budget.total_calls_per_day}"
    )
    allowed = (
        f"{budget.allowed_calls_per_day} allowed "
        f"({budget.share:.0%} of the {budget.daily_limit} daily limit)"
    )
    if ok:
        print(f"Budget: OK, {budget.total_calls_per_day} of {allowed}")
    else:
        minimum = budget.minimum_poll_interval_minutes
        hint = f"poll every {minimum} minutes or more" if minimum else "use fewer searches"
        print(
            f"Budget: EXCEEDED, {budget.total_calls_per_day} of {allowed}. "
            f"Use fewer queries or marketplaces, or {hint}."
        )
    _print_database_stats(config)
    ok = _print_vision_status(config) and ok

    env_file = _env_file(args)
    try:
        secrets = load_secrets(env_file)
    except ConfigError as exc:
        print(f"Secrets: {exc}")
        return 1
    register_secrets(*secrets.redaction_values())
    print(f"Secrets: all present ({env_file} or environment)")
    print(f"eBay environment: {_describe_environment(secrets)}")
    if args.live:
        ok = _live_checks(config, secrets) and ok
    return 0 if ok else 1


def _describe_environment(secrets: Secrets) -> str:
    if secrets.ebay_environment == "sandbox":
        return "sandbox (test listings only, not real eBay)"
    return secrets.ebay_environment


def _print_vision_status(config: AppConfig) -> bool:
    vision = config.vision
    if not vision.enabled:
        print("Vision: disabled")
        return True
    positives = len(reference_files(vision.positive_dir))
    negatives = len(reference_files(vision.negative_dir))
    mode = (
        f"filtering (match >= {vision.match_threshold:.3f}, "
        f"colour >= {vision.colour_threshold:+.3f}, web >= {vision.web_threshold:+.3f})"
        if vision.filter
        else "shadow mode (scores shown, nothing filtered)"
    )
    print(f"Vision: {vision.model}/{vision.pretrained}, {mode}")
    print(f"  Reference images: {positives} positive, {negatives} negative")
    if vision.digest_hour >= 0:
        print(
            f"  Near-miss digest: daily after {vision.digest_hour}:00 ({config.telegram.timezone})"
        )
    else:
        print("  Near-miss digest: off")
    ok = True
    if not vision_available():
        print("  FAILED: the vision extra is not installed (uv sync --extra vision)")
        ok = False
    if not positives:
        print(f"  FAILED: no images in {vision.positive_dir}")
        ok = False
    return ok


def _print_database_stats(config: AppConfig) -> None:
    path = config.runtime.database_path
    if not path.exists():
        print(f"Database: {path} (not created yet)")
        return
    with Store.open(path) as store:
        calls = store.api_calls_since(utc_now() - timedelta(days=1))
        counts = store.count_by_status()
    listings = ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
    print(f"Database: {path}")
    print(f"  Browse API calls in the last 24 hours: {calls}")
    print(f"  Listings: {listings or 'none'}")


def _live_checks(config: AppConfig, secrets: Secrets) -> bool:
    ok = True
    with open_ebay(config, secrets) as ebay:
        try:
            ebay.tokens.get()
        except EbayAuthError as exc:
            ok = False
            print(f"eBay: FAILED, {exc}")
        else:
            print("eBay: OK, application token obtained")
    with open_notifier(config, secrets) as notifier:
        try:
            description = notifier.check()
        except NotificationError as exc:
            ok = False
            print(f"Telegram: FAILED, {exc}")
        else:
            print(f"Telegram: OK, {description}")
    return ok


def _cmd_search(args: argparse.Namespace) -> int:
    config, secrets = _load(args)
    query = " ".join(args.query.split())
    marketplaces: list[str] = args.marketplace or config.search.marketplaces[:1]
    error = _input_error(query, marketplaces)
    if error:
        print(error)
        return 1
    if args.save_json and len(marketplaces) > 1:
        print("--save-json works with one marketplace at a time")
        return 1
    tz = ZoneInfo(config.telegram.timezone)
    options = config.search.buying_options
    rules = RuleEngine(config.rules, config.price)
    if secrets.ebay_environment != "production":
        print(f"eBay environment: {_describe_environment(secrets)}")
    with open_ebay(config, secrets) as ebay:
        for marketplace in marketplaces:
            try:
                data = ebay.browse.search_raw(
                    query, marketplace, limit=args.limit, buying_options=options
                )
            except (EbayApiError, EbayAuthError) as exc:
                print(f"{marketplace}: FAILED, {exc}")
                return 1
            if args.save_json:
                args.save_json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                print(f"Saved the sanitized response to {args.save_json}")
            _print_page(SearchPage.model_validate(data), marketplace, query, tz, rules)
        print(f"Browse API calls used: {ebay.browse.calls}")
    return 0


def _cmd_notify_test(args: argparse.Namespace) -> int:
    config, secrets = _load(args)
    query = " ".join((args.query or config.search.queries[0]).split())
    marketplace: str = args.marketplace or config.search.marketplaces[0]
    error = _input_error(query, [marketplace])
    if error:
        print(error)
        return 1
    if secrets.ebay_environment != "production":
        print(f"eBay environment: {_describe_environment(secrets)}")
    with open_ebay(config, secrets) as ebay:
        try:
            page = ebay.browse.search(
                query, marketplace, limit=1, buying_options=config.search.buying_options
            )
            if not page.item_summaries:
                print(f"{marketplace}: no listings for {query!r}, try a broader query")
                return 1
            listing = Listing.from_summary(
                page.item_summaries[0], marketplace=marketplace, query=query
            )
            details = ebay.browse.get_item(listing.item_id, marketplace)
        except (EbayApiError, EbayAuthError) as exc:
            print(f"{marketplace}: FAILED, {exc}")
            return 1
    if details is not None:
        listing = listing.with_details(details)
    # Printed, not enforced: the point is to see a notification, even of a dropped listing.
    result = RuleEngine(config.rules, config.price).evaluate(listing)
    listing = replace(
        listing.with_verdict(result.verdict, result.reasons), title=f"[TEST] {listing.title}"
    )
    with open_vision(config) as scorer:
        if scorer is not None:
            try:
                listing = listing.with_vision(scorer.score(listing.image_urls))
            except VisionError as exc:
                listing = listing.with_vision(None, str(exc))
    with open_notifier(config, secrets) as notifier:
        try:
            notifier.notify_listing(listing)
        except NotificationError as exc:
            print(f"Telegram: FAILED, {exc}")
            return 1
    print(f"Sent to Telegram: {listing.title}")
    print(f"  {listing.url}")
    print(
        f"  {len(listing.image_urls)} photo(s), "
        f"details {'fetched' if listing.details_fetched else 'not found'}, "
        f"verdict {result.verdict.upper()}"
        + (f": {'; '.join(result.reasons)}" if result.reasons else "")
    )
    if listing.vision is not None:
        print(f"  photos: {listing.vision.describe()}")
    elif listing.vision_error:
        print(f"  photos not scored: {listing.vision_error}")
    return 0


def _cmd_calibrate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if not vision_available():
        print("The vision extra is not installed: uv sync --extra vision")
        return 1
    # Calibration also works before vision is enabled in the configuration.
    config = config.model_copy(
        update={"vision": config.vision.model_copy(update={"enabled": True})}
    )
    with open_vision(config) as scorer:
        assert scorer is not None
        try:
            refs = scorer.references()
        except VisionError as exc:
            print(f"Reference images: {exc}")
            return 1
        if len(refs.positives) < 2:
            print("At least two positive reference images are needed to calibrate.")
            return 1
        results = score_references(refs, scorer.model_id)
        match_threshold, colour_threshold, web_threshold = suggest_thresholds(results)
        suggested = config.vision.model_copy(
            update={
                "filter": True,
                "match_threshold": match_threshold,
                "colour_threshold": colour_threshold,
                "web_threshold": web_threshold,
            }
        )
        print(f"Model: {scorer.model_id}")
        print("Reference images, each scored against all the others:")
        print("        match  negative  colour     web   at the suggested thresholds")
        notified_negatives = 0
        for result in results:
            score = result.score
            reason = below_threshold_reason(score, suggested)
            if result.positive:
                outcome = "kept" if reason is None else f"LOST: {reason}"
            else:
                outcome = "NOTIFIED" if reason is None else f"filtered: {reason}"
                notified_negatives += reason is None
            label = "pos" if result.positive else "neg"
            print(
                f"  {label}  {score.match:6.3f}  {score.negative:7.3f}  {score.colour:+7.3f}"
                f"  {score.web:+6.3f}   {result.path.name}: {outcome}"
            )
        print(
            f"The suggested thresholds keep all {len(refs.positives)} positives; "
            f"{notified_negatives} of {len(refs.negatives)} negatives would still be notified."
        )
        print(
            f"The match threshold sits {MATCH_MARGIN} below the lowest positive: listing "
            "photos are usually worse than the references, so check stored listings too."
        )
        print("To filter, set in [vision]:")
        print("  filter = true")
        print(f"  match_threshold = {match_threshold}")
        print(f"  colour_threshold = {colour_threshold}")
        print(f"  web_threshold = {web_threshold}")
        if args.listings:
            _calibrate_listings(config, scorer, suggested, args.listings, args.show)
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    path = config.runtime.database_path
    if not path.exists():
        print(f"No database yet ({path}): run run-once to collect listings first.")
        return 1
    vision = config.vision
    overrides = {
        name: value
        for name, value in (
            ("match_threshold", args.match),
            ("colour_threshold", args.colour),
            ("web_threshold", args.web),
        )
        if value is not None
    }
    thresholds: VisionConfig | None = None
    if overrides or vision.filter:
        thresholds = vision.model_copy(update={"filter": True, **overrides})
    with Store.open(path) as store:
        rows = store.recent_with_photos(args.listings)
    output: Path = args.output or path.parent / "report.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_report(rows, generated=datetime.now(), thresholds=thresholds), encoding="utf-8"
    )
    print(f"Wrote {len(rows)} listings to {output.resolve()}")
    if not args.no_open:
        webbrowser.open(output.resolve().as_uri())
    return 0


def _cmd_digest(args: argparse.Namespace) -> int:
    config, secrets = _load(args)
    path = config.runtime.database_path
    if not path.exists():
        print(f"No database yet ({path}).")
        return 1
    with Store.open(path) as store:
        if args.dry_run:
            listings = store.near_misses()
            print(format_digest(listings) if listings else "No near misses.")
            return 0
        with open_notifier(config, secrets) as notifier:
            return 0 if send_digest(store, notifier, utc_now()) else 1


def _calibrate_listings(
    config: AppConfig, scorer: VisionScorer, suggested: VisionConfig, limit: int, show: int
) -> None:
    """Score stored listings, save the scores and show which would be notified."""
    path = config.runtime.database_path
    if not path.exists():
        print(f"No database yet ({path}): run run-once to collect listings first.")
        return
    scored: list[tuple[Listing, ListingStatus]] = []
    failed = 0
    with Store.open(path) as store:
        rows = store.recent_with_photos(limit)
        print(f"Scoring the {len(rows)} most recent stored listings with photos...")
        for done, (listing, status) in enumerate(rows, start=1):
            try:
                listing = listing.with_vision(scorer.score(listing.image_urls))
            except VisionError as exc:
                failed += 1
                listing = listing.with_vision(None, str(exc))
            else:
                scored.append((listing, status))
            store.update_vision(listing)
            if done % 25 == 0:
                log.info("Scored %d of %d listings", done, len(rows))
    passing = sum(
        1
        for listing, _ in scored
        if listing.vision and below_threshold_reason(listing.vision, suggested) is None
    )
    print(
        f"Stored listings: {len(scored)} scored, {failed} failed; "
        f"{passing} would be notified at the suggested thresholds."
    )
    ranked = sorted(
        scored, key=lambda row: row[0].vision.match if row[0].vision else 0.0, reverse=True
    )
    if show and ranked:
        print(f"The {min(show, len(ranked))} closest to the positives:")
    for listing, status in ranked[:show]:
        assert listing.vision is not None
        reason = below_threshold_reason(listing.vision, suggested)
        print(
            f"  {listing.vision.match:.3f} {listing.vision.colour:+.3f} {listing.vision.web:+.3f} "
            f"{'PASS' if reason is None else '    '} [{status}] {listing.title}"
        )
        print(f"  {'':20}{listing.url}")


def _input_error(query: str, marketplaces: Sequence[str]) -> str | None:
    """Why ``query`` cannot be searched on ``marketplaces``, or None."""
    try:
        validate_query(query)
    except ValueError as exc:
        return f"Invalid query: {exc}"
    unknown = sorted(set(marketplaces) - SUPPORTED_MARKETPLACES)
    if unknown:
        return f"Unsupported marketplace(s): {', '.join(unknown)}"
    return None


def _print_page(
    page: SearchPage, marketplace: str, query: str, tz: ZoneInfo, rules: RuleEngine
) -> None:
    for warning in page.warnings:
        print(f"{marketplace}: warning {warning.describe()}")
    print(f"{marketplace}: {page.total} matching listings, showing {len(page.item_summaries)}")
    for item in page.item_summaries:
        listing = Listing.from_summary(item, marketplace=marketplace, query=query)
        listed = (
            listing.origin_date.astimezone(tz).strftime("%Y-%m-%d %H:%M")
            if listing.origin_date
            else "?"
        )
        total = listing.total
        total_text = str(total) if total is not None else "n/a"
        options = "/".join(listing.buying_options)
        print(f"  {listed}  {total_text:>14}  {options:<28}  {listing.title}")
        print(f"  {'':16}  {listing.url}")
        # The rules on search data only: condition notes need getItem.
        result = rules.evaluate(listing)
        if result.verdict is not Verdict.PASS:
            print(f"  {'':16}  {result.verdict.upper()}: {'; '.join(result.reasons)}")


def _bounded_int(low: int, high: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        value = int(text)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return value

    return parse
