from __future__ import annotations

import asyncio
import json
from pathlib import Path

import anyio
import httpx
import pytest
from typer.testing import CliRunner

from qa_cli.app import app
from qa_runtime.adapters.httpx import HttpxAdapter
from qa_runtime.contracts import ExitCode
from qa_runtime.engine import QaRuntime
from qa_runtime.errors import RuntimeFailure
from qa_runtime.evidence import RunStore
from qa_runtime.ports.product import ProductPlugin
from qa_runtime.registry import OperationRegistry


@pytest.fixture
def environment(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "qa.yaml").write_text("environment: qa\nservices:\n  fake:\n    baseUrl: https://fake.invalid\n")
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        json.dumps(
            {
                "apiVersion": "qa.runtime/v1",
                "kind": "OperationRegistry",
                "operations": [
                    {
                        "id": "create",
                        "kind": "http",
                        "mutating": True,
                        "parameters": {
                            "amount": {
                                "type": "number",
                                "minimum": 0,
                                "exclusiveMinimum": True,
                                "required": True,
                                "location": "body",
                            }
                        },
                        "config": {"service": "fake", "method": "POST", "path": "/create"},
                    },
                    {"id": "read", "kind": "http", "config": {"service": "fake", "method": "GET", "path": "/read"}},
                ],
            }
        )
    )
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "saved-id", "password": "canary-secret"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    plugin = ProductPlugin(product_id="fake", registry_path=registry)
    monkeypatch.setattr("qa_cli.app.product_plugin_for", lambda _: plugin)
    monkeypatch.setattr("qa_runtime.bootstrap.HttpxAdapter", lambda **_: HttpxAdapter(client))
    monkeypatch.setenv("QA_CLI_PRODUCT", "fake")
    monkeypatch.setenv("QA_CLI_PROFILES_DIR", str(profiles))
    monkeypatch.setenv("QA_CLI_PROFILE", "qa")
    return {"calls": calls, "registry": registry, "run": tmp_path / "run", "plugin": plugin}


def invoke(*args, **kwargs):
    result = CliRunner().invoke(app, list(args), **kwargs)
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["exit_code"] == result.exit_code
    return result.exit_code, payload


def test_plan_validates_mutation_without_io_or_authorization(environment):
    code, payload = invoke("plan", "create", "--input-json", '{"amount":"1.25"}')
    assert code == 0
    assert payload["result"]["input"] == {"amount": 1.25}
    assert payload["result"]["safety"]["requiredFlag"] == "--allow-mutation"
    assert payload["result"]["network_checked"] is False
    invalid, error = invoke("plan", "create", "--param", "amount=-1")
    assert invalid == 2 and error["error"]["code"] == "INVALID_PARAM"
    assert environment["calls"] == []


def test_input_file_and_stdin_share_validation_and_reject_duplicates(environment, tmp_path):
    file = tmp_path / "input.json"
    file.write_text('{"amount": 1}')
    assert invoke("plan", "create", "--input-file", str(file))[0] == 0
    assert invoke("plan", "create", "--input-file", "-", input='{"amount": 2}')[0] == 0
    assert invoke("plan", "create", "--input-file", str(file), "--param", "amount=2")[0] == 2
    assert invoke("plan", "create", "--input-file", str(file), "--input-json", "{}")[0] == 2
    file.write_text("[1]")
    assert invoke("plan", "create", "--input-file", str(file))[1]["error"]["code"] == "INPUT_FILE_INVALID"
    assert not environment["calls"]


def test_journal_saves_input_before_mutation_and_restores_sanitized_result(environment):
    directory = environment["run"]
    code, result = invoke("invoke", "create", "--param", "amount=2", "--run-dir", str(directory), "--allow-mutation")
    assert code == 0 and len(environment["calls"]) == 1
    assert result["result"]["body"]["password"] == "***"
    assert len(result["evidence"]) == 1
    stored = RunStore(directory).records()
    assert len(stored) == 1 and stored[0].input == {"amount": 2}
    assert stored[0].started_at <= stored[0].result.started_at
    assert stored[0].result.run_id == result["run_id"]
    assert "canary-secret" not in Path(result["evidence"][0]).read_text()
    restored = invoke("run", "show", "--run-dir", str(directory))[1]["result"]
    assert restored["pending_count"] == 0
    assert restored["attempts"][0]["input"] == {"amount": 2}
    assert len(environment["calls"]) == 1


