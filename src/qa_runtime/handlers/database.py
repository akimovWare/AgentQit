from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import DatabaseServiceConfig, ExecutionContext, OperationDefinition, Profile
from qa_runtime.ports.database import DatabasePort
from qa_runtime.ports.decoding import ValueDecoder
from qa_runtime.ports.operation import ResultPayload
from qa_runtime.ports.tls import TlsProvider
from qa_runtime.serialization import to_jsonable

POSITIONAL_PARAMETER = re.compile(r"\$(\d+)")


class DatabaseOperationConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", allow_inf_nan=False)

    database: str
    statement: str
    bindings: list[str] = Field(default_factory=list)
    fetch: Literal["all", "one", "value", "none"] = "all"
    timeout_seconds: float | None = Field(default=None, gt=0, alias="timeoutSeconds")
    column_decoders: dict[str, str] = Field(default_factory=dict, alias="columnDecoders")
    max_rows: int = Field(default=1000, ge=1, le=10_000, alias="maxRows")
    max_bytes: int = Field(default=5 * 1024 * 1024, ge=1, le=100 * 1024 * 1024, alias="maxBytes")

    @field_validator("column_decoders")
    @classmethod
    def validate_column_decoders(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not column.strip() or not decoder.strip() for column, decoder in value.items()):
            raise ValueError("columnDecoders must map non-empty column names to decoder references")
        return value

    @model_validator(mode="after")
    def validate_fetch_decoders(self) -> DatabaseOperationConfig:
        if self.column_decoders and self.fetch not in {"all", "one"}:
            raise ValueError("columnDecoders requires fetch 'all' or 'one'")
        return self


@dataclass(frozen=True, slots=True)
class CompiledDatabaseOperation:
    definition: OperationDefinition
    config: DatabaseOperationConfig
    column_decoders: dict[str, ValueDecoder]


class DatabaseOperationHandler:
    kind = "db-query"

    def __init__(
        self,
        database: DatabasePort,
        databases: Mapping[str, DatabaseServiceConfig],
        *,
        tls_provider: TlsProvider | None = None,
        value_decoders: Mapping[str, ValueDecoder] | None = None,
    ) -> None:
        self._database = database
        self._databases = dict(databases)
        self._tls_provider = tls_provider
        self._value_decoders = dict(value_decoders or {})

    def compile(self, operation: OperationDefinition, profile: Profile) -> CompiledDatabaseOperation:
        try:
            config = DatabaseOperationConfig.model_validate(operation.config)
        except ValidationError as error:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "REGISTRY_INVALID",
                f"database operation '{operation.id}' has invalid config: {error}",
                source="registry",
            ) from error
        database = self._databases.get(config.database)
        if database is None:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "DATABASE_NOT_CONFIGURED",
                f"operation '{operation.id}' references missing database '{config.database}'",
                source="compiler",
            )
        if database.tls_ref is not None:
            if self._tls_provider is None:
                raise RuntimeFailure(
                    ExitCode.ENVIRONMENT_UNAVAILABLE,
                    "TLS_PROVIDER_NOT_FOUND",
                    f"database '{config.database}' requires tlsRef '{database.tls_ref}'",
                    source="compiler",
                )
            self._tls_provider.ssl_context(database.tls_ref)

        unknown_bindings = [binding for binding in config.bindings if binding not in operation.parameters]
        if unknown_bindings:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "REGISTRY_INVALID",
                f"database operation '{operation.id}' binds unknown parameters: {', '.join(unknown_bindings)}",
                source="registry",
            )
        placeholders = {int(value) for value in POSITIONAL_PARAMETER.findall(config.statement)}
        expected = set(range(1, len(config.bindings) + 1))
        if placeholders != expected:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "REGISTRY_INVALID",
                f"database operation '{operation.id}' positional placeholders do not match bindings",
                source="registry",
            )
        column_decoders: dict[str, ValueDecoder] = {}
        for column, reference in config.column_decoders.items():
            try:
                column_decoders[column] = self._value_decoders[reference]
            except KeyError as error:
                raise RuntimeFailure(
                    ExitCode.INVALID_INPUT,
                    "VALUE_DECODER_NOT_CONFIGURED",
                    f"operation '{operation.id}' references missing value decoder '{reference}'",
                    source="compiler",
                ) from error
        return CompiledDatabaseOperation(operation, config, column_decoders)

    async def execute(
        self,
        plan: CompiledDatabaseOperation,
        params: dict[str, Any],
        context: ExecutionContext,
    ) -> ResultPayload:
        args = tuple(params.get(binding) for binding in plan.config.bindings)
        result = await self._database.execute(
            plan.config.database,
            plan.config.statement,
            args,
            fetch=plan.config.fetch,
            mutating=plan.definition.mutating,
            timeout_seconds=plan.config.timeout_seconds,
            max_rows=plan.config.max_rows,
            max_bytes=plan.config.max_bytes,
        )
        payload: dict[str, Any] = {
            "database": plan.config.database,
            "row_count": result.row_count,
            "observation": {
                "max_rows": plan.config.max_rows,
                "max_bytes": plan.config.max_bytes,
                "size_bytes": result.size_bytes,
                "truncated": result.truncated,
                "truncation_reason": result.truncation_reason,
                "next_row_size_bytes": result.next_row_size_bytes,
                "total_rows_known": not result.truncated,
            },
        }
        if plan.config.fetch in {"all", "one"}:
            payload["rows"] = to_jsonable(result.rows)
            if plan.column_decoders:
                decoded_rows: list[dict[str, Any]] = []
                decode_errors: list[dict[str, Any]] = []
                for index, row in enumerate(result.rows):
                    decoded_row = dict(row)
                    for column, decoder in plan.column_decoders.items():
                        if column not in row or row[column] is None:
                            continue
                        try:
                            decoded_row[column] = decoder.decode(_column_bytes(row[column]))
                        except Exception:
                            decode_errors.append(
                                {
                                    "row": index,
                                    "column": column,
                                    "decoder_ref": plan.config.column_decoders[column],
                                    "code": "VALUE_DECODE_FAILED",
                                }
                            )
                    decoded_rows.append(to_jsonable(decoded_row))
                payload["decoded_rows"] = decoded_rows
                payload["decode_errors"] = decode_errors
        elif plan.config.fetch == "value":
            payload["value"] = result.value
        else:
            payload["command"] = result.command
        return payload

    async def aclose(self) -> None:
        await self._database.aclose()


def _column_bytes(value: Any) -> bytes:
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value)
    if isinstance(value, Mapping) and value.get("encoding") == "base64" and isinstance(value.get("data"), str):
        try:
            return base64.b64decode(value["data"], validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("column does not contain a valid binary envelope") from error
    raise ValueError("column is not binary")
