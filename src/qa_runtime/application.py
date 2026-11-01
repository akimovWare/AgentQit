"""Shared workspace execution for CLI and MCP; no interface imports."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio

from qa_runtime.adapters.credential_helper import UnavailableCredentialSource
from qa_runtime.adapters.environment import EnvironmentCredentialSource
from qa_runtime.adapters.vault import VaultCliCredentialSource
from qa_runtime.bootstrap import build_runtime
from qa_runtime.contracts import CommandResult, ExitCode, ResultStatus
from qa_runtime.credential_config import EnvironmentCredentialConfig
from qa_runtime.errors import RuntimeFailure
from qa_runtime.handlers.browser import BrowserOperationHandler
from qa_runtime.ports.http import HttpPort
from qa_runtime.security import SecretRedactor, redact_command_result
from qa_runtime.sessions import BrowserFactory, SessionManager
from qa_runtime.workspace import Workspace


def failure_result(command: str, error: Exception, started_at: datetime, started: float) -> CommandResult:
    failure = (
        error
        if isinstance(error, RuntimeFailure)
        else RuntimeFailure(
            ExitCode.INTERNAL_ERROR,
            "INTERNAL_ERROR",
            "An unexpected application error occurred",
            source="application",
        )
    )
    return redact_command_result(
        CommandResult.failure(
            command=command,
            run_id=f"run_{uuid.uuid4().hex}",
            started_at=started_at,
            finished_at=datetime.now(UTC),
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            exit_code=failure.exit_code,
            error=failure.as_error_item(),
            status=(
                ResultStatus.BLOCKED
                if failure.exit_code == ExitCode.BLOCKED
                else ResultStatus.TIMEOUT
                if failure.code.endswith("_TIMEOUT")
                else ResultStatus.FAILURE
            ),
        )
    )


def local_result(command: str, payload: Any, *, environment: str = "local") -> CommandResult:
    now = datetime.now(UTC)
    return redact_command_result(
        CommandResult.success(
            command=command,
            run_id=f"run_{uuid.uuid4().hex}",
            environment=environment,
            started_at=now,
            finished_at=now,
            duration_ms=0,
            result=payload,
        )
    )


class WorkspaceApplication:
    def __init__(
        self,
        workspace: Workspace,
        *,
        env: Mapping[str, str] | None = None,
        http_factory: Callable[[], HttpPort] | None = None,
        mutations_allowed: bool = False,
        browser_factory: BrowserFactory | None = None,
        session_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.workspace = workspace
        self._env, self._http_factory = env, http_factory
        self._mutations_allowed = mutations_allowed
        self._closed = False
        self.sessions = SessionManager(workspace, browser_factory=browser_factory, clock=session_clock)

        def components(profile, credentials, redactor):
            result = workspace.plugin.build_components(profile, credentials, redactor)
            if workspace.manifest.browser is not None:
                result.handlers.append(BrowserOperationHandler(self.sessions, credentials))
            return result

        self._plugin = replace(workspace.plugin, runtime_factory=components)

    def list_operations(self, query: str = "", *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        if not 1 <= limit <= 100 or offset < 0 or len(query) > 256:
            raise RuntimeFailure(ExitCode.INVALID_INPUT, "DISCOVERY_INPUT_INVALID", "Invalid search bounds")
        operations = [
            item
            for item in self.workspace.registry.operations()
            if query.casefold() in f"{item.id} {item.purpose}".casefold()
        ]
        return {
            "operations": [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "purpose": item.purpose,
                    "mutating": item.mutating,
                    "admission": item.admission,
                }
                for item in operations[offset : offset + limit]
            ],
            "total": len(operations),
            "offset": offset,
            "limit": limit,
        }

    def describe_operation(self, operation_id: str) -> dict[str, Any]:
        definition = self.workspace.registry.get(operation_id)
        return SecretRedactor().redact(definition.model_dump(mode="json", by_alias=True))

    async def plan(self, operation_id: str, params: dict[str, Any]) -> CommandResult:
        return await self._execute(operation_id, params, plan_only=True)

    async def check(self, operation_id: str) -> CommandResult:
        result = await self._execute(operation_id, {}, plan_only=True, validate_input=False)
        return result.model_copy(update={"command": f"doctor {operation_id}"})

    async def invoke(
        self,
        operation_id: str,
        params: dict[str, Any],
        *,
        allow_mutation: bool = False,
        session_id: str | None = None,
    ) -> CommandResult:
        return await self._execute(operation_id, params, allow_mutation=allow_mutation, session_id=session_id)

    async def capture(self, operation_id: str, *, session_id: str | None = None) -> CommandResult:
        return await self._execute(operation_id, {}, capture_only=True, session_id=session_id)

    async def open_session(self, *, browser: bool = False) -> CommandResult:
        started_at, started = datetime.now(UTC), time.monotonic()
        try:
            return local_result("session open", await self.sessions.open(browser=browser))
        except TimeoutError:
            return failure_result(
                "session open",
                RuntimeFailure(
                    ExitCode.ENVIRONMENT_UNAVAILABLE,
                    "BROWSER_START_TIMEOUT",
                    "Browser startup exceeded its budget",
                    source="browser",
                    execution_outcome="not_sent",
                ),
                started_at,
                started,
            )
        except Exception as error:
            return failure_result("session open", error, started_at, started)

    async def close_session(self, session_id: str) -> CommandResult:
        started_at, started = datetime.now(UTC), time.monotonic()
        try:
            payload = await self.sessions.close(session_id)
            result = local_result("session close", payload)
            result.warnings.extend(payload["warnings"])
            if result.warnings:
                result.status, result.exit_code = ResultStatus.PARTIAL, ExitCode.PARTIAL
            return result
        except Exception as error:
            return failure_result("session close", error, started_at, started)

    def _credentials(self):
        config = self.workspace.profile.credentials
        if config is None:
            return UnavailableCredentialSource()
        if isinstance(config, EnvironmentCredentialConfig):
            return EnvironmentCredentialSource(config, self._env)
        return VaultCliCredentialSource(config, Path("vault"))

    async def _execute(
        self,
        operation_id: str,
        params: dict[str, Any],
        *,
        allow_mutation: bool = False,
        plan_only: bool = False,
        validate_input: bool = True,
        capture_only: bool = False,
        session_id: str | None = None,
    ) -> CommandResult:
        started_at, started = datetime.now(UTC), time.monotonic()
        command = f"{'plan' if plan_only else 'checkpoint' if capture_only else 'invoke'} {operation_id}"
        runtime = None
        session = attempt = result = None
        executing, mutating = False, False
        try:
            if self._closed:
                raise RuntimeFailure(ExitCode.ENVIRONMENT_UNAVAILABLE, "APPLICATION_CLOSED", "Application is closed")
            definition = self.workspace.registry.get(operation_id)
            mutating = definition.mutating
            if mutating and not plan_only and not capture_only and not self._mutations_allowed:
                raise RuntimeFailure(
                    ExitCode.BLOCKED,
                    "HOST_MUTATION_BLOCKED",
                    "The application host has not enabled mutation for this task",
                    source="application",
                    execution_outcome="not_sent",
                    retry_advice="do_not_retry",
                )
            if session_id is not None and not plan_only:
                session = self.sessions.acquire(session_id)
            runtime = await build_runtime(
                self._plugin,
                self.workspace.profile,
                self.workspace.registry,
                self._credentials(),
                operation_ids=(operation_id,),
                http_port=self._http_factory() if self._http_factory else None,
                redactor=session.redactor if session is not None else None,
            )
            if plan_only:
                payload = {
                    "operation": operation_id,
                    "kind": definition.kind,
                    "mutating": definition.mutating,
                    "network_checked": False,
                }
                if validate_input:
                    payload["input"] = runtime.public_input(operation_id, runtime.validate_input(operation_id, params))
                result = CommandResult.success(
                    command=command,
                    run_id=f"run_{uuid.uuid4().hex}",
                    environment=self.workspace.profile.environment,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                    result=payload,
                )
            else:
                if session is not None:
                    if session.operation_count >= self.sessions.settings.max_operations:
                        raise RuntimeFailure(
                            ExitCode.BLOCKED,
                            "SESSION_OPERATION_LIMIT",
                            "Task operation limit reached",
                            execution_outcome="not_sent",
                        )
                    attempt = session.journal.begin(operation_id, runtime.public_input(operation_id, params))
                    session.operation_count += 1
                executing = True
                budget = self.workspace.manifest.limits.operation_timeout_seconds
                if session is not None:
                    budget = min(budget, self.sessions.remaining_seconds(session))
                with anyio.fail_after(budget):
                    result = await runtime.invoke(
                        operation_id,
                        params,
                        allow_mutation=allow_mutation,
                        capture_only=capture_only,
                        session_id=session_id,
                    )
        except TimeoutError:
            effect_possible = executing and mutating and allow_mutation and self.workspace.profile.environment == "qa"
            error = RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "OPERATION_TIMEOUT",
                "Operation exceeded its overall execution budget",
                source="application",
                execution_outcome="unknown" if effect_possible else (None if executing else "not_sent"),
                retry_advice="read_state_first" if effect_possible else ("safe" if not mutating else "do_not_retry"),
                retryable=not mutating,
            )
            result = failure_result(command, error, started_at, started)
            result.status = ResultStatus.TIMEOUT
        except Exception as error:
            result = failure_result(command, error, started_at, started)
        finally:
            if runtime is not None:
                try:
                    with anyio.CancelScope(shield=True):
                        await runtime.aclose()
                except BaseException:
                    # A cleanup problem does not erase a completed external action.
                    if result is not None:
                        result.warnings.append("Resource cleanup did not complete successfully")
            if session is not None:
                try:
                    if result is not None and attempt is not None:
                        result = redact_command_result(result, session.redactor)
                        try:
                            evidence = session.journal.finish(attempt, result)
                            result.evidence.append(str(evidence))
                        except RuntimeFailure:
                            result.warnings.append(
                                "Evidence persistence failed after execution; inspect state before repeating"
                            )
                finally:
                    self.sessions.release(session)
        return runtime.redact_result(result) if runtime is not None else redact_command_result(result)

    async def aclose(self) -> None:
        self._closed = True
        await self.sessions.aclose()
