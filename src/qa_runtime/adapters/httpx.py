from __future__ import annotations

import asyncio
import json

import httpx

from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.lifecycle import CloseAttempt, close_resources
from qa_runtime.models import ExecutionContext, HttpRequest, HttpResult
from qa_runtime.ports.tls import TlsProvider
from qa_runtime.serialization import to_jsonable

SENSITIVE_HEADERS = {"authorization", "cookie", "proxy-authorization", "set-cookie"}


class HttpxAdapter:
    def __init__(self, client: httpx.AsyncClient | None = None, *, tls_provider: TlsProvider | None = None) -> None:
        self._owns_client = client is None
        self._client = client
        self._tls_provider = tls_provider
        self._clients: dict[str | None, httpx.AsyncClient] = {}

    async def send(self, request: HttpRequest, context: ExecutionContext) -> HttpResult:
        body_args = {} if request.json_body is None else {"json": request.json_body}
        try:
            client = self._client or self._client_for(request.tls_ref)
            async with (
                asyncio.timeout(request.timeout_seconds),
                client.stream(
                    request.method,
                    request.url,
                    headers=request.headers,
                    params=request.query,
                    **body_args,
                ) as response,
            ):
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > request.max_response_bytes:
                        raise RuntimeFailure(
                            ExitCode.ENVIRONMENT_UNAVAILABLE,
                            "HTTP_RESPONSE_TOO_LARGE",
                            "HTTP response exceeds the configured byte limit",
                            source="http",
                            http_status=response.status_code,
                            details={"max_response_bytes": request.max_response_bytes},
                            execution_outcome="confirmed",
                        )
                    chunks.append(chunk)
                content = b"".join(chunks)
        except (httpx.TimeoutException, TimeoutError) as error:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "HTTP_TIMEOUT",
                "HTTP request timed out",
                source="http",
                retryable=True,
                execution_outcome="not_sent" if isinstance(error, httpx.ConnectTimeout | httpx.PoolTimeout) else None,
            ) from error
        except httpx.RequestError as error:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "HTTP_UNREACHABLE",
                "HTTP request failed",
                source="http",
                retryable=True,
                execution_outcome="not_sent" if isinstance(error, httpx.ConnectError) else None,
            ) from error

        try:
            body = json.loads(content)
        except (ValueError, UnicodeDecodeError):
            try:
                body = content.decode(response.encoding or "utf-8")
            except (UnicodeDecodeError, LookupError):
                body = to_jsonable(content)
        return HttpResult(
            status_code=response.status_code,
            headers={
                name.lower(): "***" if name.lower() in SENSITIVE_HEADERS else value
                for name, value in response.headers.items()
            },
            body=body,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            clients = list(self._clients.values())
            self._clients.clear()
            await close_resources(
                [CloseAttempt(client.aclose) for client in clients], source="http", code="HTTP_CLOSE_FAILED"
            )

    def _client_for(self, tls_ref: str | None) -> httpx.AsyncClient:
        client = self._clients.get(tls_ref)
        if client is not None:
            return client
        if tls_ref is not None and self._tls_provider is None:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "TLS_PROVIDER_NOT_FOUND",
                f"HTTP configuration requires tlsRef '{tls_ref}'",
                source="http",
                execution_outcome="not_sent",
            )
        verify = True if self._tls_provider is None else self._tls_provider.ssl_context(tls_ref)
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=30, write=30, pool=10),
            follow_redirects=False,
            verify=verify,
        )
        self._clients[tls_ref] = client
        return client
