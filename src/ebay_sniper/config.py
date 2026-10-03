"""Configuration loading and validation.

Two sources:

- ``config.toml``: everything that is not secret. Parsed with :mod:`tomllib`
  and validated with pydantic. Unknown keys are rejected, so a typo fails
  loudly instead of being silently ignored. Relative paths are resolved
  against the directory of the configuration file, not the working directory,
  so that the app behaves the same when started by cron or systemd.
- ``.env`` (or the process environment, which takes precedence): the secrets,
  loaded with pydantic-settings and kept as :class:`pydantic.SecretStr`.
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
)
from pydantic_core import ErrorDetails
from pydantic_settings import BaseSettings, SettingsConfigDict

from ebay_sniper.ebay.endpoints import EbayEnvironment, keyset_environment

DEFAULT_CONFIG_PATH = Path("config.toml")

# Marketplaces supported by the Browse API. An unknown value in the
# X-EBAY-C-MARKETPLACE-ID header silently falls back to EBAY_US, so the
# configuration is checked against this list instead of trusting the API.
SUPPORTED_MARKETPLACES = frozenset(
    {
        "EBAY_AT",
        "EBAY_AU",
        "EBAY_BE",
        "EBAY_CA",
        "EBAY_CH",
        "EBAY_DE",
        "EBAY_ES",
        "EBAY_FR",
        "EBAY_GB",
        "EBAY_HK",
        "EBAY_IE",
        "EBAY_IT",
        "EBAY_MY",
        "EBAY_NL",
        "EBAY_PH",
        "EBAY_PL",
        "EBAY_SG",
        "EBAY_TW",
        "EBAY_US",
    }
)

# The Browse API truncates longer `q` values, which could silently drop a
# closing parenthesis and change the meaning of the query.
MAX_QUERY_LENGTH = 100
MAX_RESULTS_PER_PAGE = 200
MINUTES_PER_DAY = 24 * 60

BuyingOption = Literal["FIXED_PRICE", "AUCTION", "BEST_OFFER", "CLASSIFIED_AD"]


class ConfigError(Exception):
    """The configuration or the secrets are missing or invalid."""


def _resolve_path(value: Path, info: ValidationInfo) -> Path:
    base_dir = (info.context or {}).get("base_dir")
    if base_dir is not None and not value.is_absolute():
        return (Path(base_dir) / value).resolve()
    return value


ConfigPath = Annotated[Path, AfterValidator(_resolve_path)]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SearchConfig(_Section):
    marketplaces: list[str] = Field(min_length=1)
    delivery_country: str = Field(pattern=r"^[A-Z]{2}$")
    buyer_postal_code: str | None = None
    buying_options: list[BuyingOption] = Field(min_length=1)
    results_per_query: int = Field(default=100, ge=1, le=MAX_RESULTS_PER_PAGE)
    queries: list[str] = Field(min_length=1)

    @field_validator("marketplaces")
    @classmethod
    def _check_marketplaces(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - SUPPORTED_MARKETPLACES)
        if unknown:
            raise ValueError(
                f"unsupported marketplace(s) {unknown}; "
                f"supported: {', '.join(sorted(SUPPORTED_MARKETPLACES))}"
            )
        if len(set(value)) != len(value):
            raise ValueError("duplicate marketplaces")
        return value

    @field_validator("buyer_postal_code")
    @classmethod
    def _blank_postal_code_is_none(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return value.strip()

    @field_validator("queries")
    @classmethod
    def _check_queries(cls, value: list[str]) -> list[str]:
        queries = [" ".join(query.split()) for query in value]
        for query in queries:
            validate_query(query)
        if len(set(queries)) != len(queries):
            raise ValueError("duplicate queries")
        return queries


def validate_query(query: str) -> None:
    """Raise ValueError if the Browse API would reject or truncate ``query``."""
    if not query:
        raise ValueError("empty query")
    if len(query) > MAX_QUERY_LENGTH:
        raise ValueError(
            f"query is {len(query)} characters, the Browse API truncates at "
            f"{MAX_QUERY_LENGTH}: {query!r}"
        )
    if "*" in query:
        raise ValueError(f"the * wildcard is not allowed by the Browse API: {query!r}")
    depth = 0
    for char in query:
        if char == "(":
            depth += 1
            if depth > 1:
                raise ValueError(f"nested parentheses are not supported: {query!r}")
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise ValueError(f"unbalanced parentheses: {query!r}")
    if depth != 0:
        raise ValueError(f"unbalanced parentheses: {query!r}")


class PriceConfig(_Section):
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    # Maximum total (item + shipping + import charges) in `currency`.
    max_total: Decimal = Field(gt=0)
    # Approximate value of one unit of each other currency in `currency`, used
    # only to compare totals with `max_total`.
    exchange_rates: dict[str, Decimal] = Field(default_factory=dict)

    @field_validator("exchange_rates")
    @classmethod
    def _check_rates(cls, value: dict[str, Decimal]) -> dict[str, Decimal]:
        for code, rate in value.items():
            if not (len(code) == 3 and code.isascii() and code.isupper()):
                raise ValueError(f"not an ISO 4217 currency code: {code!r}")
            if rate <= 0:
                raise ValueError(f"exchange rate for {code} must be positive")
        return value


class RulesConfig(_Section):
    # Whole words or phrases, case- and accent-insensitive; a trailing * matches
    # word prefixes. Checked in the title and in the seller's condition notes.
    drop_keywords: list[str] = Field(default_factory=list)
    flag_keywords: list[str] = Field(default_factory=list)
    flag_condition_ids: list[int] = Field(default_factory=list)

    @field_validator("drop_keywords", "flag_keywords")
    @classmethod
    def _check_keywords(cls, value: list[str]) -> list[str]:
        for keyword in value:
            body = keyword.strip()
            if not any(char.isalnum() for char in body):
                raise ValueError(f"keyword without letters or digits: {keyword!r}")
            if "*" in body.rstrip("*") or body.endswith("**"):
                raise ValueError(f"* is only allowed once, at the end: {keyword!r}")
        return value


class VisionConfig(_Section):
    # Needs the optional extra: uv sync --extra vision
    enabled: bool = False
    model: str = "ViT-B-16-SigLIP"
    pretrained: str = "webli"
    positive_dir: ConfigPath = Field(
        default=Path("reference_images/positive"), validate_default=True
    )
    negative_dir: ConfigPath = Field(
        default=Path("reference_images/negative"), validate_default=True
    )
    # Photos of a listing that are compared, in listing order.
    max_photos: int = Field(default=8, ge=1, le=24)
    # eBay rendition downloaded for the model (its input is a few hundred pixels).
    image_size: str = Field(default="s-l500", pattern=r"^s-l\d+$")
    # Days a downloaded photo stays in the cache.
    image_cache_days: int = Field(default=30, ge=1)
    # False is shadow mode: scores are shown in the notifications, nothing is
    # filtered. Turn it on once `calibrate` has suggested the thresholds.
    filter: bool = False
    # Listings whose best photo is less similar to the positives are not notified.
    match_threshold: float = Field(default=0.0, ge=-1, le=1)
    # Listings that look gold-tone (colour below this) are not notified.
    colour_threshold: float = Field(default=-1.0, ge=-2, le=2)
    # Listings whose dial does not look like a spider web (web below this) are
    # not notified.
    web_threshold: float = Field(default=-1.0, ge=-2, le=2)
    # Daily Telegram summary of the listings kept out by the thresholds, the
    # safety net against a wrong threshold. Sent by the first cycle after this
    # hour ([telegram] timezone); -1 turns it off.
    digest_hour: int = Field(default=20, ge=-1, le=23)


class TelegramConfig(_Section):
    send_all_photos: bool = True
    # Telegram media groups hold 2 to 10 items.
    max_photos: int = Field(default=4, ge=1, le=10)
    # Time zone used to display auction end times.
    timezone: str = "Europe/Rome"

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value


class RuntimeConfig(_Section):
    poll_interval_minutes: int = Field(default=20, ge=1, le=MINUTES_PER_DAY)
    database_path: ConfigPath = Field(
        default=Path("data/ebay_sniper.sqlite3"), validate_default=True
    )
    image_cache_dir: ConfigPath = Field(default=Path("data/image_cache"), validate_default=True)
    # Daily Browse API call limit of the application (5,000 by default; eBay can
    # raise it after an Application Growth Check).
    daily_call_limit: int = Field(default=5000, gt=0)
    # Maximum share of the daily limit that the configuration may use.
    api_budget_share: float = Field(default=0.6, gt=0, le=1)
    # Hard cap on individual notifications per cycle; the excess is summarised
    # in a single message instead of flooding the chat.
    max_notifications_per_cycle: int = Field(default=10, ge=1)
    # getItem calls per cycle for new listings that survive the rules; beyond
    # it, listings are notified with the search data only. 0 disables details.
    max_details_per_cycle: int = Field(default=10, ge=0)
    # The first successful search of a (query, marketplace) pair only records the
    # results already online, without notifying them. Later cycles notify what
    # is really new. This also applies when a query or marketplace is added.
    seed_new_searches: bool = True
    # Daily Telegram status of the last 24 hours (cycles, failures, API calls),
    # sent by the first cycle after this hour ([telegram] timezone). Its absence
    # is the only sign of a bot that cannot start at all. -1 turns it off.
    heartbeat_hour: int = Field(default=9, ge=-1, le=23)
    # Telegram alert after this many consecutive failed cycles, and a message
    # when cycles work again. 0 turns it off.
    failure_alert_after: int = Field(default=2, ge=0)


class AppConfig(_Section):
    search: SearchConfig
    price: PriceConfig
    rules: RulesConfig = Field(default_factory=RulesConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)


class Secrets(BaseSettings):
    """Secrets from the environment or the .env file. Never log these values."""

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8", env_ignore_empty=True, extra="ignore", frozen=True
    )

    ebay_client_id: SecretStr
    ebay_client_secret: SecretStr
    # Not a credential: the environment the eBay keyset was created in.
    ebay_environment: EbayEnvironment = Field(default="production", validate_default=True)
    telegram_bot_token: SecretStr
    # Not a credential: a numeric user id or an @channel name.
    telegram_chat_id: str

    @field_validator("ebay_environment")
    @classmethod
    def _check_keyset_environment(
        cls, value: EbayEnvironment, info: ValidationInfo
    ) -> EbayEnvironment:
        client_id = info.data.get("ebay_client_id")
        keyset = keyset_environment(client_id.get_secret_value()) if client_id is not None else None
        if keyset is not None and keyset != value:
            raise ValueError(f"EBAY_CLIENT_ID is a {keyset} keyset, set EBAY_ENVIRONMENT={keyset}")
        return value

    def redaction_values(self) -> list[str]:
        """Plain values that must never appear in logs."""
        return [
            self.ebay_client_id.get_secret_value(),
            self.ebay_client_secret.get_secret_value(),
            self.telegram_bot_token.get_secret_value(),
        ]


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    """Read and validate ``config.toml``."""
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"configuration file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from None
    # Validate missing optional sections too, so that their default paths are
    # resolved against the configuration directory like explicit ones.
    for section in ("rules", "vision", "telegram", "runtime"):
        raw.setdefault(section, {})
    try:
        return AppConfig.model_validate(raw, context={"base_dir": path.resolve().parent})
    except ValidationError as exc:
        raise ConfigError(f"{path}: {_format_errors(exc)}") from None


def load_secrets(env_file: Path | None) -> Secrets:
    """Load the secrets. Error messages name the variables, never their values."""
    try:
        return Secrets(_env_file=env_file)  # type: ignore[call-arg]
    except ValidationError as exc:
        problems = sorted({_describe_secret_error(error) for error in exc.errors()})
        source = f"{env_file} or the environment" if env_file else "the environment"
        raise ConfigError(
            f"missing or invalid secrets in {source}: {', '.join(problems)}"
        ) from None


def _describe_secret_error(error: ErrorDetails) -> str:
    name = str(error["loc"][0]).upper()
    if error["type"] == "missing":
        return name
    # Pydantic messages describe the expected value, never the input.
    return f"{name} ({error['msg']})"


def default_env_file(config_path: Path) -> Path:
    """The .env file next to the configuration file."""
    return config_path.resolve().parent / ".env"


def _format_errors(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()
    )


@dataclass(frozen=True, slots=True)
class ApiBudget:
    """Worst-case daily Browse API usage implied by the configuration."""

    cycles_per_day: int
    search_calls_per_cycle: int
    detail_calls_per_cycle: int
    daily_limit: int
    share: float

    @property
    def search_calls_per_day(self) -> int:
        return self.cycles_per_day * self.search_calls_per_cycle

    @property
    def detail_calls_per_day(self) -> int:
        return self.cycles_per_day * self.detail_calls_per_cycle

    @property
    def total_calls_per_day(self) -> int:
        return self.search_calls_per_day + self.detail_calls_per_day

    @property
    def allowed_calls_per_day(self) -> int:
        return math.floor(self.daily_limit * self.share)

    @property
    def within_budget(self) -> bool:
        return self.total_calls_per_day <= self.allowed_calls_per_day

    @property
    def minimum_poll_interval_minutes(self) -> int | None:
        """The shortest poll interval that fits the budget; None if none does."""
        per_cycle = self.search_calls_per_cycle + self.detail_calls_per_cycle
        if per_cycle == 0:
            return 1
        max_cycles = self.allowed_calls_per_day // per_cycle
        if max_cycles == 0:
            return None
        return math.ceil(MINUTES_PER_DAY / max_cycles)


def compute_budget(config: AppConfig) -> ApiBudget:
    """One search call per (query, marketplace) per cycle, since results fit in one
    page, plus the getItem cap. Retries are not counted.
    """
    return ApiBudget(
        cycles_per_day=math.ceil(MINUTES_PER_DAY / config.runtime.poll_interval_minutes),
        search_calls_per_cycle=len(config.search.queries) * len(config.search.marketplaces),
        detail_calls_per_cycle=config.runtime.max_details_per_cycle,
        daily_limit=config.runtime.daily_call_limit,
        share=config.runtime.api_budget_share,
    )
