from __future__ import annotations

from collections.abc import Iterable
from contextlib import suppress
from typing import Any, Protocol

import anyio

from qa_runtime.adapters.httpx import HttpxAdapter
from qa_runtime.adapters.kafka import AioKafkaAdapter
from qa_runtime.adapters.kubernetes import KubectlAdapter
from qa_runtime.adapters.postgres import AsyncpgAdapter
from qa_runtime.adapters.s3 import S3Adapter
from qa_runtime.adapters.tls import ConfiguredTlsProvider
from qa_runtime.codecs.catalog import load_codecs
from qa_runtime.compiler import CatalogCompiler, HandlerRegistry
from qa_runtime.engine import QaRuntime
from qa_runtime.handlers.database import DatabaseOperationHandler
from qa_runtime.handlers.http import HttpOperationHandler
from qa_runtime.handlers.kafka import KafkaConsumeOperationHandler, KafkaPublishOperationHandler
from qa_runtime.handlers.kafka_offsets import KafkaOffsetsOperationHandler
from qa_runtime.handlers.kubernetes import KubernetesOperationHandler
from qa_runtime.handlers.s3 import S3OperationHandler
from qa_runtime.models import Profile
from qa_runtime.ports.credentials import CredentialSource
from qa_runtime.ports.database import DatabasePort
from qa_runtime.ports.http import HttpPort
from qa_runtime.ports.kafka import KafkaPort
from qa_runtime.ports.kubernetes import KubernetesPort
from qa_runtime.ports.product import ProductPlugin, ProductSetup
from qa_runtime.ports.s3 import S3Port
from qa_runtime.registry import OperationRegistry
from qa_runtime.resources import STANDARD_RESOURCE_BINDINGS
from qa_runtime.security import SecretRedactor

_CLEANUP_TIMEOUT_SECONDS = 2.0


class _AsyncResource(Protocol):
    async def aclose(self) -> None: ...


class _CloseOncePort:
    """One owner for a shared transport, including failed composition."""

    def __init__(self, resource: _AsyncResource) -> None:
        self._resource = resource
        self._closed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resource, name)

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._resource.aclose()


def scope_profile(
    profile: Profile,
    registry: OperationRegistry,
    operation_ids: tuple[str, ...] | None,
    setup: ProductSetup | None = None,
) -> Profile:
    """Copy a loaded profile, retaining only selected standard transport resources.

    Custom kinds can declare resource bindings through product setup metadata.
    Undocumented custom kinds retain the full profile. Endpoint dependencies are
    scoped when setup metadata describes the selected operations and authentication.
    """
    scoped = profile.model_copy(deep=True)
    if operation_ids is None:
        return scoped

    operations = tuple(registry.get(operation_id) for operation_id in operation_ids)
    refs: dict[str, set[str]] = {
        "services": set(),
        "databases": set(),
        "kafka": set(),
        "s3": set(),
        "kubernetes": set(),
        "endpoints": set(),
    }
    for operation in operations:
        binding = STANDARD_RESOURCE_BINDINGS.get(operation.kind)
        custom = setup.resource_bindings.get(operation.kind) if setup else None
        if custom is not None and custom.section == "none":
            refs["endpoints"].update(custom.dependencies.endpoint_refs)
            continue
        if binding is None:
            if custom is None or custom.section not in refs:
                return scoped
            binding = (custom.section, custom.config_field)
            refs["endpoints"].update(custom.dependencies.endpoint_refs)
        section, key = binding
        reference = operation.config.get(key)
        if isinstance(reference, str):
            refs[section].add(reference)
    scoped.services = {name: value for name, value in scoped.services.items() if name in refs["services"]}
    scoped.databases = {name: value for name, value in scoped.databases.items() if name in refs["databases"]}
    scoped.kafka = {name: value for name, value in scoped.kafka.items() if name in refs["kafka"]}
    scoped.s3 = {name: value for name, value in scoped.s3.items() if name in refs["s3"]}
    scoped.kubernetes = {name: value for name, value in scoped.kubernetes.items() if name in refs["kubernetes"]}
    if setup is not None:
        for service in scoped.services.values():
            if service.auth_ref is not None:
                auth = setup.auth.get(service.auth_ref)
                if auth is None:
                    # Keep all endpoints when an auth provider has no dependency metadata.
                    return scoped
                refs["endpoints"].update(auth.endpoint_refs)
        scoped.endpoints = {name: value for name, value in scoped.endpoints.items() if name in refs["endpoints"]}
    return scoped


