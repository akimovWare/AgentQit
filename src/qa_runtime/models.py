from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from qa_runtime.credential_config import EnvironmentCredentialConfig, VaultCredentialConfig
from qa_runtime.security import SENSITIVE_KEYS


def _contains_credential_query(query: str) -> bool:
    return any(
        "".join(character for character in name.lower() if character.isalnum()) in SENSITIVE_KEYS
        for name, _ in parse_qsl(query, keep_blank_values=True)
    )


def _validate_url_port(parsed) -> None:
    if parsed.netloc.endswith(":"):
        raise ValueError("URL port must not be empty")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("URL port must be within 1..65535")


class ContractReference(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    artifact: str
    version: str
    operation_id: str | None = Field(default=None, alias="operationId")
    message_type: str | None = Field(default=None, alias="messageType")
    provenance: Literal["unverified", "audited-checkout", "pinned-artifact"] = "unverified"
    source_revision: str | None = Field(default=None, alias="sourceRevision")
    checksum_sha256: str | None = Field(default=None, alias="checksumSha256")

    @field_validator("artifact", "version", "operation_id", "message_type", "source_revision")
    @classmethod
    def non_empty_reference_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @field_validator("checksum_sha256")
    @classmethod
    def validate_checksum(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
            raise ValueError("checksumSha256 must contain 64 hexadecimal characters")
        return normalized

    @model_validator(mode="after")
    def validate_identity_and_provenance(self) -> ContractReference:
        identities = [self.operation_id is not None, self.message_type is not None]
        if sum(identities) != 1:
            raise ValueError("exactly one of operationId or messageType must be set")
        if self.provenance == "audited-checkout" and self.source_revision is None:
            raise ValueError("audited-checkout provenance requires sourceRevision")
        if self.provenance == "pinned-artifact" and self.checksum_sha256 is None:
            raise ValueError("pinned-artifact provenance requires checksumSha256")
        return self


class EffectiveContractOverride(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    target: str
    contract_value: Any = Field(alias="contractValue")
    effective_value: Any = Field(alias="effectiveValue")
    source: str
    reason: str
    evidence: list[str] = Field(default_factory=list)

    @field_validator("target", "source", "reason")
    @classmethod
    def non_empty_override_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @field_validator("evidence")
    @classmethod
    def non_empty_evidence(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("evidence entries must not be empty")
        return normalized


class ParameterDefinition(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    location: str | None = None
    wire_name: str | None = Field(default=None, alias="name")
    required: bool = False
    default: Any = None
    enum: list[str] | None = None
    value_type: Literal["string", "number", "integer", "boolean", "object", "array"] | None = Field(
        default=None,
        alias="type",
    )
    format: Literal["uuid", "date", "date-time"] | None = None
    min_length: int | None = Field(default=None, ge=0, alias="minLength")
    max_length: int | None = Field(default=None, ge=0, alias="maxLength")
    min_items: int | None = Field(default=None, ge=0, alias="minItems")
    max_items: int | None = Field(default=None, ge=0, alias="maxItems")
    unique_items: bool = Field(default=False, alias="uniqueItems")
    items: ParameterDefinition | None = None
    properties: dict[str, ParameterDefinition] | None = None
    additional_properties: bool = Field(default=True, alias="additionalProperties")
    minimum: float | None = None
    exclusive_minimum: bool = Field(default=False, alias="exclusiveMinimum")
    maximum: float | None = None
    exclusive_maximum: bool = Field(default=False, alias="exclusiveMaximum")
    less_than: str | None = Field(default=None, alias="lessThan")

    @model_validator(mode="after")
    def validate_constraints(self) -> ParameterDefinition:
        if self.format is not None and self.value_type != "string":
            raise ValueError("format requires type 'string'")
        if (self.min_length is not None or self.max_length is not None) and self.value_type != "string":
            raise ValueError("minLength/maxLength require type 'string'")
        if self.min_length is not None and self.max_length is not None and self.min_length > self.max_length:
            raise ValueError("minLength must not exceed maxLength")
        has_array_constraints = self.min_items is not None or self.max_items is not None or self.unique_items
        if (has_array_constraints or self.items is not None) and self.value_type != "array":
            raise ValueError("items/minItems/maxItems/uniqueItems require type 'array'")
        if self.value_type == "array" and self.items is None:
            raise ValueError("type 'array' requires items")
        if self.min_items is not None and self.max_items is not None and self.min_items > self.max_items:
            raise ValueError("minItems must not exceed maxItems")
        if self.minimum is not None and self.value_type not in {"number", "integer"}:
            raise ValueError("minimum requires type 'number' or 'integer'")
        if self.exclusive_minimum and self.minimum is None:
            raise ValueError("exclusiveMinimum requires minimum")
        if self.maximum is not None and self.value_type not in {"number", "integer"}:
            raise ValueError("maximum requires type 'number' or 'integer'")
        if self.exclusive_maximum and self.maximum is None:
            raise ValueError("exclusiveMaximum requires maximum")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum must not exceed maximum")
        if self.properties is not None and self.value_type != "object":
            raise ValueError("properties require type 'object'")
        if not self.additional_properties and self.value_type != "object":
            raise ValueError("additionalProperties=false requires type 'object'")
        if self.enum is not None and self.value_type not in {None, "string"}:
            raise ValueError("enum requires type 'string'")
        if self.enum is not None and len({item.upper() for item in self.enum}) != len(self.enum):
            raise ValueError("enum values must be unique ignoring case")
        if self.less_than is not None:
            self.less_than = self.less_than.strip()
            if not self.less_than:
                raise ValueError("lessThan must reference a parameter")
            if self.value_type not in {"number", "integer"}:
                raise ValueError("lessThan requires type 'number' or 'integer'")
        return self


class OperationDefinition(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str
    kind: str = "http"
    purpose: str = ""
    mutating: bool = False
    admission: Literal["prototype", "diagnostic", "agent-read", "agent-mutate", "expert-only"] = "prototype"
    parameters: dict[str, ParameterDefinition] = Field(default_factory=dict)
    contract_ref: ContractReference | None = Field(default=None, alias="contractRef")
    effective_overrides: list[EffectiveContractOverride] = Field(default_factory=list, alias="effectiveOverrides")
    error_hints: dict[str, str] = Field(default_factory=dict, alias="errorHints")
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def non_empty_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @field_validator("kind")
    @classmethod
    def normalize_kind(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @model_validator(mode="after")
    def overrides_require_contract_reference(self) -> OperationDefinition:
        if self.effective_overrides and self.contract_ref is None:
            raise ValueError("effectiveOverrides require contractRef")
        if self.mutating and self.admission == "agent-read":
            raise ValueError("mutating operations cannot use agent-read admission")
        if not self.mutating and self.admission == "agent-mutate":
            raise ValueError("non-mutating operations cannot use agent-mutate admission")
        for name, parameter in self.parameters.items():
            if parameter.less_than is None:
                continue
            referenced = self.parameters.get(parameter.less_than)
            if referenced is None:
                raise ValueError(f"parameter '{name}' lessThan references unknown parameter '{parameter.less_than}'")
            if referenced.value_type not in {"number", "integer"}:
                raise ValueError(f"parameter '{name}' lessThan must reference a numeric parameter")
        normalized_hints = {code.strip(): hint.strip() for code, hint in self.error_hints.items()}
        if any(not code or not hint for code, hint in normalized_hints.items()):
            raise ValueError("errorHints codes and messages must not be empty")
        self.error_hints = normalized_hints
        return self


class CodecDefinition(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    format: Literal["json", "utf8", "base64", "protobuf"]
    descriptor_path: Path | None = Field(default=None, alias="descriptorPath")
    message_type: str | None = Field(default=None, alias="messageType")
    checksum_sha256: str | None = Field(default=None, alias="checksumSha256")

    @model_validator(mode="after")
    def validate_protobuf(self) -> CodecDefinition:
        if self.format == "protobuf":
            if self.descriptor_path is None or not self.message_type or not self.message_type.strip():
                raise ValueError("protobuf codec requires descriptorPath and messageType")
            if self.checksum_sha256 is None:
                raise ValueError("protobuf codec requires checksumSha256")
            self.message_type = self.message_type.strip()
        elif any(value is not None for value in (self.descriptor_path, self.message_type, self.checksum_sha256)):
            raise ValueError("descriptorPath/messageType/checksumSha256 require format 'protobuf'")
        if self.checksum_sha256 is not None:
            normalized = self.checksum_sha256.strip().lower()
            if len(normalized) != 64 or any(c not in "0123456789abcdef" for c in normalized):
                raise ValueError("checksumSha256 must contain 64 hexadecimal characters")
            self.checksum_sha256 = normalized
        return self


class RegistryDocument(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    api_version: Literal["qa.runtime/v1"] = Field(alias="apiVersion")
    kind: Literal["OperationRegistry"]
    operations: list[OperationDefinition]
    codecs: dict[str, CodecDefinition] = Field(default_factory=dict)


class HttpServiceConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    base_url: str = Field(alias="baseUrl")
    auth_ref: str | None = Field(default=None, alias="authRef")
    tls_ref: str | None = Field(default=None, alias="tlsRef")

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        _validate_url_port(parsed)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("baseUrl must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None or _contains_credential_query(parsed.query):
            raise ValueError("baseUrl must not contain credentials; use an auth provider and CredentialSource")
        return value


class DatabaseServiceConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", allow_inf_nan=False)

    dsn: str
    username_ref: str = Field(alias="usernameRef")
    password_ref: str = Field(alias="passwordRef")
    schema_name: str | None = Field(default=None, alias="schema")
    tls_ref: str | None = Field(default=None, alias="tlsRef")
    min_pool_size: int = Field(default=1, ge=1, alias="minPoolSize")
    max_pool_size: int = Field(default=5, ge=1, alias="maxPoolSize")
    connect_timeout_seconds: float = Field(default=10, gt=0, le=300, alias="connectTimeoutSeconds")
    command_timeout_seconds: float = Field(default=30, gt=0, le=300, alias="commandTimeoutSeconds")

    @field_validator("dsn")
    @classmethod
    def validate_dsn(cls, value: str) -> str:
        parsed = urlsplit(value)
        _validate_url_port(parsed)
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or not parsed.path.strip("/"):
            raise ValueError("dsn must be an absolute PostgreSQL URL with a database name")
        if parsed.username is not None or parsed.password is not None or _contains_credential_query(parsed.query):
            raise ValueError("dsn must not contain credentials; use usernameRef/passwordRef")
        return value

    @model_validator(mode="after")
    def validate_pool_size(self) -> DatabaseServiceConfig:
        if self.min_pool_size > self.max_pool_size:
            raise ValueError("minPoolSize must not exceed maxPoolSize")
        return self


class KafkaTruststoreConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    store_type: Literal["JKS"] = Field(default="JKS", alias="type")
    location: Path
    password_ref: str = Field(alias="passwordRef")

    @field_validator("location")
    @classmethod
    def normalize_location(cls, value: Path) -> Path:
        location = value.expanduser()
        if not location.is_absolute():
            raise ValueError("location must be an absolute path")
        return location

    @field_validator("password_ref")
    @classmethod
    def non_empty_password_reference(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("passwordRef must not be empty")
        return normalized


class KafkaClusterConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", allow_inf_nan=False)

    bootstrap_servers: list[str] = Field(min_length=1, alias="bootstrapServers")
    security_protocol: Literal["PLAINTEXT", "SSL", "SASL_PLAINTEXT", "SASL_SSL"] = Field(
        default="PLAINTEXT",
        alias="securityProtocol",
    )
    sasl_mechanism: Literal["PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"] | None = Field(
        default=None,
        alias="saslMechanism",
    )
    username_ref: str | None = Field(default=None, alias="usernameRef")
    password_ref: str | None = Field(default=None, alias="passwordRef")
    tls_ref: str | None = Field(default=None, alias="tlsRef")
    truststore: KafkaTruststoreConfig | None = None
    client_id_prefix: str = Field(default="qa-cli", alias="clientIdPrefix")
    request_timeout_seconds: float = Field(default=30, gt=0, le=300, alias="requestTimeoutSeconds")
    topics: dict[str, str] = Field(default_factory=dict)

    @field_validator("bootstrap_servers")
    @classmethod
    def validate_bootstrap_servers(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or ":" not in value for value in values):
            raise ValueError("each bootstrap server must use host:port syntax")
        return values

    @model_validator(mode="after")
    def validate_security(self) -> KafkaClusterConfig:
        uses_sasl = self.security_protocol.startswith("SASL_")
        uses_tls = self.security_protocol.endswith("SSL")
        has_credentials = self.username_ref is not None or self.password_ref is not None
        if uses_sasl and (self.sasl_mechanism is None or not self.username_ref or not self.password_ref):
            raise ValueError("SASL protocol requires saslMechanism, usernameRef and passwordRef")
        if not uses_sasl and (self.sasl_mechanism is not None or has_credentials):
            raise ValueError("SASL settings require a SASL securityProtocol")
        if self.truststore is not None and not uses_tls:
            raise ValueError("truststore requires an SSL securityProtocol")
        if self.truststore is not None and self.tls_ref is not None:
            raise ValueError("truststore and tlsRef are mutually exclusive")
        return self


class S3ServiceConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", allow_inf_nan=False)

    endpoint: str
    region: str
    access_key_ref: str = Field(alias="accessKeyRef")
    secret_key_ref: str = Field(alias="secretKeyRef")
    session_token_ref: str | None = Field(default=None, alias="sessionTokenRef")
    tls_ref: str | None = Field(default=None, alias="tlsRef")
    addressing_style: Literal["path", "virtual"] = Field(default="path", alias="addressingStyle")
    connect_timeout_seconds: float = Field(default=10, gt=0, le=300, alias="connectTimeoutSeconds")
    request_timeout_seconds: float = Field(default=30, gt=0, le=300, alias="requestTimeoutSeconds")
    buckets: dict[str, str] = Field(default_factory=dict)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        _validate_url_port(parsed)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("endpoint must be an absolute HTTP(S) origin")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("endpoint must not contain credentials; use credential references")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("endpoint must be an HTTP(S) origin without a path, query or fragment")
        return value.rstrip("/")

    @field_validator("region", "access_key_ref", "secret_key_ref", "session_token_ref", "tls_ref")
    @classmethod
    def non_empty_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("buckets")
    @classmethod
    def validate_buckets(cls, values: dict[str, str]) -> dict[str, str]:
        for reference, name in values.items():
            if not reference.strip() or not name or len(name) > 63:
                raise ValueError("bucket references and names must be nonempty; names must not exceed 63 characters")
            if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for character in name):
                raise ValueError("bucket names must contain only lowercase letters, digits, dots and hyphens")
            if name.startswith((".", "-")) or name.endswith((".", "-")):
                raise ValueError("bucket names must start and end with a letter or digit")
        return values

    @model_validator(mode="after")
    def validate_tls(self) -> S3ServiceConfig:
        if self.tls_ref is not None and urlsplit(self.endpoint).scheme != "https":
            raise ValueError("tlsRef requires an HTTPS endpoint")
        return self


class KubernetesServiceConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", allow_inf_nan=False)

    endpoint: str
    executable: Path
    context: str
    request_timeout_seconds: float = Field(default=15, gt=0, le=300, alias="requestTimeoutSeconds")

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        _validate_url_port(parsed)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("endpoint must be an HTTPS origin without credentials, path, query or fragment")
        return value.rstrip("/")

    @field_validator("executable")
    @classmethod
    def validate_executable(cls, value: Path) -> Path:
        value = value.expanduser()
        if not value.is_absolute():
            raise ValueError("executable must be an absolute trusted path")
        return value

    @field_validator("context")
    @classmethod
    def validate_context(cls, value: str) -> str:
        value = value.strip()
        if (
            not value
            or len(value) > 200
            or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.@:-" for c in value)
        ):
            raise ValueError("context must be a nonempty Kubernetes context name")
        return value


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment: str = "unknown"
    services: dict[str, HttpServiceConfig] = Field(default_factory=dict)
    databases: dict[str, DatabaseServiceConfig] = Field(default_factory=dict)
    kafka: dict[str, KafkaClusterConfig] = Field(default_factory=dict)
    s3: dict[str, S3ServiceConfig] = Field(default_factory=dict)
    kubernetes: dict[str, KubernetesServiceConfig] = Field(default_factory=dict)
    endpoints: dict[str, str] = Field(default_factory=dict)
    defaults: dict[str, Any] = Field(default_factory=dict)
    credentials: EnvironmentCredentialConfig | VaultCredentialConfig | None = None

    @field_validator("endpoints")
    @classmethod
    def reject_endpoint_credentials(cls, values: dict[str, str]) -> dict[str, str]:
        for value in values.values():
            parsed = urlsplit(value)
            _validate_url_port(parsed)
            if parsed.username is not None or parsed.password is not None or _contains_credential_query(parsed.query):
                raise ValueError("endpoints must not contain credentials; use CredentialSource")
        return values


class ExecutionContext(BaseModel):
    run_id: str
    environment: str
    trace_id: str
    traceparent: str
    session_id: str | None = None


class HttpRequest(BaseModel):
    method: str
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    json_body: Any = None
    tls_ref: str | None = None
    timeout_seconds: float = Field(default=30, gt=0, le=300, allow_inf_nan=False)
    max_response_bytes: int = Field(default=5 * 1024 * 1024, ge=1, le=100 * 1024 * 1024)


class HttpResult(BaseModel):
    status_code: int
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
