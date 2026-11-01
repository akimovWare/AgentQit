from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("playwright")

from qa_runtime.adapters.playwright import PlaywrightBrowserAdapter
from qa_runtime.artifacts import ArtifactStore
from qa_runtime.browser_models import BrowserCommand, BrowserSettings
from qa_runtime.errors import RuntimeFailure
from qa_runtime.security import SecretRedactor, SecretValue

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.browser,
    pytest.mark.skipif(
        os.environ.get("AGENTQIT_BROWSER_TESTS") != "1", reason="Enable provisioned browser checks explicitly"
    ),
]

_HTML = b"""<!doctype html><html><head><title>Synthetic UI</title></head><body>
<label>Name <input id="name"></label><label>Password <input id="password" type="PASSWORD"></label>
<label>Notes<textarea>synthetic-hidden-input</textarea></label>
<button id="save" onclick="localStorage.setItem('touched','yes');
document.getElementById('state').textContent='touched'">
Save local</button><div id="state"></div>
<button id="confirm" onclick="if(confirm('Confirm update?'))document.getElementById('state').textContent='confirmed'">
Confirm update</button>
<script>document.getElementById('state').textContent=localStorage.getItem('touched')?'touched':'untouched';</script>
</body></html>"""


@pytest.fixture
def local_site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(_HTML)))
            self.end_headers()
            self.wfile.write(_HTML)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


async def test_native_browser_contexts_refs_screenshots_and_traces(tmp_path, local_site):
    first_store = ArtifactStore(tmp_path / "first", "session_first", max_bytes=10_000_000, max_total_bytes=20_000_000)
    second_store = ArtifactStore(
        tmp_path / "second", "session_second", max_bytes=10_000_000, max_total_bytes=20_000_000
    )
    first = PlaywrightBrowserAdapter(
        BrowserSettings(allowedOrigins=[local_site], trace=True), first_store, SecretRedactor()
    )
    second = PlaywrightBrowserAdapter(BrowserSettings(allowedOrigins=[local_site]), second_store, SecretRedactor())
    try:
        await first.start()
        await first.execute(BrowserCommand(action="navigate", url=local_site))
        snapshot = await first.execute(BrowserCommand(action="observe"))
        assert "synthetic-hidden-input" not in str(snapshot)
        password = next(item for item in snapshot["elements"] if item["name"].strip() == "Password")
        with pytest.raises(RuntimeFailure) as caught:
            await first.execute(
                BrowserCommand(
                    action="fill",
                    elementRef=password["ref"],
                    snapshotId=snapshot["snapshot_id"],
                    value="not-a-reference",
                )
            )
        assert caught.value.code == "BROWSER_SECRET_REFERENCE_REQUIRED"
        await first.execute(
            BrowserCommand(
                action="fill",
                elementRef=password["ref"],
                snapshotId=snapshot["snapshot_id"],
                credentialRef="ui/password",
            ),
            SecretValue("synthetic-password"),
        )
        snapshot = await first.execute(BrowserCommand(action="observe"))
        assert "synthetic-password" not in str(snapshot)
        save = next(item for item in snapshot["elements"] if item["name"].strip() == "Save local")
        await first.execute(BrowserCommand(action="click", elementRef=save["ref"], snapshotId=snapshot["snapshot_id"]))
        with pytest.raises(RuntimeFailure) as caught:
            await first.execute(
                BrowserCommand(action="click", elementRef=save["ref"], snapshotId=snapshot["snapshot_id"])
            )
        assert caught.value.code == "BROWSER_STALE_REFERENCE"
        await first.execute(BrowserCommand(action="navigate", url=local_site))
        assert "untouched" not in (await first.execute(BrowserCommand(action="observe")))["text"]
        await second.start()
        await second.execute(BrowserCommand(action="navigate", url=local_site))
        assert "untouched" in (await second.execute(BrowserCommand(action="observe")))["text"]
        page_id, image = await first.screenshot()
        assert page_id.startswith("page_") and image.startswith(b"\x89PNG\r\n\x1a\n")
        info = await first_store.save(image, "image/png", "screenshot")
        assert (await first_store.read(info.id))[1] == image
        with pytest.raises(RuntimeFailure) as caught:
            await first.execute(BrowserCommand(action="navigate", url="https://outside.invalid"))
        assert caught.value.code == "BROWSER_ORIGIN_BLOCKED"
        await first.aclose()
        assert any(item["media_type"] == "application/zip" for item in first_store.list())
    finally:
        await first.aclose()
        await second.aclose()
        await first_store.aclose()
        await second_store.aclose()


async def test_semantically_changed_target_is_rejected_without_clicking(tmp_path, local_site):
    store = ArtifactStore(tmp_path / "artifacts", "session_test", max_bytes=10_000_000, max_total_bytes=20_000_000)
    browser = PlaywrightBrowserAdapter(BrowserSettings(allowedOrigins=[local_site]), store, SecretRedactor())
    try:
        await browser.start()
        await browser.execute(BrowserCommand(action="navigate", url=local_site))
        observed = await browser.execute(BrowserCommand(action="observe"))
        target = next(item for item in observed["elements"] if item["name"].strip() == "Save local")
        # Simulate an application update, not an agent-provided script capability.
        page = browser._pages[observed["page_id"]]
        await page.locator("#save").evaluate("element => element.textContent = 'Different action'")
        with pytest.raises(RuntimeFailure) as caught:
            await browser.execute(
                BrowserCommand(
                    action="click",
                    elementRef=target["ref"],
                    snapshotId=observed["snapshot_id"],
                )
            )
        assert caught.value.code == "BROWSER_STALE_REFERENCE"
        assert caught.value.execution_outcome == "not_sent"
        assert "untouched" in (await browser.execute(BrowserCommand(action="observe")))["text"]
        observed = await browser.execute(BrowserCommand(action="observe"))
        confirm = next(item for item in observed["elements"] if item["name"].strip() == "Confirm update")
        await browser.execute(
            BrowserCommand(
                action="click",
                elementRef=confirm["ref"],
                snapshotId=observed["snapshot_id"],
            )
        )
        observed = await browser.execute(BrowserCommand(action="observe"))
        assert "untouched" in observed["text"]
        assert any(item["kind"] == "dialog-dismiss" for item in observed["diagnostics"])
        confirm = next(item for item in observed["elements"] if item["name"].strip() == "Confirm update")
        await browser.execute(
            BrowserCommand(
                action="click",
                elementRef=confirm["ref"],
                snapshotId=observed["snapshot_id"],
                dialogPolicy="accept",
            )
        )
        assert "confirmed" in (await browser.execute(BrowserCommand(action="observe")))["text"]
    finally:
        await browser.aclose()
        await store.aclose()
