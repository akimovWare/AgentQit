import json

from typer.testing import CliRunner

from qa_cli.app import app


def test_new_workspace_is_useful_offline_without_a_product_package(tmp_path):
    target = tmp_path / "sample"
    runner = CliRunner()
    result = runner.invoke(app, ["workspace", "init", "--directory", str(target), "--name", "orders"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["name"] == "orders"
    for command in [["list"], ["describe", "items.list"], ["doctor", "items.list"], ["plan", "items.list"]]:
        result = runner.invoke(app, ["workspace", *command, "--workspace", str(target)])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "success"
        assert len(result.stdout.splitlines()) == 1
    result = runner.invoke(app, ["workspace", "init", "--directory", str(target)])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "WORKSPACE_EXISTS"


def test_workspace_invalid_input_is_one_json_document(tmp_path):
    runner = CliRunner()
    runner.invoke(app, ["workspace", "init", "--directory", str(tmp_path / "sample")])
    result = runner.invoke(
        app,
        [
            "workspace",
            "plan",
            "items.list",
            "--workspace",
            str(tmp_path / "sample"),
            "--param",
            "unknown=value",
        ],
    )
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "failure"
