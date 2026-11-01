from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.lifecycle import CloseAttempt, close_resources
from qa_runtime.models import ExecutionContext, HttpRequest, HttpServiceConfig, OperationDefinition, Profile
from qa_runtime.ports.auth import AuthContext, AuthProvider
from qa_runtime.ports.credentials import CredentialSource
from qa_runtime.ports.http import HttpPort
from qa_runtime.ports.operation import ResultPayload
from qa_runtime.ports.tls import TlsProvider
from qa_runtime.security import SecretRedactor

PATH_PLACEHOLDER = re.compile(r"\{([^{}]+)}")


class HttpParameterLocation(StrEnum):
    PATH = "path"
    QUERY = "query"
    HEADER = "header"
    BODY = "body"
    CONTEXT = "context"
    CREDENTIAL_BODY = "credential-body"


class HttpOperationConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", allow_inf_nan=False)

    service: str
    method: str
    path: str
    timeout_seconds: float = Field(default=30, gt=0, le=300, alias="timeoutSeconds")
    max_response_bytes: int = Field(default=5 * 1024 * 1024, ge=1, le=100 * 1024 * 1024, alias="maxResponseBytes")
    body_root_parameter: str | None = Field(default=None, alias="bodyRootParameter")
    fixed_query: dict[str, Any] = Field(default_factory=dict, alias="fixedQuery")
    fixed_body: dict[str, Any] = Field(default_factory=dict, alias="fixedBody")
    response_body_fields: list[str] | None = Field(default=None, alias="responseBodyFields")

    @field_validator("method")
    @classmethod
    def normalize_method(cls, value: str) -> str:
        method = value.upper()
        if method not in {"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"}:
            raise ValueError(f"unsupported HTTP method '{value}'")
        return method

    @field_validator("response_body_fields")
    @classmethod
    def validate_response_body_fields(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = [field.strip() for field in value]
        if not normalized or any(not field for field in normalized):
            raise ValueError("responseBodyFields must contain non-empty top-level field names")
        if len(normalized) != len(set(normalized)):
            raise ValueError("responseBodyFields must not contain duplicates")
        return normalized


@dataclass(frozen=True, slots=True)
class CompiledHttpOperation:
    definition: OperationDefinition
    config: HttpOperationConfig
    service: HttpServiceConfig
    locations: dict[str, HttpParameterLocation]


class HttpOperationHandler:
    kind = "http"

    def __init__(
        self,
        http: HttpPort,
        *,
        auth_providers: dict[str, AuthProvider] | None = None,
        credentials: CredentialSource | None = None,
        tls_provider: TlsProvider | None = None,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self._http = http
        self._auth_providers = auth_providers or {}
        self._credentials = credentials
        self._tls_provider = tls_provider
        self._redactor = redactor or SecretRedactor()

    def compile(self, operation: OperationDefinition, profile: Profile) -> CompiledHttpOperation:
        try:
            config = HttpOperationConfig.model_validate(operation.config)
        except ValidationError as error:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "REGISTRY_INVALID",
                f"HTTP operation '{operation.id}' has invalid config: {error}",
                source="registry",
            ) from error

        service = profile.services.get(config.service)
        if service is None:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "SERVICE_NOT_CONFIGURED",
                f"operation '{operation.id}' references missing service '{config.service}'",
                source="compiler",
            )
        if service.auth_ref is not None and service.auth_ref not in self._auth_providers:
            raise RuntimeFailure(
                ExitCode.AUTHENTICATION_FAILED,
                "AUTH_PROVIDER_NOT_FOUND",
                f"no auth provider is configured for authRef '{service.auth_ref}'",
                source="compiler",
            )
        if service.tls_ref is not None and self._tls_provider is None:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "TLS_PROVIDER_NOT_FOUND",
                f"service '{config.service}' requires tlsRef '{service.tls_ref}', but no TLS provider is configured",
                source="compiler",
            )
        if self._tls_provider is not None:
            self._tls_provider.ssl_context(service.tls_ref)

        locations: dict[str, HttpParameterLocation] = {}
        declared_path_parameters: set[str] = set()
        for name, parameter in operation.parameters.items():
            try:
                location = HttpParameterLocation(parameter.location)
            except (TypeError, ValueError) as error:
                raise RuntimeFailure(
                    ExitCode.INVALID_INPUT,
                    "REGISTRY_INVALID",
                    f"HTTP parameter '{name}' in operation '{operation.id}' "
                    f"has invalid location '{parameter.location}'",
                    source="registry",
                ) from error
            locations[name] = location
            if location == HttpParameterLocation.PATH:
                declared_path_parameters.add(parameter.wire_name or name)

        path_parameters = set(PATH_PLACEHOLDER.findall(config.path))
        if path_parameters != declared_path_parameters:
            missing = sorted(path_parameters - declared_path_parameters)
            unused = sorted(declared_path_parameters - path_parameters)
            details = []
            if missing:
                details.append(f"undeclared placeholders: {', '.join(missing)}")
            if unused:
                details.append(f"unused path parameters: {', '.join(unused)}")
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "REGISTRY_INVALID",
                f"HTTP operation '{operation.id}' has inconsistent path bindings ({'; '.join(details)})",
                source="registry",
            )

        if config.body_root_parameter is not None:
            root_parameter = config.body_root_parameter
            root_location = locations.get(root_parameter)
            if root_location not in {HttpParameterLocation.BODY, HttpParameterLocation.CREDENTIAL_BODY}:
                raise RuntimeFailure(
                    ExitCode.INVALID_INPUT,
                    "REGISTRY_INVALID",
                    f"HTTP operation '{operation.id}' bodyRootParameter '{root_parameter}' "
                    "must reference a body parameter",
                    source="registry",
                )
            other_body_parameters = sorted(
                name
                for name, location in locations.items()
                if location in {HttpParameterLocation.BODY, HttpParameterLocation.CREDENTIAL_BODY}
                and name != root_parameter
            )
            if other_body_parameters:
                raise RuntimeFailure(
                    ExitCode.INVALID_INPUT,
                    "REGISTRY_INVALID",
                    f"HTTP operation '{operation.id}' bodyRootParameter cannot be combined with other body parameters: "
                    f"{', '.join(other_body_parameters)}",
                    source="registry",
                )
            if config.fixed_body:
                raise RuntimeFailure(
                    ExitCode.INVALID_INPUT,
                    "REGISTRY_INVALID",
                    f"HTTP operation '{operation.id}' bodyRootParameter cannot be combined with fixedBody",
                    source="registry",
                )

        query_wire_names = {
            operation.parameters[name].wire_name or name
            for name, location in locations.items()
            if location == HttpParameterLocation.QUERY
        }
        body_wire_names = {
            operation.parameters[name].wire_name or name
            for name, location in locations.items()
            if location in {HttpParameterLocation.BODY, HttpParameterLocation.CREDENTIAL_BODY}
        }
        duplicate_query = sorted(query_wire_names & set(config.fixed_query))
        duplicate_body = sorted(body_wire_names & set(config.fixed_body))
        if duplicate_query or duplicate_body:
            details = []
            if duplicate_query:
                details.append(f"query: {', '.join(duplicate_query)}")
            if duplicate_body:
                details.append(f"body: {', '.join(duplicate_body)}")
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "REGISTRY_INVALID",
                f"HTTP operation '{operation.id}' has fixed values that overlap parameters ({'; '.join(details)})",
                source="registry",
            )
        return CompiledHttpOperation(operation, config, service, locations)

    async def execute(
        self,
        plan: CompiledHttpOperation,
        params: dict[str, Any],
        context: ExecutionContext,
    ) -> ResultPayload:
        request_params = await self._resolve_credential_parameters(plan, params)
        request = self._build_request(plan, request_params, context.traceparent)
        auth_context = AuthContext(
            execution=context,
            operation_id=plan.definition.id,
            service=plan.config.service,
            params=params,
            mutating=plan.definition.mutating,
        )
        response = await self._send(request, auth_context, plan.service.auth_ref)
        response = response.model_copy(
            update={
                "headers": self._public_response_headers(response.headers),
                "body": self._redactor.redact(response.body),
            }
        )
        projection_error = None
        if plan.config.response_body_fields is not None:
            try:
                projected_body = self._project_response_body(response.body, plan.config.response_body_fields)
            except ValueError:
                # An unexpected body shape must not bypass the declared security
                # allowlist or discard the HTTP response that was obtained.
                projected_body = None
                projection_error = {
                    "code": "HTTP_RESPONSE_PROJECTION_FAILED",
                    "allowed_fields": plan.config.response_body_fields,
                }
            response = response.model_copy(update={"body": projected_body})
        payload = {
            "status_code": response.status_code,
            "headers": response.headers,
            "body": response.body,
        }
        if projection_error is not None:
            payload["projection_error"] = projection_error
        return payload

    async def _resolve_credential_parameters(
        self,
        plan: CompiledHttpOperation,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        resolved = dict(params)
        for name, location in plan.locations.items():
            if location != HttpParameterLocation.CREDENTIAL_BODY or name not in params:
                continue
            if self._credentials is None:
                raise RuntimeFailure(
                    ExitCode.AUTHENTICATION_FAILED,
                    "CREDENTIAL_SOURCE_NOT_CONFIGURED",
                    "operation requires a credential reference, but no credential source is configured",
                    source="credentials",
                    execution_outcome="not_sent",
                )
            try:
                secret = await self._credentials.get(str(params[name]))
            except RuntimeFailure as error:
                error.execution_outcome = "not_sent"
                raise
            self._redactor.register(secret)
            resolved[name] = secret.reveal()
        return resolved

    def _public_response_headers(self, headers: dict[str, str]) -> dict[str, str]:
        # Headers are response facts. Recursive redaction masks credentials while
        # retaining transport metadata such as pagination, cache and retry headers.
        return self._redactor.redact({name.lower(): value for name, value in headers.items()})

    @staticmethod
    def _project_response_body(body: Any, fields: list[str]) -> Any:
        def project(item: Any) -> dict[str, Any]:
            if not isinstance(item, Mapping):
                raise ValueError("response body shape cannot be safely projected")
            return {field: item[field] for field in fields if field in item}

        if isinstance(body, list):
            return [project(item) for item in body]
        return project(body)

    async def aclose(self) -> None:
        resources = {id(self._http): self._http}
        resources.update({id(provider): provider for provider in self._auth_providers.values()})
        await close_resources(
            [CloseAttempt(resource.aclose) for resource in resources.values()], source="http", code="HTTP_CLOSE_FAILED"
        )

    @staticmethod
    def _build_request(plan: CompiledHttpOperation, params: dict[str, Any], traceparent: str) -> HttpRequest:
        path = plan.config.path
        query = dict(plan.config.fixed_query)
        headers = {"accept": "application/json"}
        body = dict(plan.config.fixed_body)

        for parameter_name, definition in plan.definition.parameters.items():
            value = params.get(parameter_name)
            if value is None:
                continue
            wire_name = definition.wire_name or parameter_name
            location = plan.locations[parameter_name]
            if location == HttpParameterLocation.PATH:
                path = path.replace("{" + wire_name + "}", quote(str(value), safe=""))
            elif location == HttpParameterLocation.QUERY:
                query[wire_name] = value
            elif location == HttpParameterLocation.HEADER:
                headers[wire_name] = str(value)
            elif location in {HttpParameterLocation.BODY, HttpParameterLocation.CREDENTIAL_BODY}:
                body[wire_name] = value

        url = f"{plan.service.base_url.rstrip('/')}/{path.lstrip('/')}"
        headers["traceparent"] = traceparent
        root_parameter = plan.config.body_root_parameter
        json_body = params.get(root_parameter) if root_parameter is not None else body or None
        return HttpRequest(
            method=plan.config.method,
            url=url,
            headers=headers,
            query=query,
            json_body=json_body,
            tls_ref=plan.service.tls_ref,
            timeout_seconds=plan.config.timeout_seconds,
            max_response_bytes=plan.config.max_response_bytes,
        )

    async def _send(self, request: HttpRequest, context: AuthContext, auth_ref: str | None):
        if auth_ref is None:
            return await self._http.send(request, context.execution)
        provider = self._auth_providers[auth_ref]
        try:
            authorized = await provider.authorize(request, context)
        except RuntimeFailure as error:
            error.execution_outcome = "not_sent"
            raise
        response = await self._http.send(authorized, context.execution)
        if response.status_code != 401:
            return response
        await provider.invalidate(context)
        has_idempotency_key = any(name.lower() == "idempotency-key" for name in request.headers)
        retry_allowed = not context.mutating or has_idempotency_key
        if not retry_allowed:
            return response
        authorized = await provider.authorize(request, context)
        return await self._http.send(authorized, context.execution)
