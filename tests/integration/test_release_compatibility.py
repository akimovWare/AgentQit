"""Keep a frozen consumer workspace usable across patch releases without rewriting it."""

from __future__ import annotations

import hashlib
import json
import shutil
from importlib.metadata import version
from pathlib import Path

import anyio
import pytest
from typer.testing import CliRunner

from qa_cli.app import app
from qa_mcp.server import create_server
from qa_runtime.application import WorkspaceApplication
from qa_runtime.contracts import CommandResult
from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import Profile, RegistryDocument
from qa_runtime.workspace import Workspace, WorkspaceManifest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "name,model",
    [
        ("workspace-v1", WorkspaceManifest),
        ("profile-v1", Profile),
        ("registry-v1", RegistryDocument),
        ("result-v1", CommandResult),
    ],
)
def test_public_schema_matches_reviewed_baseline(name, model):
    assert model.model_json_schema(by_alias=True) == json.loads((ROOT / "docs/schemas" / f"{name}.json").read_text())


def test_v1_workspace_plans_without_credentials_or_rewriting_files(tmp_path, monkeypatch):
    directory = tmp_path / "consumer"
    shutil.copytree(ROOT / "tests/fixtures/compatibility/v1", directory)
    before = {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }
    monkeypatch.delenv("AGENTQIT_COMPATIBILITY_TOKEN", raising=False)

    async def exercise():
        application = WorkspaceApplication(Workspace.load(directory))
        try:
            result = await application.plan("items.get", {"itemId": "sample-123"})
            assert result.exit_code == 0
            assert result.result["network_checked"] is False
            # Input and mutation admission precede credential access and network I/O.
            missing = await application.plan("items.get", {})
            assert missing.exit_code == 2
            denied = await application.invoke("items.create", {})
            assert denied.exit_code == 7
        finally:
            await application.aclose()

    anyio.run(exercise)
    assert before == {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*")
        if path.is_file() and ".agentqit" not in path.parts
    }


@pytest.mark.parametrize("change", ["unsupported-version", "unknown-field"])
def test_unknown_workspace_schema_is_rejected_without_migration(tmp_path, change):
    manifest = {"apiVersion": "agentqit/v2" if change == "unsupported-version" else "agentqit/v1", "name": "sample"}
    if change == "unknown-field":
        manifest["misspelledOption"] = True
    path = tmp_path / "agentqit.yaml"
    text = json.dumps(manifest)
    path.write_text(text)
    with pytest.raises(RuntimeFailure, match="agentqit/v1"):
        Workspace.load(tmp_path)
    assert path.read_text() == text


def test_version_commands_and_mcp_agree_with_installed_metadata():
    runner = CliRunner()
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0 and result.stdout.strip() == f"AgentQit {version('agentqit')}"
    server = create_server(ROOT / "tests/fixtures/compatibility/v1")
    assert server.version == version("agentqit")


def test_workspace_schema_command_needs_no_workspace():
    result = CliRunner().invoke(app, ["workspace", "schema"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["result"]["schema"] == WorkspaceManifest.model_json_schema(by_alias=True)
