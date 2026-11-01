from __future__ import annotations

from typing import Any, Protocol

from qa_runtime.browser_models import BrowserCommand
from qa_runtime.ports.credentials import CredentialSource
from qa_runtime.security import SecretValue


class BrowserPort(Protocol):
    """A task-owned context. Implementations never receive executable agent code."""

    async def start(self) -> None: ...

    async def execute(self, command: BrowserCommand, secret: SecretValue | None = None) -> dict[str, Any]: ...

    async def screenshot(self, page_id: str | None = None) -> tuple[str, bytes]: ...

    async def aclose(self) -> None: ...


class BrowserSessionPort(Protocol):
    async def execute(
        self,
        session_id: str,
        command: BrowserCommand,
        credentials: CredentialSource,
        *,
        run_id: str | None = None,
    ) -> dict[str, Any]: ...
