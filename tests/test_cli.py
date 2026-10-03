from __future__ import annotations

import io
import json
import logging
import re
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from ebay_sniper.cli import build_parser, main
from ebay_sniper.logsetup import RedactingFormatter, configure_logging, register_secrets
from ebay_sniper.models import Listing, VisionScore
from ebay_sniper.pipeline import utc_now
from ebay_sniper.store import ListingStatus, Store
from ebay_sniper.vision import ReferenceImage, References, VisionError, vision_available
from factories import (
    CONFIG_TOML,
    EBAY_CLIENT_SECRET,
    ITEM_URL,
    SANDBOX_ENV_FILE_CONTENT,
    SANDBOX_SEARCH_URL,
    SANDBOX_TOKEN_URL,
    SEARCH_URL,
    TELEGRAM_API,
    TELEGRAM_BOT_TOKEN,
    TOKEN_URL,
    load_fixture,
    make_item,
    make_page,
    make_page_data,
)

Q1 = "futura (spider, ragno)"
Q2 = "(spider, spiderweb) watch"
OK = {"ok": True, "result": {"message_id": 1}}


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The clients built by the CLI sleep for real between retries."""
    for module in (
        "ebay_sniper.ebay.auth",
        "ebay_sniper.ebay.browse",
        "ebay_sniper.notify.telegram",
    ):
        monkeypatch.setattr(f"{module}.backoff_delay", lambda attempt, *, base, cap=60.0: 0.0)


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)


def search_responder(
    pages: dict[tuple[str, str], dict[str, Any]],
) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        key = (request.url.params["q"], request.headers["X-EBAY-C-MARKETPLACE-ID"])
        return httpx.Response(200, json=pages.get(key, load_fixture("search_empty.json")))

    return respond


def telegram_route(respx_mock: respx.MockRouter) -> respx.Route:
    return respx_mock.post(url__regex=rf"^{re.escape(TELEGRAM_API)}/\w+$").respond(json=OK)


def telegram_methods(route: respx.Route) -> list[str]:
    return [call.request.url.path.rsplit("/", 1)[-1] for call in route.calls]


def run(config_path: Path, *args: str) -> int:
    return main(["--config", str(config_path), *args])


def test_version_flag_exits_cleanly() -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["--version"])
    assert exc_info.value.code == 0


def test_command_is_required() -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args([])
    assert exc_info.value.code == 2


def test_check_config(config_path: Path, env_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert run(config_path, "check-config") == 0
    out = capsys.readouterr().out
    assert "Marketplaces (2): EBAY_IT, EBAY_DE" in out
    assert "   22  futura (spider, ragno)" in out
    assert "288 search + 720 item details = 1008" in out
    assert "Budget: OK, 1008 of 3000 allowed (60% of the 5000 daily limit)" in out
    assert "test.sqlite3 (not created yet)" in out
    assert "Secrets: all present" in out


def test_check_config_over_budget(
    tmp_path: Path, env_path: Path, capsys: pytest.CaptureFixture
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        CONFIG_TOML.replace("poll_interval_minutes = 20", "poll_interval_minutes = 1").replace(
            "api_budget_share = 0.6", "api_budget_share = 0.1"
        ),
        encoding="utf-8",
    )
    assert run(config_path, "check-config") == 1
    assert "Budget: EXCEEDED" in capsys.readouterr().out


def test_check_config_without_secrets(config_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert run(config_path, "check-config") == 1
    out = capsys.readouterr().out
    assert "Secrets: missing or invalid secrets" in out
    assert "EBAY_CLIENT_ID" in out


def test_check_config_live(
    config_path: Path,
    env_path: Path,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture,
) -> None:
    respx_mock.post(TOKEN_URL).respond(json=load_fixture("token.json"))
    respx_mock.post(f"{TELEGRAM_API}/getMe").respond(
        json={"ok": True, "result": {"username": "spider_watch_bot"}}
    )
    respx_mock.post(f"{TELEGRAM_API}/getChat").respond(
        json={"ok": True, "result": {"type": "private", "first_name": "Ale"}}
    )
    assert run(config_path, "check-config", "--live") == 0
    out = capsys.readouterr().out
    assert "eBay: OK" in out
    assert "Telegram: OK, bot @spider_watch_bot" in out


def test_check_config_live_failures(
    config_path: Path,
    env_path: Path,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture,
) -> None:
    respx_mock.post(TOKEN_URL).respond(401, json={"error": "invalid_client"})
    respx_mock.post(f"{TELEGRAM_API}/getMe").respond(
        404, json={"ok": False, "error_code": 404, "description": "Not Found"}
    )
    assert run(config_path, "check-config", "--live") == 1
    out = capsys.readouterr().out
    assert "eBay: FAILED" in out
    assert "Telegram: FAILED" in out
    assert TELEGRAM_BOT_TOKEN not in out


@pytest.fixture
def sandbox_env_path(config_path: Path) -> Path:
    path = config_path.parent / ".env"
    path.write_text(SANDBOX_ENV_FILE_CONTENT, encoding="utf-8")
    return path


def test_check_config_live_in_the_sandbox(
    config_path: Path,
    sandbox_env_path: Path,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture,
) -> None:
    respx_mock.post(SANDBOX_TOKEN_URL).respond(json=load_fixture("token.json"))
    respx_mock.post(f"{TELEGRAM_API}/getMe").respond(
        json={"ok": True, "result": {"username": "spider_watch_bot"}}
    )
    respx_mock.post(f"{TELEGRAM_API}/getChat").respond(
        json={"ok": True, "result": {"type": "private", "first_name": "Ale"}}
    )
    assert run(config_path, "check-config", "--live") == 0
    out = capsys.readouterr().out
    assert "eBay environment: sandbox" in out
    assert "eBay: OK" in out


def test_search_command_in_the_sandbox(
    config_path: Path,
    sandbox_env_path: Path,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture,
) -> None:
    respx_mock.post(SANDBOX_TOKEN_URL).respond(json=load_fixture("token.json"))
    respx_mock.get(SANDBOX_SEARCH_URL).respond(json=load_fixture("search_empty.json"))
    assert run(config_path, "search", "spider") == 0
    out = capsys.readouterr().out
    assert "eBay environment: sandbox" in out
    assert "EBAY_IT: 0 matching listings" in out


@pytest.mark.parametrize("command", ["run-once", "watch"])
def test_pipeline_commands_refuse_the_sandbox(
    config_path: Path,
    sandbox_env_path: Path,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture,
    command: str,
) -> None:
    assert run(config_path, command) == 1
    assert "need a production keyset" in capsys.readouterr().err
    assert not respx_mock.calls
    assert not (config_path.parent / "data").exists()


def test_invalid_configuration_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG_TOML.replace('"EBAY_DE"', '"EBAY_UK"'), encoding="utf-8")
    assert run(config_path, "check-config") == 1
    assert "Configuration error" in capsys.readouterr().err


def test_log_file_replaces_stderr(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG_TOML.replace('"EBAY_DE"', '"EBAY_UK"'), encoding="utf-8")
    log_file = tmp_path / "logs" / "ebay-sniper.log"
    assert main(["--config", str(config_path), "--log-file", str(log_file), "check-config"]) == 1
    assert "Configuration error" in log_file.read_text(encoding="utf-8")
    # Only the file: cron would mail every line written to stderr.
    assert capsys.readouterr().err == ""


def test_log_file_is_utf8_and_redacted(tmp_path: Path) -> None:
    log_file = tmp_path / "ebay-sniper.log"
    title = "Montre \U0001f577 araign\u00e9e"
    register_secrets(TELEGRAM_BOT_TOKEN)
    configure_logging(log_file=log_file)
    logging.getLogger("ebay_sniper.test").error("Failed %s for %r", TELEGRAM_BOT_TOKEN, title)
    text = log_file.read_text(encoding="utf-8")
    # Unlike redirected stderr on Windows, nothing is escaped.
    assert f"Failed [REDACTED] for '{title}'" in text
    assert TELEGRAM_BOT_TOKEN not in text


def test_run_once_end_to_end(
    config_path: Path,
    env_path: Path,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture,
) -> None:
    token = respx_mock.post(TOKEN_URL).respond(json=load_fixture("token.json"))
    pages = {
        (Q1, "EBAY_IT"): load_fixture("search_ebay_it.json"),
        (Q1, "EBAY_DE"): load_fixture("search_ebay_de.json"),
    }
    search = respx_mock.get(SEARCH_URL).mock(side_effect=search_responder(pages))
    telegram = telegram_route(respx_mock)

    # First run: everything already online is recorded, one service message.
    assert run(config_path, "run-once") == 0
    assert search.call_count == 4
    assert telegram_methods(telegram) == ["sendMessage"]
    first = json.loads(telegram.calls.last.request.content)["text"]
    assert "Started watching 4 new search(es). 4 listing(s) already online" in first
    assert (config_path.parent / "data" / "test.sqlite3").exists()

    # Second run: three new listings appear in another search. The rules drop
    # one on its title; getItem runs for the other two, and the condition
    # notes drop one of them and only flag the other (negated keyword).
    pages[(Q2, "EBAY_IT")] = make_page_data(
        make_item("150000000005", title="Spider web watch"),
        make_item("150000000006", title="Spider watch, cracked crystal"),
        make_item("150000000007", title="Spider web quartz watch"),
    )
    notes = {
        "150000000005": "Perfetto, nessun vetro rotto.",
        "150000000007": "Vetro rotto, cassa ok.",
    }

    def item_responder(request: httpx.Request) -> httpx.Response:
        # The path is sent encoded (v1%7C...%7C0); httpx exposes it decoded.
        assert b"%7C" in request.url.raw_path
        listing_id = request.url.path.split("|")[1]
        item = make_item(listing_id, conditionDescription=notes[listing_id])
        return httpx.Response(200, json=item)

    get_item = respx_mock.get(url__startswith=f"{ITEM_URL}/").mock(side_effect=item_responder)
    assert run(config_path, "run-once") == 0
    assert get_item.call_count == 2
    assert telegram_methods(telegram) == ["sendMessage", "sendPhoto"]
    photo = json.loads(telegram.calls.last.request.content)
    assert photo["reply_markup"]["inline_keyboard"][0][0]["url"] == (
        "https://www.ebay.it/itm/150000000005"
    )
    assert "<b>Check:</b> negated 'vetro rotto' in condition notes" in photo["caption"]

    # Third run: nothing new, nothing sent.
    assert run(config_path, "run-once") == 0
    assert telegram.call_count == 2
    assert token.call_count == 3  # one process per run: the token is not stored on disk
    err = capsys.readouterr().err
    assert "Cycle done" in err
    assert "Dropped 150000000006" in err
    assert "Dropped 150000000007" in err
    assert TELEGRAM_BOT_TOKEN not in err

    # check-config reports the real usage recorded by the runs.
    assert run(config_path, "check-config") == 0
    out = capsys.readouterr().out
    assert "Browse API calls in the last 24 hours: 14" in out
    assert "Listings: 2 dropped, 1 notified, 4 seeded" in out


def test_run_once_fails_on_rejected_credentials(
    config_path: Path,
    env_path: Path,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture,
) -> None:
    respx_mock.post(TOKEN_URL).respond(
        401, json={"error": "invalid_client", "error_description": "client authentication failed"}
    )
    assert run(config_path, "run-once") == 1
    err = capsys.readouterr().err
    assert "invalid_client" in err
    assert EBAY_CLIENT_SECRET not in err
    # No Browse request was sent, so none is counted against the budget.
    assert run(config_path, "check-config") == 0
    assert "Browse API calls in the last 24 hours: 0" in capsys.readouterr().out


def test_notification_errors_do_not_leak_the_token(
    config_path: Path,
    env_path: Path,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture,
) -> None:
    respx_mock.post(TOKEN_URL).respond(json=load_fixture("token.json"))
    respx_mock.get(SEARCH_URL).mock(side_effect=search_responder({}))
    respx_mock.post(url__startswith=TELEGRAM_API).mock(
        side_effect=httpx.ConnectError(f"failed to reach {TELEGRAM_API}/sendMessage")
    )
    assert run(config_path, "run-once") == 0
    err = capsys.readouterr().err
    assert "Could not send a service message" in err
    assert TELEGRAM_BOT_TOKEN not in err


def test_redacting_formatter_covers_tracebacks() -> None:
    register_secrets(TELEGRAM_BOT_TOKEN)
    try:
        raise RuntimeError(f"GET {TELEGRAM_API}/getMe failed")
    except RuntimeError:
        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "token %s", (TELEGRAM_BOT_TOKEN,), sys.exc_info()
        )
    text = RedactingFormatter().format(record)
    assert TELEGRAM_BOT_TOKEN not in text
    assert text.count("[REDACTED]") == 2


def test_search_command(
    config_path: Path,
    env_path: Path,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture,
) -> None:
    respx_mock.post(TOKEN_URL).respond(json=load_fixture("token.json"))
    route = respx_mock.get(SEARCH_URL).respond(json=load_fixture("search_ebay_it.json"))
    saved = config_path.parent / "saved.json"
    code = run(
        config_path, "search", "futura  (spider, ragno)", "-n", "5", "--save-json", str(saved)
    )
    assert code == 0
    assert route.calls.last.request.url.params["limit"] == "5"
    out = capsys.readouterr().out
    assert "EBAY_IT: 3 matching listings, showing 3" in out
    assert "26.40 EUR  AUCTION" in out
    assert "https://www.ebay.it/itm/120000000002" in out
    assert "FLAG: condition 7000" in out
    assert "Browse API calls used: 1" in out
    text = saved.read_text(encoding="utf-8")
    assert "example_seller" not in text
    assert "Futura Quartz" in text


def test_notify_test_sends_the_newest_result(
    config_path: Path,
    env_path: Path,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture,
) -> None:
    respx_mock.post(TOKEN_URL).respond(json=load_fixture("token.json"))
    search = respx_mock.get(SEARCH_URL).respond(json=load_fixture("search_ebay_it.json"))
    respx_mock.get(url__startswith=f"{ITEM_URL}/").respond(
        json=load_fixture("item_110000000001.json")
    )
    telegram = telegram_route(respx_mock)
    assert run(config_path, "notify-test") == 0
    request = search.calls.last.request
    assert request.url.params["q"] == Q1
    assert request.url.params["limit"] == "1"
    assert request.headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_IT"
    assert telegram_methods(telegram) == ["sendMediaGroup", "sendMessage"]
    text = json.loads(telegram.calls.last.request.content)["text"]
    assert text.startswith("<b>[TEST] Orologio Futura Quartz")
    out = capsys.readouterr().out
    assert "4 photo(s), details fetched, verdict PASS" in out
    assert not (config_path.parent / "data").exists()


def test_notify_test_without_results(
    config_path: Path,
    env_path: Path,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture,
) -> None:
    respx_mock.post(TOKEN_URL).respond(json=load_fixture("token.json"))
    route = respx_mock.get(SEARCH_URL).respond(json=load_fixture("search_empty.json"))
    assert run(config_path, "notify-test", "spider", "-m", "EBAY_DE") == 1
    assert route.calls.last.request.headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_DE"
    assert "EBAY_DE: no listings for 'spider'" in capsys.readouterr().out


def test_search_command_validates_its_input(
    config_path: Path, env_path: Path, capsys: pytest.CaptureFixture
) -> None:
    assert run(config_path, "search", "spider*") == 1
    assert run(config_path, "search", "spider", "-m", "EBAY_UK") == 1
    assert (
        run(
            config_path,
            "search",
            "spider",
            "-m",
            "EBAY_IT",
            "-m",
            "EBAY_DE",
            "--save-json",
            "x.json",
        )
        == 1
    )
    out = capsys.readouterr().out
    assert "Invalid query" in out
    assert "Unsupported marketplace(s): EBAY_UK" in out
    assert "one marketplace at a time" in out


def test_watch_runs_cycles_until_interrupted(
    config_path: Path,
    env_path: Path,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    respx_mock.post(TOKEN_URL).respond(json=load_fixture("token.json"))
    search = respx_mock.get(SEARCH_URL).mock(side_effect=search_responder({}))
    telegram_route(respx_mock)
    monkeypatch.setattr("ebay_sniper.cli.signal.signal", lambda *args: None)
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr("ebay_sniper.cli.time.sleep", fake_sleep)
    assert run(config_path, "watch") == 130
    assert search.call_count == 8
    assert 1190 < sleeps[0] <= 1200


def test_redirected_output_in_a_legacy_encoding_does_not_crash(
    tmp_path: Path, env_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Windows uses the ANSI code page for redirected output (scheduled tasks,
    # log files), and eBay titles often contain emoji or other scripts.
    spider = "\U0001f577"
    stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", write_through=True)
    stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", write_through=True)
    monkeypatch.setattr("sys.stdout", stdout)
    monkeypatch.setattr("sys.stderr", stderr)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        CONFIG_TOML.replace('"(spider, spiderweb) watch",', f'"spider web watch {spider}",'),
        encoding="utf-8",
    )
    assert run(config_path, "check-config") == 0
    logging.getLogger("ebay_sniper.test").warning("Dropped 1 %r", f"Montre {spider} araign\u00e9e")
    out = stdout.buffer.getvalue().decode("cp1252")
    err = stderr.buffer.getvalue().decode("cp1252")
    # The emoji is escaped, characters of the code page are kept.
    assert r"spider web watch \U0001f577" in out
    assert r"Montre \U0001f577 araign" + "\u00e9e" in err
    assert "Logging error" not in err


def test_check_config_reports_the_vision_setup(
    tmp_path: Path, env_path: Path, capsys: pytest.CaptureFixture
) -> None:
    (tmp_path / "refs").mkdir()
    (tmp_path / "refs" / "a.jpg").write_bytes(b"x")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        CONFIG_TOML + '\n[vision]\nenabled = true\npositive_dir = "refs"\nnegative_dir = "none"\n',
        encoding="utf-8",
    )
    code = run(config_path, "check-config")
    out = capsys.readouterr().out
    assert "Vision: ViT-B-16-SigLIP/webli, shadow mode" in out
    assert "Reference images: 1 positive, 0 negative" in out
    assert code == (0 if vision_available() else 1)


class FakeScorer:
    """Toy vectors: listing photos named "good" match the positives."""

    model_id = "fake/model"

    def references(self) -> References:
        return References(
            positives=(
                ReferenceImage(Path("p1.jpg"), [1.0, 0.0, 0.1]),
                ReferenceImage(Path("p2.jpg"), [0.98, 0.0, 0.2]),
            ),
            negatives=(ReferenceImage(Path("gold.jpg"), [0.95, 0.0, -0.3]),),
            colour_axis=[0.0, 0.0, 1.0],
            web_axis=[1.0, 0.0, 0.0],
        )

    def score(self, image_urls: Sequence[str]) -> VisionScore:
        if not image_urls:
            raise VisionError("no photos")
        match = 0.99 if "good" in image_urls[0] else 0.2
        return VisionScore(
            match=match,
            negative=0.1,
            colour=0.15,
            best_photo=0,
            photos=1,
            model=self.model_id,
            web=match,
        )


def test_calibrate_suggests_thresholds_and_scores_stored_listings(
    config_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    @contextmanager
    def fake_open_vision(config: object) -> Iterator[FakeScorer]:
        yield FakeScorer()

    monkeypatch.setattr("ebay_sniper.cli.open_vision", fake_open_vision)
    monkeypatch.setattr("ebay_sniper.cli.vision_available", lambda: True)
    page = make_page(
        make_item("1", title="Good one", images=1),
        make_item("2", title="Other watch"),
    )
    good = Listing.from_summary(page.item_summaries[0], marketplace="EBAY_IT", query="q")
    good = replace(good, image_urls=("https://i.ebayimg.com/good.jpg",))
    other = Listing.from_summary(page.item_summaries[1], marketplace="EBAY_IT", query="q")
    with Store.open(config_path.parent / "data" / "test.sqlite3") as store:
        store.save_cycle(
            utc_now(), new=[(good, ListingStatus.SEEDED), (other, ListingStatus.SEEDED)]
        )
    assert run(config_path, "calibrate", "--show", "1") == 0
    out = capsys.readouterr().out
    # p1 . p2 = 1.0; thresholds 0.05 and 0.01 below the lowest positive.
    assert "  pos   1.000    0.920   +0.100  +1.000   p1.jpg: kept" in out
    assert "gold.jpg: filtered: photos not similar enough" in out
    assert "match_threshold = 0.95" in out
    assert "colour_threshold = 0.09" in out
    assert "web_threshold = 0.97" in out
    assert "Stored listings: 2 scored, 0 failed; 1 would be notified" in out
    assert "0.990 +0.150 +0.990 PASS [seeded] Good one" in out
    assert "Other watch" not in out
    with Store.open(config_path.parent / "data" / "test.sqlite3") as store:
        stored = store.get("1")
        assert stored is not None
        assert stored.vision is not None
        assert store.status_of("1") is ListingStatus.SEEDED


def test_digest_dry_run_prints_the_near_misses(
    config_path: Path, env_path: Path, capsys: pytest.CaptureFixture
) -> None:
    assert run(config_path, "digest", "--dry-run") == 1  # no database yet
    page = make_page(make_item("1", title="Close one"), make_item("2", title="Kept"))
    close, kept = (
        Listing.from_summary(item, marketplace="EBAY_IT", query="q") for item in page.item_summaries
    )
    score = VisionScore(match=0.61, negative=0, colour=0.0, best_photo=0, photos=1, model="m")
    with Store.open(config_path.parent / "data" / "test.sqlite3") as store:
        store.save_cycle(
            utc_now(),
            new=[(close, ListingStatus.PENDING), (kept, ListingStatus.PENDING)],
        )
        store.update_listing(close.with_vision(score), ListingStatus.BELOW_THRESHOLD)
    capsys.readouterr()
    assert run(config_path, "digest", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "Near misses: 1 listing(s)" in out
    assert "Close one</a> - 25.00 EUR (match 0.610" in out
    assert "Kept" not in out
