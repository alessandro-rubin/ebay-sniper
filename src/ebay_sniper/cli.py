"""Command line entry point.

- ``run-once``: one poll cycle, for an external scheduler (cron, systemd timer,
  Windows Task Scheduler). Exit code 0 on success, 1 on failure.
- ``watch``: poll forever at the configured interval.
- ``check-config``: validate configuration and secrets and print the daily
  Browse API budget; ``--live`` also verifies the credentials.
- ``search``: run one query and print the results, to try out query syntax.

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
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from types import FrameType
from zoneinfo import ZoneInfo

from ebay_sniper import __version__
from ebay_sniper.app import open_app, open_ebay, open_notifier
from ebay_sniper.config import (
    DEFAULT_CONFIG_PATH,
    MAX_QUERY_LENGTH,
    MAX_RESULTS_PER_PAGE,
    SUPPORTED_MARKETPLACES,
    AppConfig,
    ConfigError,
    Secrets,
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
from ebay_sniper.pipeline import CycleError, utc_now
from ebay_sniper.rules import RuleEngine
from ebay_sniper.store import Store

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


def _cmd_run_once(args: argparse.Namespace) -> int:
    config, secrets = _load(args)
    with open_app(config, secrets) as app:
        try:
            app.pipeline.run_cycle()
        except (CycleError, EbayAuthError) as exc:
            log.error("Cycle failed: %s", exc)
            return 1
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    config, secrets = _load(args)
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

    env_file = _env_file(args)
    try:
        secrets = load_secrets(env_file)
    except ConfigError as exc:
        print(f"Secrets: {exc}")
        return 1
    register_secrets(*secrets.redaction_values())
    print(f"Secrets: all present ({env_file} or environment)")
    if args.live:
        ok = _live_checks(config, secrets) and ok
    return 0 if ok else 1


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
    try:
        validate_query(query)
    except ValueError as exc:
        print(f"Invalid query: {exc}")
        return 1
    marketplaces: list[str] = args.marketplace or config.search.marketplaces[:1]
    unknown = sorted(set(marketplaces) - SUPPORTED_MARKETPLACES)
    if unknown:
        print(f"Unsupported marketplace(s): {', '.join(unknown)}")
        return 1
    if args.save_json and len(marketplaces) > 1:
        print("--save-json works with one marketplace at a time")
        return 1
    tz = ZoneInfo(config.telegram.timezone)
    options = config.search.buying_options
    rules = RuleEngine(config.rules, config.price)
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
