from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from qa_runtime.contracts import CommandResult, ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.security import SecretRedactor


class EvidenceScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    profile: str
    profile_digest: str
    registry_digest: str


class RunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["qa.run/v1"] = "qa.run/v1"
    case_id: str = Field(default_factory=lambda: f"case_{uuid.uuid4().hex}")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    scope: EvidenceScope


class EvidenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["qa.evidence/v1"] = "qa.evidence/v1"
    attempt_id: str = Field(default_factory=lambda: f"attempt_{uuid.uuid4().hex}")
    operation_id: str
    input: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    result: CommandResult | None = None


class EvidenceRedactor(Protocol):
    def redact(self, value: Any) -> Any: ...


def content_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


class RunStore:
    """Private, local invocation journal; a pending attempt never means safe to retry.

    The manifest is immutable. Each invocation has its own atomically replaced file,
    so independent processes do not overwrite one shared list of steps.
    """

    def __init__(self, directory: Path, *, redactor: EvidenceRedactor | None = None) -> None:
        self.directory = directory.expanduser().absolute()
        self.redactor = redactor if redactor is not None else SecretRedactor()
        self._scope: EvidenceScope | None = None

    def initialize(self, scope: EvidenceScope) -> RunManifest:
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self._directory_descriptor() as directory:
                os.fchmod(directory, 0o700)
                manifest = RunManifest(scope=scope)
                try:
                    self._publish_new(directory, "manifest.json", manifest.model_dump(mode="json", warnings="error"))
                except FileExistsError:
                    manifest = self._manifest(directory)
                self._check_scope(manifest, scope)
            self._scope = scope.model_copy(deep=True)
            return manifest
        except OSError as error:
            raise self._io_failure() from error

    def manifest(self) -> RunManifest:
        with self._directory_descriptor() as directory:
            return self._manifest(directory)

    def begin(self, operation_id: str, params: dict[str, Any]) -> EvidenceRecord:
        try:
            with self._directory_descriptor() as directory:
                self._manifest(directory)
                record = EvidenceRecord(operation_id=operation_id, input=self.redactor.redact(params))
                name = self._record_path(record.attempt_id).name
                payload = record.model_dump(mode="json", warnings="error")
                record = self._record(payload, name)
                self._publish_new(directory, name, payload)
        except OSError as error:
            raise self._io_failure() from error
        except (ValueError, TypeError) as error:
            raise self._invalid_failure() from error
        return record

    def finish(self, record: EvidenceRecord, result: CommandResult) -> Path:
        path = self._record_path(record.attempt_id)
        try:
            with self._directory_descriptor() as directory:
                self._manifest(directory)
                # Lock the pending inode. A second finisher may already have opened it,
                # so check its identity again after acquiring the lock and before replacing it.
                with self._file_descriptor(directory, path.name) as descriptor:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                    current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                    opened = os.fstat(descriptor)
                    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                        raise self._conflict_failure()
                    existing = self._record(self._read_descriptor(descriptor), path.name)
                    if existing.result is not None or existing != record:
                        raise self._conflict_failure()
                    if result.command not in {f"invoke {record.operation_id}", f"checkpoint {record.operation_id}"}:
                        raise self._conflict_failure()
                    completed = existing.model_copy(update={"result": result})
                    payload = self.redactor.redact(completed.model_dump(mode="json", warnings="error"))
                    self._record(payload, path.name)
                    self._replace(directory, path.name, payload)
        except OSError as error:
            raise self._io_failure() from error
        except (ValueError, TypeError) as error:
            raise self._invalid_failure() from error
        return path

    def records(self) -> tuple[EvidenceRecord, ...]:
        try:
            with self._directory_descriptor() as directory:
                self._manifest(directory)
                records = [
                    self._record(self._read(directory, name), name)
                    for name in os.listdir(directory)
                    if name.startswith("attempt_") and name.endswith(".json")
                ]
            return tuple(sorted(records, key=lambda record: (record.started_at, record.attempt_id)))
        except (OSError, ValueError, TypeError) as error:
            raise self._invalid_failure() from error

    def _record_path(self, attempt_id: str) -> Path:
        if not isinstance(attempt_id, str) or not re.fullmatch(r"attempt_[0-9a-f]{32}", attempt_id):
            raise self._invalid_failure()
        return self.directory / f"{attempt_id}.json"

    def _manifest(self, directory: int) -> RunManifest:
        try:
            payload = self._read(directory, "manifest.json")
            if not RunManifest.model_fields.keys() <= payload.keys():
                raise self._invalid_failure()
            manifest = RunManifest.model_validate(payload)
            if not re.fullmatch(r"case_[0-9a-f]{32}", manifest.case_id) or manifest.created_at.tzinfo is None:
                raise self._invalid_failure()
            if self._scope is not None:
                self._check_scope(manifest, self._scope)
            return manifest
        except (ValueError, TypeError) as error:
            raise self._invalid_failure() from error

    def _record(self, payload: dict[str, Any], name: str) -> EvidenceRecord:
        try:
            if not isinstance(payload, dict) or not EvidenceRecord.model_fields.keys() <= payload.keys():
                raise self._invalid_failure()
            record = EvidenceRecord.model_validate(payload)
            if self._record_path(record.attempt_id).name != name or record.started_at.tzinfo is None:
                raise self._invalid_failure()
            if not record.operation_id.strip():
                raise self._invalid_failure()
            if record.result is not None and record.result.command not in {
                f"invoke {record.operation_id}",
                f"checkpoint {record.operation_id}",
            }:
                raise self._invalid_failure()
            return record
        except (ValueError, TypeError) as error:
            raise self._invalid_failure() from error

    @contextmanager
    def _directory_descriptor(self) -> Iterator[int]:
        try:
            descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as error:
            raise self._invalid_failure() from error
        try:
            yield descriptor
        finally:
            with suppress(OSError):
                os.close(descriptor)

    @classmethod
    @contextmanager
    def _file_descriptor(cls, directory: int, name: str) -> Iterator[int]:
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        except OSError as error:
            raise cls._invalid_failure() from error
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise cls._invalid_failure()
            yield descriptor
        finally:
            with suppress(OSError):
                os.close(descriptor)

    @classmethod
    def _publish_new(cls, directory: int, name: str, data: dict[str, Any]) -> None:
        temporary = f".{uuid.uuid4().hex}.tmp"
        try:
            cls._create(directory, temporary, data)
            # Unlike O_EXCL followed by writes to the public name, link publishes a
            # complete file without replacing a manifest created by another process.
            os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        finally:
            with suppress(OSError):
                os.unlink(temporary, dir_fd=directory)

    @classmethod
    def _replace(cls, directory: int, name: str, data: dict[str, Any]) -> None:
        temporary = f".{uuid.uuid4().hex}.tmp"
        try:
            cls._create(directory, temporary, data)
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            # Publication is the commit point. A cleanup failure must not turn a
            # completed record into a reported failure, and leftover temps are ignored.
            with suppress(OSError):
                os.unlink(temporary, dir_fd=directory)

    @staticmethod
    def _create(directory: int, name: str, data: dict[str, Any]) -> None:
        encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(name, flags, 0o600, dir_fd=directory)
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())

    @classmethod
    def _read(cls, directory: int, name: str) -> dict[str, Any]:
        with cls._file_descriptor(directory, name) as descriptor:
            return cls._read_descriptor(descriptor)

    @classmethod
    def _read_descriptor(cls, descriptor: int) -> dict[str, Any]:
        try:
            if os.fstat(descriptor).st_size > 20_000_000:
                raise ValueError("invalid evidence file")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                encoded = stream.read(20_000_001)
            if len(encoded) > 20_000_000:
                raise ValueError("invalid evidence file")
            payload = json.loads(encoded)
            if not isinstance(payload, dict):
                raise ValueError("expected an object")
            return payload
        except (OSError, ValueError, TypeError, ValidationError) as error:
            raise cls._invalid_failure() from error

    @staticmethod
    def _check_scope(manifest: RunManifest, scope: EvidenceScope) -> None:
        if manifest.scope != scope:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "EVIDENCE_SCOPE_MISMATCH",
                "run directory belongs to another product, profile or registry revision; use a new directory",
                source="evidence",
            )

    @staticmethod
    def _conflict_failure() -> RuntimeFailure:
        return RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "EVIDENCE_ATTEMPT_CONFLICT",
            "attempt is already completed or mismatched",
            source="evidence",
        )

    @staticmethod
    def _io_failure() -> RuntimeFailure:
        return RuntimeFailure(
            ExitCode.ENVIRONMENT_UNAVAILABLE,
            "EVIDENCE_UNAVAILABLE",
            "local evidence could not be written; check directory access and free space",
            source="evidence",
        )

    @staticmethod
    def _invalid_failure() -> RuntimeFailure:
        return RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "EVIDENCE_INVALID",
            "local evidence is missing or malformed",
            source="evidence",
        )
