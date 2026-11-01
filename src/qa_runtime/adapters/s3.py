from __future__ import annotations

import math
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import anyio
import httpx
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.lifecycle import CloseAttempt, close_resources
from qa_runtime.models import ExecutionContext, S3ServiceConfig
from qa_runtime.ports.credentials import CredentialSource
from qa_runtime.ports.s3 import MAX_BYTES, MUTATING_ACTIONS, S3Request, S3Result
from qa_runtime.ports.tls import TlsProvider
from qa_runtime.security import SecretRedactor

METHODS = {
    "list-buckets": "GET",
    "head-bucket": "HEAD",
    "list-objects": "GET",
    "head-object": "HEAD",
    "get-object": "GET",
    "put-object": "PUT",
    "delete-object": "DELETE",
    "copy-object": "PUT",
}


def encode_key(key: str) -> str:
    """Preserve S3 key segments, including literal dot segments, on HTTP clients."""
    return "/".join("%2E" * len(part) if part in {".", ".."} else quote(part, safe="") for part in key.split("/"))


class S3Adapter:
    def __init__(
        self,
        storages: dict[str, S3ServiceConfig],
        credentials: CredentialSource,
        *,
        tls_provider: TlsProvider | None = None,
        redactor: SecretRedactor | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._storages = storages
        self._credentials = credentials
        self._tls_provider = tls_provider
        self._redactor = redactor or SecretRedactor()
        self._transport = transport
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._closed = False

    async def send(self, request: S3Request, context: ExecutionContext) -> S3Result:
        if self._closed:
            raise RuntimeFailure(ExitCode.INTERNAL_ERROR, "S3_CLOSED", "S3 adapter is closed", source="s3")
        service = self._storages.get(request.storage)
        if service is None:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT, "S3_NOT_CONFIGURED", "S3 storage is not configured", source="s3"
            )
        self._validate_request(request)
        client = self._client_for(request.storage, service)
        timeout = request.timeout_seconds or service.request_timeout_seconds
        signed = await self._signed_request(request, service)
        try:
            with anyio.fail_after(timeout):
                async with client.stream(
                    METHODS[request.action],
                    signed.url,
                    headers=dict(signed.headers),
                    content=request.body,
                    timeout=httpx.Timeout(timeout, connect=min(timeout, service.connect_timeout_seconds)),
                ) as response:
                    chunks = bytearray()
                    # S3 object Content-Encoding is stored metadata. Preserve its
                    # bytes rather than transparently decompressing the object.
                    # Injected transports can provide an already buffered body.
                    body_stream = response.aiter_bytes() if response.is_stream_consumed else response.aiter_raw()
                    async for chunk in body_stream:
                        if len(chunks) + len(chunk) > request.max_bytes:
                            raise RuntimeFailure(
                                ExitCode.ENVIRONMENT_UNAVAILABLE,
                                "S3_RESPONSE_TOO_LARGE",
                                "S3 response exceeds maxBytes",
                                source="s3",
                                http_status=response.status_code,
                                details={"maxBytes": request.max_bytes},
                                execution_outcome="confirmed" if request.action in MUTATING_ACTIONS else None,
                                retry_advice="read_state_first" if request.action in MUTATING_ACTIONS else None,
                            )
                        chunks.extend(chunk)
                    return S3Result(
                        response.status_code,
                        self._redactor.redact(dict(response.headers)),
                        bytes(chunks),
                    )
        except (httpx.TimeoutException, TimeoutError) as error:
            raise self._transport_failure(request, "S3_TIMEOUT", "S3 request timed out") from error
        except httpx.RequestError as error:
            raise self._transport_failure(request, "S3_UNREACHABLE", "S3 request failed") from error

    @staticmethod
    def _validate_request(request: S3Request) -> None:
        invalid = (
            request.action not in METHODS
            or isinstance(request.max_bytes, bool)
            or not isinstance(request.max_bytes, int)
            or not 1 <= request.max_bytes <= MAX_BYTES
            or isinstance(request.max_results, bool)
            or not isinstance(request.max_results, int)
            or not 1 <= request.max_results <= 1000
        )
        if request.timeout_seconds is not None:
            invalid |= not math.isfinite(request.timeout_seconds) or not 0 < request.timeout_seconds <= 300
        if invalid:
            raise RuntimeFailure(ExitCode.INVALID_INPUT, "S3_REQUEST_INVALID", "S3 request is invalid", source="s3")
        if len(request.body) > request.max_bytes:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "S3_REQUEST_TOO_LARGE",
                "S3 request body exceeds maxBytes",
                source="s3",
                execution_outcome="not_sent",
                retry_advice="do_not_retry",
            )

    async def _signed_request(self, request: S3Request, service: S3ServiceConfig) -> AWSRequest:
        secrets = []
        try:
            for reference in (service.access_key_ref, service.secret_key_ref, service.session_token_ref):
                if reference is not None:
                    secret = await self._credentials.get(reference)
                    self._redactor.register(secret)
                    secrets.append(secret.reveal())
        except RuntimeFailure:
            raise
        except Exception as error:
            raise RuntimeFailure(
                ExitCode.AUTHENTICATION_FAILED,
                "S3_CREDENTIALS_FAILED",
                "S3 credentials could not be resolved",
                source="credentials",
                execution_outcome="not_sent",
                retry_advice="do_not_retry",
            ) from error
        headers = {"accept-encoding": "identity"}
        for name, value in (
            ("content-type", request.content_type),
            ("range", request.byte_range),
            ("if-match", request.if_match),
            ("if-none-match", request.if_none_match),
        ):
            if value is not None:
                headers[name] = value
        if request.action == "copy-object":
            source = f"/{request.source_bucket}/{encode_key(request.source_key or '')}"
            if request.source_version_id is not None:
                source += "?" + urlencode({"versionId": request.source_version_id}, quote_via=quote)
            headers["x-amz-copy-source"] = source
        signed = AWSRequest(
            method=METHODS[request.action], url=self._url(request, service), data=request.body, headers=headers
        )
        S3SigV4Auth(Credentials(*secrets), "s3", service.region).add_auth(signed)
        return signed

    @staticmethod
    def _url(request: S3Request, service: S3ServiceConfig) -> str:
        endpoint = urlsplit(service.endpoint)
        path = "/"
        authority = endpoint.netloc
        if request.bucket is not None:
            if service.addressing_style == "virtual":
                authority = f"{request.bucket}.{authority}"
            else:
                path += quote(request.bucket, safe="")
        if request.key is not None:
            path = path.rstrip("/") + "/" + encode_key(request.key)
        query: dict[str, str | int] = {}
        if request.action in {"list-buckets", "list-objects"}:
            query["max-buckets" if request.action == "list-buckets" else "max-keys"] = request.max_results
            if request.action == "list-objects":
                query["list-type"] = "2"
                query["encoding-type"] = "url"
            for name, value in (
                ("prefix", request.prefix),
                ("delimiter", request.delimiter),
                ("continuation-token", request.continuation_token),
            ):
                if value is not None:
                    query[name] = value
        if request.version_id is not None:
            query["versionId"] = request.version_id
        return urlunsplit((endpoint.scheme, authority, path, urlencode(query, quote_via=quote), ""))

    def _client_for(self, name: str, service: S3ServiceConfig) -> httpx.AsyncClient:
        if name in self._clients:
            return self._clients[name]
        if service.tls_ref is not None and self._tls_provider is None:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "TLS_PROVIDER_NOT_FOUND",
                "S3 storage requires a TLS provider",
                source="tls",
            )
        verify = True if self._tls_provider is None else self._tls_provider.ssl_context(service.tls_ref)
        client = httpx.AsyncClient(verify=verify, follow_redirects=False, transport=self._transport)
        self._clients[name] = client
        return client

    @staticmethod
    def _transport_failure(request: S3Request, code: str, message: str) -> RuntimeFailure:
        mutating = request.action in MUTATING_ACTIONS
        return RuntimeFailure(
            ExitCode.ENVIRONMENT_UNAVAILABLE,
            code,
            message,
            source="s3",
            retryable=not mutating,
            execution_outcome="unknown" if mutating else None,
            retry_advice="read_state_first" if mutating else "safe",
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        clients = list(self._clients.values())
        self._clients.clear()
        await close_resources([CloseAttempt(client.aclose) for client in clients], source="s3", code="S3_CLOSE_FAILED")
