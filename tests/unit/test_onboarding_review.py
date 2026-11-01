from pathlib import Path

import pytest
import yaml

from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import Profile, RegistryDocument
from qa_runtime.onboarding import build_setup_plan
from qa_runtime.ports.product import ProductPlugin
from qa_runtime.profile import ProfileLoader
from qa_runtime.registry import OperationRegistry


def test_null_profile_default_keeps_the_operation_credential_reference():
    registry = OperationRegistry(
        RegistryDocument.model_validate(
            {
                "apiVersion": "qa.runtime/v1",
                "kind": "OperationRegistry",
                "operations": [
                    {
                        "id": "verify",
                        "kind": "http",
                        "parameters": {
                            "code-ref": {
                                "type": "string",
                                "location": "credential-body",
                                "required": True,
                                "default": "sample/default-reference",
                            }
                        },
                        "config": {"service": "sample", "method": "POST", "path": "/verify"},
                    }
                ],
            }
        )
    )
    profile = Profile.model_validate(
        {
            "services": {"sample": {"baseUrl": "https://sample.invalid"}},
            "defaults": {"code-ref": None},
        }
    )

    plan = build_setup_plan(ProductPlugin("sample", Path("unused")), registry, ["verify"], profile=profile)

    assert plan.requirements["credentialRefs"] == ["sample/default-reference"]
    assert plan.requirements["callerRequired"] == {"verify": []}


def _profile_document(section: str, query: str) -> dict:
    if section == "services":
        return {"services": {"sample": {"baseUrl": f"https://sample.invalid/api?{query}"}}}
    if section == "endpoints":
        return {"endpoints": {"issuer": f"https://sample.invalid/token?{query}"}}
    return {
        "databases": {
            "sample": {
                "dsn": f"postgresql://database.sample.invalid/example?{query}",
                "usernameRef": "sample/db/username",
                "passwordRef": "sample/db/password",
            }
        }
    }


@pytest.mark.parametrize("section", ["services", "endpoints", "databases"])
@pytest.mark.parametrize("key", ["access_token", "password", "%63lient%5fsecret", "API-KEY"])
def test_profile_rejects_secret_query_keys_without_echoing_the_url(tmp_path, section, key):
    document = _profile_document(section, f"mode=qa&{key}=synthetic-private-value")
    (tmp_path / "qa.yaml").write_text(yaml.safe_dump(document))

    with pytest.raises(RuntimeFailure) as raised:
        ProfileLoader(tmp_path, env={}).load("qa")

    assert raised.value.code == "PROFILE_INVALID"
    assert "must not contain credentials" in str(raised.value)
    assert "synthetic-private-value" not in str(raised.value)
    assert "sample.invalid" not in str(raised.value)


@pytest.mark.parametrize("section", ["services", "endpoints", "databases"])
def test_nonsecret_url_query_parameters_remain_unchanged(section):
    document = _profile_document(section, "mode=qa&client_id=public-client&sslmode=require")

    profile = Profile.model_validate(document)

    assert profile.model_dump(mode="json", by_alias=True, exclude_defaults=True) == document
