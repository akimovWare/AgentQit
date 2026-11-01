from __future__ import annotations

import json
import sys
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import anyio
import typer

from qa_cli.browser_commands import browser_app
from qa_cli.openapi_import import import_openapi
from qa_cli.parsing import StructuredTyperGroup
from qa_cli.product_scaffold import create_product_scaffold
from qa_cli.renderers import render
from qa_cli.settings import DEFAULT_PROFILE, Settings
from qa_cli.workspace_commands import workspace_app
from qa_runtime.adapters.credential_helper import SubprocessCredentialSource, UnavailableCredentialSource
from qa_runtime.adapters.environment import EnvironmentCredentialSource
from qa_runtime.adapters.vault import VaultCliCredentialSource
from qa_runtime.adapters.vault_auth import login_oidc
from qa_runtime.authoring import registry_json_schema, validate_product_authoring
from qa_runtime.bootstrap import build_runtime
from qa_runtime.bootstrap import scope_profile as _scope_profile
from qa_runtime.contracts import CommandResult, ExitCode
from qa_runtime.credential_config import EnvironmentCredentialConfig, VaultCredentialConfig
from qa_runtime.engine import QaRuntime
from qa_runtime.errors import RuntimeFailure
from qa_runtime.evidence import EvidenceScope, RunStore, content_digest
from qa_runtime.models import OperationDefinition, ParameterDefinition, Profile
from qa_runtime.onboarding import build_setup_plan, write_profile
from qa_runtime.plugins import installed_product_ids, product_plugin_for, select_product
from qa_runtime.profile import ProfileLoader
from qa_runtime.registry import OperationRegistry
from qa_runtime.security import SecretRedactor
from qa_runtime.version import VERSION

app = typer.Typer(
    name="qa",
    cls=StructuredTyperGroup,
    help="AgentQit QA runtime. Start with 'qa guide --output json'.",
    add_completion=False,
    no_args_is_help=False,
)
registry_app = typer.Typer(help="Inspect versioned operation registries.", no_args_is_help=False)
config_app = typer.Typer(
    help="Create a minimal profile, explain requirements, or validate configuration.", no_args_is_help=False
)
product_app = typer.Typer(help="Create and validate product packages from local contracts.", no_args_is_help=False)
run_app = typer.Typer(help="Create and inspect private local evidence journals.", no_args_is_help=False)
auth_app = typer.Typer(help="Explicit local login to the credential provider.", no_args_is_help=False)
app.add_typer(registry_app, name="registry")
app.add_typer(config_app, name="config")
app.add_typer(product_app, name="product")
app.add_typer(run_app, name="run")
app.add_typer(auth_app, name="auth")
app.add_typer(workspace_app, name="workspace")
app.add_typer(browser_app, name="browser")


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"AgentQit {VERSION}")
        raise typer.Exit()


@app.callback()
def root(
    version: Annotated[
        bool, typer.Option("--version", callback=_show_version, is_eager=True, help="Show the installed version.")
    ] = False,
) -> None:
    """Expose runtime capabilities through stable shell commands."""


@app.command("mcp")
def serve_mcp(
    workspace: Annotated[Path, typer.Option("--workspace")],
    allow_mutation: Annotated[bool, typer.Option("--allow-mutation")] = False,
) -> None:
    """Run the local MCP interface over stdio; stdout is reserved for the protocol."""
    try:
        from qa_mcp.server import create_server

        create_server(workspace, mutations_allowed=allow_mutation).run(transport="stdio")
    except ImportError:
        print("AgentQit dependencies are incomplete; reinstall the package", file=sys.stderr)
        raise typer.Exit(2) from None
    except Exception:
        print("AgentQit MCP startup failed; validate the workspace with the CLI", file=sys.stderr)
        raise typer.Exit(2) from None


def _decode_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _parse_params(items: list[str], input_json: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if input_json:
        try:
            loaded = json.loads(input_json)
        except json.JSONDecodeError as error:
            raise RuntimeFailure(ExitCode.INVALID_INPUT, "INVALID_INPUT_JSON", str(error), source="cli") from error
        if not isinstance(loaded, dict):
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "INVALID_INPUT_JSON",
                "--input-json must contain a JSON object",
                source="cli",
            )
        result.update(loaded)
    for item in items:
        name, separator, value = item.partition("=")
        if not separator or not name:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "INVALID_PARAM",
                f"parameter '{item}' must use KEY=VALUE syntax",
                source="cli",
            )
        if name in result:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "DUPLICATE_PARAM",
                f"parameter '{name}' was provided more than once",
                source="cli",
            )
        result[name] = _decode_value(value)
    return result


def _input_document(path: Path) -> dict[str, Any]:
    try:
        if str(path) == "-":
            raw = sys.stdin.read(1_000_001)
        else:
            with path.open() as stream:
                raw = stream.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("input too large")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        return value
    except (OSError, ValueError) as error:
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "INPUT_FILE_INVALID",
            "input file must contain one JSON object of at most 1 MB",
            source="cli",
        ) from error


def _command_input(
    items: list[str],
    input_json: str | None,
    input_file: Path | None = None,
    after_cursor: Path | None = None,
) -> dict[str, Any]:
    if input_file is not None:
        if input_json is not None:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "DUPLICATE_INPUT",
                "use either --input-json or --input-file",
                source="cli",
            )
        input_json = json.dumps(_input_document(input_file))
    params = _parse_params(items, input_json)
    if after_cursor is not None:
        if "after-cursor" in params:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "DUPLICATE_PARAM",
                "after-cursor was provided more than once",
                source="cli",
            )
        document = _input_document(after_cursor)
        if document.get("schema_version") == "qa.evidence/v1":
            document = document.get("result") or {}
        if not isinstance(document, dict):
            raise _invalid_cursor_file()
        if "result" in document:
            document = document.get("result") or {}
        if not isinstance(document, dict):
            raise _invalid_cursor_file()
        params["after-cursor"] = document.get("cursor", document)
    return params


def _invalid_cursor_file() -> RuntimeFailure:
    return RuntimeFailure(
        ExitCode.INVALID_INPUT,
        "INPUT_FILE_INVALID",
        "cursor file must contain a cursor object or a completed checkpoint result",
        source="cli",
    )


def _parameter_placeholder(parameter: ParameterDefinition) -> str | dict[str, Any]:
    if parameter.location == "credential-body":
        return "<credential-reference>"
    if parameter.enum:
        return f"<one of: {', '.join(parameter.enum)}>"
    if parameter.format == "uuid":
        return "<uuid>"
    if parameter.format == "date":
        return "<ISO-8601 date>"
    if parameter.format == "date-time":
        return "<ISO-8601 date-time with UTC offset>"
    if parameter.value_type == "object":
        if parameter.properties:
            return {
                name: _parameter_placeholder(definition) for name, definition in sorted(parameter.properties.items())
            }
        return {"<field>": "<value>"}
    if parameter.value_type == "array" and parameter.items is not None:
        return [_parameter_placeholder(parameter.items)]
    return f"<{parameter.value_type or 'value'}>"


