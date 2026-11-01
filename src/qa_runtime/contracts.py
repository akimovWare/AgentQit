from __future__ import annotations

from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

type ExecutionOutcome = Literal["not_sent", "confirmed", "unknown"]
type RetryAdvice = Literal["read_state_first", "same_key_only", "safe", "do_not_retry"]


class ExitCode(IntEnum):
    SUCCESS = 0
    INVALID_INPUT = 2
    ASSERTION_FAILED = 3
    ENVIRONMENT_UNAVAILABLE = 4
    AUTHENTICATION_FAILED = 5
    INTERNAL_ERROR = 6
    BLOCKED = 7
    PARTIAL = 8


class ResultStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"
    PARTIAL = "partial"


class Correlation(BaseModel):
    trace_id: str
    traceparent: str


class ErrorItem(BaseModel):
    code: str
    message: str
    source: str | None = None
    retryable: bool = False
    operation: str | None = None
    http_status: int | None = None
    details: dict[str, Any] | None = None
    hint: str | None = None
    documentation: str | None = None
    execution_outcome: ExecutionOutcome | None = None
    retry_advice: RetryAdvice | None = None


class CommandResult(BaseModel):
    """Versioned operation result for CLI and MCP."""

    model_config = ConfigDict(populate_by_name=True)

    schema_version: Literal["1.0"] = "1.0"
    command: str
    status: ResultStatus
    exit_code: int
    run_id: str
    environment: str
    started_at: datetime
    finished_at: datetime
    correlation: Correlation | None = None
    duration_ms: int
    result: dict[str, Any] | list[Any] | str | int | float | bool | None = None
    error: ErrorItem | None = None
    evidence: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def success(
        cls,
        *,
        command: str,
        run_id: str,
        environment: str,
        started_at: datetime,
        finished_at: datetime,
        duration_ms: int,
        result: dict[str, Any] | list[Any] | str | int | float | bool | None,
        correlation: Correlation | None = None,
    ) -> CommandResult:
        return cls(
            command=command,
            status=ResultStatus.SUCCESS,
            exit_code=ExitCode.SUCCESS,
            run_id=run_id,
            environment=environment,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            result=result,
            correlation=correlation,
        )

    @classmethod
    def failure(
        cls,
        *,
        command: str,
        run_id: str,
        environment: str = "unknown",
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        duration_ms: int,
        exit_code: ExitCode,
        error: ErrorItem,
        correlation: Correlation | None = None,
        status: ResultStatus | None = None,
    ) -> CommandResult:
        now = datetime.now(UTC)
        return cls(
            command=command,
            status=status or ResultStatus.FAILURE,
            exit_code=exit_code,
            run_id=run_id,
            environment=environment,
            started_at=started_at or now,
            finished_at=finished_at or now,
            duration_ms=duration_ms,
            error=error,
            correlation=correlation,
        )
