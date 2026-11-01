from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import anyio
import typer
import yaml

from qa_cli.parsing import StructuredTyperGroup
from qa_cli.renderers import render
from qa_runtime.application import WorkspaceApplication, failure_result, local_result
from qa_runtime.contracts import CommandResult, ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.workspace import Workspace, WorkspaceManifest

workspace_app = typer.Typer(help="Use local catalogs without installing a product package.", cls=StructuredTyperGroup)
WorkspaceOption = Annotated[Path, typer.Option("--workspace", help="Workspace directory or agentqit.yaml file.")]
OutputOption = Annotated[str, typer.Option("--output")]


def _finish(result: CommandResult, output: str) -> None:
    render(result, output)
    if result.exit_code:
        raise typer.Exit(result.exit_code)


@workspace_app.command("schema")
def schema(output: OutputOption = "json") -> None:
    """Export the supported workspace JSON Schema without files, credentials, or I/O."""
    _finish(local_result("workspace schema", {"schema": WorkspaceManifest.model_json_schema(by_alias=True)}), output)


@workspace_app.command("init")
def initialize(
    directory: Annotated[Path, typer.Option("--directory")],
    name: Annotated[str, typer.Option("--name")] = "sample",
    output: OutputOption = "json",
) -> None:
    """Create a nonsecret workspace and sample read operation in a new directory."""
    started_at, started = datetime.now(UTC), time.monotonic()
    try:
        manifest = WorkspaceManifest(apiVersion="agentqit/v1", name=name)
        directory = directory.expanduser().absolute()
        if directory.exists():
            raise RuntimeFailure(ExitCode.INVALID_INPUT, "WORKSPACE_EXISTS", "Choose a new workspace directory")
        directory.mkdir(parents=True)
        (directory / "operations").mkdir()
        (directory / "agentqit.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": manifest.api_version,
                    "name": name,
                    "profile": "profile.yaml",
                    "registry": ["operations"],
                }
            )
        )
        (directory / "profile.yaml").write_text(
            yaml.safe_dump(
                {
                    "environment": "unconfigured",
                    "services": {"api": {"baseUrl": "https://api.example.invalid"}},
                }
            )
        )
        (directory / "operations" / "items.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "qa.runtime/v1",
                    "kind": "OperationRegistry",
                    "operations": [
                        {
                            "id": "items.list",
                            "purpose": "Read items",
                            "kind": "http",
                            "mutating": False,
                            "admission": "prototype",
                            "config": {"service": "api", "method": "GET", "path": "/items"},
                        }
                    ],
                }
            )
        )
        (directory / ".gitignore").write_text(".agentqit/\n.env\nplaywright/.auth/\n")
        result = local_result("workspace init", {"directory": str(directory), "name": name})
    except Exception as error:
        result = failure_result("workspace init", error, started_at, started)
    _finish(result, output)


@workspace_app.command("list")
def list_operations(
    workspace: WorkspaceOption,
    query: str = "",
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    offset: Annotated[int, typer.Option(min=0)] = 0,
    output: OutputOption = "json",
) -> None:
    """Search bounded operation metadata without network or credentials."""
    started_at, started = datetime.now(UTC), time.monotonic()
    try:
        app = WorkspaceApplication(Workspace.load(workspace))
        result = local_result("registry list", app.list_operations(query, limit=limit, offset=offset))
    except Exception as error:
        result = failure_result("registry list", error, started_at, started)
    _finish(result, output)


@workspace_app.command("describe")
def describe(operation: str, workspace: WorkspaceOption, output: OutputOption = "json") -> None:
    """Describe an operation and its typed inputs."""
    started_at, started = datetime.now(UTC), time.monotonic()
    try:
        app = WorkspaceApplication(Workspace.load(workspace))
        result = local_result("registry describe", app.describe_operation(operation))
    except Exception as error:
        result = failure_result("registry describe", error, started_at, started)
    _finish(result, output)


def _run(
    mode: str,
    operation: str,
    workspace: Path,
    params: list[str] | None,
    input_file: Path | None,
    allow_mutation: bool,
    output: str,
) -> None:
    from qa_cli.app import _command_input

    started_at, started = datetime.now(UTC), time.monotonic()

    async def execute():
        app = WorkspaceApplication(Workspace.load(workspace), mutations_allowed=allow_mutation)
        try:
            if mode == "doctor":
                return await app.check(operation)
            values = _command_input(params or [], None, input_file)
            if mode == "plan":
                return await app.plan(operation, values)
            return await app.invoke(operation, values, allow_mutation=allow_mutation)
        finally:
            await app.aclose()

    try:
        result = anyio.run(execute)
    except Exception as error:
        result = failure_result(f"{mode} {operation}", error, started_at, started)
    _finish(result, output)


@workspace_app.command("plan")
def plan(
    operation: str,
    workspace: WorkspaceOption,
    param: Annotated[list[str] | None, typer.Option("--param")] = None,
    input_file: Annotated[Path | None, typer.Option("--input-file")] = None,
    output: OutputOption = "json",
) -> None:
    """Compile and validate one exact input without I/O."""
    _run("plan", operation, workspace, param, input_file, False, output)


@workspace_app.command("doctor")
def doctor(operation: str, workspace: WorkspaceOption, output: OutputOption = "json") -> None:
    """Compile one operation without requiring its caller inputs or performing I/O."""
    _run("doctor", operation, workspace, None, None, False, output)


@workspace_app.command("invoke")
def invoke(
    operation: str,
    workspace: WorkspaceOption,
    param: Annotated[list[str] | None, typer.Option("--param")] = None,
    input_file: Annotated[Path | None, typer.Option("--input-file")] = None,
    allow_mutation: Annotated[bool, typer.Option("--allow-mutation")] = False,
    output: OutputOption = "json",
) -> None:
    """Execute one operation within an authorized task and target."""
    _run("invoke", operation, workspace, param, input_file, allow_mutation, output)