@dataclass(frozen=True)
class InvocationScope:
    product: str
    registry_paths: tuple[Path, ...]
    profiles_dir: Path
    profile_name: str

    def argv(self) -> list[str]:
        arguments = [
            "--profile",
            self.profile_name,
            "--profiles-dir",
            str(self.profiles_dir.expanduser().absolute()),
            "--product",
            self.product,
        ]
        for path in self.registry_paths:
            arguments.extend(["--registry", str(path.expanduser().absolute())])
        return arguments


def _invocation_hint(
    operation: OperationDefinition,
    profile: Profile | None = None,
    profile_name: str = DEFAULT_PROFILE,
    *,
    scope: InvocationScope | None = None,
) -> dict[str, Any]:
    required_input = {
        name: _parameter_placeholder(parameter)
        for name, parameter in sorted(operation.parameters.items())
        if parameter.required
        and (profile is None or (profile.defaults.get(name) is None and parameter.default is None))
    }
    invocation = {
        "argvTemplate": [
            "qa",
            "invoke",
            operation.id,
            *(scope.argv() if scope is not None else ["--profile", profile_name]),
            "--input-json",
            "<JSON_OBJECT>" if required_input else "{}",
            "--output",
            "json",
        ],
        "inputTemplate": required_input,
        "safety": {
            "admission": operation.admission,
            "mutating": operation.mutating,
            "explicitAuthorizationRequired": operation.mutating,
            "requiredFlag": "--allow-mutation" if operation.mutating else None,
            "requiredEnvironment": "qa" if operation.mutating else None,
        },
    }
    if profile is not None:
        redactor = SecretRedactor()
        profile_defaults = redactor.redact(
            {
                name: profile.defaults[name]
                for name in sorted(operation.parameters)
                if profile.defaults.get(name) is not None
            }
        )
        operation_defaults = redactor.redact(
            {
                name: parameter.default
                for name, parameter in sorted(operation.parameters.items())
                if profile.defaults.get(name) is None and parameter.default is not None
            }
        )
        invocation["effectiveInput"] = {
            "callerRequired": sorted(required_input),
            "profileDefaults": profile_defaults,
            "operationDefaults": operation_defaults,
        }
    return invocation


def _task_workflow() -> list[dict[str, str]]:
    return [
        {
            "step": "requirements",
            "instruction": "Read the task requirements and expected behavior.",
        },
        {
            "step": "changes",
            "instruction": "Read the changes and affected service relationships. Implementation explains the change; "
            "it does not establish expected behavior.",
        },
        {
            "step": "context",
            "instruction": "Find guides through registry describe. Verify sources, revisions, and applicability. "
            "Record an unknown deployed version as a limitation.",
        },
        {
            "step": "select-observations",
            "instruction": "Choose main, boundary, and side-effect observations. Record expectations and their sources "
            "before acting. A guide is descriptive context.",
        },
        {
            "step": "investigate",
            "instruction": "Invoke individual operations within the authorized task. Capture baseline and responses; "
            "choose the next action from evidence. Report unavailable observations.",
        },
        {
            "step": "conclude",
            "instruction": "Compare facts with requirements and cite evidence. Distinguish defects, stale guides, "
            "and incomplete data; do not adjust expectations to match implementation.",
        },
    ]


def _related_guides(product: str, operation_id: str) -> list[dict[str, Any]]:
    try:
        plugin = product_plugin_for(product)
    except RuntimeFailure as error:
        # Describing an explicit registry does not require an installed product.
        if error.code == "PRODUCT_NOT_FOUND":
            return []
        raise
    return [
        {
            "id": guide.id,
            "purpose": guide.purpose,
            "argv": ["qa", "guide", guide.id, "--product", plugin.product_id, "--output", "json"],
        }
        for guide in sorted(plugin.guides_for(operation_id), key=lambda item: item.id)
    ]


