from __future__ import annotations

import json

import anyio
import pytest

from qa_runtime.application import WorkspaceApplication
from qa_runtime.models import HttpResult
from qa_runtime.workspace import Workspace

pytestmark = pytest.mark.anyio


@pytest.fixture
def browser_workspace(tmp_path):
    (tmp_path / "agentqit.yaml").write_text(
        json.dumps(
            {
                "apiVersion": "agentqit/v1",
                "name": "sample",
                "registry": ["operations.yaml"],
                "browser": {"allowedOrigins": ["https://web.invalid"], "credentialRefs": ["ui/password"]},
                "sessions": {"maxActive": 2, "maxTotal": 4, "lifetimeSeconds": 100, "idleSeconds": 10},
            }
        )
    )
    (tmp_path / "profile.yaml").write_text(
        json.dumps(
            {
                "environment": "qa",
                "services": {"api": {"baseUrl": "https://api.invalid"}},
                "credentials": {
                    "provider": "env",
                    "bindings": {
                        "ui/password": "UI_PASSWORD",
                        "backend/key": "BACKEND_KEY",
                    },
                },
            }
        )
    )
    (tmp_path / "operations.yaml").write_text(
        json.dumps(
            {
                "apiVersion": "qa.runtime/v1",
                "kind": "OperationRegistry",
                "operations": [
                    {
                        "id": "items.list",
                        "purpose": "Read items",
                        "config": {"service": "api", "method": "GET", "path": "/items"},
                    }
                ],
            }
        )
    )
    return Workspace.load(tmp_path)


class FakeBrowser:
    def __init__(self, settings, artifacts, redactor):
        self.calls, self.closed = [], 0

    async def start(self):
        pass

    async def execute(self, command, secret=None):
        self.calls.append(command.action)
        return {
            "page_id": "page_example",
            "snapshot_id": "snapshot_example",
            "echo": secret.reveal() if secret else command.value,
        }

    async def screenshot(self, page_id=None):
        return "page_example", b"\x89PNG\r\n\x1a\nsynthetic"

    async def aclose(self):
        self.closed += 1


async def test_browser_and_backend_share_session_evidence_and_input_redaction(browser_workspace):
    browsers, backend_contexts = [], []

    def factory(*args):
        browser = FakeBrowser(*args)
        browsers.append(browser)
        return browser

    class Http:
        async def send(self, request, context):
            backend_contexts.append(context.session_id)
            return HttpResult(status_code=200, body={"items": []})

        async def aclose(self):
            pass

    app = WorkspaceApplication(
        browser_workspace,
        browser_factory=factory,
        http_factory=Http,
        mutations_allowed=True,
        env={"UI_PASSWORD": "synthetic-secret", "BACKEND_KEY": "not-for-ui"},
    )
    try:
        opened = await app.open_session(browser=True)
        assert opened.exit_code == 0, opened.model_dump_json()
        session_id = opened.result["session_id"]
        assert (await app.invoke("browser.observe", {}, session_id=session_id)).exit_code == 0
        data = {"snapshot_id": "snapshot_example", "element_ref": "element_example", "value": "ordinary text"}
        planned = await app.plan("browser.fill", data)
        assert planned.result["input"]["value"] == "***"
        filled = await app.invoke("browser.fill", data, session_id=session_id, allow_mutation=True)
        assert filled.exit_code == 0
        assert (await app.invoke("items.list", {}, session_id=session_id)).exit_code == 0
        assert backend_contexts == [session_id]
        records = app.sessions.evidence(session_id, detail=True)["attempts"]
        assert [item["operation_id"] for item in records] == ["browser.observe", "browser.fill", "items.list"]
        assert records[1]["input"]["value"] == "***"
        assert filled.evidence
        blocked = await app.invoke(
            "browser.fill",
            {
                "snapshot_id": "snapshot_example",
                "element_ref": "element_example",
                "credential_ref": "backend/key",
            },
            session_id=session_id,
            allow_mutation=True,
        )
        assert blocked.error.code == "BROWSER_CREDENTIAL_BLOCKED"
        filled = await app.invoke(
            "browser.fill",
            {
                "snapshot_id": "snapshot_example",
                "element_ref": "element_example",
                "credential_ref": "ui/password",
            },
            session_id=session_id,
            allow_mutation=True,
        )
        assert filled.exit_code == 0 and "synthetic-secret" not in filled.model_dump_json()
        shot = await app.invoke("browser.screenshot", {}, session_id=session_id)
        info, image = await app.sessions.read_artifact(session_id, shot.result["artifact"]["id"])
        assert info.session_id == session_id and image.startswith(b"\x89PNG")
        await app.close_session(session_id)
        assert browsers[0].closed == 1
        assert (await app.invoke("browser.observe", {}, session_id=session_id)).error.code == "SESSION_CLOSED"
    finally:
        await app.aclose()


