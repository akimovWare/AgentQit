from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.handlers.kafka import _compile_endpoint, _validate_parameter_references
from qa_runtime.models import ExecutionContext, KafkaClusterConfig, OperationDefinition, Profile
from qa_runtime.ports.kafka import KafkaPort
from qa_runtime.ports.operation import ResultPayload
from qa_runtime.ports.tls import TlsProvider


class KafkaOffsetsOperationConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", allow_inf_nan=False)

    cluster: str
    topic_ref: str = Field(alias="topicRef", min_length=1)
    group_id: str = Field(alias="groupId", min_length=1)
    partition_parameter: str = Field(alias="partitionParameter")
    timeout_seconds: float = Field(default=10, gt=0, le=60, alias="timeoutSeconds")


@dataclass(frozen=True, slots=True)
class CompiledKafkaOffsetsOperation:
    definition: OperationDefinition
    config: KafkaOffsetsOperationConfig
    topic: str
    client_id: str


class KafkaOffsetsOperationHandler:
    kind = "kafka-offsets"

    def __init__(
        self,
        kafka: KafkaPort,
        clusters: Mapping[str, KafkaClusterConfig],
        *,
        tls_provider: TlsProvider | None = None,
    ) -> None:
        self._kafka = kafka
        self._clusters = dict(clusters)
        self._tls_provider = tls_provider

    def compile(self, operation: OperationDefinition, profile: Profile) -> CompiledKafkaOffsetsOperation:
        try:
            config = KafkaOffsetsOperationConfig.model_validate(operation.config)
        except ValidationError as error:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "REGISTRY_INVALID",
                "Kafka offset operation has invalid config",
                source="registry",
            ) from error
        if operation.mutating:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "REGISTRY_INVALID",
                "Kafka offset observations must be read-only",
                source="registry",
            )
        _validate_parameter_references(operation, config.partition_parameter)
        parameter = operation.parameters[config.partition_parameter]
        if (
            parameter.value_type != "integer"
            or not parameter.required
            or parameter.minimum is None
            or parameter.minimum < 0
        ):
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "REGISTRY_INVALID",
                "partitionParameter must reference a required non-negative integer",
                source="registry",
            )
        cluster, topic = _compile_endpoint(
            operation, config.cluster, config.topic_ref, self._clusters, self._tls_provider
        )
        return CompiledKafkaOffsetsOperation(operation, config, topic, cluster.client_id_prefix)

    async def execute(
        self, plan: CompiledKafkaOffsetsOperation, params: dict[str, Any], context: ExecutionContext
    ) -> ResultPayload:
        partition = params[plan.config.partition_parameter]
        offset = await self._kafka.committed_offset(
            plan.config.cluster,
            plan.topic,
            plan.config.group_id,
            partition,
            client_id=f"{plan.client_id}-{context.run_id}",
            timeout_seconds=plan.config.timeout_seconds,
        )
        return {
            "cluster": plan.config.cluster,
            "topic": plan.topic,
            "group_id": plan.config.group_id,
            "partition": partition,
            "committed_next_offset": offset,
        }

    async def aclose(self) -> None:
        await self._kafka.aclose()