def _guide_payload(product_plugin=None, settings: Settings | None = None) -> dict[str, Any]:
    selected_settings = settings or Settings()
    selected_product = (
        product_plugin.product_id if product_plugin is not None else (selected_settings.product or "<product-id>")
    )
    product_args = ["--product", selected_product]
    registry_args = (
        ["--registry", str(selected_settings.registry_path.expanduser().absolute())]
        if selected_settings.registry_path is not None
        else []
    )
    payload = {
        "productDiscovery": {
            "argv": ["qa", "product", "list", "--output", "json"],
            "selection": "Use --product or QA_CLI_PRODUCT; a single installed product is selected automatically.",
        },
        "taskWorkflow": _task_workflow(),
        "workflow": [
            {
                "step": "discover",
                "argv": ["qa", "registry", "list", *product_args, *registry_args, "--output", "json"],
                "read": "result.operations: purpose, admission, mutating and parameter names",
            },
            {
                "step": "inspect",
                "argv": [
                    "qa",
                    "registry",
                    "describe",
                    "<operation-id>",
                    *product_args,
                    *registry_args,
                    "--output",
                    "json",
                ],
                "read": "result.parameters, result.invocation and result.guides",
            },
            {
                "step": "configure-selected-operations",
                "argv": [
                    "qa",
                    "config",
                    "requirements",
                    "--operation",
                    "<operation-id>",
                    *product_args,
                    *registry_args,
                    "--output",
                    "json",
                ],
                "read": "credentialRefs, prerequisites, templateFields and callerRequired; no profile is needed",
            },
            {
                "step": "invoke",
                "argvSource": "registry describe -> result.invocation.argvTemplate",
                "instruction": "Prepare the profile with config init if needed, complete its prerequisites, "
                "then run doctor and plan for the selected operation and exact input. "
                "Replace placeholders and pass one JSON object; do not invent parameters. "
                "Invoke only within an explicitly authorized QA task.",
            },
        ],
        "configuration": {
            "defaultProfile": selected_settings.profile,
            "recommendedProfile": DEFAULT_PROFILE,
            "profilePath": str(selected_settings.profiles_dir / "<name>.yaml"),
            "authLoginArgv": [
                "qa",
                "auth",
                "login",
                "--profile",
                selected_settings.profile,
                "--profiles-dir",
                str(selected_settings.profiles_dir.expanduser().absolute()),
            ],
            "credentialsNote": "Vault bindings belong to the selected profile. auth login is an explicit local "
            "OIDC browser login; CI uses authMode=token-env and a job-scoped VAULT_TOKEN. "
            "Never print credentials or transfer a personal login session to CI.",
            "initArgv": [
                "qa",
                "config",
                "init",
                "--operation",
                "<operation-id>",
                "--profile",
                selected_settings.profile,
                "--profiles-dir",
                str(selected_settings.profiles_dir.expanduser().absolute()),
                *product_args,
                *registry_args,
                "--output",
                "json",
            ],
            "requirementsArgv": [
                "qa",
                "config",
                "requirements",
                "--operation",
                "<operation-id>",
                *product_args,
                *registry_args,
                "--output",
                "json",
            ],
            "validateArgv": [
                "qa",
                "config",
                "validate",
                "--profile",
                selected_settings.profile,
                "--profiles-dir",
                str(selected_settings.profiles_dir.expanduser().absolute()),
                *product_args,
                *registry_args,
                "--output",
                "json",
            ],
            "note": "Start with requirements and init for selected operations, then doctor and plan. "
            "Full config validate is for a complete profile and is not required for the first operation. "
            "init never overwrites a profile; repeat --operation to combine dependencies in a new profile.",
        },
        "authoring": {
            "schemaArgv": ["qa", "registry", "schema", "--output", "json"],
            "initArgv": ["qa", "product", "init", "<product-id>", "--directory", "<NEW_DIRECTORY>"],
            "importArgv": [
                "qa",
                "product",
                "import-openapi",
                "--source",
                "<OPENAPI_FILE>",
                "--service",
                "<service-ref>",
                "--directory",
                "<NEW_DRAFT_DIRECTORY>",
            ],
            "instruction": "Start from canonical contracts and source revisions. Generate drafts, review effects, "
            "then validate bindings and input on fake transports. The scaffold contains rules and offline tests.",
        },
        "admission": {
            "agent-read": "read operation available to an agent",
            "agent-mutate": "requires explicit task authorization and --allow-mutation",
            "diagnostic": "read-only DB/Kafka inspection; use only when diagnostics are needed",
            "expert-only": "do not invoke automatically; requires an operation-specific instruction",
            "prototype": "not ready for working tasks",
        },
        "rules": [
            "Prefer --output json; stdout then contains exactly one result envelope.",
            "Never add --allow-mutation without explicit authorization; mutations also require environment=qa.",
            "Treat status=success and exit_code=0 as command success; this alone does not prove the business outcome.",
            "On failure inspect error.code, error.message and recovery guidance before choosing a next action.",
        ],
    }
    if product_plugin is not None:
        payload["evidenceWorkflow"] = _evidence_workflow(product_plugin, selected_settings)
        payload["rules"].extend(product_plugin.guide_rules)
        payload["scenarios"] = [
            {"id": guide.id, "purpose": guide.purpose}
            for guide in sorted(product_plugin.guides, key=lambda item: item.id)
        ]
        payload["scenarioDiscovery"] = {
            "argv": ["qa", "guide", "<scenario>", *product_args, "--output", "json"],
            "instruction": "Read relevant business context before acting; choose observations "
            "for the task and affected change.",
        }
    return payload


def _evidence_workflow(plugin, settings: Settings) -> dict[str, Any]:
    scope = InvocationScope(
        plugin.product_id,
        (settings.registry_path or plugin.registry_path,),
        settings.profiles_dir,
        settings.profile,
    )
    return {
        "planArgv": ["qa", "plan", "<operation-id>", *scope.argv(), "--input-file", "<INPUT_JSON>", "--output", "json"],
        "doctorArgv": ["qa", "doctor", "<operation-id>", *scope.argv(), "--output", "json"],
        "checkpointArgv": [
            "qa",
            "checkpoint",
            "<observation-operation-id>",
            *scope.argv(),
            "--run-dir",
            "<RUN_DIR>",
            "--output",
            "json",
        ],
        "invokeAdditionalArgs": ["--run-dir", "<RUN_DIR>"],
        "observationAdditionalArgs": ["--after-cursor", "<CHECKPOINT_EVIDENCE_FILE>", "--run-dir", "<RUN_DIR>"],
        "resumeArgv": ["qa", "run", "show", "--run-dir", "<RUN_DIR>", "--output", "json"],
        "rules": [
            "Use one run directory per scenario with unchanged product/profile/registry.",
            "Save observation checkpoints and baseline state before an explicitly authorized mutation.",
            "For pending attempts or unknown mutation outcomes, read state using the saved IDs before any retry.",
            "plan and default doctor use no network; checkpoint and invoke do. doctor --network only executes reads.",
            "The agent selects observations and evaluates their results against the task requirements.",
            "Save task expectations and source references alongside the journal; run show returns command facts only.",
            "A cursor records an offset boundary, not causation; use independent correlation and isolated fixtures.",
        ],
    }


def _selected_registries(
    settings: Settings,
    overrides: list[Path] | None,
    product: str | None = None,
) -> tuple[Path, ...]:
    if overrides:
        return tuple(overrides)
    if settings.registry_path is not None:
        return (settings.registry_path,)
    return (product_plugin_for(select_product(product or settings.product)).registry_path,)


def _invocation_scope(
    settings: Settings,
    registry: list[Path] | None,
    product: str | None,
    profile: str | None,
    profiles_dir: Path | None,
) -> InvocationScope:
    return InvocationScope(
        product=select_product(product or settings.product),
        registry_paths=_selected_registries(settings, registry, product),
        profiles_dir=profiles_dir or settings.profiles_dir,
        profile_name=profile or settings.profile,
    )


def _failure_result(command: str, started: int, error: RuntimeFailure) -> CommandResult:
    return CommandResult.failure(
        command=command,
        run_id=f"run_{uuid.uuid4().hex}",
        duration_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
        exit_code=error.exit_code,
        error=error.as_error_item(),
    )


def _internal_failure(error: Exception) -> RuntimeFailure:
    return RuntimeFailure(
        ExitCode.INTERNAL_ERROR,
        "INTERNAL_ERROR",
        f"unexpected runtime error ({type(error).__name__})",
        source="runtime",
    )


async def _execute(
    operation: str,
    profile_name: str,
    profiles_dir: Path,
    registry_paths: tuple[Path, ...],
    params: dict[str, Any],
    allow_mutation: bool,
    product: str,
    credential_helper: Path | None,
    run_dir: Path | None = None,
    capture_only: bool = False,
) -> CommandResult:
    assembled = await _assemble_runtime(
        profile_name,
        profiles_dir,
        registry_paths,
        product,
        credential_helper,
        operation_ids=(operation,),
    )
    runtime = assembled.runtime
    store = None
    attempt = None
    try:
        if run_dir is not None:
            store = RunStore(run_dir, redactor=runtime)
            store.initialize(assembled.evidence_scope)
            resolved = {} if capture_only else runtime.validate_input(operation, params)
            attempt = store.begin(operation, resolved)
        if capture_only:
            result = await runtime.capture(operation)
        else:
            result = await runtime.invoke(operation, params, allow_mutation=allow_mutation)
    except BaseException:
        with suppress(Exception):
            await _close_runtime(runtime)
        raise
    # Persist the confirmed outcome before cancellable cleanup.
    result = runtime.redact_result(result)
    if store is not None and attempt is not None:
        evidence_path = store.directory / f"{attempt.attempt_id}.json"
        result.evidence.append(str(evidence_path))
        try:
            store.finish(attempt, result)
        except (RuntimeFailure, OSError):
            result.evidence.remove(str(evidence_path))
            result.warnings.append(
                "Evidence completion failed; the pending attempt remains uncertain. Do not repeat a mutation blindly."
            )
    if not await _close_runtime(runtime):
        result.warnings.append(
            "Resource cleanup failed after the operation completed; the reported outcome is preserved."
        )
    return runtime.redact_result(result)


