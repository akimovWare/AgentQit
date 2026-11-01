from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from qa_cli.app import app
from qa_runtime.bootstrap import build_runtime
from qa_runtime.evidence import RunStore
from qa_runtime.ports.kafka import KafkaConsumeRequest, KafkaCursor, KafkaRecord, topic_fingerprint
from qa_runtime.ports.product import ProductComponents, ProductPlugin

OPERATION = "wait-for-example-report"
EVENT_ID = "33333333-3333-4333-8333-333333333333"
TOPIC_SECRET = "evidence-canary-token"
TOPIC = f"qa.{TOPIC_SECRET}.reports"


class FakeKafka:
    def __init__(self) -> None:
        self.captures: list[tuple[str, str, float]] = []
        self.requests: list[KafkaConsumeRequest] = []
        self.closed = 0

    async def capture(self, cluster, topic, *, client_id, timeout_seconds):
        assert client_id.startswith("qa-cli-run_")
        self.captures.append((cluster, topic, timeout_seconds))
        return KafkaCursor(
            cluster=cluster,
            topic_fingerprint=topic_fingerprint(topic),
            source_fingerprint="a" * 64,
            offsets={0: 41, 1: 9},
        )

    async def consume(self, request):
        self.requests.append(request)
        return [
            KafkaRecord(
                topic=TOPIC,
                partition=0,
                offset=41,
                timestamp_ms=int(datetime.now(UTC).timestamp() * 1000),
                key=None,
                value=json.dumps({"eventId": EVENT_ID, "status": "DONE"}).encode(),
            )
        ]

    async def publish(self, *args, **kwargs):
        raise AssertionError("the checkpoint workflow must not publish")

    async def aclose(self):
        self.closed += 1


@pytest.fixture
def checkpoint_cli(tmp_path: Path, monkeypatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "qa.yaml").write_text(
        "environment: qa\nkafka:\n  fake-cluster:\n"
        "    bootstrapServers: [never-connect.example:9092]\n"
        f"    topics:\n      example-report: {TOPIC}\n",
        encoding="utf-8",
    )
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        f"""
apiVersion: qa.runtime/v1
kind: OperationRegistry
operations:
  - id: {OPERATION}
    kind: kafka-consume
    admission: diagnostic
    parameters:
      event-id:
        type: string
        format: uuid
        required: true
      expected-status:
        type: string
        enum: [NEW, DONE]
        required: true
      lookback-seconds:
        type: number
        minimum: 0
        maximum: 60
      after-cursor:
        type: object
    config:
      cluster: fake-cluster
      topicRef: example-report
      valueEncoding: json
      defaultTimeoutSeconds: 0.05
      defaultMaxRecords: 1
      defaultLookbackSeconds: 300
      lookbackParameter: lookback-seconds
      cursorParameter: after-cursor
      requireLookbackOrCursor: true
      jsonFieldFilters:
        - field: eventId
          parameter: event-id
        - field: status
          parameter: expected-status
      stopAfterMatches: 1
""",
        encoding="utf-8",
    )
    kafka = FakeKafka()

    class NoCredentials:
        async def get(self, reference):
            raise AssertionError("the fake workflow must not read credentials")

    def components(profile, credentials, redactor):
        redactor.register(TOPIC_SECRET)
        return ProductComponents()

    plugin = ProductPlugin(product_id="fake-product", registry_path=registry, runtime_factory=components)

    def selected_plugin(product):
        assert product == "fake-product"
        return plugin

    monkeypatch.setattr("qa_cli.app.product_plugin_for", selected_plugin)
    monkeypatch.setattr("qa_cli.app._credential_source", lambda _, **kwargs: NoCredentials())

    async def build_with_fake_kafka(*args, **kwargs):
        return await build_runtime(*args, kafka_port=kafka, **kwargs)

    monkeypatch.setattr("qa_cli.app.build_runtime", build_with_fake_kafka)
    scope = [
        "--product",
        "fake-product",
        "--profile",
        "qa",
        "--profiles-dir",
        str(profiles),
        "--registry",
        str(registry),
        "--output",
        "json",
    ]
    return kafka, scope, tmp_path / "run"


def _invoke(arguments: list[str], *, expected_exit: int = 0) -> dict:
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == expected_exit, result.output
    assert len(result.stdout.splitlines()) == 1
    assert not result.stderr
    assert TOPIC_SECRET not in result.stdout
    return json.loads(result.stdout)


