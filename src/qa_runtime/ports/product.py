from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from qa_runtime.models import Profile
from qa_runtime.ports.auth import AuthProvider
from qa_runtime.ports.credentials import CredentialSource
from qa_runtime.ports.operation import OperationHandler
from qa_runtime.ports.tls import TlsProvider
from qa_runtime.security import SecretRedactor

PRODUCT_PLUGIN_API_VERSION = "qa.product/v1"


@dataclass(frozen=True, slots=True)
class ProductGuide:
    id: str
    purpose: str
    payload: dict[str, Any]
    operation_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SetupPrerequisite:
    id: str
    description: str
    kind: str
    environment_variable: str | None = None
    default_path: Path | None = None


@dataclass(frozen=True, slots=True)
class AuthSetup:
    endpoint_refs: tuple[str, ...] = ()
    credential_refs: tuple[str, ...] = ()
    prerequisites: tuple[SetupPrerequisite, ...] = ()


@dataclass(frozen=True, slots=True)
class ResourceBinding:
    section: Literal["services", "databases", "kafka", "s3", "kubernetes", "endpoints", "none"]
    config_field: str
    dependencies: AuthSetup = field(default_factory=AuthSetup)


@dataclass(frozen=True, slots=True)
class ProductSetup:
    profile_template: Path
    auth: dict[str, AuthSetup] = field(default_factory=dict)
    tls: dict[str, tuple[SetupPrerequisite, ...]] = field(default_factory=dict)
    # Paths to nonsecret example values that the operator must replace.
    template_fields: tuple[str, ...] = ()
    # Custom operation kinds declare the same scoped dependencies as standard transports.
    resource_bindings: dict[str, ResourceBinding] = field(default_factory=dict)


@dataclass(slots=True)
class ProductComponents:
    handlers: list[OperationHandler] = field(default_factory=list)
    auth_providers: dict[str, AuthProvider] = field(default_factory=dict)
    tls_provider: TlsProvider | None = None


class ProductRuntimeFactory(Protocol):
    def __call__(
        self,
        profile: Profile,
        credentials: CredentialSource,
        redactor: SecretRedactor,
    ) -> ProductComponents: ...


@dataclass(frozen=True, slots=True)
class ProductPlugin:
    product_id: str
    registry_path: Path
    runtime_factory: ProductRuntimeFactory | None = None
    api_version: str = PRODUCT_PLUGIN_API_VERSION
    guides: tuple[ProductGuide, ...] = ()
    guide_rules: tuple[str, ...] = ()
    setup: ProductSetup | None = None

    def build_components(
        self,
        profile: Profile,
        credentials: CredentialSource,
        redactor: SecretRedactor,
    ) -> ProductComponents:
        if self.runtime_factory is None:
            return ProductComponents()
        return self.runtime_factory(profile, credentials, redactor)

    def guide(self, guide_id: str) -> ProductGuide | None:
        return next((guide for guide in self.guides if guide.id == guide_id), None)

    def guides_for(self, operation_id: str) -> tuple[ProductGuide, ...]:
        return tuple(guide for guide in self.guides if operation_id in guide.operation_ids)
