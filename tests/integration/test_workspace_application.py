from __future__ import annotations

import json
import os
import sys

import anyio
import httpx
import pytest
import yaml

from qa_runtime.adapters.httpx import HttpxAdapter
from qa_runtime.application import WorkspaceApplication
from qa_runtime.errors import RuntimeFailure
from qa_runtime.onboarding import build_setup_plan
from qa_runtime.workspace import Workspace

pytestmark = pytest.mark.anyio


def workspace(tmp_path, *, environment="qa", timeout=60):
    document = {
        "apiVersion": "agentqit/v1",
        "name": "orders",
        "registry": ["operations.yaml"],
        "auth": {"token": {"kind": "bearer", "credentialRef": "orders/token"}},
        "limits": {"operationTimeoutSeconds": timeout},
    }
    (tmp_path / "agentqit.yaml").write_text(yaml.safe_dump(document))
    (tmp_path / "profile.yaml").write_text(
        yaml.safe_dump(
            {
                "environment": environment,
                "services": {"orders": {"baseUrl": "https://orders.invalid", "authRef": "token"}},
                "credentials": {"provider": "env", "bindings": {"orders/token": "ORDERS_TOKEN"}},
            }
        )
    )
    (tmp_path / "operations.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "qa.runtime/v1",
                "kind": "OperationRegistry",
                "operations": [
                    {
                        "id": "orders.get",
                        "purpose": "Read an order",
                        "parameters": {
                            "id": {
                                "type": "integer",
                                "required": True,
                                "minimum": 1,
                                "location": "path",
                            }
                        },
                        "config": {"service": "orders", "method": "GET", "path": "/orders/{id}"},
                    },
                    {
                        "id": "orders.create",
                        "purpose": "Create an order",
                        "mutating": True,
                        "config": {"service": "orders", "method": "POST", "path": "/orders"},
                    },
                ],
            }
        )
    )
    return Workspace.load(tmp_path, env={})


async def test_plain_workspace_needs_no_installed_plugin_and_plan_reads_no_secrets(tmp_path):
    ws = workspace(tmp_path)
    app = WorkspaceApplication(ws, env={})
    assert app.list_operations("get", limit=1)["operations"][0]["id"] == "orders.get"
    assert app.describe_operation("orders.get")["parameters"]["id"]["type"] == "integer"
    result = await app.plan("orders.get", {"id": 3})
    assert result.exit_code == 0 and result.result["network_checked"] is False
    assert (await app.plan("orders.get", {"id": 0})).exit_code == 2
    requirements = build_setup_plan(ws.plugin, ws.registry, ["orders.get"], profile=ws.profile)
    assert requirements.requirements["credentialProvider"] == "env"
    assert requirements.requirements["credentialBindings"] == {"orders/token": "ORDERS_TOKEN"}
    assert requirements.requirements["credentialsChecked"] is False


async def test_execution_auth_redaction_and_mutation_gate_share_the_runtime(tmp_path):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(500, json={"echo": request.headers["authorization"], "id": 3})

    app = WorkspaceApplication(
        workspace(tmp_path),
        env={"ORDERS_TOKEN": "synthetic-private-token"},
        mutations_allowed=True,
        http_factory=lambda: HttpxAdapter(httpx.AsyncClient(transport=httpx.MockTransport(handle))),
    )
    blocked = await app.invoke("orders.create", {})
    assert blocked.exit_code == 7 and not calls
    result = await app.invoke("orders.get", {"id": 3})
    assert result.exit_code == 0 and result.result["status_code"] == 500
    assert "synthetic-private-token" not in result.model_dump_json()
    assert calls[0].url.path == "/orders/3"
    result = await app.invoke("orders.create", {}, allow_mutation=True)
    assert result.exit_code == 0 and len(calls) == 2
    await app.aclose()
    assert (await app.invoke("orders.get", {"id": 3})).error.code == "APPLICATION_CLOSED"


