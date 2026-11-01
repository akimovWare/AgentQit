from __future__ import annotations

import json
import os
import re
import signal
from collections.abc import Mapping
from contextlib import suppress
from subprocess import DEVNULL
from typing import Any

import anyio

from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import ExecutionContext, KubernetesServiceConfig
from qa_runtime.ports.kubernetes import KubernetesReadRequest

SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,252}")
SAFE_RESOURCE_NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")


def _failure(code: str, message: str, **kwargs: Any) -> RuntimeFailure:
    return RuntimeFailure(ExitCode.ENVIRONMENT_UNAVAILABLE, code, message, source="kubernetes", **kwargs)


class KubectlAdapter:
    """Bounded Kubernetes reads through a configured executable and context."""

    def __init__(self, clusters: Mapping[str, KubernetesServiceConfig]) -> None:
        self._clusters = dict(clusters)
        self._closed = False

    async def read(self, cluster: str, request: KubernetesReadRequest, context: ExecutionContext) -> dict[str, Any]:
        if self._closed:
            raise _failure("KUBERNETES_CLOSED", "Kubernetes adapter is closed")
        target = self._clusters.get(cluster)
        if target is None:
            raise _failure("KUBERNETES_NOT_CONFIGURED", "Kubernetes cluster is not configured")
        if not target.executable.is_file() or not os.access(target.executable, os.X_OK):
            raise _failure("KUBECTL_UNAVAILABLE", "configured Kubernetes executable is unavailable")
        command = [
            str(target.executable),
            "--context",
            target.context,
            "--server",
            target.endpoint,
            "--insecure-skip-tls-verify=false",
            f"--request-timeout={target.request_timeout_seconds:g}s",
            "--namespace",
            request.namespace,
        ]
        try:
            with anyio.fail_after(request.timeout_seconds):
                if request.action == "get-deployment":
                    raw = await self._run(
                        [*command, "get", "deployment", str(request.deployment), "-o", "json"], request.max_bytes
                    )
                    return {
                        "cluster": cluster,
                        "action": request.action,
                        "namespace": request.namespace,
                        "resource": _json_object(raw),
                        "bytes_read": len(raw),
                    }
                if request.action == "list-pods":
                    selector = ["--selector", request.label_selector] if request.label_selector else []
                    raw = await self._run([*command, "get", "pods", *selector, "-o", "json"], request.max_bytes)
                    return {
                        "cluster": cluster,
                        "action": request.action,
                        "namespace": request.namespace,
                        "resource": _json_object(raw),
                        "bytes_read": len(raw),
                    }
                return await self._deployment_logs(cluster, request, command)
        except TimeoutError as error:
            raise _failure("KUBERNETES_TIMEOUT", "bounded Kubernetes read timed out", retryable=True) from error
        except OSError as error:
            raise _failure("KUBECTL_UNAVAILABLE", "configured Kubernetes executable could not be started") from error

    async def _deployment_logs(
        self, cluster: str, request: KubernetesReadRequest, command: list[str]
    ) -> dict[str, Any]:
        remaining = request.max_bytes
        deployment_bytes = await self._run(
            [*command, "get", "deployment", str(request.deployment), "-o", "json"], remaining
        )
        remaining -= len(deployment_bytes)
        deployment = _json_object(deployment_bytes)
        specification = deployment.get("spec")
        if not isinstance(specification, dict):
            raise _failure("KUBERNETES_RESPONSE_INVALID", "deployment has no valid specification")
        selector = specification.get("selector")
        labels = selector.get("matchLabels") if isinstance(selector, dict) else None
        if (
            not isinstance(labels, dict)
            or not labels
            or len(labels) > 32
            or selector.get("matchExpressions")
            or any(
                not isinstance(key, str)
                or not isinstance(value, str)
                or not SAFE_LABEL.fullmatch(key)
                or not SAFE_LABEL.fullmatch(value)
                for key, value in labels.items()
            )
        ):
            raise _failure("KUBERNETES_SELECTOR_UNSUPPORTED", "deployment requires a supported matchLabels selector")
        selector_text = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
        container = request.container or _select_container(specification, request.container_image_suffix)
        pod_bytes = await self._run([*command, "get", "pods", "--selector", selector_text, "-o", "json"], remaining)
        remaining -= len(pod_bytes)
        pod_list = _json_object(pod_bytes)
        items = pod_list.get("items")
        if not isinstance(items, list):
            raise _failure("KUBERNETES_RESPONSE_INVALID", "pod list has no valid items")
        pods = set()
        for item in items:
            metadata = item.get("metadata") if isinstance(item, dict) else None
            name = metadata.get("name") if isinstance(metadata, dict) else None
            if not isinstance(name, str) or not SAFE_RESOURCE_NAME.fullmatch(name):
                raise _failure("KUBERNETES_RESPONSE_INVALID", "pod list includes an invalid resource name")
            pods.add(name)
        ordered_pods = sorted(pods)
        events: list[dict[str, Any]] = []
        lines_read = 0
        for pod in ordered_pods[: request.max_pods]:
            log_bytes = await self._run(
                [
                    *command,
                    "logs",
                    pod,
                    "--container",
                    container,
                    f"--since={request.lookback_seconds}s",
                    "--timestamps=true",
                ],
                remaining,
            )
            remaining -= len(log_bytes)
            for line in log_bytes.decode("utf-8", errors="replace").splitlines():
                lines_read += 1
                timestamp, record = _log_record(line)
                if request.json_field_filters and (
                    not isinstance(record, dict)
                    or not all(_field_value(record, field) == value for field, value in request.json_field_filters)
                ):
                    continue
                events.append({"pod": pod, "timestamp": timestamp, "record": record})
        events.sort(key=lambda event: (event["timestamp"] or "", event["pod"]))
        return {
            "cluster": cluster,
            "action": request.action,
            "namespace": request.namespace,
            "deployment": request.deployment,
            "container": container,
            "lookback_seconds": request.lookback_seconds,
            "pods_matched": len(ordered_pods),
            "pods_read": ordered_pods[: request.max_pods],
            "truncated_pods": len(ordered_pods) > request.max_pods,
            "lines_read": lines_read,
            "matching_event_count": len(events),
            "events": events[: request.max_events],
            "truncated_events": len(events) > request.max_events,
            "bytes_read": request.max_bytes - remaining,
            "resource_continue": _resource_continuation(pod_list),
            "limits": {
                "max_pods": request.max_pods,
                "max_events": request.max_events,
                "max_bytes": request.max_bytes,
                "timeout_seconds": request.timeout_seconds,
            },
        }

    async def _run(self, argv: list[str], max_bytes: int) -> bytes:
        process = await anyio.open_process(argv, stdin=DEVNULL, stderr=DEVNULL, start_new_session=os.name == "posix")
        completed = False
        try:
            output = bytearray()
            if process.stdout is None:
                raise _failure("KUBECTL_READ_FAILED", "Kubernetes executable has no output stream")
            async for chunk in process.stdout:
                if len(output) + len(chunk) > max_bytes:
                    raise _failure("KUBERNETES_OUTPUT_LIMIT", "Kubernetes read exceeds the declared byte budget")
                output.extend(chunk)
            return_code = await process.wait()
            completed = True
        finally:
            with anyio.move_on_after(2, shield=True):
                if not completed:
                    with suppress(ProcessLookupError):
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                await process.aclose()
        if return_code != 0:
            raise _failure(
                "KUBECTL_READ_FAILED",
                "Kubernetes executable could not complete the configured read",
                hint="Check the configured context, Kubernetes login and namespace read permissions.",
            )
        return bytes(output)

    async def aclose(self) -> None:
        self._closed = True


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError) as error:
        raise _failure(
            "KUBERNETES_RESPONSE_INVALID", "Kubernetes executable returned invalid resource metadata"
        ) from error


