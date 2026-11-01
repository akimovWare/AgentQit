from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import OperationDefinition, Profile
from qa_runtime.ports.operation import OperationHandler
from qa_runtime.registry import OperationRegistry


class HandlerRegistry:
    def __init__(self, handlers: Iterable[OperationHandler]) -> None:
        registered: dict[str, OperationHandler] = {}
        for handler in handlers:
            kind = handler.kind.strip().lower()
            if not kind:
                raise RuntimeFailure(
                    ExitCode.INVALID_INPUT,
                    "HANDLER_KIND_INVALID",
                    "operation handler kind must not be empty",
                    source="compiler",
                )
            if kind in registered:
                raise RuntimeFailure(
                    ExitCode.INVALID_INPUT,
                    "DUPLICATE_HANDLER",
                    f"more than one operation handler is registered for kind '{kind}'",
                    source="compiler",
                )
            registered[kind] = handler
        self._handlers = MappingProxyType(registered)

    def get(self, kind: str) -> OperationHandler:
        try:
            return self._handlers[kind.lower()]
        except KeyError as error:
            available = ", ".join(sorted(self._handlers)) or "<empty>"
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "HANDLER_NOT_FOUND",
                f"no operation handler is registered for kind '{kind}'; available: {available}",
                source="compiler",
            ) from error

    def all(self) -> tuple[OperationHandler, ...]:
        return tuple(self._handlers.values())


@dataclass(frozen=True, slots=True)
class CompiledOperation:
    definition: OperationDefinition
    handler: OperationHandler
    plan: Any


class CompiledCatalog:
    def __init__(
        self,
        profile: Profile,
        operations: Mapping[str, CompiledOperation],
        handlers: tuple[OperationHandler, ...],
    ) -> None:
        self.profile = profile.model_copy(deep=True)
        self._operations = MappingProxyType(dict(operations))
        self.handlers = handlers

    def get(self, operation_id: str) -> CompiledOperation:
        try:
            return self._operations[operation_id]
        except KeyError as error:
            available = ", ".join(sorted(self._operations)) or "<empty>"
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "UNKNOWN_OPERATION",
                f"no operation registered with id '{operation_id}'; available: {available}",
                source="registry",
            ) from error

    def ids(self) -> list[str]:
        return sorted(self._operations)


class CatalogCompiler:
    def compile(
        self,
        registry: OperationRegistry,
        profile: Profile,
        handlers: HandlerRegistry,
        *,
        operation_ids: Iterable[str] | None = None,
    ) -> CompiledCatalog:
        compiled: dict[str, CompiledOperation] = {}
        operations = (
            registry.operations()
            if operation_ids is None
            else tuple(registry.get(operation_id) for operation_id in dict.fromkeys(operation_ids))
        )
        for operation in operations:
            handler = handlers.get(operation.kind)
            plan = handler.compile(operation, profile)
            compiled[operation.id] = CompiledOperation(operation, handler, plan)
        return CompiledCatalog(profile, compiled, handlers.all())
