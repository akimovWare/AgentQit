from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from mcp.types import CallToolResult, TextContent
from pydantic import Field

from qa_runtime.application import failure_result, local_result
from qa_runtime.contracts import CommandResult

SessionId = Annotated[str, Field(pattern=r"^session_[0-9a-f]{32}$", max_length=80)]
OperationId = Annotated[str, Field(min_length=1, max_length=256)]


def tool_result(result: CommandResult) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=result.model_dump_json(by_alias=True, exclude_none=True))],
        structured_content=result.model_dump(mode="json", by_alias=True, exclude_none=True),
        is_error=result.exit_code != 0,
    )


def read_result(command: str, function: Callable[[], Any]) -> CallToolResult:
    started_at, started = datetime.now(UTC), time.monotonic()
    try:
        return tool_result(local_result(command, function()))
    except Exception as error:
        return tool_result(failure_result(command, error, started_at, started))
