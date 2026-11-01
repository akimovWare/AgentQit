from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from qa_runtime.models import ExecutionContext


@dataclass(frozen=True, slots=True)
class KubernetesReadRequest:
    action: Literal["get-deployment", "list-pods", "deployment-logs"]
    namespace: str
    deployment: str | None = None
    container: str | None = None
    container_image_suffix: str | None = None
    label_selector: str | None = None
    lookback_seconds: int = 600
    max_events: int = 100
    max_pods: int = 4
    max_bytes: int = 2 * 1024 * 1024
    timeout_seconds: float = 45
    json_field_filters: tuple[tuple[str, Any], ...] = ()


class KubernetesPort(Protocol):
    async def read(self, cluster: str, request: KubernetesReadRequest, context: ExecutionContext) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...
