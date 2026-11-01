from __future__ import annotations

import base64
import hashlib
import json

import pytest
import yaml
from pydantic import ValidationError

from qa_cli.app import _loaded_evidence_scope
from qa_runtime.codecs.catalog import load_codecs
from qa_runtime.models import DatabaseServiceConfig, HttpServiceConfig, Profile, S3ServiceConfig
from qa_runtime.registry import OperationRegistry
from qa_runtime.security import SecretRedactor


def write_registry(path, checksum):
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "qa.runtime/v1",
                "kind": "OperationRegistry",
                "codecs": {
                    "message": {
                        "format": "protobuf",
                        "descriptorPath": "schemas/message.pb",
                        "messageType": "sample.Message",
                        "checksumSha256": checksum,
                    }
                },
                "operations": [{"id": "sample.read", "config": {"service": "sample", "method": "GET", "path": "/"}}],
            }
        )
    )
    return OperationRegistry.load(path)


def test_registry_schema_change_changes_evidence_scope_without_changing_operation(tmp_path):
    path = tmp_path / "registry.yaml"
    first = write_registry(path, "0" * 64)
    second = write_registry(path, "1" * 64)
    assert first.ids() == second.ids()
    assert first.codecs()["message"].descriptor_path == tmp_path / "schemas/message.pb"
    assert (
        _loaded_evidence_scope("sample", "qa", Profile(), first).registry_digest
        != _loaded_evidence_scope("sample", "qa", Profile(), second).registry_digest
    )


def test_unselected_schema_does_not_require_reading_descriptor(tmp_path):
    registry = write_registry(tmp_path / "registry.yaml", "0" * 64)
    codecs = load_codecs(registry, registry.operations())
    assert codecs.encoders == codecs.decoders == {}


@pytest.mark.parametrize("port", ["bad", "0", "65536"])
def test_invalid_endpoint_port_is_rejected_before_transport_or_credentials(port):
    with pytest.raises(ValidationError):
        HttpServiceConfig(baseUrl=f"https://service.invalid:{port}")
    with pytest.raises(ValidationError):
        S3ServiceConfig(
            endpoint=f"https://storage.invalid:{port}", region="region", accessKeyRef="access", secretKeyRef="secret"
        )
    with pytest.raises(ValidationError):
        DatabaseServiceConfig(dsn=f"postgresql://db.invalid:{port}/data", usernameRef="user", passwordRef="pass")


@pytest.mark.parametrize("timeout", [float("inf"), float("nan"), 301])
def test_profile_transport_timeouts_have_finite_shared_upper_bound(timeout):
    with pytest.raises(ValidationError):
        S3ServiceConfig(
            endpoint="https://storage.invalid",
            region="region",
            accessKeyRef="a",
            secretKeyRef="s",
            requestTimeoutSeconds=timeout,
        )
    with pytest.raises(ValidationError):
        DatabaseServiceConfig(
            dsn="postgresql://db.invalid/data", usernameRef="u", passwordRef="p", commandTimeoutSeconds=timeout
        )


def test_binary_redaction_works_for_non_base64_aligned_secret_and_keeps_original_hash():
    original = b"\x00q:canary-password:\xff"
    digest = hashlib.sha256(original).hexdigest()
    redactor = SecretRedactor()
    redactor.register("canary-password")
    facts = redactor.redact(
        {"sha256": digest, "body": {"encoding": "base64", "data": base64.b64encode(original).decode()}}
    )
    assert facts["sha256"] == digest
    assert facts["body"]["redacted"] is True
    assert base64.b64decode(facts["body"]["data"]) == b"\x00q:***:\xff"


def test_sensitive_headers_inside_json_log_message_are_redacted_without_business_projection():
    message = json.dumps({"headers": {"authorization": "Bearer canary", "cookie": "canary-cookie"}, "status": 503})
    facts = SecretRedactor().redact({"message": message, "level": "ERROR"})
    parsed = json.loads(facts["message"])
    assert parsed["headers"] == {"authorization": "***", "cookie": "***"}
    assert parsed["status"] == 503
    assert facts["level"] == "ERROR"
