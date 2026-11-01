from __future__ import annotations

import pytest
from pydantic import ValidationError

from qa_runtime.adapters.environment import EnvironmentCredentialSource
from qa_runtime.adapters.static_auth import HeaderAuthConfig, HeaderAuthProvider
from qa_runtime.credential_config import EnvironmentCredentialConfig
from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import HttpRequest, Profile
from qa_runtime.security import SecretRedactor

pytestmark = pytest.mark.anyio


async def test_only_explicit_names_are_resolved_without_fallback():
    config = EnvironmentCredentialConfig(provider="env", bindings={"service/token": "SERVICE_TOKEN"})
    source = EnvironmentCredentialSource(config, {"SERVICE_TOKEN": "synthetic-secret", "OTHER_TOKEN": "unused"})
    assert (await source.get("service/token")).reveal() == "synthetic-secret"
    assert "synthetic-secret" not in repr(await source.get("service/token"))
    for reference in ["OTHER_TOKEN", "missing"]:
        with pytest.raises(RuntimeFailure) as caught:
            await source.get(reference)
        assert caught.value.code == "ENV_CREDENTIAL_UNAVAILABLE"
        assert caught.value.execution_outcome == "not_sent"
    assert isinstance(Profile(credentials=config).credentials, EnvironmentCredentialConfig)


@pytest.mark.parametrize("variable", ["", "${env:TOKEN}", "name=value", "two words", "line\nbreak"])
async def test_binding_values_are_variable_names_not_secret_values_or_substitution(variable):
    with pytest.raises(ValidationError):
        EnvironmentCredentialConfig(provider="env", bindings={"key": variable})


@pytest.mark.parametrize(
    ("kind", "name", "expected"),
    [
        ("bearer", "Authorization", "Bearer synthetic-secret"),
        ("api-key", "X-API-Key", "synthetic-secret"),
    ],
)
async def test_header_auth_resolves_lazily_and_registers_redaction(kind, name, expected):
    redactor = SecretRedactor()
    source = EnvironmentCredentialSource(
        EnvironmentCredentialConfig(provider="env", bindings={"key": "TOKEN"}),
        {"TOKEN": "synthetic-secret"},
    )
    provider = HeaderAuthProvider(HeaderAuthConfig(kind=kind, credentialRef="key"), source, redactor)
    request = HttpRequest(method="GET", url="https://service.invalid", headers={name.lower(): "old"})
    result = await provider.authorize(request, None)
    assert result.headers[name] == expected
    assert name.lower() not in result.headers
    assert redactor.redact({"echo": "synthetic-secret"}) == {"echo": "***"}


async def test_header_injection_is_rejected_without_exposing_secret():
    secret = "synthetic\r\ninjection"
    source = EnvironmentCredentialSource(
        EnvironmentCredentialConfig(provider="env", bindings={"key": "TOKEN"}),
        {"TOKEN": secret},
    )
    provider = HeaderAuthProvider(HeaderAuthConfig(kind="bearer", credentialRef="key"), source, SecretRedactor())
    with pytest.raises(RuntimeFailure) as caught:
        await provider.authorize(HttpRequest(method="GET", url="https://service.invalid"), None)
    assert caught.value.execution_outcome == "not_sent"
    assert secret not in str(caught.value)