_CLEANUP_TIMEOUT_SECONDS = 2.0


async def _close_runtime(runtime: QaRuntime) -> bool:
    try:
        with anyio.move_on_after(_CLEANUP_TIMEOUT_SECONDS, shield=True) as scope:
            await runtime.aclose()
        return not scope.cancel_called
    except Exception:
        return False


def _evidence_scope(scope: InvocationScope) -> EvidenceScope:
    profile = ProfileLoader(scope.profiles_dir).load(scope.profile_name)
    registry = OperationRegistry.load(scope.registry_paths)
    return _loaded_evidence_scope(scope.product, scope.profile_name, profile, registry)


def _loaded_evidence_scope(
    product: str, profile_name: str, profile: Profile, registry: OperationRegistry
) -> EvidenceScope:
    document = profile.model_dump(mode="json", by_alias=True)
    if profile.credentials is None:
        # Omit absent credential configuration from the profile digest.
        document.pop("credentials", None)
    return EvidenceScope(
        product=product,
        profile=profile_name,
        profile_digest=content_digest(document),
        registry_digest=content_digest(
            {
                "operations": [operation.model_dump(mode="json", by_alias=True) for operation in registry.operations()],
                "codecs": {
                    name: codec.model_dump(mode="json", by_alias=True)
                    for name, codec in sorted(registry.codecs().items())
                },
            }
        )
        if registry.codecs()
        else content_digest([operation.model_dump(mode="json", by_alias=True) for operation in registry.operations()]),
    )


@dataclass(frozen=True)
class AssembledRuntime:
    runtime: QaRuntime
    profile: Profile
    registry: OperationRegistry
    evidence_scope: EvidenceScope


async def _assemble_runtime(
    profile_name: str,
    profiles_dir: Path,
    registry_paths: tuple[Path, ...],
    product: str,
    credential_helper: Path | None,
    operation_ids: tuple[str, ...] | None = None,
) -> AssembledRuntime:
    plugin = product_plugin_for(product)
    profile = ProfileLoader(profiles_dir).load(profile_name)
    registry = OperationRegistry.load(registry_paths)
    evidence_scope = _loaded_evidence_scope(product, profile_name, profile, registry)
    if profile.credentials is None:
        credentials = _credential_source(credential_helper)
    elif isinstance(profile.credentials, EnvironmentCredentialConfig):
        if credential_helper is not None:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "CREDENTIAL_SOURCE_CONFLICT",
                "Remove the credential helper when the profile selects environment credentials",
                source="credentials",
            )
        credentials = EnvironmentCredentialSource(profile.credentials)
    else:
        credentials = _vault_credential_source(profile.credentials, credential_helper, Settings().vault_executable)
    runtime = await build_runtime(plugin, profile, registry, credentials, operation_ids=operation_ids)
    return AssembledRuntime(runtime, _scope_profile(profile, registry, operation_ids), registry, evidence_scope)


async def _validate_configuration(
    profile_name: str,
    profiles_dir: Path,
    registry_paths: tuple[Path, ...],
    product: str,
    credential_helper: Path | None,
):
    assembled = await _assemble_runtime(
        profile_name,
        profiles_dir,
        registry_paths,
        product,
        credential_helper,
    )
    await assembled.runtime.aclose()
    return assembled.profile


def _credential_source(credential_helper: Path | None):
    if credential_helper is not None:
        return SubprocessCredentialSource(credential_helper)
    return UnavailableCredentialSource()


def _vault_credential_source(config: VaultCredentialConfig, helper: Path | None, executable: Path):
    if helper is not None:
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "CREDENTIAL_SOURCE_CONFLICT",
            "remove QA_CLI_CREDENTIAL_HELPER when the profile selects Vault",
            source="credentials",
        )
    return VaultCliCredentialSource(config, executable=executable)


@auth_app.command("login", help="Open a browser for Vault OIDC login; store the session with Vault's token helper.")
def auth_login(
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
    timeout_seconds: Annotated[float, typer.Option("--timeout-seconds", min=1, max=120)] = 120,
    output: Annotated[str | None, typer.Option("--output")] = None,
) -> None:
    settings = Settings()
    started = time.monotonic_ns()
    try:
        selected_profile = profile or settings.profile
        loaded = ProfileLoader(profiles_dir or settings.profiles_dir).load(selected_profile)
        if not isinstance(loaded.credentials, VaultCredentialConfig):
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "VAULT_NOT_CONFIGURED",
                "the selected profile must configure credentials.provider as vault-cli",
                source="credentials",
            )
        _vault_credential_source(loaded.credentials, settings.credential_helper, settings.vault_executable)
        anyio.run(login_oidc, loaded.credentials, settings.vault_executable, timeout_seconds)
        result = _local_success(
            "auth login",
            started,
            {"profile": selected_profile, "provider": "vault-cli", "authenticated": True, "credentialsChecked": False},
            loaded.environment,
        )
    except RuntimeFailure as error:
        result = _failure_result("auth login", started, error)
    except Exception as error:
        result = _failure_result("auth login", started, _internal_failure(error))
    _finish_command(result, output or settings.output)


@config_app.command("requirements", help="Explain selected dependencies without a profile, credentials or network.")
def config_requirements(
    operation: Annotated[
        list[str], typer.Option("--operation", help="Operation id; repeat for a set of capabilities.")
    ],
    profile: Annotated[
        str | None, typer.Option("--profile", help="Inspect an existing profile instead of the template.")
    ] = None,
    template: Annotated[
        Path | None, typer.Option("--template", help="Trusted non-secret team profile template.")
    ] = None,
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
    registry: Annotated[list[Path] | None, typer.Option("--registry")] = None,
    product: Annotated[str | None, typer.Option("--product")] = None,
    output: Annotated[str | None, typer.Option("--output")] = None,
) -> None:
    settings = Settings()
    started = time.monotonic_ns()
    try:
        scope = _invocation_scope(settings, registry, product, profile, profiles_dir)
        loaded_profile = ProfileLoader(scope.profiles_dir).load(profile) if profile is not None else None
        setup_plan = build_setup_plan(
            product_plugin_for(scope.product),
            OperationRegistry.load(scope.registry_paths),
            operation,
            profile=loaded_profile,
            template=template,
        )
        payload = {
            **setup_plan.requirements,
            "profileTemplate": setup_plan.profile.model_dump(mode="json", by_alias=True, exclude_defaults=True),
        }
        if setup_plan.profile.credentials is not None:
            # Expose nonsecret provider metadata separately from credential values.
            payload["profileTemplate"].pop("credentials", None)
            payload["credentialConfiguration"] = setup_plan.profile.credentials.model_dump(
                mode="json", by_alias=True, exclude_none=True, exclude={"bindings"}
            )
        result = _local_success("config requirements", started, SecretRedactor().redact(payload))
    except RuntimeFailure as error:
        result = _failure_result("config requirements", started, error)
    except Exception as error:
        result = _failure_result("config requirements", started, _internal_failure(error))
    _finish_command(result, output or settings.output)


