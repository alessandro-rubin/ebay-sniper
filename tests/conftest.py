from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from ebay_sniper.config import AppConfig, Secrets, load_config
from factories import (
    CONFIG_TOML,
    EBAY_CLIENT_ID,
    EBAY_CLIENT_SECRET,
    ENV_FILE_CONTENT,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)

SECRET_ENV_VARS = (
    "EBAY_CLIENT_ID",
    "EBAY_CLIENT_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real credentials in the developer's environment must not leak into tests."""
    for name in SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG_TOML, encoding="utf-8")
    return path


@pytest.fixture
def env_path(config_path: Path) -> Path:
    path = config_path.parent / ".env"
    path.write_text(ENV_FILE_CONTENT, encoding="utf-8")
    return path


@pytest.fixture
def config(config_path: Path) -> AppConfig:
    return load_config(config_path)


@pytest.fixture
def secrets() -> Secrets:
    return Secrets(
        ebay_client_id=SecretStr(EBAY_CLIENT_ID),
        ebay_client_secret=SecretStr(EBAY_CLIENT_SECRET),
        telegram_bot_token=SecretStr(TELEGRAM_BOT_TOKEN),
        telegram_chat_id=TELEGRAM_CHAT_ID,
    )
