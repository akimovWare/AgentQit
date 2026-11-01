from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiokafka import TopicPartition
from aiokafka.errors import KafkaError

from qa_runtime.adapters.credentials import InMemoryCredentialSource
from qa_runtime.adapters.kafka import AioKafkaAdapter
from qa_runtime.errors import RuntimeFailure
from qa_runtime.handlers.kafka_offsets import KafkaOffsetsOperationHandler
from qa_runtime.models import ExecutionContext, KafkaClusterConfig, OperationDefinition, Profile
from qa_runtime.security import SecretRedactor

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("offset", [None, -1, 0, 43])
async def test_offset_fetch_is_read_only_and_distinguishes_missing_from_zero(monkeypatch, offset):
    calls = []

    class Admin:
        def __init__(self, **options):
            calls.append(("create", options))

        async def start(self):
            calls.append(("start",))

        async def list_consumer_group_offsets(self, group, *, partitions):
            calls.append(("fetch", group, partitions))
            return {} if offset is None else {partitions[0]: SimpleNamespace(offset=offset)}

        async def close(self):
            calls.append(("close",))

    monkeypatch.setattr("qa_runtime.adapters.kafka.AIOKafkaAdminClient", Admin)
    adapter = AioKafkaAdapter(
        {"cluster": KafkaClusterConfig(bootstrapServers=["broker.invalid:9092"])},
        InMemoryCredentialSource({}),
        SecretRedactor(),
    )
    result = await adapter.committed_offset(
        "cluster", "reports", "service-reports", 2, client_id="observer", timeout_seconds=1
    )
    assert result == (None if offset is None or offset < 0 else offset)
    assert calls[0][1]["client_id"] == "observer"
    assert calls[2] == ("fetch", "service-reports", [TopicPartition("reports", 2)])
    assert calls[-1] == ("close",)
    assert len(calls) == 4  # No consumer, group join, poll, offset reset or commit.


@pytest.mark.parametrize("failure", ["timeout", "broker"])
async def test_offset_failure_is_normalized_and_admin_is_closed(monkeypatch, failure):
    closed = []

    class Admin:
        def __init__(self, **options):
            pass

        async def start(self):
            if failure == "timeout":
                await asyncio.Event().wait()
            raise KafkaError("untrusted broker detail")

        async def close(self):
            closed.append(True)

    monkeypatch.setattr("qa_runtime.adapters.kafka.AIOKafkaAdminClient", Admin)
    adapter = AioKafkaAdapter(
        {"cluster": KafkaClusterConfig(bootstrapServers=["broker.invalid:9092"])},
        InMemoryCredentialSource({}),
        SecretRedactor(),
    )
    with pytest.raises(RuntimeFailure) as caught:
        await adapter.committed_offset(
            "cluster", "reports", "service-reports", 0, client_id="observer", timeout_seconds=0.02
        )
    assert caught.value.code == "KAFKA_OFFSETS_UNAVAILABLE"
    assert "untrusted broker detail" not in str(caught.value)
    assert closed == [True]


async def test_offset_handler_resolves_topic_and_exposes_next_offset_as_a_fact():
    profile = Profile.model_validate(
        {
            "kafka": {
                "cluster": {
                    "bootstrapServers": ["broker.invalid:9092"],
                    "topics": {"reports": "physical.reports"},
                }
            }
        }
    )
    calls = []

    class Port:
        async def committed_offset(self, *args, **kwargs):
            calls.append((args, kwargs))
            return 17

    operation = OperationDefinition.model_validate(
        {
            "id": "observe-offset",
            "kind": "kafka-offsets",
            "parameters": {"partition": {"type": "integer", "required": True, "minimum": 0}},
            "config": {
                "cluster": "cluster",
                "topicRef": "reports",
                "groupId": "fixed-group",
                "partitionParameter": "partition",
            },
        }
    )
    handler = KafkaOffsetsOperationHandler(Port(), profile.kafka)
    plan = handler.compile(operation, profile)
    assert calls == []
    result = await handler.execute(
        plan,
        {"partition": 2},
        ExecutionContext(
            run_id="run_test",
            environment="qa",
            trace_id="a" * 32,
            traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01",
        ),
    )
    assert calls[0][0] == ("cluster", "physical.reports", "fixed-group", 2)
    assert result["committed_next_offset"] == 17
    assert "verdict" not in result
    operation.parameters["partition"].minimum = -1
    with pytest.raises(RuntimeFailure, match="non-negative integer"):
        handler.compile(operation, profile)
