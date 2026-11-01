import json
from datetime import UTC, datetime

import pytest

from qa_cli.renderers import render
from qa_runtime.contracts import CommandResult


def _result() -> CommandResult:
    now = datetime.now(UTC)
    return CommandResult.success(
        command="invoke create-order",
        run_id="run_example",
        environment="qa",
        started_at=now,
        finished_at=now,
        duration_ms=1,
        result={"id": "saved-id"},
    )


def test_human_output_displays_completion_warning_without_changing_success(capsys):
    result = _result()
    result.warnings.append("Evidence persistence failed.")

    render(result, "human")

    output = capsys.readouterr()
    assert "[success] invoke create-order" in output.out
    assert "Warning: Evidence persistence failed." in output.out
    assert "Saved evidence:" not in output.out
    assert output.err == ""
    assert result.exit_code == 0


def test_human_output_keeps_warning_and_evidence_markup_literal(capsys):
    result = _result()
    result.warnings = ["[red]cleanup warning[/red]", "The second observation did not complete."]
    result.evidence = ["/tmp/[draft]/attempt.json", "/tmp/next/attempt.json"]

    render(result, "human")

    output = capsys.readouterr().out
    assert "[red]cleanup warning[/red]" in output
    assert "The second observation did not complete." in output
    assert "Saved evidence:" in output
    assert all(path in output for path in result.evidence)


def test_json_output_keeps_one_document_with_warnings_and_evidence(capsys):
    result = _result()
    result.warnings = ["Evidence completion failed."]
    result.evidence = ["/tmp/attempt.json"]

    render(result, "json")

    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["warnings"] == result.warnings
    assert payload["evidence"] == result.evidence
    assert output.err == ""
    assert len(output.out.splitlines()) == 1


def test_renderer_rejects_unknown_output(capsys):
    with pytest.raises(ValueError, match="output must be"):
        render(_result(), "unknown")
    assert capsys.readouterr().out == ""
