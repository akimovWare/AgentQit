from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from qa_runtime.browser_models import BrowserCommand, safe_observation_url
from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import ExecutionContext, OperationDefinition, Profile
from qa_runtime.ports.browser import BrowserSessionPort
from qa_runtime.ports.credentials import CredentialSource


class BrowserOperationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["observe", "navigate", "click", "fill", "select", "screenshot"]


@dataclass(frozen=True)
class CompiledBrowserOperation:
    definition: OperationDefinition
    config: BrowserOperationConfig


class BrowserOperationHandler:
    kind = "browser"

    def __init__(self, sessions: BrowserSessionPort, credentials: CredentialSource) -> None:
        self.sessions, self.credentials = sessions, credentials

    def compile(self, operation: OperationDefinition, profile: Profile) -> CompiledBrowserOperation:
        try:
            config = BrowserOperationConfig.model_validate(operation.config)
            if operation.mutating != (config.action not in {"observe", "screenshot"}):
                raise ValueError("browser action mutation declaration is inconsistent")
            return CompiledBrowserOperation(operation, config)
        except (ValueError, ValidationError) as error:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "REGISTRY_INVALID",
                "Invalid browser capability declaration",
                source="registry",
            ) from error

    def validate_input(self, plan: CompiledBrowserOperation, params: dict[str, Any]) -> None:
        self._command(plan, params)

    @staticmethod
    def _command(plan, params) -> BrowserCommand:
        try:
            return BrowserCommand(action=plan.config.action, **params)
        except (ValueError, ValidationError, TypeError) as error:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "BROWSER_INPUT_INVALID",
                "Invalid typed browser action input",
                source="input",
                execution_outcome="not_sent",
            ) from error

    @staticmethod
    def redact_input(plan: CompiledBrowserOperation, params: dict[str, Any]) -> dict[str, Any]:
        result = dict(params)
        if plan.config.action == "fill" and "value" in result:
            result["value"] = "***"
        if plan.config.action == "navigate" and isinstance(result.get("url"), str):
            result["url"] = safe_observation_url(result["url"])
        return result

    async def execute(self, plan, params: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        if context.session_id is None:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "BROWSER_SESSION_REQUIRED",
                "Open a task session before browser interaction",
                execution_outcome="not_sent",
            )
        return await self.sessions.execute(
            context.session_id,
            self._command(plan, params),
            self.credentials,
            run_id=context.run_id,
        )

    async def aclose(self) -> None:
        # The application owns sessions across invocations; this handler only borrows them.
        pass
