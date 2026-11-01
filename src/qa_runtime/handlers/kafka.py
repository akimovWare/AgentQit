from __future__ import annotations

import base64
import binascii
import copy
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import ExecutionContext, KafkaClusterConfig, OperationDefinition, Profile
from qa_runtime.ports.decoding import ValueDecoder, ValueEncoder
from qa_runtime.ports.kafka import (
    KafkaConsumeRequest,
    KafkaConsumeResult,
    KafkaCursor,
    KafkaJsonFieldMatch,
    KafkaPort,
    KafkaRecord,
    kafka_record_size,
    topic_fingerprint,
)
from qa_runtime.ports.operation import ResultPayload
from qa_runtime.ports.tls import TlsProvider
from qa_runtime.serialization import to_jsonable

type KafkaEncoding = Literal["auto", "json", "utf8", "base64"]
type KafkaValueEncoding = Literal["auto", "json", "utf8", "base64", "decoder"]


class KafkaJsonFieldFilterConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", allow_inf_nan=False)

    field_path: str = Field(alias="field")
    parameter: str

    @field_validator("field_path")
    @classmethod
    def valid_field_path(cls, value: str) -> str:
        components = [component.strip() for component in value.split(".")]
        if not components or any(not component for component in components):
            raise ValueError("must contain a dot-separated non-empty name")
        return ".".join(components)

    @field_validator("parameter")
    @classmethod
    def non_empty_parameter(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized


class KafkaHeaderBindingConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", allow_inf_nan=False)

    header_name: str = Field(alias="name")
    parameter: str
    encoding: Literal["utf8", "base64"] = "utf8"

    @field_validator("header_name", "parameter")
    @classmethod
    def non_empty_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized


class KafkaConsumeOperationConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", allow_inf_nan=False)

    cluster: str
    topic_ref: str = Field(alias="topicRef")
    key_encoding: KafkaEncoding = Field(default="auto", alias="keyEncoding")
    value_encoding: KafkaValueEncoding = Field(default="auto", alias="valueEncoding")
    value_decoder_ref: str | None = Field(default=None, alias="valueDecoderRef")
    default_timeout_seconds: float = Field(default=5, gt=0, alias="defaultTimeoutSeconds")
    default_max_records: int = Field(default=50, ge=1, le=1000, alias="defaultMaxRecords")
    max_bytes: int = Field(default=5 * 1024 * 1024, ge=1, le=100 * 1024 * 1024, alias="maxBytes")
    default_lookback_seconds: float | None = Field(default=300, ge=0, alias="defaultLookbackSeconds")
    key_filter_parameter: str | None = Field(default=None, alias="keyFilterParameter")
    value_contains_parameter: str | None = Field(default=None, alias="valueContainsParameter")
    timeout_parameter: str | None = Field(default=None, alias="timeoutParameter")
    max_records_parameter: str | None = Field(default=None, alias="maxRecordsParameter")
    lookback_parameter: str | None = Field(default=None, alias="lookbackParameter")
    cursor_parameter: str | None = Field(default=None, alias="cursorParameter")
    require_lookback_or_cursor: bool = Field(default=False, alias="requireLookbackOrCursor")
    json_field_filters: list[KafkaJsonFieldFilterConfig] = Field(default_factory=list, alias="jsonFieldFilters")
    stop_after_matches: int | None = Field(default=None, ge=1, alias="stopAfterMatches")

    @model_validator(mode="after")
    def validate_json_matching(self) -> KafkaConsumeOperationConfig:
        if self.require_lookback_or_cursor and (self.lookback_parameter is None or self.cursor_parameter is None):
            raise ValueError("requireLookbackOrCursor requires lookbackParameter and cursorParameter")
        if self.value_encoding == "decoder" and self.value_decoder_ref is None:
            raise ValueError("valueEncoding 'decoder' requires valueDecoderRef")
        if self.value_encoding != "decoder" and self.value_decoder_ref is not None:
            raise ValueError("valueDecoderRef requires valueEncoding 'decoder'")
        if self.json_field_filters and self.value_encoding not in {"auto", "json", "decoder"}:
            raise ValueError("jsonFieldFilters require valueEncoding 'auto', 'json' or 'decoder'")
        fields = [item.field_path for item in self.json_field_filters]
        if len(fields) != len(set(fields)):
            raise ValueError("jsonFieldFilters must not repeat a field")
        return self


class KafkaPublishOperationConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", allow_inf_nan=False)

    cluster: str
    topic_ref: str = Field(alias="topicRef")
    key_parameter: str | None = Field(default=None, alias="keyParameter")
    value_parameter: str | None = Field(default=None, alias="valueParameter")
    value_template: dict[str, Any] | None = Field(default=None, alias="valueTemplate")
    value_bindings: list[KafkaJsonFieldFilterConfig] = Field(default_factory=list, alias="valueBindings")
    key_encoding: Literal["utf8", "base64"] = Field(default="utf8", alias="keyEncoding")
    value_encoding: Literal["json", "utf8", "base64", "protobuf"] = Field(default="json", alias="valueEncoding")
    value_encoder_ref: str | None = Field(default=None, alias="valueEncoderRef")
    headers: dict[str, str] = Field(default_factory=dict)
    header_bindings: list[KafkaHeaderBindingConfig] = Field(default_factory=list, alias="headerBindings")
    max_bytes: int = Field(default=5 * 1024 * 1024, ge=1, le=100 * 1024 * 1024, alias="maxBytes")

    @model_validator(mode="after")
    def validate_value_source(self) -> KafkaPublishOperationConfig:
        if (self.value_parameter is None) == (self.value_template is None):
            raise ValueError("exactly one of valueParameter or valueTemplate is required")
        if self.value_template is not None and self.value_encoding not in {"json", "protobuf"}:
            raise ValueError("valueTemplate requires valueEncoding 'json' or 'protobuf'")
        if self.value_encoding == "protobuf" and self.value_encoder_ref is None:
            raise ValueError("valueEncoding 'protobuf' requires valueEncoderRef")
        if self.value_encoder_ref is not None and self.value_encoding != "protobuf":
            raise ValueError("valueEncoderRef requires valueEncoding 'protobuf'")
        if self.value_bindings and self.value_template is None:
            raise ValueError("valueBindings require valueTemplate")
        fields = [item.field_path for item in self.value_bindings]
        if len(fields) != len(set(fields)):
            raise ValueError("valueBindings must not repeat a field")
        if any(not name.strip() or name != name.strip() for name in self.headers):
            raise ValueError("header names must be non-empty and must not have surrounding whitespace")
        fixed_headers = {name.lower() for name in self.headers}
        if len(fixed_headers) != len(self.headers):
            raise ValueError("headers must not repeat a name ignoring case")
        bound_headers = [item.header_name.lower() for item in self.header_bindings]
        if len(bound_headers) != len(set(bound_headers)):
            raise ValueError("headerBindings must not repeat a header name")
        overlaps = sorted(fixed_headers & set(bound_headers))
        if overlaps:
            raise ValueError(f"headers and headerBindings overlap: {', '.join(overlaps)}")
        reserved = {"traceparent", "x-qa-run-id"}
        reserved_used = sorted((fixed_headers | set(bound_headers)) & reserved)
        if reserved_used:
            raise ValueError(f"runtime-managed headers cannot be configured: {', '.join(reserved_used)}")
        return self


@dataclass(frozen=True, slots=True)
class CompiledKafkaConsumeOperation:
    definition: OperationDefinition
    config: KafkaConsumeOperationConfig
    topic: str
    client_id_prefix: str
    value_decoder: ValueDecoder | None


@dataclass(frozen=True, slots=True)
class CompiledKafkaPublishOperation:
    definition: OperationDefinition
    config: KafkaPublishOperationConfig
    topic: str
    client_id_prefix: str
    value_encoder: ValueEncoder | None


class KafkaConsumeOperationHandler:
    kind = "kafka-consume"

    def __init__(
        self,
        kafka: KafkaPort,
        clusters: Mapping[str, KafkaClusterConfig],
        *,
        tls_provider: TlsProvider | None = None,
        value_decoders: Mapping[str, ValueDecoder] | None = None,
    ) -> None:
        self._kafka = kafka
        self._clusters = dict(clusters)
        self._tls_provider = tls_provider
        self._value_decoders = dict(value_decoders or {})

    def compile(self, operation: OperationDefinition, profile: Profile) -> CompiledKafkaConsumeOperation:
        try:
            config = KafkaConsumeOperationConfig.model_validate(operation.config)
        except ValidationError as error:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "REGISTRY_INVALID",
                f"Kafka consume operation '{operation.id}' has invalid config: {error}",
                source="registry",
            ) from error
        cluster, topic = _compile_endpoint(
            operation,
            config.cluster,
            config.topic_ref,
            self._clusters,
            self._tls_provider,
        )
        _validate_parameter_references(
            operation,
            config.key_filter_parameter,
            config.value_contains_parameter,
            config.timeout_parameter,
            config.max_records_parameter,
            config.lookback_parameter,
            config.cursor_parameter,
            *(item.parameter for item in config.json_field_filters),
        )
        value_decoder = None
        if config.cursor_parameter is not None and operation.parameters[config.cursor_parameter].value_type != "object":
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "REGISTRY_INVALID",
                "Kafka cursorParameter must reference an object parameter",
                source="registry",
            )
        if config.value_decoder_ref is not None:
            try:
                value_decoder = self._value_decoders[config.value_decoder_ref]
            except KeyError as error:
                raise RuntimeFailure(
                    ExitCode.INVALID_INPUT,
                    "VALUE_DECODER_NOT_CONFIGURED",
                    f"operation '{operation.id}' references missing value decoder '{config.value_decoder_ref}'",
                    source="compiler",
                ) from error
        return CompiledKafkaConsumeOperation(operation, config, topic, cluster.client_id_prefix, value_decoder)

    async def capture(self, plan: CompiledKafkaConsumeOperation, context: ExecutionContext) -> ResultPayload:
        cursor = await self._kafka.capture(
            plan.config.cluster,
            plan.topic,
            client_id=f"{plan.client_id_prefix}-{context.run_id}",
            timeout_seconds=plan.config.default_timeout_seconds,
        )
        return {"cursor": cursor.model_dump(mode="json"), "observation": {"kind": "checkpoint"}}

    def _consume_request(
        self,
        plan: CompiledKafkaConsumeOperation,
        params: dict[str, Any],
        *,
        client_id: str,
    ) -> KafkaConsumeRequest:
        config = plan.config
        cursor = None
        if config.cursor_parameter is not None and params.get(config.cursor_parameter) is not None:
            try:
                cursor = KafkaCursor.model_validate(params[config.cursor_parameter])
            except ValidationError as error:
                raise _invalid_param(config.cursor_parameter, "must be a valid Kafka checkpoint cursor") from error
            if cursor.cluster != config.cluster or not cursor.matches_topic(plan.topic):
                raise _invalid_param(config.cursor_parameter, "belongs to a different configured Kafka source or topic")
        if config.require_lookback_or_cursor and cursor is None and params.get(config.lookback_parameter) is None:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "MISSING_PARAM",
                f"operation requires parameter '{config.lookback_parameter}' or '{config.cursor_parameter}'",
                source="kafka",
                operation=plan.definition.id,
            )
        timeout = _number_param(params, config.timeout_parameter, config.default_timeout_seconds, minimum=0.001)
        max_records_number = _number_param(
            params,
            config.max_records_parameter,
            config.default_max_records,
            minimum=1,
        )
        if not max_records_number.is_integer():
            raise _invalid_param(config.max_records_parameter, "must be an integer")
        max_records = int(max_records_number)
        if max_records > 1000:
            raise _invalid_param(config.max_records_parameter, "must not exceed 1000")
        lookback = _optional_number_param(
            params,
            config.lookback_parameter,
            config.default_lookback_seconds,
            minimum=0,
        )
        key_filter = _optional_utf8(params, config.key_filter_parameter)
        value_contains = _optional_utf8(params, config.value_contains_parameter)
        json_fields_equal = tuple(
            KafkaJsonFieldMatch(
                path=tuple(field_filter.field_path.split(".")),
                expected=params[field_filter.parameter],
            )
            for field_filter in config.json_field_filters
            if field_filter.parameter in params
        )
        return KafkaConsumeRequest(
            cluster=config.cluster,
            topic=plan.topic,
            client_id=client_id,
            timeout_seconds=timeout,
            max_records=max_records,
            max_bytes=config.max_bytes,
            lookback_seconds=lookback,
            key_equals=key_filter,
            value_contains=value_contains,
            json_fields_equal=json_fields_equal,
            stop_after_matches=config.stop_after_matches,
            value_decoder=plan.value_decoder,
            after_cursor=cursor,
        )

    def validate_input(self, plan: CompiledKafkaConsumeOperation, params: dict[str, Any]) -> None:
        self._consume_request(plan, params, client_id=plan.client_id_prefix)

    async def execute(
        self,
        plan: CompiledKafkaConsumeOperation,
        params: dict[str, Any],
        context: ExecutionContext,
    ) -> ResultPayload:
        request = self._consume_request(plan, params, client_id=f"{plan.client_id_prefix}-{context.run_id}")
        config = plan.config
        cursor = request.after_cursor
        timeout = request.timeout_seconds
        expected = {".".join(item.path): item.expected for item in request.json_fields_equal}
        observation = {
            "mode": "after_cursor" if cursor is not None else "lookback",
            "topic_fingerprint": topic_fingerprint(plan.topic),
            "after_cursor": cursor.model_dump(mode="json") if cursor is not None else None,
            "lookback_seconds": None if cursor is not None else request.lookback_seconds,
            "timeout_seconds": timeout,
            "cleanup_timeout_seconds": min(1.0, timeout),
            "match_fields": expected,
            "guarantee": "matching publication; business causality requires independent correlation",
        }
        consumed = await self._kafka.consume(request)
        if not isinstance(consumed, KafkaConsumeResult):
            # Legacy/fake ports can return a list; enforce the same observation
            # budget before exposing its records through the native handler.
            retained: list[KafkaRecord] = []
            size_bytes = 0
            reason = None
            for record in consumed:
                size = kafka_record_size(record)
                if len(retained) >= request.max_records or size_bytes + size > request.max_bytes:
                    reason = "max_records" if len(retained) >= request.max_records else "max_bytes"
                    break
                retained.append(record)
                size_bytes += size
            consumed = KafkaConsumeResult(
                retained, size_bytes=size_bytes, truncated=reason is not None, truncation_reason=reason
            )
        records = consumed.records
        decode_errors = list(consumed.decode_errors)
        record_payloads = [
            _record_payload(
                record,
                config.key_encoding,
                config.value_encoding,
                plan.value_decoder,
                plan.definition.id,
            )
            for record in records
        ]
        decode_errors.extend(
            {
                "partition": record["partition"],
                "offset": record["offset"],
                **record["decode_error"],
            }
            for record in record_payloads
            if "decode_error" in record
        )
        observation["matched"] = True if records else None if consumed.truncated or decode_errors else False
        observation["decode_error_count"] = len(decode_errors)
        observation["decode_errors_truncated"] = consumed.decode_errors_truncated
        observation.update(
            max_bytes=request.max_bytes,
            size_bytes=consumed.size_bytes,
            truncated=consumed.truncated,
            truncation_reason=consumed.truncation_reason,
        )
        return {
            "cluster": config.cluster,
            "topic": plan.topic,
            "record_count": len(records),
            "observation": observation,
            "decode_errors": decode_errors,
            "records": record_payloads,
        }

    async def aclose(self) -> None:
        await self._kafka.aclose()


