from pathlib import Path

from qa_runtime.models import Profile, RegistryDocument
from qa_runtime.onboarding import build_setup_plan
from qa_runtime.ports.product import AuthSetup, ProductPlugin, ProductSetup, ResourceBinding
from qa_runtime.registry import OperationRegistry


def test_custom_kind_dependencies_are_scoped_without_product_knowledge():
    plugin = ProductPlugin(
        product_id="sample",
        registry_path=Path("unused"),
        setup=ProductSetup(
            profile_template=Path("unused"),
            resource_bindings={
                "sample-read": ResourceBinding("endpoints", "endpointRef", AuthSetup(credential_refs=("sample/key",))),
            },
        ),
    )
    registry = OperationRegistry(
        RegistryDocument.model_validate(
            {
                "apiVersion": "qa.runtime/v1",
                "kind": "OperationRegistry",
                "operations": [
                    {"id": "read-sample", "kind": "sample-read", "config": {"endpointRef": "sample-storage"}},
                ],
            }
        )
    )
    profile = Profile.model_validate(
        {
            "environment": "qa",
            "endpoints": {"sample-storage": "https://storage.example", "unused": "https://unused.example"},
            "services": {"unused": {"baseUrl": "https://unused.example"}},
            "credentials": {
                "provider": "vault-cli",
                "address": "https://vault.example",
                "authMode": "session",
                "bindings": {
                    "sample/key": {"mount": "sample", "path": "qa/credentials", "field": "key"},
                    "unused/key": {"mount": "sample", "path": "qa/credentials", "field": "unused"},
                },
            },
        }
    )
    plan = build_setup_plan(plugin, registry, ["read-sample"], profile=profile)
    assert plan.profile.endpoints == {"sample-storage": "https://storage.example"}
    assert plan.profile.services == {}
    assert plan.requirements["resources"]["endpoints"] == ["sample-storage"]
    assert plan.requirements["credentialRefs"] == ["sample/key"]
    assert set(plan.profile.credentials.bindings) == {"sample/key"}
    assert not plan.requirements["credentialsChecked"]
    assert not plan.requirements["networkChecked"]

    missing = build_setup_plan(plugin, registry, ["read-sample"], profile=Profile())
    assert missing.requirements["endpointRefs"] == ["sample-storage"]
    assert missing.requirements["missingConfiguration"] == ["endpoints.sample-storage"]
