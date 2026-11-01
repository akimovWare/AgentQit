from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from qa_runtime.ports.decoding import ValueDecoder


def topic_fingerprint(topic: str) -> str:
    """Bind a configured topic without persisting its potentially sensitive name."""
    return hashlib.sha256(topic.encode("utf-8")).hexdigest()


class KafkaCursor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["qa.kafka-cursor/v1"] = "qa.kafka-cursor/v1"
    cluster: str
    topic_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_fingerprint: str
    captured_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    offsets: dict[int, int]

    def matches_topic(self, topic: str) -> bool:
        return self.topic_fingerprint == topic_fingerprint(topic)

    @field_validator("offsets", mode="before")
    @classmethod
    def valid_offsets(cls, value):
        if not isinstance(value, dict) or not value:
            raise ValueError("cursor must include partition offsets")
        for partition, offset in value.items():
            if isinstance(partition, bool) or not str(partition).isdigit():
                raise ValueError("partition must be a non-negative integer")
            if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                raise ValueError("offset must be a non-negative integer")
        if len({int(partition) for partition in value}) != len(value):
            raise ValueError("cursor contains duplicate partitions")
        return value


@dataclass(frozen=True, slots=True)
class KafkaJsonFieldMatch:
    path: tuple[str, ...]
    expected: object


@dataclass(frozen=True, slots=True)
class KafkaConsumeRequest:
    cluster: str
    topic: str
    client_id: str
    timeout_seconds: float
    max_records: int
    max_bytes: int = 5 * 1024 * 1024
    lookback_seconds: float | None = None
    key_equals: bytes | None = None
    value_contains: bytes | None = None
    json_fields_equal: tuple[KafkaJsonFieldMatch, ...] = ()
    stop_after_matches: int | None = None
    value_decoder: ValueDecoder | None = None
    after_cursor: KafkaCursor | None = None


@dataclass(frozen=True, slots=True)
class KafkaRecord:
    topic: str
    partition: int
    offset: int
    timestamp_ms: int | None
    key: bytes | None
    value: bytes | None
    headers: list[tuple[str, bytes | None]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class KafkaConsumeResult:
    records: list[KafkaRecord] = field(default_factory=list)
    decode_errors: list[dict[str, Any]] = field(default_factory=list)
    decode_errors_truncated: bool = False
    size_bytes: int = 0
    truncated: bool = False
    truncation_reason: Literal["max_records", "max_bytes", "stop_after_matches"] | None = None


def kafka_record_size(record: KafkaRecord) -> int:
    return (
        len(record.key or b"")
        + len(record.value or b"")
        + sum(len(name.encode("utf-8")) + len(value or b"") for name, value in record.headers)
    )


@dataclass(frozen=True, slots=True)
class KafkaPublishResult:
    topic: str
    partition: int
    offset: int
    timestamp_ms: int | None


class KafkaPort(Protocol):
    async def committed_offset(
        self,
        cluster: str,
        topic: str,
        group_id: str,
        partition: int,
        *,
        client_id: str,
        timeout_seconds: float,
    ) -> int | None: ...

    async def capture(
        self,
        cluster: str,
        topic: str,
        *,
        client_id: str,
        timeout_seconds: float,
    ) -> KafkaCursor: ...

    async def consume(self, request: KafkaConsumeRequest) -> KafkaConsumeResult | list[KafkaRecord]: ...

    async def publish(
        self,
        cluster: str,
        topic: str,
        *,
        key: bytes | None,
        value: bytes,
        headers: list[tuple[str, bytes | None]],
        client_id: str,
    ) -> KafkaPublishResult: ...

    async def aclose(self) -> None: ...
