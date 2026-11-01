import pytest

from qa_cli.app import _loaded_evidence_scope
from qa_runtime.errors import RuntimeFailure
from qa_runtime.evidence import EvidenceScope, RunStore, content_digest
from qa_runtime.models import Profile, RegistryDocument
from qa_runtime.registry import OperationRegistry


def _registry():
    return OperationRegistry(
        RegistryDocument.model_validate(
            {
                "apiVersion": "qa.runtime/v1",
                "kind": "OperationRegistry",
                "operations": [],
            }
        )
    )


def test_manifest_digest_matches_profile_without_credentials(tmp_path):
    profile = Profile.model_validate(
        {
            "environment": "qa",
            "services": {"sample": {"baseUrl": "https://sample.invalid"}},
            "defaults": {"optional-value": None},
        }
    )
    profile_document = profile.model_dump(mode="json", by_alias=True)
    del profile_document["credentials"]
    assert profile_document["services"]["sample"]["authRef"] is None
    assert profile_document["defaults"]["optional-value"] is None
    saved_scope = EvidenceScope(
        product="sample",
        profile="qa",
        profile_digest=content_digest(profile_document),
        registry_digest=content_digest([]),
    )
    store = RunStore(tmp_path / "sample-run")
    manifest = store.initialize(saved_scope)

    current_scope = _loaded_evidence_scope("sample", "qa", profile, _registry())

    assert current_scope == saved_scope
    assert store.initialize(current_scope) == manifest


def test_vault_binding_changes_require_a_new_evidence_scope(tmp_path):
    profile = Profile.model_validate(
        {
            "environment": "qa",
            "credentials": {
                "address": "https://vault.sample.invalid",
                "bindings": {"sample/password": {"mount": "teams/kv", "path": "application/qa", "field": "password"}},
            },
        }
    )
    scope = _loaded_evidence_scope("sample", "qa", profile, _registry())
    assert scope.profile_digest == content_digest(profile.model_dump(mode="json", by_alias=True))
    store = RunStore(tmp_path / "vault-run")
    store.initialize(scope)
    changed_profile = profile.model_copy(deep=True)
    changed_profile.credentials.bindings["sample/password"].field = "rotated-password"

    changed_scope = _loaded_evidence_scope("sample", "qa", changed_profile, _registry())

    assert changed_scope.profile_digest != scope.profile_digest
    with pytest.raises(RuntimeFailure) as raised:
        store.initialize(changed_scope)
    assert raised.value.code == "EVIDENCE_SCOPE_MISMATCH"
