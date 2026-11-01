"""Offline authoring aids; semantic execution plans still belong to CatalogCompiler."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel, ValidationError

from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.handlers.browser import BrowserOperationConfig
from qa_runtime.handlers.database import DatabaseOperationConfig
from qa_runtime.handlers.http import HttpOperationConfig
from qa_runtime.handlers.kafka import KafkaConsumeOperationConfig, KafkaPublishOperationConfig
from qa_runtime.handlers.kafka_offsets import KafkaOffsetsOperationConfig
from qa_runtime.handlers.kubernetes import KubernetesOperationConfig
from qa_runtime.handlers.s3 import S3OperationConfig
from qa_runtime.models import ParameterDefinition, RegistryDocument
from qa_runtime.ports.product import ProductPlugin
from qa_runtime.registry import OperationRegistry

_CONFIG_MODELS: dict[str, type[BaseModel]] = {
    "http": HttpOperationConfig,
    "browser": BrowserOperationConfig,
    "db-query": DatabaseOperationConfig,
    "kafka-consume": KafkaConsumeOperationConfig,
    "kafka-publish": KafkaPublishOperationConfig,
    "kafka-offsets": KafkaOffsetsOperationConfig,
    "s3": S3OperationConfig,
    "kubernetes": KubernetesOperationConfig,
}


def registry_json_schema() -> dict[str, Any]:
    """Describe canonical YAML names and built-in configs, preserving custom kinds.

    Pydantic's Python validators and cross-resource/binding checks cannot all be
    expressed here. Loading and compiling the registry remains authoritative.
    """
    schema = RegistryDocument.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["description"] = (
        "Registry authoring schema for canonical YAML aliases and lowercase kinds. "
        "Custom kinds keep an open config. This schema does not replace registry loading, "
        "handler compilation, source verification, or mutation authorization."
    )
    definitions = schema["$defs"]
    conditions: list[dict[str, Any]] = []
    for kind, model in _CONFIG_MODELS.items():
        config_schema = model.model_json_schema(by_alias=True)
        definitions.update(config_schema.pop("$defs", {}))
        definitions[model.__name__] = config_schema
        condition: dict[str, Any] = {"properties": {"kind": {"const": kind}}}
        if kind != "http":
            condition["required"] = ["kind"]
        conditions.append(
            {
                "if": condition,
                "then": {
                    "properties": {"config": {"$ref": f"#/$defs/{model.__name__}"}},
                    "required": ["config"],
                },
            }
        )
    definitions["OperationDefinition"]["allOf"] = conditions
    return schema


def _parameters(
    parameters: dict[str, ParameterDefinition], prefix: str = ""
) -> Iterator[tuple[str, ParameterDefinition]]:
    for name, parameter in parameters.items():
        path = f"{prefix}{name}"
        yield path, parameter
        if parameter.properties:
            yield from _parameters(parameter.properties, f"{path}.")
        if parameter.items is not None:
            yield from _parameters({"[]": parameter.items}, path)


def validate_product_authoring(plugin: ProductPlugin) -> OperationRegistry:
    """Apply the new-product scaffold's opt-in policy without a profile or I/O.

    This deliberately does not change admission or validation of existing packages.
    Verified provenance means metadata is present; authors must verify its source.
    Custom handler configs are checked by the product's compilation tests.
    """
    registry = OperationRegistry.load(plugin.registry_path)
    issues: list[str] = []
    for operation in registry.operations():
        label = f"operation '{operation.id}'"
        if not operation.purpose.strip():
            issues.append(f"{label}: purpose is required")
        for name, parameter in _parameters(operation.parameters):
            if parameter.value_type is None:
                issues.append(f"{label}: parameter '{name}' requires type")
        config_model = _CONFIG_MODELS.get(operation.kind)
        if config_model is not None:
            try:
                config_model.model_validate(operation.config)
            except ValidationError as error:
                # Config values are intentionally excluded from public errors.
                for item in error.errors(include_input=False, include_context=False, include_url=False):
                    location = ".".join(str(part) for part in item["loc"]) or "config"
                    issues.append(f"{label}: config.{location}: {item['type']}")
        if operation.kind == "kafka-publish" and not operation.mutating:
            issues.append(f"{label}: kafka-publish requires mutating: true")
        if (
            operation.kind == "http"
            and str(operation.config.get("method", "")).upper() in {"PUT", "PATCH", "DELETE"}
            and not operation.mutating
        ):
            issues.append(f"{label}: PUT/PATCH/DELETE requires mutating: true")
        if operation.admission != "prototype" and operation.kind in {"http", "kafka-consume", "kafka-publish"}:
            contract = operation.contract_ref
            if contract is None or contract.provenance == "unverified":
                issues.append(f"{label}: non-prototype HTTP/Kafka operations require verified contractRef metadata")

    guide_ids: set[str] = set()
    operation_ids = set(registry.ids())
    for guide in plugin.guides:
        label = f"guide '{guide.id}'"
        if not guide.id.strip() or not guide.purpose.strip() or guide.id in guide_ids:
            issues.append(f"{label}: id and purpose must be non-empty and id must be unique")
        guide_ids.add(guide.id)
        if len(guide.operation_ids) != len(set(guide.operation_ids)):
            issues.append(f"{label}: operation_ids must not repeat")
        missing = sorted(set(guide.operation_ids) - operation_ids)
        if missing:
            issues.append(f"{label}: unknown operation_ids: {', '.join(missing)}")
        if "steps" in guide.payload:
            issues.append(f"{label}: describe effects and observations instead of executable steps")
    if issues:
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "PRODUCT_AUTHORING_INVALID",
            "product authoring checks failed",
            source="authoring",
            details={"issues": issues},
            hint="Fix the reported declarations, then run the product's offline compilation and transport tests.",
        )
    return registry
