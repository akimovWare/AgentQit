from __future__ import annotations

import time

import anyio
import pytest

import qa_runtime.engine as engine_module
from qa_runtime.bootstrap import _CloseOncePort
from qa_runtime.compiler import CompiledCatalog
from qa_runtime.engine import QaRuntime
from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import Profile

pytestmark = pytest.mark.anyio


class FakeResource:
    def __init__(self, *, hang=False, error=None):
        self.started = 0
        self.finished = 0
        self.hang = hang
        self.error = error

    async def aclose(self):
        self.started += 1
        await anyio.lowlevel.checkpoint()
        if self.error is not None:
            raise self.error
        if self.hang:
            await anyio.sleep_forever()
        self.finished += 1


def runtime(*resources):
    return QaRuntime(CompiledCatalog(Profile(), {}, tuple(resources)))


async def test_successful_runtime_cleanup_is_shielded_from_caller_cancellation():
    first, second = FakeResource(), FakeResource()
    selected = runtime(_CloseOncePort(first), _CloseOncePort(second))
    with anyio.CancelScope() as scope:
        scope.cancel()
        await selected.aclose()
    assert first.started == first.finished == 1
    assert second.started == second.finished == 1


async def test_hanging_cleanup_has_own_finite_budget_and_does_not_skip_remaining_ports(monkeypatch):
    monkeypatch.setattr(engine_module, "_CLEANUP_TIMEOUT_SECONDS", 0.02, raising=False)
    first, second = FakeResource(hang=True), FakeResource()
    selected = runtime(_CloseOncePort(first), _CloseOncePort(second))
    started = time.monotonic()
    try:
        with anyio.fail_after(0.5):
            await selected.aclose()
    except RuntimeFailure:
        pass
    assert time.monotonic() - started < 0.5
    assert first.started == 1
    assert first.finished == 0
    assert second.started == second.finished == 1


async def test_shared_port_is_closed_once_when_referenced_by_two_handlers():
    shared = FakeResource()
    lease = _CloseOncePort(shared)
    selected = runtime(lease, lease)
    await selected.aclose()
    await selected.aclose()
    assert shared.started == shared.finished == 1


async def test_one_failing_cleanup_does_not_skip_other_resources():
    first, second = FakeResource(error=ValueError("synthetic close error")), FakeResource()
    selected = runtime(_CloseOncePort(first), _CloseOncePort(second))
    with pytest.raises((RuntimeFailure, ValueError)):
        await selected.aclose()
    assert first.started == 1
    assert second.started == second.finished == 1
