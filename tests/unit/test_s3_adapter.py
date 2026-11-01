from __future__ import annotations

import gzip
import hashlib

import anyio
import httpx
import pytest

from qa_runtime.adapters.s3 import S3Adapter
from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import ExecutionContext, S3ServiceConfig
from qa_runtime.ports.s3 import S3Request
from qa_runtime.security import SecretRedactor, SecretValue

pytestmark = pytest.mark.anyio


class FakeCredentials:
    def __init__(self):
        self.references = []

    async def get(self, reference):
        self.references.append(reference)
        return SecretValue({"access": "fake-access", "secret": "fake-secret", "session": "fake-session"}[reference])


def context():
    return ExecutionContext(run_id="test", environment="qa", trace_id="trace", traceparent="00-trace-span-01")


def service(**overrides):
    return S3ServiceConfig.model_validate(
        {
            "endpoint": "https://storage.example:9443",
            "region": "region-1",
            "accessKeyRef": "access",
            "secretKeyRef": "secret",
            "sessionTokenRef": "session",
            "buckets": {"files": "files-bucket"},
            **overrides,
        }
    )


async def test_signed_request_preserves_exact_key_and_temporary_credentials():
    calls = []

    def handle(request):
        calls.append(request)
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=fake-access/")
        assert "/region-1/s3/aws4_request" in request.headers["authorization"]
        assert request.headers["x-amz-security-token"] == "fake-session"
        assert request.headers["x-amz-content-sha256"] == hashlib.sha256(b"").hexdigest()
        return httpx.Response(200, content=b"payload", headers={"etag": '"not-a-checksum"'})

    credentials = FakeCredentials()
    adapter = S3Adapter({"store": service()}, credentials, transport=httpx.MockTransport(handle))
    try:
        result = await adapter.send(
            S3Request("store", "get-object", bucket="files-bucket", key="a/.././ space+%/Юникод.xml"), context()
        )
    finally:
        await adapter.aclose()
        await adapter.aclose()
    assert (
        calls[0].url.raw_path == b"/files-bucket/a/%2E%2E/%2E/%20space%2B%25/%D0%AE%D0%BD%D0%B8%D0%BA%D0%BE%D0%B4.xml"
    )
    assert result.body == b"payload"
    assert credentials.references == ["access", "secret", "session"]


@pytest.mark.parametrize(
    "style,authority,path",
    [("path", "storage.example:9443", "/files-bucket/f"), ("virtual", "files-bucket.storage.example:9443", "/f")],
)
async def test_addressing_styles(style, authority, path):
    calls = []
    adapter = S3Adapter(
        {"store": service(addressingStyle=style)},
        FakeCredentials(),
        transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200)),
    )
    try:
        await adapter.send(S3Request("store", "head-object", bucket="files-bucket", key="f"), context())
    finally:
        await adapter.aclose()
    assert calls[0].url.netloc.decode() == authority
    assert calls[0].url.path == path


@pytest.mark.parametrize(
    "action,method",
    [
        ("list-buckets", "GET"),
        ("head-bucket", "HEAD"),
        ("list-objects", "GET"),
        ("head-object", "HEAD"),
        ("get-object", "GET"),
        ("put-object", "PUT"),
        ("delete-object", "DELETE"),
        ("copy-object", "PUT"),
    ],
)
async def test_actions_have_single_request_no_redirect_or_retry(action, method):
    calls = []
    adapter = S3Adapter(
        {"store": service()},
        FakeCredentials(),
        transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(307, headers={"location": "/else"})
        ),
    )
    try:
        result = await adapter.send(
            S3Request(
                "store",
                action,
                bucket=None if action == "list-buckets" else "files-bucket",
                key="f" if action.endswith("object") else None,
                source_bucket="files-bucket",
                source_key="src",
            ),
            context(),
        )
    finally:
        await adapter.aclose()
    assert len(calls) == 1
    assert calls[0].method == method
    assert result.status_code == 307


async def test_list_query_is_bounded_explicit_page_and_escapes_cursor():
    calls = []
    adapter = S3Adapter(
        {"store": service()},
        FakeCredentials(),
        transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200)),
    )
    try:
        await adapter.send(
            S3Request(
                "store",
                "list-objects",
                bucket="files-bucket",
                prefix="a +%",
                delimiter="/",
                continuation_token="+/==",
                max_results=7,
            ),
            context(),
        )
        await adapter.send(S3Request("store", "list-buckets", max_results=7, continuation_token="next"), context())
    finally:
        await adapter.aclose()
    assert dict(calls[0].url.params) == {
        "max-keys": "7",
        "list-type": "2",
        "encoding-type": "url",
        "prefix": "a +%",
        "delimiter": "/",
        "continuation-token": "+/==",
    }
    assert dict(calls[1].url.params) == {"max-buckets": "7", "continuation-token": "next"}


async def test_copy_source_version_and_put_bytes_are_signed():
    calls = []
    adapter = S3Adapter(
        {"store": service()},
        FakeCredentials(),
        transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200)),
    )
    try:
        await adapter.send(
            S3Request(
                "store",
                "copy-object",
                bucket="files-bucket",
                key="dest",
                source_bucket="source",
                source_key="a +%",
                source_version_id="+==",
            ),
            context(),
        )
        await adapter.send(
            S3Request(
                "store",
                "put-object",
                bucket="files-bucket",
                key="dest",
                body=b"a\x00b",
                content_type="application/octet-stream",
                if_none_match="*",
            ),
            context(),
        )
    finally:
        await adapter.aclose()
    assert calls[0].headers["x-amz-copy-source"] == "/source/a%20%2B%25?versionId=%2B%3D%3D"
    assert calls[1].content == b"a\x00b"
    assert calls[1].headers["x-amz-content-sha256"] == hashlib.sha256(b"a\x00b").hexdigest()
    assert calls[1].headers["if-none-match"] == "*"