async def test_overall_budget_records_uncertain_write_and_closes_resources(tmp_path):
    closed = []

    class HangingPort:
        async def send(self, request, context):
            await anyio.sleep_forever()

        async def aclose(self):
            closed.append(True)

    app = WorkspaceApplication(
        workspace(tmp_path, timeout=0.02),
        env={"ORDERS_TOKEN": "synthetic"},
        http_factory=HangingPort,
        mutations_allowed=True,
    )
    result = await app.invoke("orders.create", {}, allow_mutation=True)
    assert result.status == "timeout"
    assert result.error.execution_outcome == "unknown"
    assert result.error.retry_advice == "read_state_first"
    assert result.error.retryable is False and closed == [True]


async def test_environment_source_missing_value_does_not_send_request(tmp_path):
    calls = []
    app = WorkspaceApplication(
        workspace(tmp_path),
        env={},
        http_factory=lambda: HttpxAdapter(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: calls.append(r)))),
    )
    result = await app.invoke("orders.get", {"id": 3})
    assert result.exit_code == 5 and calls == []


async def test_manifest_paths_cannot_escape_the_workspace(tmp_path):
    workspace(tmp_path)
    (tmp_path / "agentqit.yaml").write_text(
        json.dumps(
            {
                "apiVersion": "agentqit/v1",
                "name": "orders",
                "profile": "../outside.yaml",
            }
        )
    )
    with pytest.raises(RuntimeFailure) as caught:
        Workspace.load(tmp_path)
    assert caught.value.code == "WORKSPACE_PATH_OUTSIDE_ROOT"


async def test_caller_cannot_enable_mutation_when_the_host_has_not_granted_it(tmp_path):
    app = WorkspaceApplication(workspace(tmp_path), env={"ORDERS_TOKEN": "synthetic"})
    result = await app.invoke("orders.create", {}, allow_mutation=True)
    assert result.exit_code == 7 and result.status == "blocked"
    assert result.error.code == "HOST_MUTATION_BLOCKED"
    assert result.error.execution_outcome == "not_sent"


@pytest.mark.parametrize("auth_mode", ["session", "token-env"])
async def test_vault_workspace_plan_is_lazy_and_execution_uses_the_selected_environment(tmp_path, auth_mode):
    workspace(tmp_path)
    profile_path = tmp_path / "profile.yaml"
    profile = yaml.safe_load(profile_path.read_text())
    profile["credentials"] = {
        "provider": "vault-cli",
        "address": "https://vault.invalid",
        "authMode": auth_mode,
        "bindings": {"orders/token": {"mount": "team", "path": "qa/orders", "field": "api-token"}},
    }
    profile_path.write_text(yaml.safe_dump(profile))
    marker = tmp_path / "vault-called"
    executable = tmp_path / "vault"
    expected_token = "synthetic-vault-session" if auth_mode == "token-env" else None
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('called')\n"
        "assert sys.argv[1:3] == ['kv', 'get']\n"
        "assert '-field=api-token' in sys.argv\n"
        f"assert os.environ.get('VAULT_TOKEN') == {expected_token!r}\n"
        "sys.stdout.write('synthetic-vault-secret')\n"
    )
    executable.chmod(0o700)
    sent = []

    def handle(request):
        sent.append(request)
        return httpx.Response(200, json={"echo": request.headers["authorization"]})

    app = WorkspaceApplication(
        Workspace.load(tmp_path),
        env={"PATH": str(tmp_path) + os.pathsep + os.defpath, "VAULT_TOKEN": "synthetic-vault-session"},
        http_factory=lambda: HttpxAdapter(httpx.AsyncClient(transport=httpx.MockTransport(handle))),
    )
    try:
        planned = await app.plan("orders.get", {"id": 3})
        assert planned.exit_code == 0 and planned.result["network_checked"] is False
        assert not marker.exists() and sent == []
        observed = await app.invoke("orders.get", {"id": 3})
        assert observed.exit_code == 0 and observed.result["status_code"] == 200
        assert marker.exists() and len(sent) == 1
        assert sent[0].headers["authorization"] == "Bearer synthetic-vault-secret"
        assert "synthetic-vault-secret" not in observed.model_dump_json()
    finally:
        await app.aclose()
