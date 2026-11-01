from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass

import anyio

from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure

ADAPTER_CLEANUP_TIMEOUT_SECONDS = 1.5


@dataclass(frozen=True, slots=True)
class CloseAttempt:
    close: Callable[[], Awaitable[None]]
    timeout_seconds: float | None = None
    fallback: Callable[[], None] | None = None


async def close_resources(attempts: Sequence[CloseAttempt], *, source: str, code: str) -> None:
    """Attempt every child close within one shielded adapter budget.

    Remaining time is divided between remaining children so one hanging SDK
    cannot consume every other child's opportunity to close. Timeout is a
    reported cleanup failure; it does not prove a driver released its resources.
    """
    first_error: BaseException | None = None
    with anyio.CancelScope(shield=True):
        deadline = anyio.current_time() + ADAPTER_CLEANUP_TIMEOUT_SECONDS
        for index, attempt in enumerate(attempts):
            remaining = max(0, deadline - anyio.current_time())
            budget = remaining / (len(attempts) - index)
            if attempt.timeout_seconds is not None:
                budget = min(budget, attempt.timeout_seconds)
            try:
                with anyio.move_on_after(budget) as cleanup:
                    await attempt.close()
                if cleanup.cancel_called:
                    raise TimeoutError("resource close exceeded its cleanup budget")
            except BaseException as error:
                first_error = first_error or error
                if attempt.fallback is not None:
                    with suppress(BaseException):
                        attempt.fallback()
    if first_error is not None:
        raise RuntimeFailure(
            ExitCode.ENVIRONMENT_UNAVAILABLE,
            code,
            f"{source} resources could not all be closed gracefully",
            source=source,
        ) from first_error
