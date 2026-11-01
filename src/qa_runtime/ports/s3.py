from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from qa_runtime.models import ExecutionContext

type S3Action = Literal[
    "list-buckets",
    "head-bucket",
    "list-objects",
    "head-object",
    "get-object",
    "put-object",
    "delete-object",
    "copy-object",
]

MUTATING_ACTIONS = frozenset({"put-object", "delete-object", "copy-object"})
MAX_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class S3Request:
    storage: str
    action: S3Action
    bucket: str | None = None
    key: str | None = None
    prefix: str | None = None
    delimiter: str | None = None
    continuation_token: str | None = None
    max_results: int = 100
    version_id: str | None = None
    byte_range: str | None = None
    if_match: str | None = None
    if_none_match: str | None = None
    body: bytes = b""
    content_type: str | None = None
    source_bucket: str | None = None
    source_key: str | None = None
    source_version_id: str | None = None
    timeout_seconds: float | None = None
    max_bytes: int = 5 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class S3Result:
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


class S3Port(Protocol):
    async def send(self, request: S3Request, context: ExecutionContext) -> S3Result: ...

    async def aclose(self) -> None: ...
