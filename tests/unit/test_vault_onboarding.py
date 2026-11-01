import os
from pathlib import Path

import pytest
import yaml

from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import Profile, RegistryDocument
from qa_runtime.onboarding import build_setup_plan, write_profile
from qa_runtime.ports.product import AuthSetup, ProductPlugin, ProductSetup
from qa_runtime.profile import ProfileLoader
from qa_runtime.registry import OperationRegistry


@pytest.fixture
def configured_product(tmp_path):
    operations = [
        {"id": "read", "kind": "http", "config": {"service": "private", "method": "GET", "path": "/state"}},
        {"id": "public", "kind": "http", "config": {"service": "public", "method": "GET", "path": "/state"}},
    ]
    for identifier, default in (("verify-default", "sample/sms"), ("verify-dynamic", None)):
        parameter = {"type": "string", "location": "credential-body", "required": True}
        if default is not None:
            parameter["default"] = default
        operations.append(
            {
                "id": identifier,
                "kind": "http",
                "mutating": True,
                "parameters": {"code-ref": parameter},
                "config": {"service": "private", "method": "POST", "path": "/verify"},
            }
        )
    registry = OperationRegistry(
        RegistryDocument.model_validate(
            {
                "apiVersion": "qa.runtime/v1",
                "kind": "OperationRegistry",
                "operations": operations,
            }
        )
    )
    document = {
        "environment": "qa",
        "services": {
            "private": {"baseUrl": "https://service.sample.invalid", "authRef": "reader"},
            "public": {"baseUrl": "https://public.sample.invalid"},
        },
        "endpoints": {"issuer": "https://issuer.sample.invalid/token"},
        "credentials": {
            "provider": "vault-cli",
            "address": "https://vault.sample.invalid",
            "authMode": "session",
            "namespace": "team/qa",
            "caBundle": str(tmp_path / "missing.pem"),
            "authPath": "oidc",
            "authRole": "qa-reader",
            "timeoutSeconds": 12,
            "bindings": {
                "sample/user": {"mount": "teams/kv", "path": "application/qa", "field": "username"},
                "sample/password": {"mount": "teams/kv", "path": "application/qa", "field": "password", "version": 2},
                "sample/sms": {"mount": "teams/kv", "path": "verification/current", "field": "code"},
                "sample/another-code": {"mount": "teams/kv", "path": "verification/another", "field": "code"},
            },
        },
    }
    template = tmp_path / "team.yaml"
    template.write_text(yaml.safe_dump(document))
    setup = ProductSetup(
        template,
        auth={
            "reader": AuthSetup(
                endpoint_refs=("issuer",),
                credential_refs=("sample/user", "sample/password"),
            )
        },
    )

    def no_runtime(*args):
        pytest.fail("onboarding must not build providers or transports")

    plugin = ProductPlugin("sample", Path("unused"), runtime_factory=no_runtime, setup=setup)
    return plugin, registry, document


def test_known_bindings_are_scoped_before_environment_resolution(configured_product, monkeypatch):
    plugin, registry, document = configured_product
    document["credentials"]["bindings"]["irrelevant"] = {
        "mount": "unused",
        "path": "${env:UNUSED_VAULT_PATH}",
        "field": "value",
    }
    plugin.setup.profile_template.write_text(yaml.safe_dump(document))
    monkeypatch.delenv("UNUSED_VAULT_PATH", raising=False)

    plan = build_setup_plan(plugin, registry, ["read", "verify-default"])

    assert set(plan.profile.credentials.bindings) == {"sample/user", "sample/password", "sample/sms"}
    assert plan.profile.credentials.address == "https://vault.sample.invalid"
    assert plan.profile.credentials.ca_bundle == plugin.setup.profile_template.parent / "missing.pem"
    assert plan.requirements["credentialProvider"] == "vault-cli"
    assert plan.requirements["credentialBindings"]["sample/password"]["version"] == 2
    assert plan.requirements["missingCredentialBindings"] == []
    assert plan.requirements["dynamicCredentialParameters"] == {}
    assert plan.requirements["dynamicCredentialBindingSelection"] is False
    assert plan.requirements["credentialsChecked"] is plan.requirements["networkChecked"] is False


def test_profile_preserves_provider_settings_and_remains_loadable(configured_product, tmp_path):
    plugin, registry, document = configured_product
    original = Profile.model_validate(document)
    plan = build_setup_plan(plugin, registry, ["read"], profile=original)
    target = write_profile(plan, tmp_path / "profiles", "vault")
    loaded = ProfileLoader(target.parent, env={}).load("vault")

    assert loaded.credentials == plan.profile.credentials
    assert loaded.credentials.namespace == "team/qa"
    assert loaded.credentials.auth_path == "oidc"
    assert loaded.credentials.auth_role == "qa-reader"
    assert loaded.credentials.timeout_seconds == 12
    assert len(original.credentials.bindings) == 4
    assert len(loaded.credentials.bindings) == 2
    assert loaded.model_dump(mode="json", by_alias=True)["credentials"]["authMode"] == "session"


def test_dynamic_credential_input_preserves_candidate_bindings(configured_product):
    plugin, registry, _ = configured_product
    plan = build_setup_plan(plugin, registry, ["verify-dynamic"])

    assert set(plan.profile.credentials.bindings) == {
        "sample/user",
        "sample/password",
        "sample/sms",
        "sample/another-code",
    }
    assert plan.requirements["credentialRefs"] == ["sample/password", "sample/user"]
    assert plan.requirements["dynamicCredentialParameters"] == {"verify-dynamic": ["code-ref"]}
    assert plan.requirements["dynamicCredentialBindingSelection"] is True
    assert plan.requirements["callerRequired"] == {"verify-dynamic": ["code-ref"]}


