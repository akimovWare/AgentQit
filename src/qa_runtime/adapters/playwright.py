"""Interactive Playwright context. SDK imports and browser launch are execution-only."""

from __future__ import annotations

import uuid
from collections import deque
from contextlib import suppress
from typing import Any

import anyio

from qa_runtime.artifacts import ArtifactStore
from qa_runtime.browser_models import BrowserCommand, BrowserSettings, http_origin
from qa_runtime.browser_models import safe_observation_url as safe_url
from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.security import SecretRedactor, SecretValue

_ELEMENTS = (
    "button,input,select,textarea,a[href],[role=button],[role=link],[role=checkbox],[role=textbox],[data-testid]"
)
_META = """el => {
    const tag = el.tagName.toLowerCase(), type = (el.getAttribute('type') || '').toLowerCase();
    const control = ['input','textarea','select'].includes(tag) || el.isContentEditable;
    const role = el.getAttribute('role') || ({button:'button',a:'link',select:'combobox',textarea:'textbox',
        input: type === 'checkbox' ? 'checkbox' : type === 'radio' ? 'radio' : 'textbox'}[tag] || 'element');
    const labelText = label => {
        const copy = label.cloneNode(true);
        copy.querySelectorAll('input,textarea,select,[contenteditable]').forEach(control=>control.remove());
        return copy.textContent;
    };
    const name = el.getAttribute('aria-label') ||
        (el.labels && Array.from(el.labels).map(labelText).join(' ')) ||
        (control ? el.getAttribute('placeholder') || el.getAttribute('name') || el.id : el.innerText) || '';
    return {tag, type, role, name:name.slice(0,1024), disabled:!!el.disabled};
}"""
_TEXT = r"""limit => {
    const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
    let node, text = '', scanned = 0, truncated = false;
    while ((node = walker.nextNode())) {
        if (++scanned > 20000) { truncated = true; break; }
        const parent = node.parentElement;
        if (!parent || parent.closest('script,style,noscript,input,textarea,select,[contenteditable],[hidden]'))
            continue;
        const style = getComputedStyle(parent);
        if (!parent.getClientRects().length || style.visibility === 'hidden' || style.display === 'none') continue;
        const part = (node.textContent || '').replace(/\s+/g,' ').trim();
        if (!part) continue;
        const addition = (text ? ' ' : '') + part;
        if (text.length + addition.length > limit) {
            text += addition.slice(0,limit-text.length); truncated=true; break;
        }
        text += addition;
    }
    return {text, truncated};
}"""


