"""Composition root: HTTP clients, store and pipeline, with their lifetimes."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import httpx

from ebay_sniper import __version__
from ebay_sniper.config import AppConfig, Secrets
from ebay_sniper.ebay import BrowseClient, EbayAppAuth, TokenProvider
from ebay_sniper.notify import TelegramClient, TelegramNotifier
from ebay_sniper.pipeline import Pipeline
from ebay_sniper.store import Store

HEADERS = {"User-Agent": f"ebay-sniper/{__version__}"}
EBAY_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
# Telegram downloads the photos from eBay while the request is open.
TELEGRAM_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


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


@contextmanager
def open_ebay(config: AppConfig, secrets: Secrets) -> Iterator[EbayServices]:
    with httpx.Client(timeout=EBAY_TIMEOUT, headers=HEADERS) as http:
        tokens = TokenProvider(http, secrets.ebay_client_id, secrets.ebay_client_secret)
        # The token request passes its own Basic auth, which overrides this one.
        http.auth = EbayAppAuth(tokens)
        browse = BrowseClient(
            http,
            delivery_country=config.search.delivery_country,
            postal_code=config.search.buyer_postal_code,
        )
        yield EbayServices(tokens=tokens, browse=browse)


@contextmanager
def open_notifier(config: AppConfig, secrets: Secrets) -> Iterator[TelegramNotifier]:
    with httpx.Client(timeout=TELEGRAM_TIMEOUT, headers=HEADERS) as http:
        client = TelegramClient(http, secrets.telegram_bot_token)
        yield TelegramNotifier(client, secrets.telegram_chat_id, config.telegram)


@contextmanager
def open_app(config: AppConfig, secrets: Secrets) -> Iterator[App]:
    with (
        open_ebay(config, secrets) as ebay,
        open_notifier(config, secrets) as notifier,
        Store.open(config.runtime.database_path) as store,
    ):
        pipeline = Pipeline(config, ebay.browse, store, notifier)
        yield App(config=config, ebay=ebay, notifier=notifier, store=store, pipeline=pipeline)
