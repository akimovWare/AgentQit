from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from qa_runtime.adapters.vault_auth import login_oidc
from qa_runtime.credential_config import VaultCredentialConfig, VaultSecretBinding
from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import Profile
from qa_runtime.profile import ProfileLoader


def test_vault_config_round_trips_aliases_without_accessing_files(tmp_path):
    document = {
        "provider": "vault-cli",
        "address": "https://vault.sample.invalid:8200/proxy",
        "authMode": "token-env",
        "namespace": "teams/sample",
        "caBundle": str(tmp_path / "not-installed.pem"),
        "authPath": "teams/oidc",
        "authRole": "qa-reader",
        "timeoutSeconds": 12.5,
        "bindings": {
            "sample/secret": {"mount": "teams/qa-kv", "path": "application/qa", "field": "value", "version": 2}
        },
    }

    config = VaultCredentialConfig.model_validate(document)

    assert config.model_dump(mode="json", by_alias=True) == document
    assert config.ca_bundle == tmp_path / "not-installed.pem"
    assert not config.ca_bundle.exists()
    assert config.auth_mode == "token-env"
    assert config.bindings["sample/secret"].version == 2
    assert Profile.model_validate({"credentials": document}).credentials == config


def test_vault_defaults_accept_profile_without_credentials():
    config = VaultCredentialConfig(address="https://vault.sample.invalid")
    assert config.provider == "vault-cli"
    assert config.auth_mode == "session"
    assert config.timeout_seconds == 30
    assert config.bindings == {}
    assert Profile().credentials is None


@pytest.mark.parametrize(
    "address",
    [
        "http://vault.sample.invalid",
        "https:///missing-host",
        "https://name:canary-private@vault.sample.invalid",
        "https://vault.sample.invalid?token=canary-private",
        "https://vault.sample.invalid#canary-private",
        "https://vault.sample.invalid?",
        "https://vault.sample.invalid#",
        "https://vault.sample.invalid:bad",
        "https://vault.sample.invalid:65536",
        "https://vault.sample.invalid:0",
        "https://vault.sample.invalid:",
        " https://vault.sample.invalid",
        "https://vault.sample.invalid\n",
        "https://bad host.invalid",
    ],
)
def test_vault_address_is_https_without_credentials_or_ambiguous_components(address):
    with pytest.raises(ValidationError) as raised:
        VaultCredentialConfig(address=address)
    assert "Vault address must" in str(raised.value)
    assert "canary-private" not in str(raised.value)


@pytest.mark.parametrize("target", ["mount", "path", "namespace", "authPath"])
@pytest.mark.parametrize(
    "unsafe",
    [
        "-format=json",
        "../private",
        "team/./private",
        "team/%2e%2e/private",
        "team/%252e%252e/private",
        "team//private",
        "/private",
        "team/private/",
        "team\\private",
        "team/private?format=json",
        "team/private#fragment",
        "team/%2dflag",
        "team/%00/private",
        "team/private\n",
    ],
)
def test_vault_paths_reject_options_traversal_and_control_characters(target, unsafe):
    with pytest.raises(ValidationError):
        if target in {"mount", "path"}:
            VaultSecretBinding.model_validate(
                {"mount": "teams/kv", "path": "application/qa", "field": "value", target: unsafe}
            )
        else:
            VaultCredentialConfig.model_validate({"address": "https://vault.sample.invalid", target: unsafe})


def test_field_is_an_arbitrary_nonempty_single_line_argument_value():
    binding = VaultSecretBinding(mount="teams/kv", path="application/qa", field="-name=with spaces")
    assert binding.field == "-name=with spaces"
    for field in ("", " ", "name\nsecond", "name\x00", "name\u2028second"):
        with pytest.raises(ValidationError):
            VaultSecretBinding(mount="kv", path="qa", field=field)


@pytest.mark.parametrize("role", ["-", "@private-file", "\\@private-file"])
def test_auth_role_rejects_vault_login_value_substitution(role):
    with pytest.raises(ValidationError) as raised:
        VaultCredentialConfig(address="https://vault.sample.invalid", auth_role=role)
    assert "authRole must be a literal name" in str(raised.value)
    assert "private-file" not in str(raised.value)


@pytest.mark.parametrize("role", ["qa-reader", "-reader", "%40private-file", "name@team", "role=value"])
def test_auth_role_preserves_literal_names_without_url_decoding(role):
    config = VaultCredentialConfig(address="https://vault.sample.invalid", auth_role=role)
    assert config.auth_role == role


@pytest.mark.anyio
async def test_login_cannot_receive_a_profile_role_that_reads_a_local_file(tmp_path, monkeypatch):
    private_file = tmp_path / "private-file"
    private_file.write_text("canary-private")
    started = []

    async def forbidden_process(*args, **kwargs):
        started.append(args)
        raise AssertionError("invalid authRole must fail before Vault starts")

    monkeypatch.setattr("qa_runtime.adapters.vault_auth.anyio.open_process", forbidden_process)
    with pytest.raises(ValidationError) as raised:
        await login_oidc(
            VaultCredentialConfig(
                address="https://vault.sample.invalid", auth_path="oidc", auth_role=f"@{private_file}"
            )
        )
    assert not started
    assert str(private_file) not in str(raised.value)
    assert "canary-private" not in str(raised.value)


@pytest.mark.parametrize("value", [0, -1, 121, float("inf"), float("nan"), True])
def test_vault_timeout_is_finite_and_bounded(value):
    with pytest.raises(ValidationError):
        VaultCredentialConfig(address="https://vault.sample.invalid", timeout_seconds=value)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "2"])
def test_secret_version_requires_a_positive_integer(value):
    with pytest.raises(ValidationError):
        VaultSecretBinding(mount="kv", path="qa", field="value", version=value)


def test_relative_ca_bundle_and_credential_fields_are_rejected(tmp_path):
    with pytest.raises(ValidationError):
        VaultCredentialConfig(address="https://vault.sample.invalid", ca_bundle=Path("relative.pem"))
    path = tmp_path / "qa.yaml"
    path.write_text(
        yaml.safe_dump({"credentials": {"address": "https://vault.sample.invalid", "token": "canary-private"}})
    )
    with pytest.raises(RuntimeFailure) as raised:
        ProfileLoader(tmp_path, env={}).load("qa")
    assert raised.value.code == "PROFILE_INVALID"
    assert "canary-private" not in str(raised.value)
