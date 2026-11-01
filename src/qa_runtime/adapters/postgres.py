from __future__ import annotations

import json
from collections.abc import AsyncIterable, Mapping
from contextlib import aclosing, suppress
from typing import Any, Literal

import anyio
import asyncpg

from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.lifecycle import CloseAttempt, close_resources
from qa_runtime.models import DatabaseServiceConfig
from qa_runtime.ports.credentials import CredentialSource
from qa_runtime.ports.database import DatabaseResult
from qa_runtime.ports.tls import TlsProvider
from qa_runtime.security import SecretRedactor
from qa_runtime.serialization import to_jsonable


class AsyncpgAdapter:
    def __init__(
        self,
        databases: Mapping[str, DatabaseServiceConfig],
        credentials: CredentialSource,
        redactor: SecretRedactor,
        *,
        tls_provider: TlsProvider | None = None,
    ) -> None:
        self._databases = dict(databases)
        self._credentials = credentials
        self._redactor = redactor
        self._tls_provider = tls_provider
        self._pools: dict[str, asyncpg.Pool] = {}
        self._pool_lock = anyio.Lock()

    async def execute(
        self,
        database: str,
        statement: str,
        args: tuple[Any, ...],
        *,
        fetch: Literal["all", "one", "value", "none"],
        mutating: bool,
        timeout_seconds: float | None,
        max_rows: int = 1000,
        max_bytes: int = 5 * 1024 * 1024,
    ) -> DatabaseResult:
        config = self._database(database)
        timeout = timeout_seconds or config.command_timeout_seconds
        action_started = False
        try:
            pool = await self._pool_for(database, config)
            async with (
                pool.acquire(timeout=config.connect_timeout_seconds) as connection,
                connection.transaction(readonly=not mutating),
            ):
                if config.schema_name is not None:
                    await connection.fetchval(
                        "SELECT pg_catalog.set_config('search_path', pg_catalog.quote_ident($1), true)",
                        config.schema_name,
                        timeout=timeout,
                    )
                action_started = True
                if fetch == "all":
                    with anyio.fail_after(timeout):
                        return await self._bounded_rows(
                            self._record_stream(
                                connection, statement, args, timeout=timeout, mutating=mutating, max_rows=max_rows
                            ),
                            max_rows=max_rows,
                            max_bytes=max_bytes,
                        )
                if fetch == "one":
                    record = await connection.fetchrow(statement, *args, timeout=timeout)
                    rows = [] if record is None else [to_jsonable(dict(record))]
                    size = sum(self._size(row) for row in rows)
                    if size > max_bytes:
                        return DatabaseResult(truncated=True, truncation_reason="max_bytes", next_row_size_bytes=size)
                    return DatabaseResult(rows=rows, row_count=len(rows), size_bytes=size)
                if fetch == "value":
                    value = await connection.fetchval(statement, *args, timeout=timeout)
                    value = to_jsonable(value)
                    size = self._size(value)
                    if size > max_bytes:
                        return DatabaseResult(truncated=True, truncation_reason="max_bytes", next_row_size_bytes=size)
                    return DatabaseResult(value=value, row_count=0 if value is None else 1, size_bytes=size)
                command = await connection.execute(statement, *args, timeout=timeout)
                return DatabaseResult(command=command, row_count=self._affected_rows(command))
        except RuntimeFailure as error:
            if not action_started:
                error.execution_outcome = "not_sent"
            raise
        except TimeoutError as error:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "DB_TIMEOUT",
                f"database '{database}' did not respond in time",
                source="database",
                retryable=True,
                execution_outcome=None if action_started else "not_sent",
            ) from error
        except (asyncpg.InvalidPasswordError, asyncpg.InvalidAuthorizationSpecificationError) as error:
            raise RuntimeFailure(
                ExitCode.AUTHENTICATION_FAILED,
                "DATABASE_AUTH_FAILED",
                f"database '{database}' rejected authentication",
                source="database",
                details={"sqlstate": error.sqlstate},
                execution_outcome=None if action_started else "not_sent",
            ) from error
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as error:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "DB_UNAVAILABLE",
                f"database '{database}' request failed",
                source="database",
                retryable=True,
                details={"sqlstate": error.sqlstate} if isinstance(error, asyncpg.PostgresError) else None,
                execution_outcome=None if action_started else "not_sent",
            ) from error

    async def aclose(self) -> None:
        pools = list(self._pools.items())
        self._pools.clear()
        await close_resources(
            [
                CloseAttempt(pool.close, self._database(database).connect_timeout_seconds, pool.terminate)
                for database, pool in pools
            ],
            source="database",
            code="DB_CLOSE_FAILED",
        )

    async def _pool_for(self, database: str, config: DatabaseServiceConfig) -> asyncpg.Pool:
        pool = self._pools.get(database)
        if pool is not None:
            return pool
        async with self._pool_lock:
            pool = self._pools.get(database)
            if pool is not None:
                return pool
            username = await self._credentials.get(config.username_ref)
            self._redactor.register(username)
            password = await self._credentials.get(config.password_ref)
            self._redactor.register(password)
            options: dict[str, Any] = {}
            if config.schema_name:
                options["server_settings"] = {"search_path": config.schema_name}
            if config.tls_ref is not None:
                if self._tls_provider is None:
                    raise RuntimeFailure(
                        ExitCode.ENVIRONMENT_UNAVAILABLE,
                        "TLS_PROVIDER_NOT_FOUND",
                        f"database '{database}' requires tlsRef '{config.tls_ref}'",
                        source="database",
                    )
                options["ssl"] = self._tls_provider.ssl_context(config.tls_ref)
            created_pool = asyncpg.create_pool(
                dsn=config.dsn,
                user=username.reveal(),
                password=password.reveal(),
                min_size=config.min_pool_size,
                max_size=config.max_pool_size,
                timeout=config.connect_timeout_seconds,
                command_timeout=config.command_timeout_seconds,
                **options,
            )
            try:
                with anyio.fail_after(config.connect_timeout_seconds):
                    pool = await created_pool
            except BaseException:
                # create_pool returns an awaitable Pool. Its initialization can
                # have opened connections before another connection fails.
                with suppress(Exception):
                    created_pool.terminate()
                raise
            if pool is None:  # pragma: no cover - asyncpg type guard
                raise RuntimeFailure(
                    ExitCode.ENVIRONMENT_UNAVAILABLE,
                    "DB_UNAVAILABLE",
                    f"database '{database}' pool could not be created",
                    source="database",
                )
            self._pools[database] = pool
            return pool

    def _database(self, reference: str) -> DatabaseServiceConfig:
        try:
            return self._databases[reference]
        except KeyError as error:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "DATABASE_NOT_CONFIGURED",
                f"database '{reference}' is not configured",
                source="database",
            ) from error

    @staticmethod
    def _affected_rows(command: str) -> int:
        tail = command.rsplit(" ", maxsplit=1)[-1]
        return int(tail) if tail.isdigit() else 0

    @staticmethod
    def _size(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    @staticmethod
    async def _record_stream(connection, statement, args, *, timeout, mutating, max_rows) -> AsyncIterable:
        if mutating:
            # Complete RETURNING before restricting observations: interrupting a
            # mutating portal must not change the registered statement's effect.
            for record in await connection.fetch(statement, *args, timeout=timeout):
                yield record
        else:
            async for record in connection.cursor(statement, *args, prefetch=min(50, max_rows + 1), timeout=timeout):
                yield record

    @classmethod
    async def _bounded_rows(cls, records: AsyncIterable, *, max_rows: int, max_bytes: int) -> DatabaseResult:
        rows: list[dict[str, Any]] = []
        size_bytes = 0
        async with aclosing(records):
            async for record in records:
                row = to_jsonable(dict(record))
                row_size = cls._size(row)
                reason = (
                    "max_rows" if len(rows) >= max_rows else "max_bytes" if size_bytes + row_size > max_bytes else None
                )
                if reason is not None:
                    return DatabaseResult(
                        rows=rows,
                        row_count=len(rows),
                        size_bytes=size_bytes,
                        truncated=True,
                        truncation_reason=reason,
                        next_row_size_bytes=row_size,
                    )
                rows.append(row)
                size_bytes += row_size
        return DatabaseResult(rows=rows, row_count=len(rows), size_bytes=size_bytes)
