from __future__ import annotations

import anyio
import pytest

from qa_runtime.adapters.credentials import InMemoryCredentialSource
from qa_runtime.adapters.httpx import HttpxAdapter
from qa_runtime.adapters.kafka import AioKafkaAdapter
from qa_runtime.adapters.postgres import AsyncpgAdapter
from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import DatabaseServiceConfig, KafkaClusterConfig
from qa_runtime.security import SecretRedactor

pytestmark = pytest.mark.anyio


class Resource:
    def __init__(self, name, calls, *, hanging=False):
        self.name = name
        self.calls = calls
        self.hanging = hanging
        self.closed = False
        self.terminated = False

    async def aclose(self):
        self.calls.append(self.name)
        if self.hanging:
            await anyio.sleep_forever()
        await anyio.sleep(0)
        self.closed = True

    async def close(self):
        await self.aclose()

    async def stop(self):
        await self.aclose()

    def terminate(self):
        self.terminated = True


def adapter(kind, resources):
    credentials = InMemoryCredentialSource({})
    if kind == "http":
        instance = HttpxAdapter()
        instance._clients = resources
        return instance, instance._clients
    if kind == "database":
        config = DatabaseServiceConfig(dsn="postgresql://fake/db", usernameRef="u", passwordRef="p")
        instance = AsyncpgAdapter({name: config for name in resources}, credentials, SecretRedactor())
        instance._pools = resources
        return instance, instance._pools
    config = KafkaClusterConfig(bootstrapServers=["fake:9092"])
    instance = AioKafkaAdapter({name: config for name in resources}, credentials, SecretRedactor())
    instance._producers = resources
    return instance, instance._producers


@pytest.mark.parametrize("kind", ["http", "database", "kafka"])
async def test_hanging_first_child_does_not_skip_other_resources_or_exceed_budget(kind, monkeypatch):
    monkeypatch.setattr("qa_runtime.lifecycle.ADAPTER_CLEANUP_TIMEOUT_SECONDS", 0.04)
    calls = []
    first = Resource("first", calls, hanging=True)
    second = Resource("second", calls)
    instance, cache = adapter(kind, {"a": first, "b": second})

    with anyio.fail_after(0.5):
        with pytest.raises(RuntimeFailure) as caught:
            await instance.aclose()

    assert (
        caught.value.code
        == {"http": "HTTP_CLOSE_FAILED", "database": "DB_CLOSE_FAILED", "kafka": "KAFKA_CLOSE_FAILED"}[kind]
    )
    assert calls == ["first", "second"]
    assert second.closed
    assert not cache
    if kind == "database":
        assert first.terminated
    await instance.aclose()  # Cache ownership is cleared even after failed cleanup.
    assert calls == ["first", "second"]


@pytest.mark.parametrize("kind", ["http", "database", "kafka"])
async def test_adapter_cleanup_is_shielded_from_already_cancelled_caller(kind):
    calls = []
    first, second = Resource("first", calls), Resource("second", calls)
    instance, cache = adapter(kind, {"a": first, "b": second})

    with anyio.CancelScope() as caller:
        caller.cancel()
        await instance.aclose()

    assert calls == ["first", "second"]
    assert first.closed and second.closed
    assert not cache
