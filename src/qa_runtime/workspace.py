"""Load a trusted local workspace without installing a product distribution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from qa_runtime.adapters.static_auth import HeaderAuthConfig, HeaderAuthProvider
from qa_runtime.browser_catalog import include_browser_operations
from qa_runtime.browser_models import BrowserSettings, SessionSettings
from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import Profile
from qa_runtime.plugins import product_plugin_for
from qa_runtime.ports.product import AuthSetup, ProductComponents, ProductPlugin, ProductSetup, ResourceBinding
from qa_runtime.profile import ProfileLoader
from qa_runtime.registry import OperationRegistry


class WorkspaceLimits(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", allow_inf_nan=False)

    operation_timeout_seconds: float = Field(default=60, gt=0, le=300, strict=True, alias="operationTimeoutSeconds")


class WorkspaceManifest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", hide_input_in_errors=True)

    api_version: Literal["agentqit/v1"] = Field(alias="apiVersion")
    name: str = Field(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", max_length=64)
    profile: Path = Path("profile.yaml")
    registry: list[Path] | None = Field(default=None, min_length=1, max_length=64)
    product: str | None = None
    auth: dict[str, HeaderAuthConfig] = Field(default_factory=dict)
    limits: WorkspaceLimits = Field(default_factory=WorkspaceLimits)
    browser: BrowserSettings | None = None
    sessions: SessionSettings = Field(default_factory=SessionSettings)


def workspace_path(root: Path, value: Path) -> Path:
    """Configuration references are local, bounded to the trusted workspace root."""
    target = (root / value).resolve()
    if not target.is_relative_to(root):
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "WORKSPACE_PATH_OUTSIDE_ROOT",
            "Workspace file references must stay inside its directory",
            source="workspace",
        )
    return target


@dataclass(frozen=True)
class Workspace:
    path: Path
    manifest: WorkspaceManifest
    profile: Profile
    registry: OperationRegistry
    plugin: ProductPlugin

    @classmethod
    def load(cls, path: Path, *, env: Mapping[str, str] | None = None) -> Workspace:
        path = path.expanduser().resolve()
        if path.is_dir():
            path = path / "agentqit.yaml"
        try:
            if path.stat().st_size > 1024 * 1024:
                raise ValueError("manifest too large")
            manifest = WorkspaceManifest.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, yaml.YAMLError, ValidationError) as error:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "WORKSPACE_INVALID",
                "Workspace must be readable UTF-8 YAML matching agentqit/v1",
                source="workspace",
            ) from error
        root = path.parent
        profile_path = workspace_path(root, manifest.profile)
        if profile_path.suffix != ".yaml":
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "WORKSPACE_PROFILE_INVALID",
                "Workspace profile must be a .yaml file",
                source="workspace",
            )
        profile = ProfileLoader(profile_path.parent, env=env).load(profile_path.stem)
        installed = product_plugin_for(manifest.product) if manifest.product is not None else None
        registry_paths = (
            [workspace_path(root, value) for value in manifest.registry]
            if manifest.registry is not None
            else [installed.registry_path if installed is not None else root / "operations"]
        )
        registry = OperationRegistry.load(registry_paths)
        if manifest.browser is not None:
            registry = include_browser_operations(registry)

        def components(scoped, credentials, redactor):
            selected = installed.build_components(scoped, credentials, redactor) if installed else ProductComponents()
            needed = {service.auth_ref for service in scoped.services.values() if service.auth_ref}
            for reference, config in manifest.auth.items():
                if reference not in needed:
                    continue
                if reference in selected.auth_providers:
                    raise RuntimeFailure(
                        ExitCode.INVALID_INPUT,
                        "WORKSPACE_AUTH_CONFLICT",
                        "Workspace and installed product declare the same auth provider",
                        source="workspace",
                    )
                selected.auth_providers[reference] = HeaderAuthProvider(config, credentials, redactor)
            return selected

        auth_setup = dict(installed.setup.auth) if installed and installed.setup else {}
        auth_setup.update(
            {key: AuthSetup(credential_refs=(value.credential_ref,)) for key, value in manifest.auth.items()}
        )
        setup = ProductSetup(
            profile_template=profile_path,
            auth=auth_setup,
            tls=installed.setup.tls if installed and installed.setup else {},
            resource_bindings={
                **(installed.setup.resource_bindings if installed and installed.setup else {}),
                **({"browser": ResourceBinding("none", "")} if manifest.browser is not None else {}),
            },
        )
        plugin = ProductPlugin(
            product_id=manifest.name,
            registry_path=registry_paths[0],
            runtime_factory=components,
            guides=installed.guides if installed else (),
            guide_rules=installed.guide_rules if installed else (),
            setup=setup,
        )
        return cls(path, manifest, profile, registry, plugin)