@pytest.mark.parametrize("status", [400, 403, 404, 500])
async def test_error_responses_remain_facts(status):
    adapter = S3Adapter(
        {"store": service()},
        FakeCredentials(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, content=b"<Error><Code>AccessDenied</Code></Error>")
        ),
    )
    try:
        result = await adapter.send(S3Request("store", "get-object", bucket="files-bucket", key="f"), context())
    finally:
        await adapter.aclose()
    assert result.status_code == status
    assert b"AccessDenied" in result.body


class Chunks(httpx.AsyncByteStream):
    closed = False

    async def __aiter__(self):
        yield b"12"
        yield b"345"

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("status", [200, 403])
async def test_response_cap_closes_stream_even_for_error_body(status):
    stream = Chunks()
    adapter = S3Adapter(
        {"store": service()},
        FakeCredentials(),
        transport=httpx.MockTransport(lambda request: httpx.Response(status, stream=stream)),
    )
    try:
        with pytest.raises(RuntimeFailure) as raised:
            await adapter.send(S3Request("store", "get-object", bucket="files-bucket", key="f", max_bytes=4), context())
    finally:
        await adapter.aclose()
    assert raised.value.code == "S3_RESPONSE_TOO_LARGE"
    assert stream.closed


@pytest.mark.parametrize(
    "options",
    [
        {"max_bytes": 0},
        {"max_bytes": True},
        {"max_results": 1001},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"body": b"123", "max_bytes": 2},
    ],
)
async def test_invalid_limits_are_rejected_before_credentials_and_network(options):
    credentials = FakeCredentials()
    calls = []
    adapter = S3Adapter(
        {"store": service()},
        credentials,
        transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200)),
    )
    try:
        with pytest.raises(RuntimeFailure):
            await adapter.send(S3Request("store", "put-object", bucket="files-bucket", key="f", **options), context())
    finally:
        await adapter.aclose()
    assert not credentials.references
    assert not calls


async def test_credentials_fail_safely_before_io():
    class BrokenCredentials:
        async def get(self, reference):
            raise ValueError("fake-secret should not appear")

    adapter = S3Adapter({"store": service()}, BrokenCredentials())
    try:
        with pytest.raises(RuntimeFailure) as raised:
            await adapter.send(S3Request("store", "get-object", bucket="files-bucket", key="f"), context())
    finally:
        await adapter.aclose()
    assert raised.value.exit_code == ExitCode.AUTHENTICATION_FAILED
    assert "fake-secret" not in str(raised.value)
    assert raised.value.execution_outcome == "not_sent"


@pytest.mark.parametrize("action", ["get-object", "put-object"])
async def test_total_timeout_and_mutation_ambiguity(action):
    async def handle(request):
        await anyio.sleep(1)
        return httpx.Response(200)

    adapter = S3Adapter({"store": service()}, FakeCredentials(), transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(RuntimeFailure) as raised:
            await adapter.send(
                S3Request("store", action, bucket="files-bucket", key="f", timeout_seconds=0.01), context()
            )
    finally:
        await adapter.aclose()
    assert raised.value.code == "S3_TIMEOUT"
    if action == "put-object":
        assert raised.value.execution_outcome == "unknown"
        assert raised.value.retry_advice == "read_state_first"
        assert not raised.value.retryable
    else:
        assert raised.value.retryable


async def test_credentials_registered_and_headers_redacted():
    redactor = SecretRedactor()
    adapter = S3Adapter(
        {"store": service()},
        FakeCredentials(),
        redactor=redactor,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"x-error": "fake-secret", "set-cookie": "secret-cookie"})
        ),
    )
    try:
        result = await adapter.send(S3Request("store", "get-object", bucket="files-bucket", key="f"), context())
    finally:
        await adapter.aclose()
    assert result.headers["x-error"] == "***"
    assert result.headers["set-cookie"] == "***"
    assert redactor.redact("fake-access fake-secret fake-session") == "*** *** ***"


async def test_get_preserves_compressed_object_bytes_and_content_encoding():
    stored = gzip.compress(b"original object data" * 100)
    adapter = S3Adapter(
        {"store": service()},
        FakeCredentials(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-encoding": "gzip", "content-length": str(len(stored))},
                stream=httpx.ByteStream(stored),
            )
        ),
    )
    try:
        result = await adapter.send(S3Request("store", "get-object", bucket="files-bucket", key="object.gz"), context())
    finally:
        await adapter.aclose()
    assert result.body == stored
    assert result.headers["content-encoding"] == "gzip"
    assert result.headers["content-length"] == str(len(stored))


async def test_close_attempts_all_cached_clients_when_first_hangs(monkeypatch):
    import qa_runtime.lifecycle as lifecycle

    class Client:
        def __init__(self, hang=False):
            self.calls = 0
            self.hang = hang

        async def aclose(self):
            self.calls += 1
            if self.hang:
                await anyio.sleep_forever()

    monkeypatch.setattr(lifecycle, "ADAPTER_CLEANUP_TIMEOUT_SECONDS", 0.05)
    first, second = Client(hang=True), Client()
    adapter = S3Adapter({"store": service()}, FakeCredentials())
    adapter._clients = {"first": first, "second": second}
    with pytest.raises(RuntimeFailure) as raised:
        with anyio.fail_after(0.5):
            await adapter.aclose()
    assert raised.value.code == "S3_CLOSE_FAILED"
    assert first.calls == second.calls == 1
    assert adapter._clients == {}
    await adapter.aclose()
    assert first.calls == second.calls == 1
