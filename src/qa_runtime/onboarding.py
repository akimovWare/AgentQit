from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from qa_runtime.codecs.catalog import codec_references
from qa_runtime.contracts import ExitCode
from qa_runtime.credential_config import EnvironmentCredentialConfig, VaultCredentialConfig
from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import OperationDefinition, Profile
from qa_runtime.ports.product import ProductPlugin, ProductSetup, SetupPrerequisite
from qa_runtime.profile import ENV_PLACEHOLDER
from qa_runtime.registry import OperationRegistry
from qa_runtime.resources import STANDARD_RESOURCE_BINDINGS


@dataclass(frozen=True, slots=True)
class SetupPlan:
    profile: Profile
    requirements: dict[str, Any]


def build_setup_plan(
    plugin: ProductPlugin,
    registry: OperationRegistry,
    operation_ids: tuple[str, ...] | list[str],
    *,
    profile: Profile | None = None,
    template: Path | None = None,
) -> SetupPlan:
    """Describe selected dependencies without building components or accessing secrets/network."""
    if not operation_ids:
        raise _failure("SETUP_OPERATION_REQUIRED", "select at least one operation with --operation")
    operations = tuple(registry.get(identifier) for identifier in dict.fromkeys(operation_ids))
    setup = plugin.setup
    if profile is not None and template is not None:
        raise _failure("SETUP_SOURCE_CONFLICT", "use either an existing profile or a template, not both")
    if profile is None:
        if setup is None and template is None:
            raise _failure("PRODUCT_SETUP_UNAVAILABLE", "product does not provide an onboarding profile template")
        template_path = template if template is not None else setup.profile_template
        try:
            document = yaml.safe_load(template_path.expanduser().read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError("expected a mapping")
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as error:
            raise _failure("SETUP_TEMPLATE_INVALID", "product profile template could not be loaded") from error
    else:
        document = profile.model_dump(mode="json", by_alias=True)

    try:
        selected, resources, missing = _select_document(document, operations, setup)
        selected = Profile.model_validate(_resolve_environment(selected) if profile is None else selected).model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
    except (ValidationError, TypeError, ValueError) as error:
        raise _failure("SETUP_TEMPLATE_INVALID", "selected profile configuration is invalid") from error
    auth_refs = sorted({service["authRef"] for service in selected["services"].values() if service.get("authRef")})
    endpoint_refs: set[str] = set(selected.get("endpoints", {}))
    credential_refs: set[str] = set()
    prerequisites: dict[str, SetupPrerequisite] = {}
    unresolved_auth: list[str] = []
    for operation in operations:
        binding = setup.resource_bindings.get(operation.kind) if setup else None
        if binding is not None:
            if binding.section == "endpoints":
                endpoint_refs.add(operation.config[binding.config_field])
            endpoint_refs.update(binding.dependencies.endpoint_refs)
            credential_refs.update(binding.dependencies.credential_refs)
            prerequisites.update((item.id, item) for item in binding.dependencies.prerequisites)
    for reference in auth_refs:
        auth = setup.auth.get(reference) if setup else None
        if auth is None:
            unresolved_auth.append(reference)
            continue
        endpoint_refs.update(auth.endpoint_refs)
        credential_refs.update(auth.credential_refs)
        prerequisites.update((item.id, item) for item in auth.prerequisites)
    all_endpoints = document.get("endpoints") or {}
    if endpoint_refs and not isinstance(all_endpoints, dict):
        raise _failure("SETUP_TEMPLATE_INVALID", "selected auth endpoints must be a mapping")
    selected_endpoints = {key: all_endpoints[key] for key in sorted(endpoint_refs) if key in all_endpoints}
    selected["endpoints"] = _resolve_environment(selected_endpoints) if profile is None else selected_endpoints
    missing.extend(f"endpoints.{key}" for key in sorted(endpoint_refs) if key not in all_endpoints)
    tls_refs: set[str] = set()
    for section in ("services", "databases", "kafka", "s3"):
        for resource in selected[section].values():
            if reference := resource.get("tlsRef"):
                tls_refs.add(reference)
            for key in ("usernameRef", "passwordRef", "accessKeyRef", "secretKeyRef", "sessionTokenRef"):
                if reference := resource.get(key):
                    credential_refs.add(reference)
    for reference in sorted(tls_refs):
        for prerequisite in setup.tls.get(reference, ()) if setup else ():
            prerequisites[prerequisite.id] = prerequisite
    parameter_names = {name for operation in operations for name in operation.parameters}
    # Example identity strings must never silently become defaults for a live invocation.
    selected["defaults"] = (
        {key: value for key, value in (document.get("defaults") or {}).items() if key in parameter_names}
        if profile is not None
        else {}
    )
    try:
        scoped_profile = Profile.model_validate(selected)
    except (ValidationError, TypeError, ValueError) as error:
        raise _failure("SETUP_TEMPLATE_INVALID", "selected profile configuration is invalid") from error
    for name, cluster in scoped_profile.kafka.items():
        if truststore := cluster.truststore:
            credential_refs.add(truststore.password_ref)
            prerequisite = SetupPrerequisite(
                id=f"kafka.{name}.truststore",
                description="Install the configured JKS truststore; its password is obtained through CredentialSource.",
                kind="file",
                default_path=truststore.location,
            )
            prerequisites[prerequisite.id] = prerequisite
    credential_parameters: dict[str, list[str]] = {}
    for name, cluster in scoped_profile.kubernetes.items():
        prerequisites[f"kubernetes.{name}.executable"] = SetupPrerequisite(
            id=f"kubernetes.{name}.executable",
            description="Install the configured trusted executable and authorize its Kubernetes context.",
            kind="executable",
            default_path=cluster.executable,
        )
    selected_codecs = codec_references(operations)
    for reference in sorted(selected_codecs):
        definition = registry.codecs().get(reference)
        if definition is None:
            missing.append(f"codecs.{reference}")
        elif definition.descriptor_path is not None:
            prerequisites[f"codecs.{reference}.descriptor"] = SetupPrerequisite(
                id=f"codecs.{reference}.descriptor",
                description="Install the declared local descriptor matching its pinned checksum.",
                kind="file",
                default_path=definition.descriptor_path,
            )
    dynamic_credential_parameters: dict[str, list[str]] = {}
    for operation in operations:
        for name, parameter in operation.parameters.items():
            if parameter.location == "credential-body":
                credential_parameters.setdefault(operation.id, []).append(name)
                value = scoped_profile.defaults.get(name)
                if value is None:
                    value = parameter.default
                if isinstance(value, str) and value:
                    credential_refs.add(value)
                else:
                    dynamic_credential_parameters.setdefault(operation.id, []).append(name)
    credentials = _selected_credentials(
        document.get("credentials"), credential_refs, bool(dynamic_credential_parameters), resolve_env=profile is None
    )
    if credentials is not None:
        scoped_profile = scoped_profile.model_copy(update={"credentials": credentials})
        selected["credentials"] = credentials.model_dump(mode="json", by_alias=True, exclude_none=True)
        if isinstance(credentials, VaultCredentialConfig):
            prerequisites.update((item.id, item) for item in _vault_prerequisites(credentials))
        else:
            for variable in sorted(set(credentials.bindings.values())):
                item = SetupPrerequisite(
                    id=f"env:{variable}",
                    description="Supply the explicitly mapped environment credential",
                    kind="environment",
                    environment_variable=variable,
                )
                prerequisites[item.id] = item
    missing_credential_bindings = (
        sorted(credential_refs - credentials.bindings.keys()) if credentials is not None else []
    )
    template_fields = (
        [path for path in setup.template_fields if _contains_path(selected, path)]
        if profile is None and setup is not None
        else []
    )
    requirements = {
        "operations": [operation.id for operation in operations],
        "profileSource": (
            "selected-profile"
            if profile is not None
            else "provided-template"
            if template is not None
            else "product-template"
        ),
        "resources": resources,
        "endpointRefs": sorted(endpoint_refs),
        "authRefs": auth_refs,
        "tlsRefs": sorted(tls_refs),
        "credentialRefs": sorted(credential_refs),
        "codecRefs": sorted(selected_codecs),
        "credentialProvider": credentials.provider if credentials is not None else "unconfigured",
        "credentialBindings": {
            reference: binding if isinstance(binding, str) else binding.model_dump(mode="json", exclude_none=True)
            for reference, binding in sorted(credentials.bindings.items())
        }
        if credentials is not None
        else {},
        "missingCredentialBindings": missing_credential_bindings,
        "credentialParameters": {key: sorted(value) for key, value in credential_parameters.items()},
        "dynamicCredentialParameters": {key: sorted(value) for key, value in dynamic_credential_parameters.items()},
        "dynamicCredentialBindingSelection": bool(dynamic_credential_parameters),
        "prerequisites": [_prerequisite_payload(item) for _, item in sorted(prerequisites.items())],
        "missingConfiguration": sorted(set(missing)),
        "unresolvedAuthRefs": unresolved_auth,
        "templateFields": sorted(template_fields),
        "callerRequired": {
            operation.id: sorted(
                name
                for name, parameter in operation.parameters.items()
                if parameter.required and parameter.default is None and scoped_profile.defaults.get(name) is None
            )
            for operation in operations
        },
        "configurationCompiled": False,
        "credentialsChecked": False,
        "networkChecked": False,
    }
    return SetupPlan(scoped_profile, requirements)


def write_profile(plan: SetupPlan, profiles_dir: Path, profile_name: str) -> Path:
    """Create a new private profile; an existing file is never replaced."""
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", profile_name) is None:
        raise _failure("PROFILE_NAME_INVALID", "profile name must contain only letters, digits, '.', '_' or '-'")
    if plan.requirements["missingConfiguration"]:
        raise _failure("SETUP_TEMPLATE_INCOMPLETE", "selected operations have dependencies absent from the template")
    if plan.requirements["unresolvedAuthRefs"]:
        raise _failure("SETUP_AUTH_UNDOCUMENTED", "selected authentication providers have no onboarding metadata")
    if plan.requirements.get("missingCredentialBindings"):
        raise _failure(
            "SETUP_CREDENTIAL_BINDINGS_MISSING",
            "selected credential references have no Vault bindings; configure them in the template before init",
        )
    directory = profiles_dir.expanduser()
    path = directory / f"{profile_name}.yaml"
    document = plan.profile.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_defaults=True)
    document = {"environment": plan.profile.environment, **{key: value for key, value in document.items() if value}}
    content = yaml.safe_dump(document, sort_keys=False)
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise _failure("PROFILE_ALREADY_EXISTS", "profile already exists; choose a new profile name") from error
    except OSError as error:
        raise _failure("PROFILE_WRITE_FAILED", "profile could not be created") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
    except OSError as error:
        path.unlink(missing_ok=True)
        raise _failure("PROFILE_WRITE_FAILED", "profile could not be written") from error
    return path


