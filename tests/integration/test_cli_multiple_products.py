from __future__ import annotations

import json

import httpx
from typer.testing import CliRunner

from qa_cli.app import app
from qa_runtime.adapters.credentials import InMemoryCredentialSource
from qa_runtime.adapters.httpx import HttpxAdapter
from qa_runtime.ports.product import AuthSetup, ProductComponents, ProductPlugin, ProductSetup


class TokenAuth:
    def __init__(self, credentials, redactor, reference):
        self.credentials, self.redactor, self.reference = credentials, redactor, reference

    async def authorize(self, request, context):
        secret = await self.credentials.get(self.reference)
        self.redactor.register(secret)
        return request.model_copy(update={"headers": {**request.headers, "Authorization": secret.reveal()}})

    async def invalidate(self, context):
        pass

    async def aclose(self):
        pass


class EntryPoint:
    def __init__(self, plugin):
        self.name, self.value, self.plugin = plugin.product_id, "test:plugin", plugin

    def load(self):
        return self.plugin


def test_independent_products_select_their_own_registry_setup_and_credentials(tmp_path, monkeypatch):
    points = []
    for name in ("inventory", "billing"):
        registry = tmp_path / f"{name}-registry.json"
        registry.write_text(
            json.dumps(
                {
                    "apiVersion": "qa.runtime/v1",
                    "kind": "OperationRegistry",
                    "operations": [
                        {
                            "id": "get-state",
                            "kind": "http",
                            "admission": "agent-read",
                            "config": {"service": f"{name}-api", "method": "GET", "path": "/state"},
                        }
                    ],
                }
            )
        )
        template = tmp_path / f"{name}-template.json"
        template.write_text(
            json.dumps(
                {
                    "environment": "qa",
                    "services": {f"{name}-api": {"baseUrl": f"https://{name}.invalid", "authRef": "user"}},
                }
            )
        )

        def factory(profile, credentials, redactor, reference=f"{name}/read-token"):
            return ProductComponents(auth_providers={"user": TokenAuth(credentials, redactor, reference)})

        points.append(
            EntryPoint(
                ProductPlugin(
                    name,
                    registry,
                    runtime_factory=factory,
                    setup=ProductSetup(template, auth={"user": AuthSetup(credential_refs=(f"{name}/read-token",))}),
                )
            )
        )

    def discover(**options):
        assert options["group"] == "qa_runtime.products"
        return [point for point in points if "name" not in options or point.name == options["name"]]

    source_requests, credential_reads, calls = [], [], []

    class TrackingCredentials(InMemoryCredentialSource):
        async def get(self, reference):
            credential_reads.append(reference)
            return await super().get(reference)

    shared_credentials = TrackingCredentials(
        {f"{name}/read-token": f"{name}-secret" for name in ("inventory", "billing")}
    )

    def credentials(helper):
        source_requests.append(helper)
        return shared_credentials

    def respond(request):
        calls.append((request.url.host, request.headers["Authorization"]))
        return httpx.Response(200, json={"source": request.url.host, "echo": request.headers["Authorization"]})

    monkeypatch.setattr("qa_runtime.plugins.entry_points", discover)
    monkeypatch.setattr("qa_cli.app._credential_source", credentials)
    monkeypatch.setattr(
        "qa_runtime.bootstrap.HttpxAdapter",
        lambda **_: HttpxAdapter(httpx.AsyncClient(transport=httpx.MockTransport(respond))),
    )
    monkeypatch.delenv("QA_CLI_PRODUCT", raising=False)
    monkeypatch.delenv("QA_CLI_REGISTRY_PATH", raising=False)
    monkeypatch.delenv("QA_CLI_CREDENTIAL_HELPER", raising=False)
    monkeypatch.setenv("QA_CLI_PROFILES_DIR", str(tmp_path / "profiles"))

    def command(*args):
        response = CliRunner().invoke(app, list(args))
        assert response.stderr == ""
        return response.exit_code, json.loads(response.stdout)

    assert command("product", "list")[1]["result"]["products"] == ["billing", "inventory"]
    assert command("guide")[0] == 0
    code, result = command("registry", "list")
    assert code == 2 and result["error"]["code"] == "PRODUCT_REQUIRED"
    for name in ("inventory", "billing"):
        code, result = command("config", "init", "--operation", "get-state", "--product", name, "--profile", name)
        assert code == 0, result
        assert result["result"]["requirements"]["credentialRefs"] == [f"{name}/read-token"]
    assert source_requests == credential_reads == calls == []
    for name in ("inventory", "billing"):
        code, result = command("invoke", "get-state", "--product", name, "--profile", name)
        assert code == 0, result
        assert result["result"]["body"] == {"source": f"{name}.invalid", "echo": "***"}
    assert source_requests == [None, None]
    assert credential_reads == ["inventory/read-token", "billing/read-token"]
    assert calls == [("inventory.invalid", "inventory-secret"), ("billing.invalid", "billing-secret")]


def test_general_help_and_guide_work_without_product_packages(monkeypatch):
    monkeypatch.delenv("QA_CLI_PRODUCT", raising=False)
    monkeypatch.setattr("qa_runtime.plugins.entry_points", lambda **_: [])
    response = CliRunner().invoke(app, ["guide"])
    assert response.exit_code == 0
    result = json.loads(response.stdout)["result"]
    assert "scenarios" not in result
    assert result["productDiscovery"]["argv"] == ["qa", "product", "list", "--output", "json"]
