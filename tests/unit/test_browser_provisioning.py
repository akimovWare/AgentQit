import json
from types import SimpleNamespace

from typer.testing import CliRunner

from qa_cli import browser_commands
from qa_cli.app import app


def test_provisioning_uses_current_interpreter_and_keeps_installer_logs_off_stdout(monkeypatch):
    calls = []

    async def run(command, **options):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=b"untrusted installer text", stderr=b"private diagnostic")

    monkeypatch.setattr(browser_commands.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(browser_commands, "version", lambda name: "1.64.1")
    monkeypatch.setattr(browser_commands.anyio, "run_process", run)
    result = CliRunner().invoke(app, ["browser", "install", "--headless-only", "--dry-run"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["result"]["playwright_version"] == "1.64.1"
    assert calls[0][0] == browser_commands.sys.executable
    assert calls[0][1:4] == ["-m", "playwright", "install"]
    assert "--dry-run" in calls[0] and "--only-shell" in calls[0]
    assert "private diagnostic" not in result.stdout and len(result.stdout.splitlines()) == 1


def test_base_install_explains_missing_optional_browser_dependency(monkeypatch):
    monkeypatch.setattr(browser_commands.importlib.util, "find_spec", lambda name: None)
    result = CliRunner().invoke(app, ["browser", "install", "--dry-run"])
    assert result.exit_code == 4
    assert json.loads(result.stdout)["error"]["code"] == "BROWSER_DEPENDENCY_MISSING"