def test_scope_mismatch_stops_next_mutation_before_io(environment):
    directory = str(environment["run"])
    assert invoke("run", "init", "--run-dir", directory)[0] == 0
    document = json.loads(environment["registry"].read_text())
    document["operations"][0]["config"]["path"] = "/another-action"
    environment["registry"].write_text(json.dumps(document))
    code, error = invoke("invoke", "create", "--param", "amount=2", "--run-dir", directory, "--allow-mutation")
    assert code == 2 and error["error"]["code"] == "EVIDENCE_SCOPE_MISMATCH"
    assert not environment["calls"]


def test_failed_evidence_finish_preserves_outcome_and_pending_attempt(environment, monkeypatch):
    def fail(*args):
        raise RuntimeFailure(ExitCode.ENVIRONMENT_UNAVAILABLE, "EVIDENCE_UNAVAILABLE", "write failed")

    monkeypatch.setattr(RunStore, "finish", fail)
    code, result = invoke(
        "invoke",
        "create",
        "--param",
        "amount=2",
        "--run-dir",
        str(environment["run"]),
        "--allow-mutation",
    )
    assert code == 0 and result["result"]["body"]["id"] == "saved-id"
    assert result["evidence"] == [] and result["warnings"]
    assert len(environment["calls"]) == 1
    assert RunStore(environment["run"]).records()[0].result is None


def test_doctor_defaults_to_offline_and_never_probes_mutation(environment):
    code, result = invoke("doctor", "create")
    assert code == 0 and result["result"]["network_checked"] is False
    assert invoke("doctor", "create", "--network", "--param", "amount=2")[0] == 7
    assert not environment["calls"]
    assert invoke("doctor", "read", "--network")[0] == 0
    assert len(environment["calls"]) == 1


def test_removed_check_command_does_not_execute_io(environment):
    code, result = invoke("check", "sample")
    assert code == 2
    assert result["error"]["source"] == "cli"
    assert not environment["calls"]


@pytest.mark.parametrize("document", [{"result": [1]}, {"schema_version": "qa.evidence/v1", "result": "bad"}])
def test_malformed_checkpoint_wrapper_is_input_error(environment, tmp_path, document):
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps(document))
    code, result = invoke("plan", "read", "--after-cursor", str(checkpoint))
    assert code == 2 and result["error"]["code"] == "INPUT_FILE_INVALID"
    assert not environment["calls"]


def test_journal_uses_exact_compiled_snapshot_when_registry_file_changes(environment, monkeypatch):
    original_load = OperationRegistry.load
    loads = []

    def load(paths):
        registry = original_load(paths)
        loads.append(registry)
        document = json.loads(environment["registry"].read_text())
        document["operations"][0]["config"]["path"] = "/changed-after-load"
        environment["registry"].write_text(json.dumps(document))
        return registry

    monkeypatch.setattr(OperationRegistry, "load", load)
    code, result = invoke(
        "invoke", "create", "--param", "amount=2", "--allow-mutation", "--run-dir", str(environment["run"])
    )
    assert code == 0 and result["status"] == "success"
    assert len(loads) == 1
    assert environment["calls"][0].url.path == "/create"
    from qa_runtime.evidence import content_digest

    expected = content_digest([op.model_dump(mode="json", by_alias=True) for op in loads[0].operations()])
    assert RunStore(environment["run"]).manifest().scope.registry_digest == expected


def test_completed_result_is_durable_before_cleanup_cancellation(environment, monkeypatch):
    async def cancelled_cleanup(self):
        records = RunStore(environment["run"]).records()
        assert len(records) == 1 and records[0].result.status.value == "success"
        raise asyncio.CancelledError()

    monkeypatch.setattr(QaRuntime, "aclose", cancelled_cleanup)
    with pytest.raises(asyncio.CancelledError):
        CliRunner().invoke(app, ["invoke", "read", "--run-dir", str(environment["run"])])
    saved = RunStore(environment["run"]).records()[0]
    assert saved.result is not None and saved.result.exit_code == 0


def test_hanging_cleanup_is_bounded_and_cannot_erase_known_outcome(environment, monkeypatch):
    async def hanging_cleanup(self):
        await anyio.sleep_forever()

    monkeypatch.setattr(QaRuntime, "aclose", hanging_cleanup)
    monkeypatch.setattr("qa_cli.app._CLEANUP_TIMEOUT_SECONDS", 0.01)
    code, result = invoke("invoke", "read", "--run-dir", str(environment["run"]))
    assert code == 0 and result["warnings"]
    assert RunStore(environment["run"]).records()[0].result.exit_code == 0
