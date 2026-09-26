from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from ebay_sniper.cli import build_parser, main
from ebay_sniper.logsetup import RedactingFormatter, register_secrets
from factories import (
    CONFIG_TOML,
    EBAY_CLIENT_SECRET,
    SEARCH_URL,
    TELEGRAM_API,
    TELEGRAM_BOT_TOKEN,
    TOKEN_URL,
    load_fixture,
    make_item,
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
    assert "288 search + 0 item details = 288" in out
    assert "Budget: OK, 288 of 3000 allowed (60% of the 5000 daily limit)" in out
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


def test_invalid_configuration_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG_TOML.replace('"EBAY_DE"', '"EBAY_UK"'), encoding="utf-8")
    assert run(config_path, "check-config") == 1
    assert "Configuration error" in capsys.readouterr().err


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

    # Second run: one new listing appears in another search.
    pages[(Q2, "EBAY_IT")] = make_page_data(make_item("150000000005", title="Spider web watch"))
    assert run(config_path, "run-once") == 0
    assert telegram_methods(telegram) == ["sendMessage", "sendPhoto"]
    photo = json.loads(telegram.calls.last.request.content)
    assert photo["reply_markup"]["inline_keyboard"][0][0]["url"] == (
        "https://www.ebay.it/itm/150000000005"
    )

    # Third run: nothing new, nothing sent.
    assert run(config_path, "run-once") == 0
    assert telegram.call_count == 2
    assert token.call_count == 3  # one process per run: the token is not stored on disk
    err = capsys.readouterr().err
    assert "Cycle done" in err
    assert TELEGRAM_BOT_TOKEN not in err

    # check-config reports the real usage recorded by the runs.
    assert run(config_path, "check-config") == 0
    out = capsys.readouterr().out
    assert "Browse API calls in the last 24 hours: 12" in out
    assert "Listings: 1 notified, 4 seeded" in out


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
    assert "19.90 EUR  AUCTION" in out
    assert "https://www.ebay.it/itm/120000000002" in out
    assert "Browse API calls used: 1" in out
    text = saved.read_text(encoding="utf-8")
    assert "example_seller" not in text
    assert "Futura Quartz" in text


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