class KafkaPublishOperationHandler:
    kind = "kafka-publish"

    def __init__(
        self,
        kafka: KafkaPort,
        clusters: Mapping[str, KafkaClusterConfig],
        *,
        tls_provider: TlsProvider | None = None,
        value_encoders: Mapping[str, ValueEncoder] | None = None,
    ) -> None:
        self._kafka = kafka
        self._clusters = dict(clusters)
        self._tls_provider = tls_provider
        self._value_encoders = dict(value_encoders or {})

    def compile(self, operation: OperationDefinition, profile: Profile) -> CompiledKafkaPublishOperation:
        if not operation.mutating:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "REGISTRY_INVALID",
                f"Kafka publish operation '{operation.id}' must declare mutating: true",
                source="registry",
            )
        try:
            config = KafkaPublishOperationConfig.model_validate(operation.config)
        except ValidationError as error:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "REGISTRY_INVALID",
                f"Kafka publish operation '{operation.id}' has invalid config: {error}",
                source="registry",
            ) from error
        cluster, topic = _compile_endpoint(
            operation,
            config.cluster,
            config.topic_ref,
            self._clusters,
            self._tls_provider,
        )
        _validate_parameter_references(
            operation,
            config.key_parameter,
            config.value_parameter,
            *(item.parameter for item in config.value_bindings),
            *(item.parameter for item in config.header_bindings),
        )
        _validate_required_publish_parameter(operation, profile, config.key_parameter, "keyParameter")
        _validate_required_publish_parameter(operation, profile, config.value_parameter, "valueParameter")
        if config.value_template is not None:
            for binding in config.value_bindings:
                _validate_json_template_path(operation, config.value_template, binding.field_path)
        value_encoder = None
        if config.value_encoder_ref is not None:
            try:
                value_encoder = self._value_encoders[config.value_encoder_ref]
            except KeyError as error:
                raise RuntimeFailure(
                    ExitCode.INVALID_INPUT,
                    "VALUE_ENCODER_NOT_CONFIGURED",
                    f"operation '{operation.id}' references missing value encoder '{config.value_encoder_ref}'",
                    source="compiler",
                ) from error
        return CompiledKafkaPublishOperation(operation, config, topic, cluster.client_id_prefix, value_encoder)

    def _publish_payload(
        self,
        plan: CompiledKafkaPublishOperation,
        params: dict[str, Any],
    ) -> tuple[bytes | None, bytes, list[tuple[str, bytes | None]]]:
        config = plan.config
        key = None
        if config.key_parameter is not None:
            key = _encode(
                params[config.key_parameter],
                config.key_encoding,
                parameter_name=config.key_parameter,
            )
        if config.value_parameter is not None:
            raw_value = params[config.value_parameter]
            value_parameter_name = config.value_parameter
        else:
            raw_value = copy.deepcopy(config.value_template)
            for binding in config.value_bindings:
                if binding.parameter in params:
                    _set_json_template_value(raw_value, binding.field_path, params[binding.parameter])
            value_parameter_name = "valueTemplate"
        if plan.value_encoder is not None:
            try:
                value = plan.value_encoder.encode(raw_value)
            except Exception as error:
                raise RuntimeFailure(
                    ExitCode.INVALID_INPUT,
                    "VALUE_ENCODE_FAILED",
                    f"Kafka publish value does not match the configured encoder for parameter '{value_parameter_name}'",
                    source="kafka",
                    operation=plan.definition.id,
                    execution_outcome="not_sent",
                ) from error
        else:
            value = _encode(raw_value, config.value_encoding, parameter_name=value_parameter_name)
        headers = [(name, value.encode("utf-8")) for name, value in config.headers.items()]
        headers.extend(
            (
                binding.header_name,
                _encode(params[binding.parameter], binding.encoding, parameter_name=binding.parameter),
            )
            for binding in config.header_bindings
            if binding.parameter in params
        )
        size_bytes = (
            len(key or b"")
            + len(value)
            + sum(len(name.encode("utf-8")) + len(header_value or b"") for name, header_value in headers)
        )
        if size_bytes > config.max_bytes:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "KAFKA_PAYLOAD_TOO_LARGE",
                "Kafka publish payload exceeds the configured byte limit",
                source="kafka",
                details={"max_bytes": config.max_bytes, "size_bytes": size_bytes},
                execution_outcome="not_sent",
            )
        return key, value, headers

    def validate_input(self, plan: CompiledKafkaPublishOperation, params: dict[str, Any]) -> None:
        self._publish_payload(plan, params)

    async def execute(
        self,
        plan: CompiledKafkaPublishOperation,
        params: dict[str, Any],
        context: ExecutionContext,
    ) -> ResultPayload:
        config = plan.config
        key, value, headers = self._publish_payload(plan, params)
        headers.extend(
            [
                ("traceparent", context.traceparent.encode("ascii")),
                ("x-qa-run-id", context.run_id.encode("ascii")),
            ]
        )
        result = await self._kafka.publish(
            config.cluster,
            plan.topic,
            key=key,
            value=value,
            headers=headers,
            client_id=plan.client_id_prefix,
        )
        return {
            "cluster": config.cluster,
            "topic": result.topic,
            "partition": result.partition,
            "offset": result.offset,
            "timestamp_ms": result.timestamp_ms,
        }

    async def aclose(self) -> None:
        await self._kafka.aclose()


