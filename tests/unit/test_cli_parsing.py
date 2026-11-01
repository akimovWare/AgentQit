from __future__ import annotations

import json

import pytest
from rich.text import Text
from typer.testing import CliRunner

from qa_cli.app import app


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["registry"],
        ["invoke", "--output", "json"],
        ["invoke", "--output"],
        ["registry", "describe", "--output", "json"],
        ["registry", "list", "--unknown", "--output", "json"],
        ["config", "validate", "--unknown", "--output=json"],
        ["guide", "--unknown"],
        ["context", "--unknown"],
        ["unknown-command", "canary-private-argument"],
    ],
)
def test_json_usage_errors_have_one_safe_result(arguments: list[str]) -> None:
    result = CliRunner().invoke(app, arguments, env={"QA_CLI_OUTPUT": "json"})

    assert result.exit_code == 2, result.output
    assert len(result.stdout.splitlines()) == 1
    assert not result.stderr
    assert "canary-private-argument" not in result.output
    envelope = json.loads(result.stdout)
    assert envelope["schema_version"] == "1.0"
    assert envelope["exit_code"] == 2
    assert envelope["error"]["source"] == "cli"


@pytest.mark.parametrize(
    "command",
    [
        ["guide"],
        ["context"],
        ["invoke", "anything"],
        ["registry", "list"],
        ["registry", "describe", "anything"],
        ["config", "validate"],
    ],
)
@pytest.mark.parametrize("via_environment", [False, True])
def test_invalid_output_is_rejected_consistently(command: list[str], via_environment: bool) -> None:
    arguments = command if via_environment else [*command, "--output", "jsno"]
    environment = {"QA_CLI_OUTPUT": "jsno"} if via_environment else {}
    result = CliRunner().invoke(app, arguments, env=environment)

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "INVALID_OUTPUT"
    assert not result.stderr


def test_explicit_human_output_overrides_json_environment() -> None:
    result = CliRunner().invoke(app, ["guide", "--output", "human"], env={"QA_CLI_OUTPUT": "json"})

    assert result.exit_code == 0
    assert "[success] guide" in result.stdout


@pytest.mark.parametrize("colored", [False, True], ids=["plain", "colored"])
def test_explicit_help_stays_human_even_with_invalid_output_environment(colored: bool) -> None:
    result = CliRunner().invoke(
        app,
        ["invoke", "--help"],
        env={
            "QA_CLI_OUTPUT": "invalid",
            "FORCE_COLOR": "1" if colored else None,
            "NO_COLOR": None if colored else "1",
            "TERM": "xterm-256color" if colored else "dumb",
        },
    )

    assert result.exit_code == 0
    help_text = Text.from_ansi(result.stdout).plain
    assert "Usage:" in help_text
    assert "--input-json" in help_text
