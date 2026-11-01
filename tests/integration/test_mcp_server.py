from __future__ import annotations

import json
import sys

import anyio
import pytest

pytest.importorskip("mcp")

from mcp import Client, StdioServerParameters

from qa_mcp.server import create_server
from qa_runtime.application import WorkspaceApplication
from qa_runtime.models import HttpResult

pytestmark = pytest.mark.anyio


@pytest.fixture
def workspace_files(tmp_path):
    (tmp_path / "agentqit.yaml").write_text(
        json.dumps(
            {
                "apiVersion": "agentqit/v1",
                "name": "sample",
                "registry": ["operations.yaml"],
            }
        )
    )
    (tmp_path / "profile.yaml").write_text(
        json.dumps(
            {
                "environment": "qa",
                "services": {"api": {"baseUrl": "https://api.invalid"}},
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
                    },
                    {
                        "id": "items.create",
                        "purpose": "Create an item",
                        "mutating": True,
                        "config": {"service": "api", "method": "POST", "path": "/items"},
                    },
                ],
            }
        )
    )
    return tmp_path


async def test_memory_client_discovery_execution_and_host_mutation_admission(workspace_files):
    calls, applications = [], []

    class Port:
        async def send(self, request, context):
            calls.append(request)
            return HttpResult(status_code=200, body={"items": []})

        async def aclose(self):
            pass

    def factory(workspace, allowed):
        app = WorkspaceApplication(workspace, http_factory=Port, mutations_allowed=allowed)
        applications.append(app)
        return app

    server = create_server(workspace_files, application_factory=factory)
    async with Client(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert tools["observe_operation"].annotations.read_only_hint is True
        assert tools["invoke_operation"].annotations.destructive_hint is True
        assert "ctx" not in tools["plan_operation"].input_schema.get("properties", {})
        search = await client.call_tool("search_operations", {"query": "items", "limit": 1})
        assert search.structured_content["result"]["total"] == 2
        planned = await client.call_tool("plan_operation", {"operation_id": "items.list"})
        assert planned.structured_content["result"]["network_checked"] is False and calls == []
        observed = await client.call_tool("observe_operation", {"operation_id": "items.list"})
        assert observed.structured_content["result"]["status_code"] == 200
        rejected = await client.call_tool("observe_operation", {"operation_id": "items.create"})
        assert rejected.is_error and rejected.structured_content["error"]["code"] == "READ_ONLY_TOOL"
        rejected = await client.call_tool(
            "invoke_operation",
            {
                "operation_id": "items.create",
                "allow_mutation": True,
            },
        )
        assert rejected.is_error and rejected.structured_content["error"]["code"] == "HOST_MUTATION_BLOCKED"
        assert len(calls) == 1
    assert (await applications[0].invoke("items.list", {})).error.code == "APPLICATION_CLOSED"


async def test_host_and_caller_can_authorize_a_fake_qa_write(workspace_files):
    calls = []

    class Port:
        async def send(self, request, context):
            calls.append(request.method)
            return HttpResult(status_code=201, body={"id": "synthetic-item"})

        async def aclose(self):
            pass

    server = create_server(
        workspace_files,
        mutations_allowed=True,
        application_factory=lambda ws, allowed: WorkspaceApplication(ws, http_factory=Port, mutations_allowed=allowed),
    )
    async with Client(server) as client:
        denied = await client.call_tool("invoke_operation", {"operation_id": "items.create"})
        assert denied.is_error and calls == []
        result = await client.call_tool(
            "invoke_operation",
            {
                "operation_id": "items.create",
                "allow_mutation": True,
            },
        )
        assert not result.is_error and result.structured_content["result"]["status_code"] == 201
        assert calls == ["POST"]


async def test_actual_stdio_process_keeps_protocol_stdout_clean(workspace_files):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "qa_mcp", "--workspace", str(workspace_files)],
        cwd=workspace_files,
    )
    with anyio.fail_after(120):
        async with Client(parameters, read_timeout_seconds=90) as client:
            result = await client.call_tool("plan_operation", {"operation_id": "items.list"})
            assert not result.is_error
            assert result.structured_content["schema_version"] == "1.0"
            assert result.structured_content["result"]["network_checked"] is False


@pytest.mark.parametrize("browser_enabled", [False, True])
async def test_mcp_advertises_only_configured_browser_capabilities(workspace_files, browser_enabled):
    manifest_path = workspace_files / "agentqit.yaml"
    manifest = json.loads(manifest_path.read_text())
    if browser_enabled:
        manifest["browser"] = {"allowedOrigins": ["https://ui.invalid"]}
        manifest_path.write_text(json.dumps(manifest))
    async with Client(create_server(workspace_files)) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert len(names) == (13 if browser_enabled else 10)
        browser_names = {"observe_page", "browser_action", "screenshot"}
        assert browser_names.intersection(names) == (browser_names if browser_enabled else set())
        # Backend sessions and evidence remain useful with browser tools absent.
        opened = await client.call_tool("open_session", {})
        assert not opened.is_error
        sid = opened.structured_content["result"]["session_id"]
        evidence = await client.call_tool("read_evidence", {"session_id": sid})
        assert not evidence.is_error
        assert not (await client.call_tool("close_session", {"session_id": sid})).is_error


async def test_catalog_growth_does_not_grow_mcp_definitions(workspace_files):
    first = await create_server(workspace_files).list_tools()
    path = workspace_files / "operations.yaml"
    document = json.loads(path.read_text())
    document["operations"] = [
        {
            "id": f"items.read-{index}",
            "purpose": "Read one item",
            "config": {"service": "api", "method": "GET", "path": f"/items/{index}"},
        }
        for index in range(500)
    ]
    path.write_text(json.dumps(document))
    expanded = await create_server(workspace_files).list_tools()
    assert [item.model_dump(mode="json") for item in expanded] == [item.model_dump(mode="json") for item in first]