@config_app.command("init", help="Create a minimal non-secret profile for selected operations; never overwrite.")
def initialize_config(
    operation: Annotated[list[str], typer.Option("--operation", help="Operation id; repeat to combine dependencies.")],
    profile: Annotated[str | None, typer.Option("--profile", help="New profile name.")] = None,
    template: Annotated[
        Path | None, typer.Option("--template", help="Trusted non-secret team profile template.")
    ] = None,
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
    registry: Annotated[list[Path] | None, typer.Option("--registry")] = None,
    product: Annotated[str | None, typer.Option("--product")] = None,
    output: Annotated[str | None, typer.Option("--output")] = None,
) -> None:
    settings = Settings()
    started = time.monotonic_ns()
    try:
        scope = _invocation_scope(settings, registry, product, profile, profiles_dir)
        setup_plan = build_setup_plan(
            product_plugin_for(scope.product),
            OperationRegistry.load(scope.registry_paths),
            operation,
            template=template,
        )
        path = write_profile(setup_plan, scope.profiles_dir, scope.profile_name)
        result = _local_success(
            "config init",
            started,
            {
                "profile": scope.profile_name,
                "path": str(path.absolute()),
                "requirements": setup_plan.requirements,
                "nextSteps": [
                    "Verify templateFields, replace example values if present, and supply the credential references.",
                    "Provide callerRequired values from the task or an explicitly selected QA fixture.",
                    "Run doctor for each selected operation, then plan its exact input before authorized QA use.",
                ],
                "doctorArgv": [["qa", "doctor", identifier, *scope.argv()] for identifier in dict.fromkeys(operation)],
            },
        )
    except RuntimeFailure as error:
        result = _failure_result("config init", started, error)
    except Exception as error:
        result = _failure_result("config init", started, _internal_failure(error))
    _finish_command(result, output or settings.output)


@registry_app.command("schema", help="Return a JSON Schema for registry YAML and standard transport configs.")
def registry_schema(output: Annotated[str | None, typer.Option("--output")] = None) -> None:
    started = time.monotonic_ns()
    try:
        result = _local_success("registry schema", started, {"schema": registry_json_schema()})
    except Exception as error:
        result = _failure_result("registry schema", started, _internal_failure(error))
    _finish_command(result, output or Settings().output)


@product_app.command("init", help="Create an installable empty product package with authoring rules and offline tests.")
def initialize_product(
    product_id: Annotated[str, typer.Argument(help="New lowercase product identifier.")],
    directory: Annotated[Path, typer.Option("--directory", help="New directory; must not already exist.")],
    output: Annotated[str | None, typer.Option("--output")] = None,
) -> None:
    started = time.monotonic_ns()
    try:
        result = _local_success("product init", started, create_product_scaffold(directory, product_id))
    except RuntimeFailure as error:
        result = _failure_result("product init", started, error)
    except Exception as error:
        result = _failure_result("product init", started, _internal_failure(error))
    _finish_command(result, output or Settings().output)


@product_app.command("list", help="List installed product names without importing their providers.")
def list_products(output: Annotated[str | None, typer.Option("--output")] = None) -> None:
    started = time.monotonic_ns()
    try:
        result = _local_success("product list", started, {"products": installed_product_ids()})
    except Exception as error:
        result = _failure_result("product list", started, _internal_failure(error))
    _finish_command(result, output or Settings().output)


@product_app.command("import-openapi", help="Generate reviewable read-only drafts from a local OpenAPI contract.")
def import_product_openapi(
    source: Annotated[Path, typer.Option("--source", help="Local OpenAPI YAML or JSON; no network fetching.")],
    directory: Annotated[Path, typer.Option("--directory", help="New directory for drafts and the import report.")],
    service: Annotated[
        str, typer.Option("--service", help="Logical HTTP service reference for the generated operations.")
    ],
    contract_root: Annotated[
        Path | None,
        typer.Option("--contract-root", help="Allow local file references only inside this trusted directory."),
    ] = None,
    output: Annotated[str | None, typer.Option("--output")] = None,
) -> None:
    started = time.monotonic_ns()
    try:
        result = _local_success(
            "product import-openapi", started, import_openapi(source, directory, service, contract_root=contract_root)
        )
    except RuntimeFailure as error:
        result = _failure_result("product import-openapi", started, error)
    except Exception as error:
        result = _failure_result("product import-openapi", started, _internal_failure(error))
    _finish_command(result, output or Settings().output)


@product_app.command("validate", help="Apply the opt-in authoring policy for new products; does not compile or invoke.")
def validate_product(
    product: Annotated[str | None, typer.Option("--product")] = None,
    output: Annotated[str | None, typer.Option("--output")] = None,
) -> None:
    settings = Settings()
    started = time.monotonic_ns()
    try:
        registry = validate_product_authoring(product_plugin_for(select_product(product or settings.product)))
        result = _local_success(
            "product validate",
            started,
            {"valid": True, "operations": registry.ids(), "configurationCompiled": False, "networkChecked": False},
        )
    except RuntimeFailure as error:
        result = _failure_result("product validate", started, error)
    except Exception as error:
        result = _failure_result("product validate", started, _internal_failure(error))
    _finish_command(result, output or settings.output)