class PlaywrightBrowserAdapter:
    def __init__(self, settings: BrowserSettings, artifacts: ArtifactStore, redactor: SecretRedactor) -> None:
        self.settings, self.artifacts, self.redactor = settings, artifacts, redactor
        self._playwright = self._browser = self._context = None
        self._pages: dict[str, Any] = {}
        self._initial_page: str | None = None
        self._snapshots: dict[str, tuple[str, dict[str, Any]]] = {}
        self._logs: deque[dict[str, Any]] = deque(maxlen=100)
        self._blocked: deque[str] = deque(maxlen=100)
        self._log_count = self._blocked_count = 0
        self._trace_started = False
        self._closed = False
        self._timeout_error: type[Exception] = TimeoutError
        self._dialog_policy = "dismiss"

    async def start(self) -> None:
        if self._playwright is not None or self._closed:
            raise RuntimeFailure(ExitCode.INVALID_INPUT, "BROWSER_ALREADY_STARTED", "Create a new browser context")
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ImportError as error:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "BROWSER_DEPENDENCY_MISSING",
                "Install agentqit[browser] and provision a supported browser",
                source="browser",
            ) from error
        self._timeout_error = PlaywrightTimeoutError
        try:
            self._playwright = await async_playwright().start()
            browser_type = getattr(self._playwright, self.settings.browser)
            self._browser = await browser_type.launch(
                headless=self.settings.headless,
                timeout=self.settings.action_timeout_seconds * 1000,
            )
            self._context = await self._browser.new_context(
                viewport={"width": self.settings.viewport_width, "height": self.settings.viewport_height},
                ignore_https_errors=False,
                accept_downloads=False,
                service_workers="block",
            )
            self._context.set_default_timeout(self.settings.action_timeout_seconds * 1000)
            await self._context.route("**/*", self._route)
            await self._context.route_web_socket("**/*", self._route_websocket)
            if self.settings.trace:
                await self._context.tracing.start(screenshots=True, snapshots=True, sources=False)
                self._trace_started = True
            page = await self._context.new_page()
            self._initial_page = self._register(page)
            self._context.on("page", self._on_page)
        except BaseException as error:
            with suppress(BaseException):
                await self.aclose()
            if not isinstance(error, Exception):
                raise
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "BROWSER_START_FAILED",
                "Browser could not start; verify browser provisioning and local dependencies",
                source="browser",
            ) from error

    def _register(self, page) -> str:
        for page_id, known in self._pages.items():
            if known is page:
                return page_id
        page_id = f"page_{uuid.uuid4().hex}"
        self._pages[page_id] = page
        page.on("console", lambda message: self._log("console", message.text))
        page.on("pageerror", lambda error: self._log("pageerror", str(error)))
        page.on("response", lambda response: self._log("response", safe_url(response.url), response.status))
        page.on("close", lambda: self._forget(page_id))
        page.on("dialog", self._dismiss_dialog)
        return page_id

    async def _on_page(self, page) -> None:
        if len(self._pages) >= self.settings.max_pages:
            self._log("limit", "Page limit reached; the additional page was closed")
            with suppress(Exception):
                await page.close()
            return
        self._register(page)

    async def _dismiss_dialog(self, dialog) -> None:
        decision = self._dialog_policy
        self._log(f"dialog-{decision}", f"{dialog.type}: {dialog.message}")
        # A modal must not hang an operation or silently accept a consequential action.
        with suppress(Exception):
            await dialog.accept() if decision == "accept" else await dialog.dismiss()

    def _forget(self, page_id: str) -> None:
        self._pages.pop(page_id, None)
        self._snapshots.pop(page_id, None)

    def _log(self, kind: str, text: str, status: int | None = None) -> None:
        self._log_count += 1
        self._logs.append({"kind": kind, "text": self.redactor.redact_text(text)[:2048], "status": status})

    def _is_allowed(self, url: str) -> bool:
        try:
            return http_origin(url) in self.settings.allowed_origins
        except ValueError:
            return False

    def _blocked_request(self, url: str) -> None:
        self._blocked_count += 1
        self._blocked.append(safe_url(url))

    async def _route(self, route) -> None:
        if self._is_allowed(route.request.url):
            await route.continue_()
        else:
            self._blocked_request(route.request.url)
            await route.abort("blockedbyclient")

    async def _route_websocket(self, route) -> None:
        url = route.url
        target = "https:" + url[4:] if url.startswith("wss:") else "http:" + url[3:] if url.startswith("ws:") else url
        if self._is_allowed(target):
            route.connect_to_server()
        else:
            self._blocked_request(url)
            await route.close(code=1008, reason="Origin is outside the configured browser scope")

    def _page(self, page_id: str | None):
        selected = page_id or self._initial_page
        if self._closed or selected not in self._pages:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "BROWSER_PAGE_NOT_FOUND",
                "Unknown or closed page reference",
                source="browser",
                execution_outcome="not_sent",
            )
        return selected, self._pages[selected]

    async def _drop_snapshot(self, page_id: str) -> None:
        previous = self._snapshots.pop(page_id, None)
        if previous:
            for handle, _ in previous[1].values():
                with suppress(Exception):
                    await handle.dispose()

    async def _observe(self, page_id: str | None, offset: int = 0) -> dict[str, Any]:
        selected, page = self._page(page_id)
        await self._drop_snapshot(selected)
        snapshot_id = f"snapshot_{uuid.uuid4().hex}"
        locator = page.locator(_ELEMENTS)
        count = await locator.count()
        references, elements = {}, []
        self._snapshots[selected] = snapshot_id, references
        for index in range(offset, min(count, offset + self.settings.max_elements)):
            handle = await locator.nth(index).element_handle()
            if handle is None:
                continue
            try:
                if not await handle.is_visible():
                    await handle.dispose()
                    continue
                metadata = await handle.evaluate(_META)
            except BaseException:
                with suppress(Exception):
                    await handle.dispose()
                raise
            reference = f"element_{uuid.uuid4().hex}"
            references[reference] = (handle, metadata)
            elements.append({"ref": reference, **self.redactor.redact(metadata)})
        self._snapshots[selected] = snapshot_id, references
        text = await page.evaluate(_TEXT, self.settings.max_text_characters)
        return {
            "page_id": selected,
            "snapshot_id": snapshot_id,
            "url": safe_url(page.url),
            "title": self.redactor.redact_text(await page.title()),
            "text": self.redactor.redact_text(text["text"]),
            "text_truncated": text["truncated"],
            "elements": elements,
            "elements_truncated": offset > 0 or count > offset + self.settings.max_elements,
            "element_offset": offset,
            "element_total": count,
            "pages": [{"id": key, "url": safe_url(value.url)} for key, value in self._pages.items()],
            "diagnostics": list(self._logs),
            "diagnostics_truncated": self._log_count > len(self._logs),
            "blocked_requests": list(self._blocked),
            "blocked_requests_truncated": self._blocked_count > len(self._blocked),
            "content_trust": "untrusted",
        }

    async def execute(self, command: BrowserCommand, secret: SecretValue | None = None) -> dict[str, Any]:
        sent = False
        self._dialog_policy = command.dialog_policy
        try:
            if command.action == "observe":
                return await self._observe(command.page_id, command.offset)
            selected, page = self._page(command.page_id)
            if command.action == "navigate":
                if not self._is_allowed(command.url):
                    raise RuntimeFailure(
                        ExitCode.BLOCKED,
                        "BROWSER_ORIGIN_BLOCKED",
                        "Navigation target is outside browser scope",
                        source="browser",
                        execution_outcome="not_sent",
                    )
                await self._drop_snapshot(selected)
                sent = True
                response = await page.goto(command.url, wait_until="domcontentloaded")
                return {
                    "page_id": selected,
                    "url": safe_url(page.url),
                    "status_code": response.status if response is not None else None,
                }
            snapshot = self._snapshots.get(selected)
            if not snapshot or snapshot[0] != command.snapshot_id or command.element_ref not in snapshot[1]:
                raise RuntimeFailure(
                    ExitCode.INVALID_INPUT,
                    "BROWSER_STALE_REFERENCE",
                    "Observe the page again before selecting a target",
                    source="browser",
                    execution_outcome="not_sent",
                    retry_advice="read_state_first",
                )
            handle, metadata = snapshot[1][command.element_ref]
            if not await handle.evaluate("element => element.isConnected") or await handle.evaluate(_META) != metadata:
                await self._drop_snapshot(selected)
                raise RuntimeFailure(
                    ExitCode.INVALID_INPUT,
                    "BROWSER_STALE_REFERENCE",
                    "The observed element is detached",
                    source="browser",
                    execution_outcome="not_sent",
                    retry_advice="read_state_first",
                )
            if (
                command.action == "fill"
                and (await handle.get_attribute("type") or "").casefold() == "password"
                and secret is None
            ):
                raise RuntimeFailure(
                    ExitCode.BLOCKED,
                    "BROWSER_SECRET_REFERENCE_REQUIRED",
                    "Password fields require a credential reference",
                    source="browser",
                    execution_outcome="not_sent",
                )
            # References are single-observation capabilities, including after uncertain actions.
            previous = self._snapshots.pop(selected)
            try:
                sent = True
                if command.action == "click":
                    await handle.click()
                elif command.action == "fill":
                    if secret is not None:
                        self.redactor.register(secret)
                    await handle.fill(secret.reveal() if secret is not None else command.value)
                elif command.action == "select":
                    await handle.select_option(value=command.value)
                else:
                    raise RuntimeFailure(ExitCode.INVALID_INPUT, "BROWSER_ACTION_INVALID", "Unsupported browser action")
            finally:
                for item, _ in previous[1].values():
                    with suppress(Exception):
                        await item.dispose()
            return {"page_id": selected, "url": safe_url(page.url), "action": command.action, "observe_next": True}
        except RuntimeFailure:
            raise
        except Exception as error:
            timeout = isinstance(error, self._timeout_error)
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "BROWSER_TIMEOUT" if timeout else "BROWSER_ACTION_FAILED",
                "Browser observation/action did not complete",
                source="browser",
                execution_outcome=None if sent else "not_sent",
                retryable=False,
                retry_advice="read_state_first" if sent else "do_not_retry",
            ) from error
        finally:
            self._dialog_policy = "dismiss"

    async def screenshot(self, page_id: str | None = None) -> tuple[str, bytes]:
        selected, page = self._page(page_id)
        # Mask all inputs; this is a useful policy, not a claim of complete pixel sanitization.
        return selected, await page.screenshot(
            type="png",
            full_page=False,
            mask=[page.locator("input,textarea,[contenteditable]")],
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        failures = []
        if self._trace_started and self._context is not None:
            path = self.artifacts.temporary_path()
            try:
                with anyio.fail_after(2, shield=True):
                    await self._context.tracing.stop(path=path)
                    await self.artifacts.import_file(path, "application/zip", "playwright-trace")
            except BaseException:
                failures.append("trace")
                with suppress(OSError):
                    path.unlink()
        for resource, method in [(self._context, "close"), (self._browser, "close"), (self._playwright, "stop")]:
            if resource is not None:
                try:
                    with anyio.fail_after(2, shield=True):
                        await getattr(resource, method)()
                except BaseException:
                    failures.append("cleanup")
        self._pages.clear()
        self._snapshots.clear()
        if failures:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "BROWSER_CLEANUP_FAILED",
                "Browser cleanup or trace retention did not complete",
                source="browser",
            )
