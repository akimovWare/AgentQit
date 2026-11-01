from __future__ import annotations

import secrets
import time
import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any

import anyio

from qa_runtime.compiler import CompiledCatalog
from qa_runtime.contracts import CommandResult, Correlation, ErrorItem, ExitCode, ResultStatus
from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import ExecutionContext, OperationDefinition, ParameterDefinition
from qa_runtime.ports.operation import InputRedactor, InputValidator, ObservationHandler
from qa_runtime.security import SecretRedactor, redact_command_result

_CLEANUP_TIMEOUT_SECONDS = 2.0


class QaRuntime:
    def __init__(
        self,
        catalog: CompiledCatalog,
        *,
        redactor: SecretRedactor | None = None,
        resources: Iterable[Any] | None = None,
    ) -> None:
        self._catalog = catalog
        self._redactor = redactor if redactor is not None else SecretRedactor()
        self._resources = tuple(catalog.handlers if resources is None else resources)
        self._closed_resources: set[int] = set()
        self._closed = False

    def validate_input(self, operation_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Normalize declared input without mutation admission, credential lookup or transport I/O."""
        operation = self._catalog.get(operation_id)
        resolved = self._resolve_params(operation.definition, params)
        if isinstance(operation.handler, InputValidator):
            operation.handler.validate_input(operation.plan, resolved)
        return resolved

    def redact(self, value: Any) -> Any:
        return self._redactor.redact(value)

    def public_input(self, operation_id: str, params: dict[str, Any]) -> dict[str, Any]:
        operation = self._catalog.get(operation_id)
        if isinstance(operation.handler, InputRedactor):
            params = operation.handler.redact_input(operation.plan, params)
        return self._redactor.redact(params)

    def redact_result(self, result: CommandResult) -> CommandResult:
        return redact_command_result(result, self._redactor)

    async def capture(self, operation_id: str, *, session_id: str | None = None) -> CommandResult:
        """Capture an observation boundary using the selected read-only handler capability."""
        return await self.invoke(operation_id, {}, capture_only=True, session_id=session_id)

    async def invoke(
        self,
        operation_id: str,
        params: dict[str, Any],
        *,
        allow_mutation: bool = False,
        capture_only: bool = False,
        session_id: str | None = None,
    ) -> CommandResult:
        started = time.monotonic_ns()
        started_at = datetime.now(UTC)
        run_id = f"run_{uuid.uuid4().hex}"
        trace_id = secrets.token_hex(16)
        traceparent = f"00-{trace_id}-{secrets.token_hex(8)}-01"
        correlation = Correlation(trace_id=trace_id, traceparent=traceparent)
        profile = self._catalog.profile
        context = ExecutionContext(
            run_id=run_id,
            environment=profile.environment,
            trace_id=trace_id,
            traceparent=traceparent,
            session_id=session_id,
        )
        command = f"{'checkpoint' if capture_only else 'invoke'} {operation_id}"
        execution_started = False
        mutating = False

        try:
            operation = self._catalog.get(operation_id)
            mutating = operation.definition.mutating
            if self._closed:
                raise RuntimeFailure(
                    ExitCode.ENVIRONMENT_UNAVAILABLE,
                    "RUNTIME_CLOSED",
                    "runtime resources have been closed",
                    source="runtime",
                )
            if capture_only and (mutating or not isinstance(operation.handler, ObservationHandler)):
                raise RuntimeFailure(
                    ExitCode.INVALID_INPUT,
                    "CHECKPOINT_UNSUPPORTED",
                    "operation does not support a read-only observation checkpoint",
                    source="runtime",
                )
            if operation.definition.mutating and not allow_mutation:
                raise RuntimeFailure(
                    ExitCode.BLOCKED,
                    "MUTATION_BLOCKED",
                    "mutating operation requires --allow-mutation",
                    source="runtime",
                )
            if operation.definition.mutating and profile.environment != "qa":
                raise RuntimeFailure(
                    ExitCode.BLOCKED,
                    "MUTATION_ENVIRONMENT_BLOCKED",
                    "mutating operations require profile environment 'qa'",
                    source="runtime",
                )
            if capture_only:
                execution_started = True
                result = await operation.handler.capture(operation.plan, context)
            else:
                resolved_params = self.validate_input(operation_id, params)
                execution_started = True
                result = await operation.handler.execute(operation.plan, resolved_params, context)
            response = CommandResult.success(
                command=command,
                run_id=run_id,
                environment=profile.environment,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                duration_ms=self._elapsed_ms(started),
                result=result,
                correlation=correlation,
            )
        except Exception as error:
            failure = (
                error
                if isinstance(error, RuntimeFailure)
                else RuntimeFailure(
                    ExitCode.INTERNAL_ERROR,
                    "INTERNAL_ERROR",
                    f"unexpected runtime error ({type(error).__name__})",
                    source="runtime",
                )
            )
            response = CommandResult.failure(
                command=command,
                run_id=run_id,
                environment=profile.environment,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                duration_ms=self._elapsed_ms(started),
                exit_code=failure.exit_code,
                error=self._execution_error(
                    failure,
                    operation_id=operation_id,
                    execution_started=execution_started,
                    mutating=mutating,
                ),
                correlation=correlation,
                status=self._failure_status(failure),
            )
        return self.redact_result(response)

    @staticmethod
    def _execution_error(
        failure: RuntimeFailure,
        *,
        operation_id: str,
        execution_started: bool,
        mutating: bool,
    ) -> ErrorItem:
        error = failure.as_error_item()
        updates: dict[str, Any] = {"operation": error.operation or operation_id}
        if not execution_started:
            updates.update(execution_outcome="not_sent", retry_advice="do_not_retry", retryable=False)
        elif mutating:
            outcome = error.execution_outcome or "unknown"
            updates["execution_outcome"] = outcome
            if outcome == "unknown":
                updates.update(retry_advice="read_state_first", retryable=False)
            elif outcome == "confirmed":
                updates.update(retry_advice="do_not_retry", retryable=False)
            else:
                updates["retry_advice"] = error.retry_advice or ("safe" if error.retryable else "do_not_retry")
        else:
            updates["retry_advice"] = error.retry_advice or ("safe" if error.retryable else "do_not_retry")
        return error.model_copy(update=updates)

    async def aclose(self) -> None:
        self._closed = True
        first_error: BaseException | None = None
        for resource in self._resources:
            if id(resource) in self._closed_resources:
                continue
            try:
                with anyio.move_on_after(_CLEANUP_TIMEOUT_SECONDS, shield=True) as cleanup:
                    await resource.aclose()
                if cleanup.cancel_called:
                    raise RuntimeFailure(
                        ExitCode.ENVIRONMENT_UNAVAILABLE,
                        "RESOURCE_CLEANUP_TIMEOUT",
                        "runtime resource cleanup timed out",
                        source="runtime",
                    )
            except BaseException as error:
                first_error = first_error or error
            finally:
                self._closed_resources.add(id(resource))
        if first_error is not None:
            raise first_error

    def _resolve_params(self, operation: OperationDefinition, params: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(params) - set(operation.parameters))
        if unknown:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "UNKNOWN_PARAM",
                f"operation '{operation.id}' does not accept: {', '.join(unknown)}",
                source="runtime",
            )

        resolved: dict[str, Any] = {}
        for parameter_name, definition in operation.parameters.items():
            value = params.get(parameter_name, self._catalog.profile.defaults.get(parameter_name))
            if value is None:
                value = definition.default
            if value is None and definition.required:
                raise RuntimeFailure(
                    ExitCode.INVALID_INPUT,
                    "MISSING_PARAM",
                    f"operation '{operation.id}' requires parameter '{parameter_name}'",
                    source="runtime",
                )
            if value is None:
                continue
            try:
                value = self._normalize_param(value, definition)
            except (InvalidOperation, TypeError, ValueError) as error:
                raise RuntimeFailure(
                    ExitCode.INVALID_INPUT,
                    "INVALID_PARAM",
                    f"parameter '{parameter_name}' {error}",
                    source="runtime",
                ) from error
            resolved[parameter_name] = value
        self._validate_parameter_relations(operation, resolved)
        return resolved

    @staticmethod
    def _validate_parameter_relations(operation: OperationDefinition, params: dict[str, Any]) -> None:
        for name, definition in operation.parameters.items():
            other_name = definition.less_than
            if other_name is None or name not in params or other_name not in params:
                continue
            if Decimal(str(params[name])) >= Decimal(str(params[other_name])):
                raise RuntimeFailure(
                    ExitCode.INVALID_INPUT,
                    "INVALID_PARAM",
                    f"parameter '{name}' must be less than parameter '{other_name}'",
                    source="runtime",
                )

    @staticmethod
    def _normalize_param(value: Any, definition: ParameterDefinition) -> Any:
        normalized = value
        if definition.value_type == "string":
            if isinstance(value, (bool, dict, list, tuple, set)):
                raise ValueError("must be a string")
            normalized = str(value)
        elif definition.value_type in {"number", "integer"}:
            if isinstance(value, bool):
                raise ValueError(f"must be a {definition.value_type}")
            numeric = Decimal(str(value))
            if not numeric.is_finite():
                raise ValueError(f"must be a finite {definition.value_type}")
            if definition.value_type == "integer" and numeric != numeric.to_integral_value():
                raise ValueError("must be an integer")
            if definition.minimum is not None:
                minimum = Decimal(str(definition.minimum))
                outside_bound = numeric <= minimum if definition.exclusive_minimum else numeric < minimum
                if outside_bound:
                    qualifier = "greater than" if definition.exclusive_minimum else "greater than or equal to"
                    raise ValueError(f"must be {qualifier} {definition.minimum:g}")
            if definition.maximum is not None:
                maximum = Decimal(str(definition.maximum))
                outside_bound = numeric >= maximum if definition.exclusive_maximum else numeric > maximum
                if outside_bound:
                    qualifier = "less than" if definition.exclusive_maximum else "less than or equal to"
                    raise ValueError(f"must be {qualifier} {definition.maximum:g}")
            if definition.value_type == "integer" or numeric == numeric.to_integral_value():
                normalized = int(numeric)
            else:
                normalized = float(numeric)
                if not isfinite(normalized):
                    raise ValueError("must be a finite number")
        elif definition.value_type == "boolean":
            if isinstance(value, bool):
                normalized = value
            elif isinstance(value, str) and value.lower() in {"true", "false"}:
                normalized = value.lower() == "true"
            else:
                raise ValueError("must be a boolean")
        elif definition.value_type == "object":
            if not isinstance(value, dict):
                raise ValueError("must be an object")
            properties = definition.properties
            if properties is None:
                normalized = dict(value)
            else:
                unknown = sorted(set(value) - set(properties))
                if unknown and not definition.additional_properties:
                    raise ValueError(f"does not accept properties: {', '.join(unknown)}")
                normalized = {name: item for name, item in value.items() if name not in properties}
                for property_name, property_definition in properties.items():
                    property_value = value.get(property_name, property_definition.default)
                    if property_value is None and property_definition.required:
                        raise ValueError(f"requires property '{property_name}'")
                    if property_value is None:
                        continue
                    try:
                        normalized[property_name] = QaRuntime._normalize_param(property_value, property_definition)
                    except (InvalidOperation, TypeError, ValueError) as error:
                        raise ValueError(f"property '{property_name}' {error}") from error
        elif definition.value_type == "array":
            if not isinstance(value, list):
                raise ValueError("must be an array")
            if definition.items is None:  # pragma: no cover - guarded by ParameterDefinition
                raise ValueError("must declare array items")
            normalized = [QaRuntime._normalize_param(item, definition.items) for item in value]

        if definition.format == "uuid":
            try:
                normalized = str(uuid.UUID(normalized))
            except (AttributeError, TypeError, ValueError) as error:
                raise ValueError("must be a valid UUID") from error
        elif definition.format == "date":
            try:
                if len(normalized) != 10 or normalized[4] != "-" or normalized[7] != "-":
                    raise ValueError
                normalized = date.fromisoformat(normalized).isoformat()
            except (AttributeError, TypeError, ValueError) as error:
                raise ValueError("must be a calendar date in YYYY-MM-DD form") from error
        elif definition.format == "date-time":
            try:
                parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
            except (AttributeError, TypeError, ValueError) as error:
                raise ValueError("must be an ISO-8601 date-time") from error
            if parsed.tzinfo is None:
                raise ValueError("must include a UTC offset")

        if definition.min_length is not None and len(normalized) < definition.min_length:
            raise ValueError(f"must have length at least {definition.min_length}")
        if definition.max_length is not None and len(normalized) > definition.max_length:
            raise ValueError(f"must have length at most {definition.max_length}")
        if definition.min_items is not None and len(normalized) < definition.min_items:
            raise ValueError(f"must contain at least {definition.min_items} items")
        if definition.max_items is not None and len(normalized) > definition.max_items:
            raise ValueError(f"must contain at most {definition.max_items} items")
        if definition.unique_items and any(
            item == previous for index, item in enumerate(normalized) for previous in normalized[:index]
        ):
            raise ValueError("must contain unique items")

        if definition.enum:
            declared_by_normalized = {item.upper(): item for item in definition.enum}
            candidate = str(normalized).upper()
            if candidate not in declared_by_normalized:
                allowed = ", ".join(definition.enum)
                raise ValueError(f"must be one of: {allowed}")
            normalized = declared_by_normalized[candidate]
        return normalized

    @staticmethod
    def _elapsed_ms(started_ns: int) -> int:
        return max(0, (time.monotonic_ns() - started_ns) // 1_000_000)

    @staticmethod
    def _failure_status(error: RuntimeFailure) -> ResultStatus:
        if error.exit_code == ExitCode.BLOCKED:
            return ResultStatus.BLOCKED
        if error.code.endswith("_TIMEOUT"):
            return ResultStatus.TIMEOUT
        return ResultStatus.FAILURE