@app.command(help="Explain the requirements/change investigation workflow or business context for a product process.")
def guide(
    scenario: Annotated[
        str | None,
        typer.Argument(help="Optional guide id declared by the selected product."),
    ] = None,
    product: Annotated[str | None, typer.Option("--product", help="Installed product package name.")] = None,
    output: Annotated[str | None, typer.Option("--output", help="json or human.")] = None,
) -> None:
    settings = Settings()
    selected_output = output or settings.output
    if selected_output not in {"json", "human"}:
        raise typer.BadParameter("must be 'json' or 'human'", param_hint="--output")
    started = time.monotonic_ns()
    started_at = datetime.now(UTC)
    command = f"guide {scenario}" if scenario else "guide"
    try:
        selected_product = product or settings.product
        plugin = (
            product_plugin_for(select_product(selected_product))
            if selected_product or len(installed_product_ids()) == 1 or scenario is not None
            else None
        )
        if scenario is None:
            payload = _guide_payload(plugin, settings)
        else:
            selected_guide = plugin.guide(scenario)
            if selected_guide is None:
                available = ", ".join(sorted(guide.id for guide in plugin.guides)) or "<none>"
                raise RuntimeFailure(
                    ExitCode.INVALID_INPUT,
                    "UNKNOWN_GUIDE",
                    f"product '{plugin.product_id}' has no guide '{scenario}'; available: {available}",
                    source="plugins",
                )
            payload = {
                **selected_guide.payload,
                "id": selected_guide.id,
                "purpose": selected_guide.purpose,
                "operationIds": list(selected_guide.operation_ids),
                "taskWorkflow": _task_workflow(),
                "evidenceWorkflow": _evidence_workflow(plugin, settings),
            }
        result = CommandResult.success(
            command=command,
            run_id=f"run_{uuid.uuid4().hex}",
            environment="local",
            started_at=started_at,
            finished_at=datetime.now(UTC),
            duration_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
            result=payload,
        )
    except RuntimeFailure as error:
        result = _failure_result(command, started, error)
    except Exception as error:
        result = _failure_result(command, started, _internal_failure(error))
    render(result, selected_output)
    if result.exit_code:
        raise typer.Exit(result.exit_code)


@app.command(help="Show non-secret profile defaults and effective caller input for an optional operation.")
def context(
    operation: Annotated[
        str | None,
        typer.Argument(help="Optional registry operation id whose effective input should be explained."),
    ] = None,
    profile: Annotated[str | None, typer.Option("--profile", help="Profile name under the profiles directory.")] = None,
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir", help="Override the profile directory.")] = None,
    registry: Annotated[
        list[Path] | None,
        typer.Option("--registry", help="Registry YAML file or directory; repeatable."),
    ] = None,
    product: Annotated[str | None, typer.Option("--product", help="Installed product package name.")] = None,
    output: Annotated[str | None, typer.Option("--output", help="json or human.")] = None,
) -> None:
    settings = Settings()
    selected_output = output or settings.output
    if selected_output not in {"json", "human"}:
        raise typer.BadParameter("must be 'json' or 'human'", param_hint="--output")
    command = f"context {operation}" if operation else "context"
    started = time.monotonic_ns()
    started_at = datetime.now(UTC)
    try:
        scope = _invocation_scope(settings, registry, product, profile, profiles_dir)
        loaded_profile = ProfileLoader(scope.profiles_dir).load(scope.profile_name)
        payload: dict[str, Any] = {
            "profile": scope.profile_name,
            "environment": loaded_profile.environment,
            "defaults": SecretRedactor().redact(loaded_profile.defaults),
        }
        if operation is not None:
            loaded_registry = OperationRegistry.load(scope.registry_paths)
            definition = loaded_registry.get(operation)
            payload["operation"] = {
                "id": definition.id,
                "purpose": definition.purpose,
                "invocation": _invocation_hint(definition, loaded_profile, scope=scope),
                "guides": _related_guides(scope.product, operation),
            }
        result = CommandResult.success(
            command=command,
            run_id=f"run_{uuid.uuid4().hex}",
            environment=loaded_profile.environment,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            duration_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
            result=payload,
        )
    except RuntimeFailure as error:
        result = _failure_result(command, started, error)
    except Exception as error:
        result = _failure_result(command, started, _internal_failure(error))
    render(result, selected_output)
    if result.exit_code:
        raise typer.Exit(result.exit_code)


@app.command(help="Execute one registered operation with validated input and a stable result envelope.")
def invoke(
    operation: Annotated[str, typer.Argument(help="Registry operation id.")],
    param: Annotated[
        list[str] | None,
        typer.Option("--param", "-p", help="Operation input as KEY=VALUE; repeatable."),
    ] = None,
    input_json: Annotated[
        str | None,
        typer.Option("--input-json", help="Operation input as one JSON object."),
    ] = None,
    input_file: Annotated[Path | None, typer.Option("--input-file", help="JSON input file, or - for stdin.")] = None,
    after_cursor: Annotated[
        Path | None,
        typer.Option("--after-cursor", help="Checkpoint JSON file for a declared after-cursor input."),
    ] = None,
    run_dir: Annotated[Path | None, typer.Option("--run-dir", help="Persist a pending attempt and its result.")] = None,
    profile: Annotated[str | None, typer.Option("--profile", help="Profile name under the profiles directory.")] = None,
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir", help="Override the profile directory.")] = None,
    registry: Annotated[
        list[Path] | None,
        typer.Option("--registry", help="Registry YAML file or directory; repeatable."),
    ] = None,
    product: Annotated[str | None, typer.Option("--product", help="Installed product package name.")] = None,
    output: Annotated[str | None, typer.Option("--output", help="json or human.")] = None,
    allow_mutation: Annotated[
        bool,
        typer.Option("--allow-mutation", help="Allow a registry-marked mutating operation."),
    ] = False,
) -> None:
    settings = Settings()
    selected_output = output or settings.output
    if selected_output not in {"json", "human"}:
        raise typer.BadParameter("must be 'json' or 'human'", param_hint="--output")
    command = f"invoke {operation}"
    started = time.monotonic_ns()
    try:
        params = _command_input(param or [], input_json, input_file, after_cursor)
        selected_product = select_product(product or settings.product)
        result = anyio.run(
            _execute,
            operation,
            profile or settings.profile,
            profiles_dir or settings.profiles_dir,
            _selected_registries(settings, registry, product),
            params,
            allow_mutation,
            selected_product,
            settings.credential_helper,
            run_dir,
        )
    except RuntimeFailure as error:
        result = _failure_result(command, started, error)
    except Exception as error:
        result = _failure_result(command, started, _internal_failure(error))
    render(result, selected_output)
    if result.exit_code:
        raise typer.Exit(result.exit_code)