def _compile_endpoint(
    operation: OperationDefinition,
    cluster_ref: str,
    topic_ref: str,
    clusters: Mapping[str, KafkaClusterConfig],
    tls_provider: TlsProvider | None,
) -> tuple[KafkaClusterConfig, str]:
    cluster = clusters.get(cluster_ref)
    if cluster is None:
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "KAFKA_CLUSTER_NOT_CONFIGURED",
            f"operation '{operation.id}' references missing Kafka cluster '{cluster_ref}'",
            source="compiler",
        )
    try:
        topic = cluster.topics[topic_ref]
    except KeyError as error:
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "KAFKA_TOPIC_NOT_CONFIGURED",
            f"operation '{operation.id}' references missing topic '{topic_ref}' in cluster '{cluster_ref}'",
            source="compiler",
        ) from error
    if cluster.truststore is not None and not cluster.truststore.location.is_file():
        raise RuntimeFailure(
            ExitCode.ENVIRONMENT_UNAVAILABLE,
            "KAFKA_TRUSTSTORE_NOT_FOUND",
            f"Kafka cluster '{cluster_ref}' truststore file is missing",
            source="compiler",
        )
    if cluster.tls_ref is not None:
        if tls_provider is None:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "TLS_PROVIDER_NOT_FOUND",
                f"Kafka cluster '{cluster_ref}' requires tlsRef '{cluster.tls_ref}'",
                source="compiler",
            )
        tls_provider.ssl_context(cluster.tls_ref)
    return cluster, topic


