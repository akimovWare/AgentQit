from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from threading import Event

import pytest

from qa_runtime.contracts import CommandResult
from qa_runtime.errors import RuntimeFailure
from qa_runtime.evidence import EvidenceRecord, EvidenceScope, RunStore, content_digest
from qa_runtime.security import SecretRedactor


def _scope(**updates) -> EvidenceScope:
    return EvidenceScope(
        **{
            "product": "example",
            "profile": "qa",
            "profile_digest": content_digest({"account": "QA-ACCOUNT"}),
            "registry_digest": content_digest({"operation": "example.read"}),
            **updates,
        }
    )


def _result(operation_id: str = "example.read", *, checkpoint: bool = False, payload=None) -> CommandResult:
    return CommandResult.success(
        command=f"{'checkpoint' if checkpoint else 'invoke'} {operation_id}",
        run_id="run_test",
        environment="qa",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        duration_ms=1,
        result={"confirmed": True} if payload is None else payload,
    )


def test_private_store_keeps_redacted_input_and_complete_result(tmp_path: Path) -> None:
    redactor = SecretRedactor()
    redactor.register("canary-token")

    class RuntimeRedactor:
        def redact(self, value):
            return redactor.redact(value)

    store = RunStore(tmp_path / "run", redactor=RuntimeRedactor())
    manifest = store.initialize(_scope())
    record = store.begin("example.read", {"account": "QA-ACCOUNT", "password": "private", "note": "canary-token"})
    result = _result(payload={"message": "canary-token", "token": "another-private-value"})
    path = store.finish(record, result)

    assert store.manifest() == manifest
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((store.directory / "manifest.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    saved = RunStore(store.directory).records()[0]
    assert saved.input == {"account": "QA-ACCOUNT", "password": "***", "note": "***"}
    assert saved.result.result == {"message": "***", "token": "***"}
    assert result.result == {"message": "canary-token", "token": "another-private-value"}
    assert not list(store.directory.glob("*.tmp"))


def test_checkpoint_results_are_supported(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    store.initialize(_scope())
    attempt = store.begin("example.read", {"external-id": "known-before-mutation"})
    store.finish(attempt, _result(checkpoint=True))
    assert store.records()[0].result.command == "checkpoint example.read"


def test_begin_returns_the_canonical_persisted_input(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    store.initialize(_scope())
    pending = store.begin("example.read", {"amount": Decimal("1.20"), "ids": ("one", "two")})

    assert pending == store.records()[0]
    store.finish(pending, _result())
    assert store.records()[0].input == {"amount": "1.20", "ids": ["one", "two"]}


def test_pending_attempt_survives_process_interruption(tmp_path: Path) -> None:
    script = """
import os
import sys
from pathlib import Path
from qa_runtime.evidence import EvidenceScope, RunStore
store = RunStore(Path(sys.argv[1]))
store.initialize(EvidenceScope.model_validate_json(sys.argv[2]))
store.begin("example.read", {"external-id": "retain-this-id"})
os._exit(23)
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), _scope().model_dump_json()],
        capture_output=True,
        check=False,
        # Start the crash deadline after the child signals readiness.
        timeout=60,
    )
    assert process.returncode == 23, process.stderr
    restored = RunStore(tmp_path)
    restored.initialize(_scope())
    assert len(restored.records()) == 1
    pending = restored.records()[0]
    assert pending.operation_id == "example.read"
    assert pending.input == {"external-id": "retain-this-id"}
    assert pending.result is None


@pytest.mark.parametrize("field", ["product", "profile", "profile_digest", "registry_digest"])
def test_scope_mismatch_is_rejected_without_an_attempt(tmp_path: Path, field: str) -> None:
    store = RunStore(tmp_path)
    initial = store.initialize(_scope())
    original = (tmp_path / "manifest.json").read_bytes()
    with pytest.raises(RuntimeFailure) as caught:
        RunStore(tmp_path).initialize(_scope(**{field: "different"}))
    assert caught.value.code == "EVIDENCE_SCOPE_MISMATCH"
    assert (tmp_path / "manifest.json").read_bytes() == original
    assert store.manifest() == initial
    assert store.records() == ()


def test_changed_manifest_scope_is_checked_again_before_begin(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    store.initialize(_scope())
    manifest_path = tmp_path / "manifest.json"
    payload = json.loads(manifest_path.read_text())
    payload["scope"]["profile"] = "different"
    manifest_path.write_text(json.dumps(payload))
    with pytest.raises(RuntimeFailure) as caught:
        store.begin("example.read", {})
    assert caught.value.code == "EVIDENCE_SCOPE_MISMATCH"
    assert not list(tmp_path.glob("attempt_*.json"))


def test_manifest_is_published_only_after_the_complete_temporary_write(tmp_path: Path) -> None:
    partial_written = Event()
    continue_write = Event()

    class PausedStore(RunStore):
        @staticmethod
        def _create(directory, name, data):
            encoded = json.dumps(data).encode()
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded[:10])
                stream.flush()
                partial_written.set()
                assert continue_write.wait(5)
                stream.write(encoded[10:])
                stream.flush()
                os.fsync(stream.fileno())

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(PausedStore(tmp_path).initialize, _scope())
        try:
            assert partial_written.wait(5)
            assert not (tmp_path / "manifest.json").exists()
            published = RunStore(tmp_path).initialize(_scope())
            assert RunStore(tmp_path).manifest() == published
        finally:
            continue_write.set()
        assert first.result(timeout=5) == published


def test_concurrent_independent_attempts_do_not_overwrite_each_other(tmp_path: Path) -> None:
    def invoke(index):
        store = RunStore(tmp_path)
        manifest = store.initialize(_scope())
        attempt = store.begin("example.read", {"index": index})
        store.finish(attempt, _result(payload={"index": index}))
        return manifest.case_id, attempt.attempt_id

    with ThreadPoolExecutor(max_workers=8) as pool:
        attempts = list(pool.map(invoke, range(24)))
    assert len({case_id for case_id, _ in attempts}) == 1
    assert len({attempt_id for _, attempt_id in attempts}) == 24
    records = RunStore(tmp_path).records()
    assert len(records) == 24
    assert {record.input["index"] for record in records} == set(range(24))
    assert all(record.result.result == record.input for record in records)
    assert list(records) == sorted(records, key=lambda record: (record.started_at, record.attempt_id))


def test_concurrent_finish_accepts_exactly_one_completion(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    store.initialize(_scope())
    pending = store.begin("example.read", {})

    def finish(index):
        try:
            RunStore(tmp_path).finish(pending, _result(payload={"winner": index}))
            return index
        except RuntimeFailure as error:
            assert error.code == "EVIDENCE_ATTEMPT_CONFLICT"
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(finish, range(8)))
    winners = [winner for winner in outcomes if winner is not None]
    assert len(winners) == 1
    assert store.records()[0].result.result == {"winner": winners[0]}


@pytest.mark.parametrize("failure", ["write", "replace", "invalid-result"])
def test_failed_finish_leaves_the_existing_pending_record(tmp_path: Path, monkeypatch, failure: str) -> None:
    store = RunStore(tmp_path)
    store.initialize(_scope())
    pending = store.begin("example.read", {})
    path = tmp_path / f"{pending.attempt_id}.json"
    original = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("private-canary-path")

    if failure == "write":
        monkeypatch.setattr(RunStore, "_create", fail)
    elif failure == "replace":
        monkeypatch.setattr(os, "replace", fail)
    result = _result()
    if failure == "invalid-result":
        result = result.model_copy(update={"duration_ms": "invalid-canary-value"})
    with pytest.raises(RuntimeFailure) as caught:
        store.finish(pending, result)
    assert caught.value.code in {"EVIDENCE_UNAVAILABLE", "EVIDENCE_INVALID"}
    assert "canary" not in str(caught.value)
    assert path.read_bytes() == original
    assert store.records() == (pending,)


@pytest.mark.parametrize("modified", ["input", "operation", "timestamp", "result-command", "completed"])
def test_finish_rejects_a_modified_or_completed_attempt(tmp_path: Path, modified: str) -> None:
    store = RunStore(tmp_path)
    store.initialize(_scope())
    pending = store.begin("example.read", {"amount": 1})
    supplied = pending.model_copy(deep=True)
    result = _result()
    if modified == "input":
        supplied.input["amount"] = 2
    elif modified == "operation":
        supplied.operation_id = "other.read"
    elif modified == "timestamp":
        supplied.started_at = datetime(2020, 1, 1, tzinfo=UTC)
    elif modified == "result-command":
        result = _result("other.read")
    else:
        store.finish(pending, result)
    with pytest.raises(RuntimeFailure) as caught:
        store.finish(supplied, result)
    assert caught.value.code == "EVIDENCE_ATTEMPT_CONFLICT"


@pytest.mark.parametrize("payload", ["{", "[]", "{}", '{"schema_version":"unknown"}', "private-canary"])
def test_corrupt_manifest_is_a_normalized_failure(tmp_path: Path, payload: str) -> None:
    (tmp_path / "manifest.json").write_text(payload)
    store = RunStore(tmp_path)
    for action in (store.manifest, store.records, lambda: store.initialize(_scope()), lambda: store.begin("x", {})):
        with pytest.raises(RuntimeFailure) as caught:
            action()
        assert caught.value.code == "EVIDENCE_INVALID"
        assert "private-canary" not in str(caught.value)


@pytest.mark.parametrize("field", ["schema_version", "case_id", "created_at"])
def test_stored_manifest_must_not_regenerate_missing_identity(tmp_path: Path, field: str) -> None:
    store = RunStore(tmp_path)
    store.initialize(_scope())
    path = tmp_path / "manifest.json"
    payload = json.loads(path.read_text())
    del payload[field]
    path.write_text(json.dumps(payload))

    with pytest.raises(RuntimeFailure) as caught:
        store.manifest()
    assert caught.value.code == "EVIDENCE_INVALID"


@pytest.mark.parametrize("field", ["schema_version", "started_at", "input", "result"])
def test_stored_attempt_must_not_regenerate_missing_fields(tmp_path: Path, field: str) -> None:
    store = RunStore(tmp_path)
    store.initialize(_scope())
    pending = store.begin("example.read", {})
    path = tmp_path / f"{pending.attempt_id}.json"
    payload = json.loads(path.read_text())
    del payload[field]
    path.write_text(json.dumps(payload))

    with pytest.raises(RuntimeFailure) as caught:
        store.records()
    assert caught.value.code == "EVIDENCE_INVALID"


@pytest.mark.parametrize("corruption", ["id-mismatch", "bad-name", "malformed", "missing", "result-command"])
def test_invalid_attempt_files_are_rejected(tmp_path: Path, corruption: str) -> None:
    store = RunStore(tmp_path)
    store.initialize(_scope())
    record = store.begin("example.read", {})
    path = tmp_path / f"{record.attempt_id}.json"
    if corruption == "missing":
        path.unlink()
        action = partial(store.finish, record, _result())
    else:
        if corruption == "bad-name":
            path.rename(tmp_path / "attempt_bad.json")
        elif corruption == "malformed":
            path.write_text("private-canary")
        else:
            payload = json.loads(path.read_text())
            if corruption == "id-mismatch":
                payload["attempt_id"] = "attempt_" + "0" * 32
            else:
                payload["result"] = _result("other.read").model_dump(mode="json")
            path.write_text(json.dumps(payload))
        action = store.records
    with pytest.raises(RuntimeFailure) as caught:
        action()
    assert caught.value.code == "EVIDENCE_INVALID"
    assert "private-canary" not in str(caught.value)


def test_missing_manifest_and_invalid_record_id_are_safe(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    for action in (store.manifest, store.records, lambda: store.begin("example.read", {})):
        with pytest.raises(RuntimeFailure) as caught:
            action()
        assert caught.value.code == "EVIDENCE_INVALID"
    invalid = EvidenceRecord(attempt_id="../../private-canary", operation_id="example.read")
    with pytest.raises(RuntimeFailure) as caught:
        store.finish(invalid, _result())
    assert caught.value.code == "EVIDENCE_INVALID"


@pytest.mark.parametrize("target", ["directory", "manifest", "attempt"])
def test_symlinks_are_rejected_without_changing_the_target(tmp_path: Path, target: str) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o755)
    linked = tmp_path / "linked"
    if target == "directory":
        linked.symlink_to(real, target_is_directory=True)
        before_mode = stat.S_IMODE(real.stat().st_mode)
        action = partial(RunStore(linked).initialize, _scope())
    else:
        store = RunStore(linked)
        store.initialize(_scope())
        path = linked / "manifest.json"
        if target == "attempt":
            record = store.begin("example.read", {})
            path = linked / f"{record.attempt_id}.json"
        original = path.read_bytes()
        outside = real / "target.json"
        path.rename(outside)
        path.symlink_to(outside)
        action = store.records if target == "manifest" else partial(store.finish, record, _result())
    with pytest.raises(RuntimeFailure) as caught:
        action()
    assert caught.value.code == "EVIDENCE_INVALID"
    if target == "directory":
        assert stat.S_IMODE(real.stat().st_mode) == before_mode
        assert list(real.iterdir()) == []
    else:
        assert outside.read_bytes() == original
