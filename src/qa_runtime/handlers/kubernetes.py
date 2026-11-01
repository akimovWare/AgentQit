from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import ExecutionContext, OperationDefinition, Profile
from qa_runtime.ports.kubernetes import KubernetesPort, KubernetesReadRequest
from qa_runtime.security import SecretRedactor

RESOURCE_NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")


class KubernetesJsonFieldFilter(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    field: str = Field(min_length=1)
    parameter: str = Field(min_length=1)


class KubernetesOperationConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    cluster: str = Field(min_length=1)
    action: Literal["get-deployment", "list-pods", "deployment-logs"]
    namespace: str = Field(min_length=1)
    deployment: str | None = None
    container: str | None = None
    container_image_suffix: str | None = Field(default=None, alias="containerImageSuffix", min_length=1, max_length=512)
    label_selector: str | None = Field(default=None, alias="labelSelector", min_length=1, max_length=2048)
    lookback_seconds: int = Field(default=600, ge=1, le=3600, alias="lookbackSeconds")
    lookback_seconds_parameter: str | None = Field(default=None, alias="lookbackSecondsParameter")
    max_events: int = Field(default=100, ge=1, le=10000, alias="maxEvents")
    max_events_parameter: str | None = Field(default=None, alias="maxEventsParameter")
    max_pods: int = Field(default=4, ge=1, le=100, alias="maxPods")
    max_bytes: int = Field(default=2 * 1024 * 1024, ge=1, le=100 * 1024 * 1024, alias="maxBytes")
    timeout_seconds: float = Field(default=45, gt=0, le=300, allow_inf_nan=False, alias="timeoutSeconds")
    json_field_filters: list[KubernetesJsonFieldFilter] = Field(default_factory=list, alias="jsonFieldFilters")

    @model_validator(mode="after")
    def validate_action_fields(self) -> KubernetesOperationConfig:
        for name in (self.namespace, self.deployment, self.container):
            if name is not None and not RESOURCE_NAME.fullmatch(name):
                raise ValueError("namespace, deployment and container must use Kubernetes resource names")
        if self.action in {"get-deployment", "deployment-logs"} and self.deployment is None:
            raise ValueError("deployment is required for this action")
        if self.action == "list-pods" and self.deployment is not None:
            raise ValueError("list-pods uses labelSelector rather than deployment")
        if self.container is not None and self.container_image_suffix is not None:
            raise ValueError("container and containerImageSuffix are mutually exclusive")
        if self.action != "deployment-logs" and (
            self.container is not None
            or self.container_image_suffix is not None
            or self.lookback_seconds_parameter is not None
            or self.max_events_parameter is not None
            or self.json_field_filters
        ):
            raise ValueError("log options require deployment-logs")
        return self


@dataclass(frozen=True, slots=True)
class CompiledKubernetesOperation:
    definition: OperationDefinition
    config: KubernetesOperationConfig


class KubernetesOperationHandler:
    kind = "kubernetes"

    def __init__(self, kubernetes: KubernetesPort, *, redactor: SecretRedactor | None = None) -> None:
        self._kubernetes = kubernetes
        self._redactor = redactor or SecretRedactor()

    def compile(self, operation: OperationDefinition, profile: Profile) -> CompiledKubernetesOperation:
        try:
            config = KubernetesOperationConfig.model_validate(operation.config)
        except ValidationError as error:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT, "REGISTRY_INVALID", "Kubernetes operation has invalid config", source="registry"
            ) from error
        if operation.mutating:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "REGISTRY_INVALID",
                "Kubernetes read operations must declare mutating: false",
                source="registry",
            )
        if config.cluster not in profile.kubernetes:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "KUBERNETES_NOT_CONFIGURED",
                f"Kubernetes cluster '{config.cluster}' is not configured",
                source="compiler",
            )
        bindings = [entry.parameter for entry in config.json_field_filters]
        for field, reference, minimum, maximum in (
            ("lookbackSecondsParameter", config.lookback_seconds_parameter, 1, 3600),
            ("maxEventsParameter", config.max_events_parameter, 1, 10000),
        ):
            if reference is None:
                continue
            parameter = operation.parameters.get(reference)
            if (
                parameter is None
                or parameter.value_type != "integer"
                or parameter.minimum is None
                or parameter.minimum < minimum
                or parameter.maximum is None
                or parameter.maximum > maximum
            ):
                raise RuntimeFailure(
                    ExitCode.INVALID_INPUT,
                    "REGISTRY_INVALID",
                    f"{field} requires a declared bounded integer parameter",
                    source="registry",
                )
            bindings.append(reference)
        if any(reference not in operation.parameters for reference in bindings):
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "REGISTRY_INVALID",
                "Kubernetes operation binds an unknown parameter",
                source="registry",
            )
        if set(operation.parameters) != set(bindings):
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "REGISTRY_INVALID",
                "Kubernetes operation declares an unbound parameter",
                source="registry",
            )
        if any(parameter.location not in {None, "context"} for parameter in operation.parameters.values()):
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "REGISTRY_INVALID",
                "Kubernetes parameters must use context location",
                source="registry",
            )
        return CompiledKubernetesOperation(operation, config)

    async def execute(
        self, plan: CompiledKubernetesOperation, params: dict[str, Any], context: ExecutionContext
    ) -> dict[str, Any]:
        config = plan.config
        request = KubernetesReadRequest(
            action=config.action,
            namespace=config.namespace,
            deployment=config.deployment,
            container=config.container,
            container_image_suffix=config.container_image_suffix,
            label_selector=config.label_selector,
            lookback_seconds=int(params.get(config.lookback_seconds_parameter) or config.lookback_seconds),
            max_events=int(params.get(config.max_events_parameter) or config.max_events),
            max_pods=config.max_pods,
            max_bytes=config.max_bytes,
            timeout_seconds=config.timeout_seconds,
            json_field_filters=tuple(
                (entry.field, params[entry.parameter])
                for entry in config.json_field_filters
                if entry.parameter in params and params[entry.parameter] is not None
            ),
        )
        return self._redactor.redact(await self._kubernetes.read(config.cluster, request, context))

    async def aclose(self) -> None:
        await self._kubernetes.aclose()