def _select_document(
    document: dict[str, Any], operations: tuple[OperationDefinition, ...], setup: ProductSetup | None = None
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    refs: dict[str, set[str]] = {
        "services": set(),
        "databases": set(),
        "kafka": set(),
        "s3": set(),
        "kubernetes": set(),
        "endpoints": set(),
    }
    topics: dict[str, set[str]] = {}
    buckets: dict[str, set[str]] = {}
    for operation in operations:
        binding = STANDARD_RESOURCE_BINDINGS.get(operation.kind)
        if binding is None and setup is not None and (custom := setup.resource_bindings.get(operation.kind)):
            if custom.section == "none":
                continue
            if custom.section not in refs or not custom.config_field:
                raise _failure("SETUP_CONFIG_INVALID", "product declares an invalid custom resource binding")
            binding = (custom.section, custom.config_field)
        if binding is None:
            raise _failure("SETUP_KIND_UNSUPPORTED", f"onboarding does not support operation kind '{operation.kind}'")
        section, key = binding
        reference = operation.config.get(key)
        if not isinstance(reference, str) or not reference:
            raise _failure("SETUP_CONFIG_INVALID", f"operation '{operation.id}' has no valid config.{key}")
        refs[section].add(reference)
        if section == "kafka":
            topic = operation.config.get("topicRef")
            if not isinstance(topic, str) or not topic:
                raise _failure("SETUP_CONFIG_INVALID", f"operation '{operation.id}' has no valid config.topicRef")
            topics.setdefault(reference, set()).add(topic)
        if section == "s3" and operation.config.get("action") != "list-buckets":
            bucket = operation.config.get("bucketRef")
            if not isinstance(bucket, str) or not bucket:
                raise _failure("SETUP_CONFIG_INVALID", f"operation '{operation.id}' has no valid config.bucketRef")
            buckets.setdefault(reference, set()).add(bucket)
            if source := operation.config.get("sourceBucketRef"):
                if not isinstance(source, str):
                    raise _failure("SETUP_CONFIG_INVALID", "sourceBucketRef must be a string reference")
                buckets[reference].add(source)
    selected: dict[str, Any] = {"environment": document.get("environment", "unknown")}
    missing = []
    for section, references in refs.items():
        configured = document.get(section) or {}
        if not isinstance(configured, dict):
            raise _failure("SETUP_TEMPLATE_INVALID", f"selected {section} must be a mapping")
        selected[section] = {
            key: configured[key] if section == "endpoints" else dict(configured[key])
            for key in sorted(references)
            if key in configured
        }
        missing.extend(f"{section}.{key}" for key in references if key not in configured)
    for name, cluster in selected["kafka"].items():
        configured_topics = cluster.get("topics") or {}
        cluster["topics"] = {key: configured_topics[key] for key in sorted(topics[name]) if key in configured_topics}
        missing.extend(f"kafka.{name}.topics.{key}" for key in topics[name] if key not in configured_topics)
    for name, storage in selected["s3"].items():
        bucket_refs = buckets.get(name, set())
        configured_buckets = storage.get("buckets") or {}
        storage["buckets"] = {key: configured_buckets[key] for key in sorted(bucket_refs) if key in configured_buckets}
        missing.extend(f"s3.{name}.buckets.{key}" for key in bucket_refs if key not in configured_buckets)
    resources = {
        "services": sorted(refs["services"]),
        "databases": sorted(refs["databases"]),
        "kafka": {name: sorted(keys) for name, keys in sorted(topics.items())},
    }
    if refs["endpoints"]:
        resources["endpoints"] = sorted(refs["endpoints"])
    if refs["s3"]:
        resources["s3"] = {name: sorted(buckets.get(name, set())) for name in sorted(refs["s3"])}
    if refs["kubernetes"]:
        resources["kubernetes"] = sorted(refs["kubernetes"])
    return selected, resources, missing


def _selected_credentials(
    configured: Any, references: set[str], dynamic: bool, *, resolve_env: bool
) -> EnvironmentCredentialConfig | VaultCredentialConfig | None:
    if configured is None or (not references and not dynamic):
        return None
    if not isinstance(configured, dict):
        raise _failure("SETUP_TEMPLATE_INVALID", "credential configuration must be a mapping")
    bindings = configured.get("bindings", {})
    if not isinstance(bindings, dict):
        raise _failure("SETUP_TEMPLATE_INVALID", "credential bindings must be a mapping")
    selected = {
        **configured,
        "bindings": dict(bindings) if dynamic else {key: value for key, value in bindings.items() if key in references},
    }
    try:
        if selected.get("provider") == "env":
            return EnvironmentCredentialConfig.model_validate(selected)
        return VaultCredentialConfig.model_validate(_resolve_environment(selected) if resolve_env else selected)
    except (ValidationError, TypeError, ValueError) as error:
        raise _failure("SETUP_TEMPLATE_INVALID", "selected credential configuration is invalid") from error


def _resolve_environment(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _resolve_environment(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_environment(item) for item in value]
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise _failure("PROFILE_INVALID", f"selected configuration requires environment variable '{name}'")
            return os.environ[name]

        return ENV_PLACEHOLDER.sub(replace, value)
    return value


def _contains_path(document: dict[str, Any], path: str) -> bool:
    value: Any = document
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return False
        value = value[key]
    return True


def _vault_prerequisites(config: VaultCredentialConfig) -> tuple[SetupPrerequisite, ...]:
    prerequisites = [
        SetupPrerequisite(
            id="vault-cli",
            description="Install a trusted Vault CLI binary; set QA_CLI_VAULT_EXECUTABLE when needed.",
            kind="tool",
            environment_variable="QA_CLI_VAULT_EXECUTABLE",
        )
    ]
    if config.auth_mode == "session":
        prerequisites.append(
            SetupPrerequisite(
                id="vault-session",
                description=(
                    "Authenticate explicitly with qa auth login for the selected profile."
                    if config.auth_path is not None
                    else "Authenticate with Vault CLI using the standard Vault token helper for this user."
                ),
                kind="login",
            )
        )
    else:
        prerequisites.append(
            SetupPrerequisite(
                id="vault-token",
                description="Provide a short-lived job token in VAULT_TOKEN through the job's secret environment.",
                kind="credential",
                environment_variable="VAULT_TOKEN",
            )
        )
    if config.ca_bundle is not None:
        prerequisites.append(
            SetupPrerequisite(
                id="vault-ca",
                description="Install the configured local CA bundle for Vault TLS verification.",
                kind="file",
                default_path=config.ca_bundle,
            )
        )
    return tuple(prerequisites)


def _prerequisite_payload(item: SetupPrerequisite) -> dict[str, Any]:
    # Credential environment variables describe required inputs; never read their values.
    configured = (
        os.environ.get(item.environment_variable)
        if item.environment_variable and item.kind in {"file", "tool", "executable"}
        else None
    )
    path = Path(configured) if configured else item.default_path
    return {
        "id": item.id,
        "description": item.description,
        "kind": item.kind,
        "environmentVariable": item.environment_variable,
        "path": str(path.expanduser()) if path is not None else None,
    }


def _failure(code: str, message: str) -> RuntimeFailure:
    return RuntimeFailure(ExitCode.INVALID_INPUT, code, message, source="configuration")
