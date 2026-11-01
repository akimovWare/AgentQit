from __future__ import annotations

import ssl
import time
from dataclasses import dataclass

import anyio
import httpx

from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import HttpRequest
from qa_runtime.ports.auth import AuthContext
from qa_runtime.ports.credentials import CredentialSource
from qa_runtime.security import SecretRedactor


@dataclass(frozen=True, slots=True)
class ClientCredentialsConfig:
    token_url: str
    client_id_ref: str
    client_secret_ref: str
    scopes: tuple[str, ...] = ()
    default_ttl_seconds: int = 300
    expiry_skew_seconds: int = 30


@dataclass(frozen=True, slots=True)
class _CachedToken:
    value: str
    expires_at: float


class OAuth2ClientCredentialsProvider:
    def __init__(
        self,
        config: ClientCredentialsConfig,
        credentials: CredentialSource,
        redactor: SecretRedactor,
        *,
        client: httpx.AsyncClient | None = None,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        self._config = config
        self._credentials = credentials
        self._redactor = redactor
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=20, write=20, pool=10),
            follow_redirects=False,
            verify=tls_context or True,
        )
        self._cached: _CachedToken | None = None
        self._lock = anyio.Lock()

    async def authorize(self, request: HttpRequest, context: AuthContext) -> HttpRequest:
        token = await self._token()
        headers = {**request.headers, "Authorization": f"Bearer {token}"}
        return request.model_copy(update={"headers": headers})

    async def invalidate(self, context: AuthContext) -> None:
        self._cached = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _token(self) -> str:
        now = time.monotonic()
        if self._cached is not None and self._cached.expires_at > now:
            return self._cached.value
        async with self._lock:
            now = time.monotonic()
            if self._cached is not None and self._cached.expires_at > now:
                return self._cached.value
            self._cached = await self._fetch_token(now)
            return self._cached.value

    async def _fetch_token(self, now: float) -> _CachedToken:
        client_id = await self._credentials.get(self._config.client_id_ref)
        client_secret = await self._credentials.get(self._config.client_secret_ref)
        self._redactor.register(client_id)
        self._redactor.register(client_secret)
        form = {
            "client_id": client_id.reveal(),
            "client_secret": client_secret.reveal(),
            "grant_type": "client_credentials",
        }
        if self._config.scopes:
            form["scope"] = " ".join(self._config.scopes)
        try:
            response = await self._client.post(
                self._config.token_url,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data=form,
            )
        except (httpx.TimeoutException, httpx.RequestError) as error:
            raise RuntimeFailure(
                ExitCode.AUTHENTICATION_FAILED,
                "AUTH_PROVIDER_UNAVAILABLE",
                "OAuth2 token provider is unavailable",
                source="auth",
                retryable=True,
            ) from error
        if not response.is_success:
            raise RuntimeFailure(
                ExitCode.AUTHENTICATION_FAILED,
                "AUTH_TOKEN_REJECTED",
                f"OAuth2 token request was rejected with HTTP {response.status_code}",
                source="auth",
            )
        try:
            payload = response.json()
            token = payload["access_token"]
            ttl = int(payload.get("expires_in", self._config.default_ttl_seconds))
        except (TypeError, ValueError, KeyError) as error:
            raise RuntimeFailure(
                ExitCode.AUTHENTICATION_FAILED,
                "AUTH_TOKEN_INVALID",
                "OAuth2 token response has an invalid shape",
                source="auth",
            ) from error
        if not isinstance(token, str) or not token:
            raise RuntimeFailure(
                ExitCode.AUTHENTICATION_FAILED,
                "AUTH_TOKEN_INVALID",
                "OAuth2 token response has no access token",
                source="auth",
            )
        self._redactor.register(token)
        usable_ttl = max(1, ttl - self._config.expiry_skew_seconds)
        return _CachedToken(token, now + usable_ttl)
