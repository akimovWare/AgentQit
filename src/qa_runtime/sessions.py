from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio

from qa_runtime.artifacts import ArtifactStore
from qa_runtime.browser_models import BrowserCommand, BrowserSettings, SessionSettings
from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.evidence import EvidenceScope, RunStore, content_digest
from qa_runtime.ports.browser import BrowserPort
from qa_runtime.ports.credentials import CredentialSource
from qa_runtime.security import SecretRedactor


@dataclass
class TaskSession:
    id: str
    created: float
    last_used: float
    directory: Path
    artifacts: ArtifactStore
    journal: RunStore
    redactor: SecretRedactor
    browser: BrowserPort | None = None
    status: str = "opening"
    active_calls: int = 0
    operation_count: int = 0
    browser_lock: Any = field(default_factory=anyio.Lock)
    cleanup_started: bool = False
    cleanup_done: Any = field(default_factory=anyio.Event)
    warnings: list[str] = field(default_factory=list)


BrowserFactory = Callable[[BrowserSettings, ArtifactStore, SecretRedactor], BrowserPort]


class SessionManager:
    def __init__(
        self,
        workspace,
        *,
        browser_factory: BrowserFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.workspace = workspace
        self.settings: SessionSettings = workspace.manifest.sessions
        self._factory, self._clock = browser_factory, clock
        self._sessions: dict[str, TaskSession] = {}
        self._closing = False

    def _root(self) -> Path:
        directory = self.workspace.path.parent
        for name in [".agentqit", "runs"]:
            directory = directory / name
            directory.mkdir(mode=0o700, exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise RuntimeFailure(
                    ExitCode.BLOCKED, "SESSION_STORAGE_INVALID", "Session storage must be a private directory"
                )
        return directory

    async def open(self, *, browser: bool = False) -> dict[str, Any]:
        await self.reap_expired()
        if self._closing:
            raise RuntimeFailure(ExitCode.BLOCKED, "SESSION_MANAGER_CLOSED", "Session manager is closing")
        active = sum(item.status in {"active", "opening"} for item in self._sessions.values())
        if active >= self.settings.max_active or len(self._sessions) >= self.settings.max_total:
            raise RuntimeFailure(ExitCode.BLOCKED, "SESSION_LIMIT", "Task session limit reached")
        config = self.workspace.manifest.browser
        if browser and config is None:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT, "BROWSER_NOT_CONFIGURED", "Configure allowed browser origins first"
            )
        session_id = f"session_{uuid.uuid4().hex}"
        directory = self._root() / session_id
        directory.mkdir(mode=0o700)
        artifacts = ArtifactStore(
            directory / "artifacts",
            session_id,
            max_bytes=self.settings.max_artifact_bytes,
            max_total_bytes=self.settings.max_total_artifact_bytes,
        )
        redactor = SecretRedactor()
        journal = RunStore(directory / "journal", redactor=redactor)
        try:
            journal.initialize(
                EvidenceScope(
                    product=self.workspace.manifest.name,
                    profile="workspace",
                    profile_digest=content_digest(self.workspace.profile.model_dump(mode="json", by_alias=True)),
                    registry_digest=content_digest(
                        [item.model_dump(mode="json", by_alias=True) for item in self.workspace.registry.operations()]
                    ),
                )
            )
        except BaseException:
            await artifacts.aclose()
            raise
        now = self._clock()
        session = TaskSession(session_id, now, now, directory, artifacts, journal, redactor)
        self._sessions[session_id] = session
        try:
            if browser:
                if self._factory is None:
                    from qa_runtime.adapters.playwright import PlaywrightBrowserAdapter

                    factory = PlaywrightBrowserAdapter
                else:
                    factory = self._factory
                session.browser = factory(config, artifacts, redactor)
                with anyio.fail_after(config.action_timeout_seconds):
                    await session.browser.start()
            session.status = "active"
            return self.info(session_id)
        except BaseException:
            session.status = "failed"
            with anyio.CancelScope(shield=True):
                await self._cleanup(session)
            raise

    def _lookup(self, session_id: str) -> TaskSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "SESSION_NOT_FOUND",
                "Unknown task session reference",
                execution_outcome="not_sent",
            )
        return session

    def _expired(self, session: TaskSession) -> bool:
        now = self._clock()
        return now - session.created >= self.settings.lifetime_seconds or (
            session.active_calls == 0 and now - session.last_used >= self.settings.idle_seconds
        )

    def acquire(self, session_id: str) -> TaskSession:
        session = self._lookup(session_id)
        if session.status != "active" or self._expired(session):
            if session.status == "active":
                session.status = "expired"
            raise RuntimeFailure(
                ExitCode.BLOCKED,
                "SESSION_EXPIRED" if session.status == "expired" else "SESSION_CLOSED",
                "Open a new session and observe state before acting",
                execution_outcome="not_sent",
                retry_advice="read_state_first",
            )
        session.active_calls += 1
        session.last_used = self._clock()
        return session

    def release(self, session: TaskSession) -> None:
        session.active_calls -= 1
        session.last_used = self._clock()

    def remaining_seconds(self, session: TaskSession) -> float:
        return max(0.001, self.settings.lifetime_seconds - (self._clock() - session.created))

    def info(self, session_id: str) -> dict[str, Any]:
        session = self._lookup(session_id)
        return {
            "session_id": session.id,
            "status": session.status,
            "browser": session.browser is not None,
            "journal": str(session.directory / "journal"),
            "artifacts": session.artifacts.list(),
            "warnings": list(session.warnings),
        }

    async def execute(
        self,
        session_id: str,
        command: BrowserCommand,
        credentials: CredentialSource,
        *,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        session = self.acquire(session_id)
        try:
            if session.browser is None:
                raise RuntimeFailure(
                    ExitCode.INVALID_INPUT, "BROWSER_SESSION_REQUIRED", "Open a browser-enabled session"
                )
            if session.browser_lock.locked():
                raise RuntimeFailure(
                    ExitCode.BLOCKED,
                    "BROWSER_SESSION_BUSY",
                    "Wait for the current browser action before observing again",
                    execution_outcome="not_sent",
                )
            async with session.browser_lock:
                secret = None
                if command.credential_ref is not None:
                    if command.credential_ref not in self.workspace.manifest.browser.credential_refs:
                        raise RuntimeFailure(
                            ExitCode.BLOCKED,
                            "BROWSER_CREDENTIAL_BLOCKED",
                            "The credential reference is not approved for browser input",
                            execution_outcome="not_sent",
                        )
                    secret = await credentials.get(command.credential_ref)
                    session.redactor.register(secret)
                budget = min(self.workspace.manifest.browser.action_timeout_seconds, self.remaining_seconds(session))
                try:
                    with anyio.fail_after(budget):
                        if command.action == "screenshot":
                            page_id, data = await session.browser.screenshot(command.page_id)
                            artifact = await session.artifacts.save(
                                data, "image/png", "browser-screenshot", run_id=run_id
                            )
                            return {"page_id": page_id, "artifact": artifact.model_dump(mode="json")}
                        return session.redactor.redact(await session.browser.execute(command, secret))
                except TimeoutError as error:
                    raise RuntimeFailure(
                        ExitCode.ENVIRONMENT_UNAVAILABLE,
                        "BROWSER_TIMEOUT",
                        "Browser action/observation exceeded its budget",
                        source="browser",
                        retry_advice="read_state_first",
                    ) from error
        finally:
            self.release(session)

    async def _cleanup(self, session: TaskSession) -> None:
        if session.cleanup_started:
            await session.cleanup_done.wait()
            return
        session.cleanup_started = True
        if session.browser is not None:
            try:
                with anyio.fail_after(10, shield=True):
                    await session.browser.aclose()
            except BaseException:
                session.warnings.append("Browser cleanup or trace retention did not complete")
        session.cleanup_done.set()

    async def close(self, session_id: str) -> dict[str, Any]:
        session = self._lookup(session_id)
        if session.active_calls:
            raise RuntimeFailure(ExitCode.BLOCKED, "SESSION_BUSY", "A session operation is still active")
        session.status = "closed"
        await self._cleanup(session)
        return self.info(session_id)

    async def reap_expired(self) -> None:
        for session in tuple(self._sessions.values()):
            if session.active_calls == 0 and (
                session.status == "expired" or (session.status == "active" and self._expired(session))
            ):
                session.status = "expired"
                await self._cleanup(session)

    async def sweep(self) -> None:
        while not self._closing:
            await anyio.sleep(1)
            await self.reap_expired()

    async def read_artifact(self, session_id: str, artifact_id: str):
        return await self._lookup(session_id).artifacts.read(artifact_id)

    def evidence(self, session_id: str, *, limit: int = 20, offset: int = 0, detail: bool = False) -> dict[str, Any]:
        session = self._lookup(session_id)
        if not 1 <= limit <= 100 or offset < 0:
            raise RuntimeFailure(ExitCode.INVALID_INPUT, "EVIDENCE_INPUT_INVALID", "Invalid evidence page bounds")
        records = session.journal.records()
        attempts = []
        for item in records[offset : offset + limit]:
            payload = item.model_dump(mode="json")
            if not detail:
                payload.pop("input", None)
                payload["input_keys"] = list(item.input)
                payload["result"] = (
                    None
                    if item.result is None
                    else {
                        "status": item.result.status,
                        "exit_code": item.result.exit_code,
                        "run_id": item.result.run_id,
                        "error": None
                        if item.result.error is None
                        else {
                            "code": item.result.error.code,
                            "execution_outcome": item.result.error.execution_outcome,
                            "retry_advice": item.result.error.retry_advice,
                        },
                    }
                )
            elif len(item.model_dump_json()) > 128 * 1024:
                payload.pop("input", None)
                payload["result"] = None
                payload["record_not_inlined"] = True
            attempts.append(payload)
        return {"session_id": session_id, "attempts": attempts, "total": len(records), "offset": offset, "limit": limit}

    async def aclose(self) -> None:
        self._closing = True

        async def close_one(session):
            if session.status in {"active", "opening"}:
                session.status = "closed"
            await self._cleanup(session)
            await session.artifacts.aclose()

        async with anyio.create_task_group() as tasks:
            for session in self._sessions.values():
                tasks.start_soon(close_one, session)