async def build_runtime(
    plugin: ProductPlugin,
    profile: Profile,
    registry: OperationRegistry,
    credentials: CredentialSource,
    *,
    operation_ids: tuple[str, ...] | None = None,
    http_port: HttpPort | None = None,
    database_port: DatabasePort | None = None,
    kafka_port: KafkaPort | None = None,
    s3_port: S3Port | None = None,
    kubernetes_port: KubernetesPort | None = None,
    redactor: SecretRedactor | None = None,
) -> QaRuntime:
    """Compose one runtime from loaded configuration without discovering local settings.

    Compilation performs no transport I/O or credential lookup. Product factories
    may validate local trust material. Each build has its own redactor and components;
    callers must also provide credentials and plugin factories with suitable isolation.

    Ownership of an injected HTTP port transfers to this build attempt. On success,
    the caller owns the returned runtime and must call aclose(). On failure, returned
    product resources and the port are closed with a shielded timeout per resource.
    A factory that raises before returning components owns its partial allocations.
    """
    resources: list[_AsyncResource] = []
    leases: dict[int, _CloseOncePort] = {}

    def own(port: _AsyncResource) -> _CloseOncePort:
        lease = leases.get(id(port))
        if lease is None:
            lease = _CloseOncePort(port)
            leases[id(port)] = lease
            resources.append(lease)
        return lease

    for port in (http_port, database_port, kafka_port, s3_port, kubernetes_port):
        if port is not None:
            own(port)
    try:
        scoped = scope_profile(profile, registry, operation_ids, plugin.setup)
        selected = (
            registry.operations()
            if operation_ids is None
            else tuple(registry.get(identifier) for identifier in dict.fromkeys(operation_ids))
        )
        codecs = load_codecs(registry, selected)
        redactor = redactor if redactor is not None else SecretRedactor()
        components = plugin.build_components(scoped, credentials, redactor)
        if components.tls_provider is None:
            components.tls_provider = ConfiguredTlsProvider()
        resources.extend(components.handlers)
        resources.extend(components.auth_providers.values())
        if http_port is None:
            http_port = HttpxAdapter(tls_provider=components.tls_provider)
        http = own(http_port)
        if database_port is None:
            database_port = AsyncpgAdapter(
                scoped.databases, credentials, redactor, tls_provider=components.tls_provider
            )
        database = own(database_port)
        if kafka_port is None:
            kafka_port = AioKafkaAdapter(scoped.kafka, credentials, redactor, tls_provider=components.tls_provider)
        kafka = own(kafka_port)
        if s3_port is None:
            s3_port = S3Adapter(scoped.s3, credentials, tls_provider=components.tls_provider, redactor=redactor)
        s3 = own(s3_port)
        if kubernetes_port is None:
            kubernetes_port = KubectlAdapter(scoped.kubernetes)
        kubernetes = own(kubernetes_port)
        http_handler = HttpOperationHandler(
            http,
            auth_providers=components.auth_providers,
            credentials=credentials,
            tls_provider=components.tls_provider,
            redactor=redactor,
        )
        handlers = HandlerRegistry(
            [
                http_handler,
                DatabaseOperationHandler(
                    database, scoped.databases, tls_provider=components.tls_provider, value_decoders=codecs.decoders
                ),
                KafkaConsumeOperationHandler(
                    kafka, scoped.kafka, tls_provider=components.tls_provider, value_decoders=codecs.decoders
                ),
                KafkaPublishOperationHandler(
                    kafka, scoped.kafka, tls_provider=components.tls_provider, value_encoders=codecs.encoders
                ),
                KafkaOffsetsOperationHandler(kafka, scoped.kafka, tls_provider=components.tls_provider),
                S3OperationHandler(s3, redactor=redactor, tls_provider=components.tls_provider),
                KubernetesOperationHandler(kubernetes, redactor=redactor),
                *components.handlers,
            ]
        )
        catalog = CatalogCompiler().compile(registry, scoped, handlers, operation_ids=operation_ids)
        return QaRuntime(catalog, redactor=redactor, resources=resources)
    except BaseException:
        await _close_failed_build(resources)
        raise


async def _close_failed_build(resources: Iterable[_AsyncResource]) -> None:
    closed: set[int] = set()
    for resource in resources:
        if id(resource) in closed:
            continue
        closed.add(id(resource))
        with (
            suppress(Exception, anyio.get_cancelled_exc_class()),
            anyio.move_on_after(_CLEANUP_TIMEOUT_SECONDS, shield=True),
        ):
            await resource.aclose()