def _select_container(specification: dict[str, Any], image_suffix: str | None) -> str:
    template = specification.get("template")
    pod_spec = template.get("spec") if isinstance(template, dict) else None
    containers = pod_spec.get("containers") if isinstance(pod_spec, dict) else None
    if isinstance(containers, list) and image_suffix is not None:
        containers = [
            item
            for item in containers
            if isinstance(item, dict)
            and isinstance(item.get("image"), str)
            and _image_repository(item["image"]).endswith(image_suffix)
        ]
    if not isinstance(containers, list) or len(containers) != 1:
        raise _failure(
            "KUBERNETES_CONTAINER_REQUIRED", "configure container or an unambiguous image suffix for this deployment"
        )
    name = containers[0].get("name") if isinstance(containers[0], dict) else None
    if not isinstance(name, str) or not SAFE_RESOURCE_NAME.fullmatch(name):
        raise _failure("KUBERNETES_RESPONSE_INVALID", "deployment has an invalid container name")
    return name


def _log_record(line: str) -> tuple[str | None, Any]:
    timestamp = None
    body = line
    if re.match(r"\d{4}-\d{2}-\d{2}T\S+ ", line):
        timestamp, _, body = line.partition(" ")
    try:
        return timestamp, json.loads(body)
    except ValueError:
        return timestamp, body


def _field_value(record: dict[str, Any], field: str) -> Any:
    value: Any = record
    for part in field.split("."):
        if not isinstance(value, dict) or part not in value:
            return object()
        value = value[part]
    return value


def _image_repository(image: str) -> str:
    repository = image.split("@", 1)[0]
    head, slash, tail = repository.rpartition("/")
    tail = tail.split(":", 1)[0]
    return f"{head}{slash}{tail}"


def _resource_continuation(resource: dict[str, Any]) -> str | None:
    metadata = resource.get("metadata")
    token = metadata.get("continue") if isinstance(metadata, dict) else None
    return token if isinstance(token, str) and token else None
