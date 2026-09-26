from __future__ import annotations

from pathlib import Path

import pytest

from ebay_sniper.config import (
    AppConfig,
    ConfigError,
    compute_budget,
    default_env_file,
    load_config,
    load_secrets,
    validate_query,
)
from factories import (
    CONFIG_TOML,
    EBAY_CLIENT_SECRET,
    ENV_FILE_CONTENT,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def replace_line(old: str, new: str) -> str:
    assert old in CONFIG_TOML
    return CONFIG_TOML.replace(old, new)


def test_repository_config_is_valid_and_within_budget() -> None:
    config = load_config(REPO_ROOT / "config.toml")
    budget = compute_budget(config)
    assert budget.within_budget
    assert budget.search_calls_per_cycle == len(config.search.queries) * len(
        config.search.marketplaces
    )


def test_relative_paths_are_resolved_against_the_config_directory(config: AppConfig) -> None:
    base = config.runtime.database_path.parent.parent
    assert config.runtime.database_path == base / "data" / "test.sqlite3"
    assert config.runtime.database_path.is_absolute()
    # The [vision] section is missing: its defaults are resolved the same way.
    assert config.vision.positive_dir == base / "reference_images" / "positive"


def test_queries_are_normalised(tmp_path: Path) -> None:
    text = replace_line('"futura (spider, ragno)",', '"  futura   (spider,  ragno) ",')
    config = load_config(write_config(tmp_path, text))
    assert config.search.queries[0] == "futura (spider, ragno)"


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("results_per_query = 100", "results_per_qery = 100", "results_per_qery"),
        ("results_per_query = 100", "results_per_query = 500", "results_per_query"),
        ('["EBAY_IT", "EBAY_DE"]', '["EBAY_IT", "EBAY_UK"]', "EBAY_UK"),
        ('["EBAY_IT", "EBAY_DE"]', '["EBAY_IT", "EBAY_IT"]', "duplicate marketplaces"),
        ('"(spider, spiderweb) watch",', '"futura (spider, ragno)",', "duplicate queries"),
        ('"(spider, spiderweb) watch",', '"(spider, (web)) watch",', "nested"),
        ('"(spider, spiderweb) watch",', '"(spider, spiderweb watch",', "unbalanced"),
        ('"(spider, spiderweb) watch",', '"spider* watch",', "wildcard"),
        ('"(spider, spiderweb) watch",', f'"{"x" * 101}",', "truncates"),
        ('delivery_country = "IT"', 'delivery_country = "Italy"', "delivery_country"),
        ('timezone = "Europe/Rome"', 'timezone = "Europe/Padova"', "time zone"),
        ('["FIXED_PRICE", "AUCTION", "BEST_OFFER"]', '["BUY_NOW"]', "buying_options"),
    ],
)
def test_invalid_values_are_rejected(tmp_path: Path, old: str, new: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(tmp_path, replace_line(old, new)))


def test_validate_query_accepts_the_documented_or_syntax() -> None:
    validate_query("(iphone, ipad)")
    validate_query("futura (spider, ragno) (watch, orologio)")
    validate_query("x" * 100)


def test_missing_or_broken_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.toml")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(write_config(tmp_path, "[search\n"))


def test_budget_of_the_test_config(config: AppConfig) -> None:
    budget = compute_budget(config)
    assert budget.cycles_per_day == 72
    assert budget.search_calls_per_cycle == 4
    assert budget.total_calls_per_day == 288
    assert budget.allowed_calls_per_day == 3000
    assert budget.within_budget


def test_budget_exceeded_suggests_a_minimum_interval(tmp_path: Path) -> None:
    text = replace_line("poll_interval_minutes = 20", "poll_interval_minutes = 1")
    text = text.replace("api_budget_share = 0.6", "api_budget_share = 0.1")
    budget = compute_budget(load_config(write_config(tmp_path, text)))
    # 1440 cycles x 4 calls = 5760 > 500 allowed; 500 // 4 = 125 cycles fit.
    assert not budget.within_budget
    assert budget.allowed_calls_per_day == 500
    assert budget.minimum_poll_interval_minutes == 12
    assert 1440 // 12 * 4 <= 500


def test_load_secrets_from_env_file(env_path: Path) -> None:
    secrets = load_secrets(env_path)
    assert secrets.telegram_chat_id == TELEGRAM_CHAT_ID
    assert secrets.telegram_bot_token.get_secret_value() == TELEGRAM_BOT_TOKEN
    assert TELEGRAM_BOT_TOKEN not in repr(secrets)
    assert TELEGRAM_CHAT_ID not in secrets.redaction_values()


def test_environment_overrides_env_file(env_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
    assert load_secrets(env_path).telegram_chat_id == "999"


def test_missing_secrets_are_named_without_values(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        ENV_FILE_CONTENT.replace(
            f"EBAY_CLIENT_SECRET={EBAY_CLIENT_SECRET}", "EBAY_CLIENT_SECRET="
        ).replace(f"TELEGRAM_CHAT_ID={TELEGRAM_CHAT_ID}\n", ""),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc_info:
        load_secrets(env)
    message = str(exc_info.value)
    assert "EBAY_CLIENT_SECRET" in message
    assert "TELEGRAM_CHAT_ID" in message
    assert "EBAY_CLIENT_ID" not in message.replace("EBAY_CLIENT_SECRET", "")
    assert TELEGRAM_BOT_TOKEN not in message


def test_default_env_file_is_next_to_the_config(config_path: Path) -> None:
    assert default_env_file(config_path) == config_path.parent.resolve() / ".env"