@registry_app.command("list", help="List operations and their agent admission, safety and input summary.")
def list_registry(
    registry: Annotated[
        list[Path] | None,
        typer.Option("--registry", help="Registry YAML file or directory; repeatable."),
    ] = None,
    product: Annotated[str | None, typer.Option("--product", help="Installed product package name.")] = None,
    mutating: Annotated[
        bool | None,
        typer.Option("--mutating/--read-only", help="Filter by mutation safety marker."),
    ] = None,
    admission: Annotated[
        list[str] | None,
        typer.Option("--admission", help="Filter by admission; repeatable."),
    ] = None,
    kind: Annotated[
        list[str] | None,
        typer.Option("--kind", help="Filter by operation handler kind; repeatable."),
    ] = None,
    search: Annotated[
        str | None,
        typer.Option("--search", help="Case-insensitive substring in operation id or purpose."),
    ] = None,
    service: Annotated[
        list[str] | None,
        typer.Option("--service", help="Filter HTTP operations by config.service; repeatable."),
    ] = None,
    output: Annotated[str | None, typer.Option("--output", help="json or human.")] = None,
) -> None:
    settings = Settings()
    selected_output = output or settings.output
    started = time.monotonic_ns()
    started_at = datetime.now(UTC)
    try:
        admissions = set(admission or [])
        allowed_admissions = {"prototype", "diagnostic", "agent-read", "agent-mutate", "expert-only"}
        invalid_admissions = sorted(admissions - allowed_admissions)
        if invalid_admissions:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "INVALID_ADMISSION_FILTER",
                f"unknown admission values: {', '.join(invalid_admissions)}",
                source="cli",
            )
        loaded = OperationRegistry.load(_selected_registries(settings, registry, product))
        finished_at = datetime.now(UTC)
        result = CommandResult.success(
            command="registry list",
            run_id=f"run_{uuid.uuid4().hex}",
            environment="local",
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
            result={
                "operations": loaded.summaries(
                    mutating=mutating,
                    admissions=admissions,
                    kinds={value.strip().lower() for value in kind or [] if value.strip()},
                    search=search,
                    services={value.strip() for value in service or [] if value.strip()},
                )
            },
        )
    except RuntimeFailure as error:
        result = _failure_result("registry list", started, error)
    except Exception as error:
        result = _failure_result("registry list", started, _internal_failure(error))
    render(result, selected_output)
    if result.exit_code:
        raise typer.Exit(result.exit_code)


@registry_app.command(help="Show the exact operation contract and a ready-to-fill invocation template.")
def describe(
    operation: Annotated[str, typer.Argument(help="Registry operation id.")],
    registry: Annotated[
        list[Path] | None,
        typer.Option("--registry", help="Registry YAML file or directory; repeatable."),
    ] = None,
    product: Annotated[str | None, typer.Option("--product", help="Installed product package name.")] = None,
    profile: Annotated[
        str | None,
        typer.Option("--profile", help="Optional profile used to resolve effective caller input."),
    ] = None,
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir", help="Override the profile directory.")] = None,
    output: Annotated[str | None, typer.Option("--output", help="json or human.")] = None,
) -> None:
    settings = Settings()
    selected_output = output or settings.output
    started = time.monotonic_ns()
    started_at = datetime.now(UTC)
    try:
        scope = _invocation_scope(settings, registry, product, profile, profiles_dir)
        loaded = OperationRegistry.load(scope.registry_paths)
        definition = loaded.get(operation)
        loaded_profile = None
        if profile is not None:
            loaded_profile = ProfileLoader(scope.profiles_dir).load(scope.profile_name)
        description = definition.model_dump(by_alias=True, mode="json")
        description["invocation"] = _invocation_hint(definition, loaded_profile, scope=scope)
        description["guides"] = _related_guides(scope.product, operation)
        result = CommandResult.success(
            command=f"registry describe {operation}",
            run_id=f"run_{uuid.uuid4().hex}",
            environment=loaded_profile.environment if loaded_profile is not None else "local",
            started_at=started_at,
            finished_at=datetime.now(UTC),
            duration_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
            result=description,
        )
    except RuntimeFailure as error:
        result = _failure_result(f"registry describe {operation}", started, error)
    except Exception as error:
        result = _failure_result(f"registry describe {operation}", started, _internal_failure(error))
    render(result, selected_output)
    if result.exit_code:
        raise typer.Exit(result.exit_code)


@config_app.command("validate", help="Compile the full registry against a profile without network I/O.")
def validate_config(
    profile: Annotated[str | None, typer.Option("--profile", help="Profile name under the profiles directory.")] = None,
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir", help="Override the profile directory.")] = None,
    registry: Annotated[
        list[Path] | None,
        typer.Option("--registry", help="Registry YAML file or directory; repeatable."),
    ] = None,
    product: Annotated[str | None, typer.Option("--product", help="Installed product package name.")] = None,
    output: Annotated[str | None, typer.Option("--output", help="json or human.")] = None,
) -> None:
    settings = Settings()
    selected_output = output or settings.output
    selected_profile = profile or settings.profile
    started = time.monotonic_ns()
    started_at = datetime.now(UTC)
    try:
        selected_product = select_product(product or settings.product)
        loaded = anyio.run(
            _validate_configuration,
            selected_profile,
            profiles_dir or settings.profiles_dir,
            _selected_registries(settings, registry, product),
            selected_product,
            settings.credential_helper,
        )
        result = CommandResult.success(
            command="config validate",
            run_id=f"run_{uuid.uuid4().hex}",
            environment=loaded.environment,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            duration_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
            result={"profile": selected_profile, "valid": True},
        )
    except RuntimeFailure as error:
        result = _failure_result("config validate", started, error)
    except Exception as error:
        result = _failure_result("config validate", started, _internal_failure(error))
    render(result, selected_output)
    if result.exit_code:
        raise typer.Exit(result.exit_code)