def _validate_json_template_path(operation: OperationDefinition, template: dict[str, Any], field_path: str) -> None:
    cursor: Any = template
    components = field_path.split(".")
    for component in components[:-1]:
        if not isinstance(cursor, Mapping) or component not in cursor:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "REGISTRY_INVALID",
                f"Kafka publish operation '{operation.id}' value binding '{field_path}' "
                f"references missing object '{component}'",
                source="registry",
            )
        cursor = cursor[component]
    if not isinstance(cursor, Mapping):
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "REGISTRY_INVALID",
            f"Kafka publish operation '{operation.id}' value binding '{field_path}' parent is not an object",
            source="registry",
        )


def _set_json_template_value(template: Any, field_path: str, value: Any) -> None:
    cursor = template
    components = field_path.split(".")
    for component in components[:-1]:
        cursor = cursor[component]
    cursor[components[-1]] = value


def _validate_parameter_references(operation: OperationDefinition, *references: str | None) -> None:
    unknown = [reference for reference in references if reference is not None and reference not in operation.parameters]
    if unknown:
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "REGISTRY_INVALID",
            f"operation '{operation.id}' references unknown parameters: {', '.join(unknown)}",
            source="registry",
        )


def _validate_required_publish_parameter(
    operation: OperationDefinition,
    profile: Profile,
    reference: str | None,
    config_field: str,
) -> None:
    if reference is None:
        return
    parameter = operation.parameters[reference]
    if parameter.required or parameter.default is not None or profile.defaults.get(reference) is not None:
        return
    raise RuntimeFailure(
        ExitCode.INVALID_INPUT,
        "REGISTRY_INVALID",
        f"Kafka publish operation '{operation.id}' {config_field} '{reference}' "
        "must reference a required parameter or a non-null default",
        source="registry",
    )


