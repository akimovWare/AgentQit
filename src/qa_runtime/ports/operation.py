from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from qa_runtime.models import ExecutionContext, OperationDefinition, Profile

type ResultPayload = dict[str, Any] | list[Any] | str | int | float | bool | None


@runtime_checkable
class ObservationHandler(Protocol):
    async def capture(self, plan: Any, context: ExecutionContext) -> ResultPayload: ...


@runtime_checkable
class InputValidator(Protocol):
    def validate_input(self, plan: Any, params: dict[str, Any]) -> None: ...


@runtime_checkable
class InputRedactor(Protocol):
    def redact_input(self, plan: Any, params: dict[str, Any]) -> dict[str, Any]: ...


class OperationHandler(Protocol):
    @property
    def kind(self) -> str: ...

    def compile(self, operation: OperationDefinition, profile: Profile) -> Any: ...

    async def execute(
        self,
        plan: Any,
        params: dict[str, Any],
        context: ExecutionContext,
    ) -> ResultPayload: ...

    async def aclose(self) -> None: ...
