"""OAuth client-credentials (application) token for the eBay Buy APIs.

The token is cached in memory only, until shortly before it expires: it is a
credential and must not be written to disk or logs.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Generator

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from ebay_sniper.retry import backoff_delay

log = logging.getLogger(__name__)

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
PUBLIC_SCOPE = "https://api.ebay.com/oauth/api_scope"


class EbayAuthError(RuntimeError):
    """The token could not be obtained. Never carries credential values."""


class _TokenResponse(BaseModel):
    access_token: SecretStr
    expires_in: int


class TokenProvider:
    """Fetch an application access token and reuse it until it is about to expire."""

    def __init__(
        self,
        http: httpx.Client,
        client_id: SecretStr,
        client_secret: SecretStr,
        *,
        scope: str = PUBLIC_SCOPE,
        refresh_margin_s: float = 300.0,
        max_attempts: int = 3,
        backoff_base_s: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._refresh_margin_s = refresh_margin_s
        self._max_attempts = max(max_attempts, 1)
        self._backoff_base_s = backoff_base_s
        self._clock = clock
        self._sleep = sleep
        self._token: str | None = None
        self._expires_at = 0.0

    def get(self) -> str:
        if self._token is None or self._clock() >= self._expires_at:
            self._refresh()
        assert self._token is not None
        return self._token

    def invalidate(self) -> None:
        self._token = None

    def _refresh(self) -> None:
        auth = httpx.BasicAuth(
            self._client_id.get_secret_value(), self._client_secret.get_secret_value()
        )
        data = {"grant_type": "client_credentials", "scope": self._scope}
        for attempt in range(1, self._max_attempts + 1):
            last_attempt = attempt == self._max_attempts
            try:
                response = self._http.post(TOKEN_URL, data=data, auth=auth)
            except httpx.TransportError as exc:
                if last_attempt:
                    raise EbayAuthError(f"token request failed: {type(exc).__name__}") from None
                self._wait(attempt, f"transport error {type(exc).__name__}")
                continue
            if response.status_code >= 500 and not last_attempt:
                self._wait(attempt, f"HTTP {response.status_code}")
                continue
            break
        if response.status_code != 200:
            raise EbayAuthError(
                f"token request rejected with HTTP {response.status_code}: {_oauth_error(response)}"
            )
        try:
            token = _TokenResponse.model_validate_json(response.content)
        except ValidationError:
            raise EbayAuthError("unexpected token response format") from None
        self._token = token.access_token.get_secret_value()
        self._expires_at = self._clock() + max(token.expires_in - self._refresh_margin_s, 0.0)
        log.debug("Obtained eBay application token valid for %d s", token.expires_in)

    def _wait(self, attempt: int, reason: str) -> None:
        delay = backoff_delay(attempt, base=self._backoff_base_s)
        log.warning("eBay token request failed (%s), retrying in %.1f s", reason, delay)
        self._sleep(delay)


def _oauth_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "no details"
    if not isinstance(body, dict):
        return "no details"
    error = body.get("error", "unknown_error")
    description = body.get("error_description")
    return f"{error} ({description})" if description else str(error)


class EbayAppAuth(httpx.Auth):
    """Bearer authentication with one refresh-and-retry when the token is rejected."""

    def __init__(self, tokens: TokenProvider) -> None:
        self._tokens = tokens

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._tokens.get()}"
        response = yield request
        if response.status_code == 401:
            log.info("eBay rejected the access token, fetching a new one")
            self._tokens.invalidate()
            request.headers["Authorization"] = f"Bearer {self._tokens.get()}"
            yield request
