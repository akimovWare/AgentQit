from __future__ import annotations

from typing import Protocol

from qa_runtime.models import ExecutionContext, HttpRequest, HttpResult


class HttpPort(Protocol):
    async def send(self, request: HttpRequest, context: ExecutionContext) -> HttpResult: ...

    async def aclose(self) -> None: ...
