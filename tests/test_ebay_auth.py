from __future__ import annotations

import base64
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from pydantic import SecretStr

from ebay_sniper.ebay.auth import PUBLIC_SCOPE, EbayAppAuth, EbayAuthError, TokenProvider
from ebay_sniper.ebay.endpoints import API_ROOTS
from factories import (
    EBAY_CLIENT_ID,
    EBAY_CLIENT_SECRET,
    SANDBOX_TOKEN_URL,
    TOKEN_URL,
    load_fixture,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def provider(clock: FakeClock, sleeps: list[float]) -> TokenProvider:
    return TokenProvider(
        httpx.Client(),
        SecretStr(EBAY_CLIENT_ID),
        SecretStr(EBAY_CLIENT_SECRET),
        clock=clock,
        sleep=sleeps.append,
    )


def test_token_request_uses_basic_auth_and_form_body(
    respx_mock: respx.MockRouter, provider: TokenProvider
) -> None:
    route = respx_mock.post(TOKEN_URL).respond(json=load_fixture("token.json"))
    assert provider.get() == "test-access-token"
    request = route.calls.last.request
    expected = base64.b64encode(f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
    assert request.headers["Authorization"] == f"Basic {expected}"
    assert parse_qs(request.content.decode()) == {
        "grant_type": ["client_credentials"],
        "scope": [PUBLIC_SCOPE],
    }


def test_sandbox_token_endpoint_with_the_same_scope(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(SANDBOX_TOKEN_URL).respond(json=load_fixture("token.json"))
    provider = TokenProvider(
        httpx.Client(),
        SecretStr(EBAY_CLIENT_ID),
        SecretStr(EBAY_CLIENT_SECRET),
        api_root=API_ROOTS["sandbox"],
    )
    assert provider.get() == "test-access-token"
    assert parse_qs(route.calls.last.request.content.decode())["scope"] == [PUBLIC_SCOPE]


def test_token_is_cached_until_shortly_before_expiry(
    respx_mock: respx.MockRouter, provider: TokenProvider, clock: FakeClock
) -> None:
    route = respx_mock.post(TOKEN_URL).respond(json=load_fixture("token.json"))
    provider.get()
    clock.now += 7200 - 301
    provider.get()
    assert route.call_count == 1
    clock.now += 2
    provider.get()
    assert route.call_count == 2


def test_invalidate_forces_a_new_token(
    respx_mock: respx.MockRouter, provider: TokenProvider
) -> None:
    route = respx_mock.post(TOKEN_URL).respond(json=load_fixture("token.json"))
    provider.get()
    provider.invalidate()
    provider.get()
    assert route.call_count == 2


def test_rejected_credentials_raise_without_leaking_them(
    respx_mock: respx.MockRouter, provider: TokenProvider
) -> None:
    respx_mock.post(TOKEN_URL).respond(
        401, json={"error": "invalid_client", "error_description": "client authentication failed"}
    )
    with pytest.raises(EbayAuthError, match="invalid_client") as exc_info:
        provider.get()
    assert EBAY_CLIENT_SECRET not in str(exc_info.value)
    assert EBAY_CLIENT_ID not in str(exc_info.value)


def test_server_errors_are_retried(
    respx_mock: respx.MockRouter, provider: TokenProvider, sleeps: list[float]
) -> None:
    respx_mock.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.ConnectError("connection refused"),
            httpx.Response(200, json=load_fixture("token.json")),
        ]
    )
    assert provider.get() == "test-access-token"
    assert len(sleeps) == 2


def test_persistent_server_errors_give_up(
    respx_mock: respx.MockRouter, provider: TokenProvider
) -> None:
    respx_mock.post(TOKEN_URL).respond(503)
    with pytest.raises(EbayAuthError, match="HTTP 503"):
        provider.get()


def test_malformed_token_response(respx_mock: respx.MockRouter, provider: TokenProvider) -> None:
    respx_mock.post(TOKEN_URL).respond(json={"unexpected": True})
    with pytest.raises(EbayAuthError, match="unexpected token response"):
        provider.get()


def test_app_auth_refreshes_the_token_once_on_401(
    respx_mock: respx.MockRouter, provider: TokenProvider
) -> None:
    respx_mock.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "old", "expires_in": 7200}),
            httpx.Response(200, json={"access_token": "new", "expires_in": 7200}),
        ]
    )
    api = respx_mock.get("https://api.ebay.com/buy/browse/v1/item/x").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={})]
    )
    client = httpx.Client(auth=EbayAppAuth(provider))
    response = client.get("https://api.ebay.com/buy/browse/v1/item/x")
    assert response.status_code == 200
    assert [call.request.headers["Authorization"] for call in api.calls] == [
        "Bearer old",
        "Bearer new",
    ]
