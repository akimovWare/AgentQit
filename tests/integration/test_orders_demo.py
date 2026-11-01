from __future__ import annotations

import importlib.util
import json
import os
import re
import threading
from pathlib import Path

import anyio
import pytest

pytest.importorskip("playwright")

from mcp import Client

from qa_mcp.server import create_server

ROOT = Path(__file__).parents[2]
EXAMPLE = ROOT / "examples" / "orders" / "src" / "qa_product_orders"
pytestmark = [
    pytest.mark.anyio,
    pytest.mark.browser,
    pytest.mark.skipif(os.environ.get("AGENTQIT_BROWSER_TESTS") != "1", reason="Enable provisioned browser acceptance"),
]


@pytest.fixture(params=[False, True], ids=["correct-ui", "deliberate-ui-defect"])
def demo_workspace(tmp_path, request, monkeypatch):
    spec = importlib.util.spec_from_file_location("orders_demo_fixture", EXAMPLE / "demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = module.create_server(0, defect_ui=request.param, data_file=tmp_path / "records.json")
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    (tmp_path / "agentqit.yaml").write_text(
        json.dumps(
            {
                "apiVersion": "agentqit/v1",
                "name": "orders",
                "registry": ["operations.yaml"],
                "browser": {"allowedOrigins": [origin], "trace": True},
            }
        )
    )
    (tmp_path / "operations.yaml").write_bytes((EXAMPLE / "operations.yaml").read_bytes())
    (tmp_path / "profile.yaml").write_text(
        json.dumps(
            {
                "environment": "qa",
                "services": {"orders": {"baseUrl": origin}},
            }
        )
    )
    try:
        yield tmp_path, origin, request.param
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


async def call(client, name, arguments):
    result = await client.call_tool(name, arguments)
    assert not result.is_error, result.structured_content
    return result.structured_content["result"]


async def test_agent_protocol_correlates_ui_api_and_retained_evidence(demo_workspace):
    workspace, origin, defect = demo_workspace
    server = create_server(workspace, mutations_allowed=True)
    with anyio.fail_after(180):
        async with Client(server, read_timeout_seconds=90) as client:
            session = await call(client, "open_session", {"browser": True})
            sid = session["session_id"]
            await call(
                client,
                "browser_action",
                {
                    "session_id": sid,
                    "command": {"action": "navigate", "url": origin},
                    "allow_mutation": True,
                },
            )
            observed = await call(client, "observe_page", {"session_id": sid})
            title = next(item for item in observed["elements"] if item["name"].strip() == "Order title")
            await call(
                client,
                "browser_action",
                {
                    "session_id": sid,
                    "allow_mutation": True,
                    "command": {
                        "action": "fill",
                        "snapshot_id": observed["snapshot_id"],
                        "element_ref": title["ref"],
                        "value": "Agent protocol sample",
                    },
                },
            )
            observed = await call(client, "observe_page", {"session_id": sid})
            create = next(item for item in observed["elements"] if item["name"].strip() == "Create order")
            await call(
                client,
                "browser_action",
                {
                    "session_id": sid,
                    "allow_mutation": True,
                    "command": {
                        "action": "click",
                        "snapshot_id": observed["snapshot_id"],
                        "element_ref": create["ref"],
                    },
                },
            )
            for _ in range(30):
                data = await call(client, "observe_operation", {"session_id": sid, "operation_id": "orders.list"})
                if data["body"]["orders"]:
                    break
                await anyio.sleep(0.05)
            orders = data["body"]["orders"]
            assert len(orders) == 1 and orders[0]["title"] == "Agent protocol sample"
            order_id = orders[0]["id"]
            assert orders[0]["status"] == "CREATED"
            for _ in range(30):
                observed = await call(client, "observe_page", {"session_id": sid})
                cancel = next((item for item in observed["elements"] if item["name"].startswith("Cancel order ")), None)
                if cancel is not None:
                    break
                await anyio.sleep(0.05)
            assert cancel is not None
            await call(
                client,
                "browser_action",
                {
                    "session_id": sid,
                    "allow_mutation": True,
                    "command": {
                        "action": "click",
                        "snapshot_id": observed["snapshot_id"],
                        "element_ref": cancel["ref"],
                    },
                },
            )
            for _ in range(30):
                api = await call(
                    client,
                    "observe_operation",
                    {
                        "session_id": sid,
                        "operation_id": "orders.get",
                        "inputs": {"order-id": order_id},
                    },
                )
                observed = await call(client, "observe_page", {"session_id": sid})
                if api["body"]["status"] == "CANCELLED" and "Cancellation request completed." in observed["text"]:
                    break
                await anyio.sleep(0.05)
            assert api["body"]["status"] == "CANCELLED"
            ui_cancelled = bool(re.search(re.escape(order_id) + r"\s+CANCELLED\b", observed["text"]))
            assert ui_cancelled is not defect
            shot = await client.call_tool("screenshot", {"session_id": sid})
            assert not shot.is_error and any(item.type == "image" for item in shot.content)
            evidence = await call(client, "read_evidence", {"session_id": sid, "detail": True, "limit": 100})
            assert any(item["operation_id"] == "orders.get" for item in evidence["attempts"])
            fills = [item for item in evidence["attempts"] if item["operation_id"] == "browser.fill"]
            assert fills[0]["input"]["value"] == "***"
            closed = await call(client, "close_session", {"session_id": sid})
            assert closed["status"] == "closed"
            assert any(item["media_type"] == "application/zip" for item in closed["artifacts"])
    retained = json.loads((workspace / "records.json").read_text())
    assert retained["orders"][order_id]["status"] == "CANCELLED"
