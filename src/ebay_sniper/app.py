"""Composition root: HTTP clients, store and pipeline, with their lifetimes."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta

import httpx

from ebay_sniper import __version__
from ebay_sniper.config import AppConfig, ConfigError, Secrets
from ebay_sniper.ebay import API_ROOTS, BrowseClient, EbayAppAuth, TokenProvider
from ebay_sniper.images import ImageCache
from ebay_sniper.models import CurrencyConverter
from ebay_sniper.notify import TelegramClient, TelegramNotifier
from ebay_sniper.pipeline import Pipeline
from ebay_sniper.store import Store
from ebay_sniper.vision import EmbeddingCache, OpenClipEmbedder, VisionScorer, vision_available

HEADERS = {"User-Agent": f"ebay-sniper/{__version__}"}
EBAY_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
# Telegram downloads the photos from eBay while the request is open.
TELEGRAM_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
IMAGE_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


@dataclass(frozen=True, slots=True)
class EbayServices:
    tokens: TokenProvider
    browse: BrowseClient


@dataclass(frozen=True, slots=True)
class App:
    config: AppConfig
    ebay: EbayServices
    notifier: TelegramNotifier
    store: Store
    pipeline: Pipeline
    vision: VisionScorer | None


@contextmanager
def open_ebay(config: AppConfig, secrets: Secrets) -> Iterator[EbayServices]:
    api_root = API_ROOTS[secrets.ebay_environment]
    with httpx.Client(timeout=EBAY_TIMEOUT, headers=HEADERS) as http:
        tokens = TokenProvider(
            http, secrets.ebay_client_id, secrets.ebay_client_secret, api_root=api_root
        )
        # The token request passes its own Basic auth, which overrides this one.
        http.auth = EbayAppAuth(tokens)
        browse = BrowseClient(
            http,
            api_root=api_root,
            delivery_country=config.search.delivery_country,
            postal_code=config.search.buyer_postal_code,
        )
        yield EbayServices(tokens=tokens, browse=browse)


@contextmanager
def open_notifier(config: AppConfig, secrets: Secrets) -> Iterator[TelegramNotifier]:
    with httpx.Client(timeout=TELEGRAM_TIMEOUT, headers=HEADERS) as http:
        client = TelegramClient(http, secrets.telegram_bot_token)
        converter = CurrencyConverter(config.price.currency, config.price.exchange_rates)
        yield TelegramNotifier(
            client, secrets.telegram_chat_id, config.telegram, converter=converter
        )


@contextmanager
def open_vision(config: AppConfig) -> Iterator[VisionScorer | None]:
    """The photo scorer, or None when ``vision.enabled`` is off. The model loads on first use."""
    vision = config.vision
    if not vision.enabled:
        yield None
        return
    if not vision_available():
        raise ConfigError(
            "vision.enabled is true but the vision extra is not installed: "
            "run uv sync --extra vision, or set vision.enabled = false"
        )
    cache_dir = config.runtime.image_cache_dir
    embedder = OpenClipEmbedder(vision.model, vision.pretrained)
    with httpx.Client(timeout=IMAGE_TIMEOUT, headers=HEADERS, follow_redirects=True) as http:
        images = ImageCache(
            http,
            cache_dir / "photos",
            size=vision.image_size,
            max_age=timedelta(days=vision.image_cache_days),
        )
        images.prune()
        yield VisionScorer(
            vision, embedder, EmbeddingCache(cache_dir / "embeddings", embedder.model_id), images
        )


@contextmanager
def open_app(config: AppConfig, secrets: Secrets) -> Iterator[App]:
    with (
        open_ebay(config, secrets) as ebay,
        open_notifier(config, secrets) as notifier,
        open_vision(config) as vision,
        Store.open(config.runtime.database_path) as store,
    ):
        pipeline = Pipeline(config, ebay.browse, store, notifier, classifier=vision)
        yield App(
            config=config,
            ebay=ebay,
            notifier=notifier,
            store=store,
            pipeline=pipeline,
            vision=vision,
        )
