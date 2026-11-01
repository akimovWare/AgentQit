from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from qa_runtime.models import ExecutionContext, HttpRequest


@dataclass(frozen=True, slots=True)
class AuthContext:
    execution: ExecutionContext
    operation_id: str
    service: str
    params: dict[str, Any]
    mutating: bool


class AuthProvider(Protocol):
    async def authorize(self, request: HttpRequest, context: AuthContext) -> HttpRequest: ...

    async def invalidate(self, context: AuthContext) -> None: ...

    async def aclose(self) -> None: ...
