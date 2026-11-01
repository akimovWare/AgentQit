from __future__ import annotations

import json
import sys

import httpx
import pytest
from typer.testing import CliRunner

from qa_cli.app import app
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


def call(*args):
    result = CliRunner().invoke(app, list(args))
    assert result.stderr == ""
    document = json.loads(result.stdout)
    assert document["exit_code"] == result.exit_code
    return result.exit_code, document


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    registry = tmp_path / "operations.json"
    registry.write_text(
        json.dumps(
            {
                "apiVersion": "qa.runtime/v1",
                "kind": "OperationRegistry",
                "operations": [
                    {
                        "id": "read",
                        "kind": "http",
                        "admission": "agent-read",
                        "config": {"service": "api", "method": "GET", "path": "/state"},
                    }
                ],
            }
        )
    )
    profile = tmp_path / "qa.yaml"
    config = {
        "environment": "qa",
        "services": {"api": {"baseUrl": "https://sample.invalid", "authRef": "test"}},
        "credentials": {
            "provider": "vault-cli",
            "address": "https://vault.invalid",
            "authPath": "employees",
            "bindings": {"sample/read-token": {"mount": "team", "path": "qa/sample", "field": "api-token"}},
        },
    }
    profile.write_text(json.dumps(config))
    vault = tmp_path / "vault"
    marker = tmp_path / "reads"
    vault.write_text(
        f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('called')\n"
        "sys.stdout.write('test-service-secret')\n"
    )
    vault.chmod(0o700)
    plugin = ProductPlugin(
        "sample",
        registry,
        runtime_factory=lambda p, c, r: ProductComponents(
            auth_providers={"test": TokenAuth(c, r, "sample/read-token")}
        ),
        setup=ProductSetup(profile, auth={"test": AuthSetup(credential_refs=("sample/read-token",))}),
    )
    monkeypatch.setattr("qa_cli.app.product_plugin_for", lambda _: plugin)
    monkeypatch.setattr("qa_cli.app.select_product", lambda _: "sample")
    monkeypatch.setattr(
        "qa_cli.app._credential_source", lambda *a, **k: pytest.fail("unexpected credential provider fallback")
    )
    monkeypatch.setenv("QA_CLI_PROFILES_DIR", str(tmp_path))
    monkeypatch.setenv("QA_CLI_VAULT_EXECUTABLE", str(vault))
    monkeypatch.delenv("QA_CLI_CREDENTIAL_HELPER", raising=False)
    monkeypatch.delenv("QA_CLI_REGISTRY_PATH", raising=False)

    def respond(request):
        assert request.headers["Authorization"] == "test-service-secret"
        return httpx.Response(200, json={"echo": request.headers["Authorization"]})

    monkeypatch.setattr(
        "qa_runtime.bootstrap.HttpxAdapter",
        lambda **_: HttpxAdapter(httpx.AsyncClient(transport=httpx.MockTransport(respond))),
    )
    return profile, config, marker


def test_vault_is_lazy_and_result_redacts_secret(configured):
    _, _, marker = configured
    for args in (("config", "validate"), ("doctor", "read"), ("plan", "read")):
        code, result = call(*args)
        assert code == 0, result
        assert not marker.exists()
    code, result = call("invoke", "read")
    assert code == 0, result
    assert marker.exists()
    assert result["result"]["body"] == {"echo": "***"}
    assert "test-service-secret" not in json.dumps(result)


def test_config_requirements_exposes_vault_metadata_without_reading(configured):
    _, _, marker = configured
    code, result = call("config", "requirements", "--operation", "read")
    assert code == 0, result
    data = result["result"]
    assert data["credentialProvider"] == "vault-cli"
    assert data["credentialConfiguration"]["authPath"] == "employees"
    assert data["credentialBindings"]["sample/read-token"]["path"] == "qa/sample"
    assert not marker.exists()


def test_explicit_helper_and_vault_are_conflicting(configured, monkeypatch):
    _, _, marker = configured
    monkeypatch.setenv("QA_CLI_CREDENTIAL_HELPER", "/unused-helper")
    code, result = call("doctor", "read")
    assert code == 2 and result["error"]["code"] == "CREDENTIAL_SOURCE_CONFLICT"
    assert not marker.exists()


def test_auth_login_is_independent_of_installed_products_and_has_one_result(configured, monkeypatch):
    monkeypatch.setattr("qa_cli.app.product_plugin_for", lambda _: pytest.fail("login needs only profile"))
    code, result = call("auth", "login")
    assert code == 0, result
    assert result["result"] == {
        "profile": "qa",
        "provider": "vault-cli",
        "authenticated": True,
        "credentialsChecked": False,
    }
    assert "test-service-secret" not in json.dumps(result)


def test_ci_missing_token_never_falls_back_to_local_session(configured, monkeypatch):
    profile, config, marker = configured
    config["credentials"]["authMode"] = "token-env"
    profile.write_text(json.dumps(config))
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    code, result = call("invoke", "read")
    assert code == 5 and result["error"]["code"] == "VAULT_TOKEN_REQUIRED"
    assert not marker.exists()
    code, result = call("auth", "login")
    assert code == 2 and result["error"]["code"] == "VAULT_LOGIN_NOT_CONFIGURED"
    assert not marker.exists()
