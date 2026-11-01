from __future__ import annotations

from typing import Any

from qa_runtime.contracts import ErrorItem, ExecutionOutcome, ExitCode, RetryAdvice


class RuntimeFailure(Exception):
    def __init__(
        self,
        exit_code: ExitCode,
        code: str,
        message: str,
        *,
        source: str | None = None,
        retryable: bool = False,
        operation: str | None = None,
        http_status: int | None = None,
        details: dict[str, Any] | None = None,
        hint: str | None = None,
        documentation: str | None = None,
        execution_outcome: ExecutionOutcome | None = None,
        retry_advice: RetryAdvice | None = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.code = code
        self.source = source
        self.retryable = retryable
        self.operation = operation
        self.http_status = http_status
        self.details = details
        self.hint = hint
        self.documentation = documentation
        self.execution_outcome = execution_outcome
        self.retry_advice = retry_advice

    def as_error_item(self) -> ErrorItem:
        return ErrorItem(
            code=self.code,
            message=str(self),
            source=self.source,
            retryable=self.retryable,
            operation=self.operation,
            http_status=self.http_status,
            details=self.details,
            hint=self.hint,
            documentation=self.documentation,
            execution_outcome=self.execution_outcome,
            retry_advice=self.retry_advice,
        )
