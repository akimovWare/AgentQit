from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True, slots=True)
class DatabaseResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    value: Any = None
    command: str | None = None
    row_count: int = 0
    size_bytes: int | None = None
    truncated: bool = False
    truncation_reason: Literal["max_rows", "max_bytes"] | None = None
    next_row_size_bytes: int | None = None


class DatabasePort(Protocol):
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
    ) -> DatabaseResult: ...

    async def aclose(self) -> None: ...
