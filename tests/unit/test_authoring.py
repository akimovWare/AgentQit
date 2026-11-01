from pathlib import Path

import pytest
import yaml

from qa_runtime.authoring import registry_json_schema, validate_product_authoring
from qa_runtime.errors import RuntimeFailure
from qa_runtime.ports.product import ProductGuide, ProductPlugin


def _plugin(tmp_path: Path, operation: dict) -> ProductPlugin:
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        yaml.safe_dump({"apiVersion": "qa.runtime/v1", "kind": "OperationRegistry", "operations": [operation]}),
        encoding="utf-8",
    )
    return ProductPlugin("sample", registry)


def _operation(**changes) -> dict:
    return {
        "id": "customer.get",
        "purpose": "Read the requested customer.",
        "config": {"service": "customers", "method": "GET", "path": "/customers"},
        **changes,
    }


def test_schema_includes_builtin_config_aliases_and_preserves_custom_kinds() -> None:
    schema = registry_json_schema()
    assert schema["$schema"].endswith("2020-12/schema")
    definitions = schema["$defs"]
    http = definitions["HttpOperationConfig"]
    assert http["additionalProperties"] is False
    assert "expectedStatus" not in http["properties"]
    assert "timeoutSeconds" in http["properties"]
    assert "maxResponseBytes" in http["properties"]
    assert "S3OperationConfig" in definitions
    assert "KubernetesOperationConfig" in definitions
    assert "CodecDefinition" in definitions
    assert "expected_status" not in http["properties"]
    assert "topicRef" in definitions["KafkaConsumeOperationConfig"]["properties"]
    operation = definitions["OperationDefinition"]
    assert operation["properties"]["config"]["additionalProperties"] is True
    assert "enum" not in operation["properties"]["kind"]
    assert operation["allOf"][0]["if"] == {"properties": {"kind": {"const": "http"}}}
    assert all(item["if"]["required"] == ["kind"] for item in operation["allOf"][1:])


def test_schema_references_resolve_and_calls_do_not_share_mutable_state() -> None:
    schema = registry_json_schema()

    def visit(value):
        if isinstance(value, dict):
            if "$ref" in value:
                assert value["$ref"].removeprefix("#/$defs/") in schema["$defs"]
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(schema)
    schema["$defs"].clear()
    assert "HttpOperationConfig" in registry_json_schema()["$defs"]


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"purpose": ""}, "purpose is required"),
        ({"parameters": {"customer": {"required": True}}}, "requires type"),
        ({"admission": "agent-read"}, "verified contractRef"),
        ({"config": {"service": "x", "method": "GET", "path": "/", "unknown": "secret"}}, "extra_forbidden"),
        ({"config": {"service": "x", "method": "DELETE", "path": "/"}}, "requires mutating"),
        (
            {"kind": "kafka-publish", "config": {"cluster": "x", "topicRef": "events", "valueTemplate": {}}},
            "requires mutating",
        ),
        (
            {"parameters": {"body": {"type": "object", "properties": {"nested": {}}}}},
            "body.nested",
        ),
    ],
)
def test_authoring_rejects_unsafe_or_incomplete_new_declarations(tmp_path, changes, expected) -> None:
    with pytest.raises(RuntimeFailure) as raised:
        validate_product_authoring(_plugin(tmp_path, _operation(**changes)))
    assert raised.value.code == "PRODUCT_AUTHORING_INVALID"
    assert expected in str(raised.value.details)
    assert "secret" not in str(raised.value.details)


def test_authoring_keeps_prototypes_and_custom_handlers_available(tmp_path) -> None:
    plugin = _plugin(tmp_path, _operation(kind="custom", config={"customConfig": True}))
    assert validate_product_authoring(plugin).ids() == ["customer.get"]


def test_authoring_rejects_dangling_guides_and_executable_steps(tmp_path) -> None:
    loaded = _plugin(tmp_path, _operation())
    plugin = ProductPlugin(
        "sample",
        loaded.registry_path,
        guides=(ProductGuide("customer", "Read a customer", {"steps": []}, ("missing",)),),
    )
    with pytest.raises(RuntimeFailure) as raised:
        validate_product_authoring(plugin)
    assert "unknown operation_ids: missing" in str(raised.value.details)
    assert "instead of executable steps" in str(raised.value.details)