def test_existing_default_resolves_dynamic_selection_and_preserves_its_binding(configured_product):
    plugin, registry, document = configured_product
    document["defaults"] = {"code-ref": "sample/another-code"}
    plan = build_setup_plan(plugin, registry, ["verify-dynamic"], profile=Profile.model_validate(document))

    assert set(plan.profile.credentials.bindings) == {"sample/user", "sample/password", "sample/another-code"}
    assert plan.requirements["dynamicCredentialParameters"] == {}
    assert plan.requirements["callerRequired"] == {"verify-dynamic": []}


def test_missing_known_binding_is_reported_and_stops_init(configured_product, tmp_path):
    plugin, registry, document = configured_product
    del document["credentials"]["bindings"]["sample/password"]
    plugin.setup.profile_template.write_text(yaml.safe_dump(document))

    plan = build_setup_plan(plugin, registry, ["read"])

    assert plan.requirements["missingCredentialBindings"] == ["sample/password"]
    with pytest.raises(RuntimeFailure) as raised:
        write_profile(plan, tmp_path / "profiles", "incomplete")
    assert raised.value.code == "SETUP_CREDENTIAL_BINDINGS_MISSING"
    assert not (tmp_path / "profiles").exists()


def test_anonymous_operation_does_not_require_unrelated_vault_environment(configured_product, monkeypatch):
    plugin, registry, document = configured_product
    document["credentials"]["address"] = "${env:UNUSED_VAULT_ADDRESS}"
    plugin.setup.profile_template.write_text(yaml.safe_dump(document))
    monkeypatch.delenv("UNUSED_VAULT_ADDRESS", raising=False)

    plan = build_setup_plan(plugin, registry, ["public"])

    assert plan.profile.credentials is None
    assert plan.requirements["credentialProvider"] == "unconfigured"
    assert plan.requirements["credentialBindings"] == {}
    assert plan.requirements["missingCredentialBindings"] == []


def test_profile_without_provider_is_reported_as_unconfigured(configured_product):
    plugin, registry, document = configured_product
    del document["credentials"]
    plan = build_setup_plan(plugin, registry, ["read"], profile=Profile.model_validate(document))

    assert plan.profile.credentials is None
    assert plan.requirements["credentialProvider"] == "unconfigured"
    assert "credentialStoreService" not in plan.requirements
    assert plan.requirements["credentialRefs"] == ["sample/password", "sample/user"]
    assert plan.requirements["missingCredentialBindings"] == []


def test_rejected_vault_configuration_does_not_expose_input(configured_product):
    plugin, registry, document = configured_product
    document["credentials"]["token"] = "canary-private-token"
    plugin.setup.profile_template.write_text(yaml.safe_dump(document))

    with pytest.raises(RuntimeFailure) as raised:
        build_setup_plan(plugin, registry, ["read"])

    assert raised.value.code == "SETUP_TEMPLATE_INVALID"
    assert "canary-private-token" not in str(raised.value)


@pytest.mark.parametrize("auth_path", ["oidc", None])
def test_vault_session_prerequisites_describe_explicit_login_and_local_files(configured_product, auth_path):
    plugin, registry, document = configured_product
    document["credentials"]["authPath"] = auth_path
    plan = build_setup_plan(plugin, registry, ["read"], profile=Profile.model_validate(document))
    prerequisites = {item["id"]: item for item in plan.requirements["prerequisites"]}

    assert set(prerequisites) == {"vault-cli", "vault-session", "vault-ca"}
    assert prerequisites["vault-cli"]["kind"] == "tool"
    assert prerequisites["vault-cli"]["environmentVariable"] == "QA_CLI_VAULT_EXECUTABLE"
    assert prerequisites["vault-session"]["kind"] == "login"
    expected_instruction = "qa auth login" if auth_path else "standard Vault token helper"
    assert expected_instruction in prerequisites["vault-session"]["description"]
    assert prerequisites["vault-ca"]["kind"] == "file"
    assert prerequisites["vault-ca"]["path"] == document["credentials"]["caBundle"]
    assert not plan.requirements["credentialsChecked"]


def test_vault_token_prerequisite_never_reads_or_exposes_environment_value(configured_product, monkeypatch):
    plugin, registry, document = configured_product
    document["credentials"].update({"authMode": "token-env", "caBundle": None})
    profile = Profile.model_validate(document)
    monkeypatch.setenv("VAULT_TOKEN", "canary-private-token")
    original_get = os.environ.get

    def safe_get(key, *args):
        assert key != "VAULT_TOKEN", "offline requirements must not read the token"
        return original_get(key, *args)

    monkeypatch.setattr(os.environ, "get", safe_get)
    plan = build_setup_plan(plugin, registry, ["read"], profile=profile)
    prerequisites = {item["id"]: item for item in plan.requirements["prerequisites"]}

    assert set(prerequisites) == {"vault-cli", "vault-token"}
    assert prerequisites["vault-token"]["kind"] == "credential"
    assert prerequisites["vault-token"]["environmentVariable"] == "VAULT_TOKEN"
    assert prerequisites["vault-token"]["path"] is None
    assert "canary-private-token" not in str(plan.requirements)