async def test_sessions_expire_without_recreating_or_repeating_actions(browser_workspace):
    clock, browsers = [1.0], []

    def factory(*args):
        item = FakeBrowser(*args)
        browsers.append(item)
        return item

    app = WorkspaceApplication(browser_workspace, browser_factory=factory, session_clock=lambda: clock[0])
    try:
        first = (await app.open_session(browser=True)).result["session_id"]
        second = (await app.open_session(browser=True)).result["session_id"]
        assert (await app.open_session(browser=True)).error.code == "SESSION_LIMIT"
        assert first != second and len(browsers) == 2
        clock[0] += 11
        result = await app.invoke("browser.observe", {}, session_id=first)
        assert result.error.code == "SESSION_EXPIRED"
        await app.sessions.reap_expired()
        assert all(item.closed == 1 and item.calls == [] for item in browsers)
    finally:
        await app.aclose()


async def test_browser_timeout_preserves_uncertain_outcome_without_retry(browser_workspace):
    browser_workspace.manifest.browser.action_timeout_seconds = 0.02
    calls = []

    class HangingBrowser(FakeBrowser):
        async def execute(self, command, secret=None):
            calls.append(command.action)
            await anyio.sleep_forever()

    app = WorkspaceApplication(browser_workspace, browser_factory=HangingBrowser, mutations_allowed=True)
    try:
        session_id = (await app.open_session(browser=True)).result["session_id"]
        result = await app.invoke(
            "browser.click",
            {
                "snapshot_id": "snapshot_example",
                "element_ref": "element_example",
            },
            session_id=session_id,
            allow_mutation=True,
        )
        assert result.status == "timeout", result.model_dump_json()
        assert result.error.execution_outcome == "unknown" and result.error.retry_advice == "read_state_first"
        assert calls == ["click"]
        records = app.sessions.evidence(session_id, detail=True)["attempts"]
        assert records[0]["result"]["error"]["execution_outcome"] == "unknown"
    finally:
        await app.aclose()


async def test_compile_and_plan_do_not_launch_browser_or_create_session_storage(browser_workspace):
    def forbidden(*args):
        raise AssertionError("planning launched a browser")

    app = WorkspaceApplication(browser_workspace, browser_factory=forbidden)
    try:
        assert (await app.check("browser.observe")).exit_code == 0
        assert (await app.plan("browser.navigate", {"url": "https://web.invalid"})).exit_code == 0
        assert not (browser_workspace.path.parent / ".agentqit").exists()
    finally:
        await app.aclose()


async def test_parallel_browser_calls_are_blocked_without_a_second_action(browser_workspace):
    started, release = anyio.Event(), anyio.Event()
    calls = []

    class BusyBrowser(FakeBrowser):
        async def execute(self, command, secret=None):
            calls.append(command.action)
            started.set()
            await release.wait()
            return {"action": command.action}

    app = WorkspaceApplication(browser_workspace, browser_factory=BusyBrowser, mutations_allowed=True)
    try:
        sid = (await app.open_session(browser=True)).result["session_id"]

        async def first():
            await app.invoke(
                "browser.click",
                {
                    "snapshot_id": "snapshot_example",
                    "element_ref": "element_example",
                },
                allow_mutation=True,
                session_id=sid,
            )

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(first)
            await started.wait()
            rejected = await app.invoke("browser.observe", {}, session_id=sid)
            assert rejected.error.code == "BROWSER_SESSION_BUSY"
            assert rejected.error.execution_outcome == "not_sent"
            release.set()
        assert calls == ["click"]
    finally:
        await app.aclose()


async def test_interrupted_action_leaves_durable_pending_evidence(browser_workspace):
    started = anyio.Event()
    calls = []

    class InterruptedBrowser(FakeBrowser):
        async def execute(self, command, secret=None):
            calls.append(command.action)
            started.set()
            await anyio.sleep_forever()

    app = WorkspaceApplication(browser_workspace, browser_factory=InterruptedBrowser, mutations_allowed=True)
    try:
        sid = (await app.open_session(browser=True)).result["session_id"]

        async def action():
            await app.invoke(
                "browser.click",
                {
                    "snapshot_id": "snapshot_example",
                    "element_ref": "element_example",
                },
                allow_mutation=True,
                session_id=sid,
            )

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(action)
            await started.wait()
            tasks.cancel_scope.cancel()
        evidence = app.sessions.evidence(sid, detail=True)
        assert calls == ["click"] and evidence["attempts"][0]["result"] is None
        assert (await app.close_session(sid)).exit_code == 0
    finally:
        await app.aclose()


async def test_state_storage_rejects_symlinks_before_creating_records(browser_workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (browser_workspace.path.parent / ".agentqit").symlink_to(outside, target_is_directory=True)
    app = WorkspaceApplication(browser_workspace)
    try:
        result = await app.open_session()
        assert result.error.code == "SESSION_STORAGE_INVALID"
        assert list(outside.iterdir()) == []
    finally:
        await app.aclose()


async def test_browser_actions_require_host_and_caller_authorization(browser_workspace):
    browsers = []

    def factory(*args):
        browser = FakeBrowser(*args)
        browsers.append(browser)
        return browser

    app = WorkspaceApplication(browser_workspace, browser_factory=factory)
    try:
        sid = (await app.open_session(browser=True)).result["session_id"]
        result = await app.invoke(
            "browser.navigate", {"url": "https://web.invalid"}, allow_mutation=True, session_id=sid
        )
        assert result.error.code == "HOST_MUTATION_BLOCKED" and browsers[0].calls == []
    finally:
        await app.aclose()