def _local_success(command: str, started: int, payload: Any, environment: str = "local") -> CommandResult:
    elapsed = max(0, (time.monotonic_ns() - started) // 1_000_000)
    finished = datetime.now(UTC)
    return CommandResult.success(
        command=command,
        run_id=f"run_{uuid.uuid4().hex}",
        environment=environment,
        started_at=finished - timedelta(milliseconds=elapsed),
        finished_at=finished,
        duration_ms=elapsed,
        result=payload,
    )


def _finish_command(result: CommandResult, output: str) -> None:
    render(result, output)
    if result.exit_code:
        raise typer.Exit(result.exit_code)


async def _plan_operation(
    operation: str,
    scope: InvocationScope,
    params: dict[str, Any],
    credential_helper: Path | None,
    validate_input: bool = True,
) -> dict[str, Any]:
    assembled = await _assemble_runtime(
        scope.profile_name,
        scope.profiles_dir,
        scope.registry_paths,
        scope.product,
        credential_helper,
        operation_ids=(operation,),
    )
    runtime = assembled.runtime
    try:
        definition = assembled.registry.get(operation)
        resolved = runtime.validate_input(operation, params) if validate_input else None
        return runtime.redact(
            {
                "operation": operation,
                "configuration_valid": True,
                "network_checked": False,
                "scope": assembled.evidence_scope.model_dump(mode="json"),
                "input": resolved,
                "config": definition.config,
                "contractRef": definition.contract_ref.model_dump(mode="json", by_alias=True)
                if definition.contract_ref is not None
                else None,
                "safety": {
                    "mutating": definition.mutating,
                    "requiredFlag": "--allow-mutation" if definition.mutating else None,
                    "requiredEnvironment": "qa" if definition.mutating else None,
                },
            }
        )
    finally:
        await runtime.aclose()


@app.command(help="Validate exact input and show its scoped plan without credentials or network I/O.")
def plan(
    operation: Annotated[str, typer.Argument(help="Registry operation id.")],
    param: Annotated[list[str] | None, typer.Option("--param", "-p")] = None,
    input_json: Annotated[str | None, typer.Option("--input-json")] = None,
    input_file: Annotated[Path | None, typer.Option("--input-file", help="JSON file, or - for stdin.")] = None,
    after_cursor: Annotated[Path | None, typer.Option("--after-cursor")] = None,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
    registry: Annotated[list[Path] | None, typer.Option("--registry")] = None,
    product: Annotated[str | None, typer.Option("--product")] = None,
    output: Annotated[str | None, typer.Option("--output")] = None,
) -> None:
    settings = Settings()
    started = time.monotonic_ns()
    command = f"plan {operation}"
    try:
        scope = _invocation_scope(settings, registry, product, profile, profiles_dir)
        params = _command_input(param or [], input_json, input_file, after_cursor)
        payload = anyio.run(_plan_operation, operation, scope, params, settings.credential_helper)
        result = _local_success(command, started, payload)
    except RuntimeFailure as error:
        result = _failure_result(command, started, error)
    except Exception as error:
        result = _failure_result(command, started, _internal_failure(error))
    _finish_command(result, output or settings.output)


@app.command(help="Check selected configuration; --network explicitly probes one registered read-only operation.")
def doctor(
    operation: Annotated[str, typer.Argument(help="Operation whose dependencies should be checked.")],
    network: Annotated[
        bool, typer.Option("--network", help="Execute this read-only operation, including auth.")
    ] = False,
    param: Annotated[list[str] | None, typer.Option("--param", "-p")] = None,
    input_json: Annotated[str | None, typer.Option("--input-json")] = None,
    input_file: Annotated[Path | None, typer.Option("--input-file")] = None,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
    registry: Annotated[list[Path] | None, typer.Option("--registry")] = None,
    product: Annotated[str | None, typer.Option("--product")] = None,
    output: Annotated[str | None, typer.Option("--output")] = None,
) -> None:
    settings = Settings()
    started = time.monotonic_ns()
    command = f"doctor {operation}"
    try:
        scope = _invocation_scope(settings, registry, product, profile, profiles_dir)
        params = _command_input(param or [], input_json, input_file)
        definition = OperationRegistry.load(scope.registry_paths).get(operation)
        if network:
            if definition.mutating:
                raise RuntimeFailure(
                    ExitCode.BLOCKED,
                    "DOCTOR_MUTATION_BLOCKED",
                    "network doctor can only execute a registered read-only operation",
                    source="cli",
                )
            result = anyio.run(
                _execute,
                operation,
                scope.profile_name,
                scope.profiles_dir,
                scope.registry_paths,
                params,
                False,
                scope.product,
                settings.credential_helper,
            )
            result.command = command
        else:
            payload = anyio.run(_plan_operation, operation, scope, params, settings.credential_helper, False)
            result = _local_success(command, started, payload)
    except RuntimeFailure as error:
        result = _failure_result(command, started, error)
    except Exception as error:
        result = _failure_result(command, started, _internal_failure(error))
    _finish_command(result, output or settings.output)


@app.command(help="Capture partition offsets for an operation supporting observation checkpoints (network read).")
def checkpoint(
    operation: Annotated[str, typer.Argument(help="Registered read-only observation operation.")],
    run_dir: Annotated[Path | None, typer.Option("--run-dir", help="Save the checkpoint as scenario evidence.")] = None,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
    registry: Annotated[list[Path] | None, typer.Option("--registry")] = None,
    product: Annotated[str | None, typer.Option("--product")] = None,
    output: Annotated[str | None, typer.Option("--output")] = None,
) -> None:
    settings = Settings()
    started = time.monotonic_ns()
    command = f"checkpoint {operation}"
    try:
        scope = _invocation_scope(settings, registry, product, profile, profiles_dir)
        result = anyio.run(
            _execute,
            operation,
            scope.profile_name,
            scope.profiles_dir,
            scope.registry_paths,
            {},
            False,
            scope.product,
            settings.credential_helper,
            run_dir,
            True,
        )
    except RuntimeFailure as error:
        result = _failure_result(command, started, error)
    except Exception as error:
        result = _failure_result(command, started, _internal_failure(error))
    _finish_command(result, output or settings.output)


@run_app.command("init", help="Create a local evidence journal pinned to product, profile and registry content.")
def initialize_run(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
    registry: Annotated[list[Path] | None, typer.Option("--registry")] = None,
    product: Annotated[str | None, typer.Option("--product")] = None,
    output: Annotated[str | None, typer.Option("--output")] = None,
) -> None:
    settings = Settings()
    started = time.monotonic_ns()
    try:
        scope = _invocation_scope(settings, registry, product, profile, profiles_dir)
        manifest = RunStore(run_dir).initialize(_evidence_scope(scope))
        result = _local_success("run init", started, manifest.model_dump(mode="json"))
    except RuntimeFailure as error:
        result = _failure_result("run init", started, error)
    except Exception as error:
        result = _failure_result("run init", started, _internal_failure(error))
    _finish_command(result, output or settings.output)


@run_app.command("show", help="Restore saved inputs, outcomes and pending attempts without credentials or network.")
def show_run(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    full: Annotated[bool, typer.Option("--full", help="Include complete saved command results.")] = False,
    output: Annotated[str | None, typer.Option("--output")] = None,
) -> None:
    started = time.monotonic_ns()
    try:
        store = RunStore(run_dir)
        manifest = store.manifest()
        records = store.records()
        steps = [
            record.model_dump(mode="json")
            if full
            else {
                "attempt_id": record.attempt_id,
                "operation_id": record.operation_id,
                "input": record.input,
                "started_at": record.started_at.isoformat(),
                "status": record.result.status if record.result is not None else "pending",
                "run_id": record.result.run_id if record.result is not None else None,
                "error": record.result.error.model_dump(exclude_none=True)
                if record.result is not None and record.result.error is not None
                else None,
                "evidence": str(store.directory / f"{record.attempt_id}.json"),
            }
            for record in records
        ]
        result = _local_success(
            "run show",
            started,
            {
                "manifest": manifest.model_dump(mode="json"),
                "attempts": steps,
                "pending_count": sum(record.result is None for record in records),
                "pending_rule": "A pending attempt has unknown outcome; observe state before considering any retry.",
            },
        )
    except RuntimeFailure as error:
        result = _failure_result("run show", started, error)
    except Exception as error:
        result = _failure_result("run show", started, _internal_failure(error))
    _finish_command(result, output or Settings().output)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