def _number_param(
    params: dict[str, Any],
    name: str | None,
    default: float | int,
    *,
    minimum: float,
) -> float:
    value = default if name is None else params.get(name, default)
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise _invalid_param(name, "must be numeric") from error
    if not math.isfinite(number) or number < minimum:
        raise _invalid_param(name, f"must be at least {minimum}")
    return number


def _optional_number_param(
    params: dict[str, Any],
    name: str | None,
    default: float | None,
    *,
    minimum: float,
) -> float | None:
    value = default if name is None else params.get(name, default)
    if value is None:
        return None
    return _number_param({name or "value": value}, name or "value", value, minimum=minimum)


def _optional_utf8(params: dict[str, Any], name: str | None) -> bytes | None:
    if name is None or params.get(name) is None:
        return None
    return str(params[name]).encode("utf-8")


def _invalid_param(name: str | None, detail: str) -> RuntimeFailure:
    return RuntimeFailure(
        ExitCode.INVALID_INPUT,
        "INVALID_PARAM",
        f"parameter '{name or '<configured default>'}' {detail}",
        source="kafka",
        execution_outcome="not_sent",
    )


def _record_payload(
    record: KafkaRecord,
    key_encoding: KafkaEncoding,
    value_encoding: KafkaValueEncoding,
    value_decoder: ValueDecoder | None,
    operation_id: str,
) -> dict[str, Any]:
    payload = {
        "topic": record.topic,
        "partition": record.partition,
        "offset": record.offset,
        "timestamp_ms": record.timestamp_ms,
        "key": _decode(record.key, key_encoding),
        "headers": [{"name": name, "value": _decode(value, "auto")} for name, value in record.headers],
    }
    try:
        payload["value"] = _decode_record_value(record.value, value_encoding, value_decoder, operation_id)
    except Exception:
        payload["value"] = _decode(record.value, "base64")
        payload["decode_error"] = {"code": "KAFKA_VALUE_DECODE_FAILED"}
    return payload