def test_checkpoint_evidence_roundtrip_through_plan_and_invoke(checkpoint_cli, tmp_path: Path) -> None:
    kafka, scope, run_directory = checkpoint_cli
    checkpoint = _invoke(["checkpoint", OPERATION, *scope, "--run-dir", str(run_directory)])

    assert kafka.captures == [("fake-cluster", TOPIC, 0.05)]
    assert kafka.requests == []
    checkpoint_file = Path(checkpoint["evidence"][0])
    checkpoint_record = RunStore(run_directory).records()[0]
    assert checkpoint_record.operation_id == OPERATION
    assert checkpoint_record.input == {}  # The required event ID/status belong only to execution.
    assert checkpoint_record.result.command == f"checkpoint {OPERATION}"
    assert "topic" not in checkpoint["result"]["cursor"]
    assert checkpoint["result"]["cursor"]["topic_fingerprint"] == topic_fingerprint(TOPIC)
    assert TOPIC_SECRET not in checkpoint_file.read_text()

    inputs = tmp_path / "wait-input.json"
    inputs.write_text(json.dumps({"event-id": EVENT_ID, "expected-status": "DONE"}), encoding="utf-8")
    selected_input = ["--input-file", str(inputs), "--after-cursor", str(checkpoint_file)]
    planned = _invoke(["plan", OPERATION, *scope, *selected_input])

    assert planned["result"]["configuration_valid"] is True
    assert planned["result"]["network_checked"] is False
    assert planned["result"]["input"]["after-cursor"] == checkpoint["result"]["cursor"]
    assert kafka.requests == []
    assert len(kafka.captures) == 1
    assert len(RunStore(run_directory).records()) == 1

    observed = _invoke(["invoke", OPERATION, *scope, *selected_input, "--run-dir", str(run_directory)])

    assert len(kafka.requests) == 1
    request = kafka.requests[0]
    assert request.after_cursor == KafkaCursor.model_validate(checkpoint["result"]["cursor"])
    assert request.topic == TOPIC
    assert request.timeout_seconds == 0.05
    assert request.stop_after_matches == 1
    assert {".".join(match.path): match.expected for match in request.json_fields_equal} == {
        "eventId": EVENT_ID,
        "status": "DONE",
    }
    observation = observed["result"]["observation"]
    assert observation["mode"] == "after_cursor"
    assert observation["lookback_seconds"] is None
    assert observation["topic_fingerprint"] == topic_fingerprint(TOPIC)
    assert observation["after_cursor"] == checkpoint["result"]["cursor"]
    records = RunStore(run_directory).records()
    assert len(records) == 2
    assert records[0] == checkpoint_record
    assert records[1].input["after-cursor"] == checkpoint["result"]["cursor"]
    assert records[1].result.result["records"][0]["value"] == {"eventId": EVENT_ID, "status": "DONE"}
    assert records[1].result.evidence == observed["evidence"]
    assert all(record.result is not None for record in records)
    assert kafka.closed == 3


@pytest.mark.parametrize("command", ["plan", "invoke"])
@pytest.mark.parametrize("malformation", ["missing-fingerprint", "negative-offset", "different-topic"])
def test_malformed_cursor_rejects_before_consume_even_with_valid_lookback(
    checkpoint_cli, tmp_path: Path, command: str, malformation: str
) -> None:
    kafka, scope, run_directory = checkpoint_cli
    captured = _invoke(["checkpoint", OPERATION, *scope, "--run-dir", str(run_directory)])
    cursor = captured["result"]["cursor"]
    if malformation == "missing-fingerprint":
        del cursor["topic_fingerprint"]
    elif malformation == "negative-offset":
        cursor["offsets"]["0"] = -1
    else:
        cursor["topic_fingerprint"] = topic_fingerprint("another-topic")
    malformed = tmp_path / "malformed-cursor.json"
    malformed.write_text(json.dumps(cursor), encoding="utf-8")
    arguments = [
        command,
        OPERATION,
        *scope,
        "--input-json",
        json.dumps({"event-id": EVENT_ID, "expected-status": "DONE", "lookback-seconds": 1}),
        "--after-cursor",
        str(malformed),
    ]
    if command == "invoke":
        arguments.extend(["--run-dir", str(run_directory)])
    result = _invoke(arguments, expected_exit=2)

    assert result["error"]["code"] == "INVALID_PARAM"
    assert kafka.requests == []
    assert len(RunStore(run_directory).records()) == 1


@pytest.mark.parametrize("command", ["plan", "invoke"])
def test_missing_boundary_never_uses_configured_default_lookback(checkpoint_cli, command: str) -> None:
    kafka, scope, _ = checkpoint_cli
    result = _invoke(
        [command, OPERATION, *scope, "--input-json", json.dumps({"event-id": EVENT_ID, "expected-status": "DONE"})],
        expected_exit=2,
    )

    assert result["error"]["code"] == "MISSING_PARAM"
    assert kafka.captures == []
    assert kafka.requests == []


def test_explicit_lookback_remains_an_available_alternative(checkpoint_cli) -> None:
    kafka, scope, _ = checkpoint_cli
    result = _invoke(
        [
            "invoke",
            OPERATION,
            *scope,
            "--input-json",
            json.dumps({"event-id": EVENT_ID, "expected-status": "DONE", "lookback-seconds": 1}),
        ]
    )

    assert len(kafka.requests) == 1
    assert kafka.requests[0].after_cursor is None
    assert kafka.requests[0].lookback_seconds == 1
    assert result["result"]["observation"]["mode"] == "lookback"
    assert result["result"]["observation"]["lookback_seconds"] == 1