def _decode_record_value(
    value: bytes | None,
    encoding: KafkaValueEncoding,
    decoder: ValueDecoder | None,
    operation_id: str,
) -> Any:
    if value is None or encoding != "decoder":
        return _decode(value, encoding)
    if decoder is None:  # pragma: no cover - compile invariant
        raise RuntimeFailure(
            ExitCode.INTERNAL_ERROR,
            "VALUE_DECODER_NOT_CONFIGURED",
            "compiled Kafka operation has no value decoder",
            source="kafka",
            operation=operation_id,
        )
    return decoder.decode(value)


def _decode(value: bytes | None, encoding: KafkaEncoding) -> Any:
    if value is None:
        return None
    if encoding == "base64":
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    if encoding == "utf8":
        return text
    if encoding in {"auto", "json"}:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text


def _encode(
    value: Any,
    encoding: Literal["json", "utf8", "base64"],
    *,
    parameter_name: str | None = None,
) -> bytes:
    if encoding == "base64":
        try:
            return base64.b64decode(str(value), validate=True)
        except (binascii.Error, ValueError, TypeError) as error:
            raise _invalid_param(parameter_name, "must contain valid base64") from error
    if encoding == "utf8":
        return str(value).encode("utf-8")
    return json.dumps(to_jsonable(value), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
