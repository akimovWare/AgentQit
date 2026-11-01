from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from contextlib import suppress
from pathlib import Path
from threading import RLock

import anyio
from pydantic import BaseModel, ConfigDict

from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure

_EXTENSIONS = {"image/png": ".png", "application/zip": ".zip", "application/json": ".json", "text/plain": ".txt"}


class ArtifactInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    session_id: str
    run_id: str | None = None
    media_type: str
    size: int
    sha256: str
    label: str
    file: str
    private: bool = True
    sanitized: bool = False


class ArtifactStore:
    """Private run-owned files, referenced by opaque IDs rather than caller paths."""

    def __init__(self, directory: Path, session_id: str, *, max_bytes: int, max_total_bytes: int) -> None:
        self.directory, self.session_id = directory, session_id
        directory.mkdir(mode=0o700, parents=False, exist_ok=False)
        self._directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self._max_bytes, self._max_total = max_bytes, max_total_bytes
        self._used = 0
        self._items: dict[str, ArtifactInfo] = {}
        self._lock = RLock()
        self._closed = False

    def list(self) -> list[dict]:
        with self._lock:
            return [item.model_dump(mode="json") for item in self._items.values()]

    async def save(
        self,
        data: bytes,
        media_type: str,
        label: str,
        *,
        sanitized: bool = False,
        run_id: str | None = None,
    ) -> ArtifactInfo:
        return await anyio.to_thread.run_sync(self._save, data, media_type, label, sanitized, run_id)

    def _save(self, data: bytes, media_type: str, label: str, sanitized: bool, run_id: str | None) -> ArtifactInfo:
        with self._lock:
            self._check_open()
            if len(data) > self._max_bytes or self._used + len(data) > self._max_total:
                raise RuntimeFailure(
                    ExitCode.ENVIRONMENT_UNAVAILABLE,
                    "ARTIFACT_LIMIT",
                    "Artifact retention limit exceeded",
                    source="artifacts",
                )
            if media_type not in _EXTENSIONS:
                raise RuntimeFailure(ExitCode.INVALID_INPUT, "ARTIFACT_TYPE", "Unsupported artifact type")
            artifact_id = f"artifact_{uuid.uuid4().hex}"
            filename = artifact_id + _EXTENSIONS[media_type]
            try:
                fd = os.open(
                    filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._directory_fd
                )
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                item = ArtifactInfo(
                    id=artifact_id,
                    session_id=self.session_id,
                    run_id=run_id,
                    media_type=media_type,
                    size=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    label=label[:80],
                    file=filename,
                    sanitized=sanitized,
                )
                self._items[artifact_id] = item
                self._used += len(data)
                self._write_manifest()
                return item
            except OSError as error:
                if artifact_id in self._items:
                    self._items.pop(artifact_id)
                    self._used -= len(data)
                with suppress(OSError):
                    os.unlink(filename, dir_fd=self._directory_fd)
                raise RuntimeFailure(
                    ExitCode.ENVIRONMENT_UNAVAILABLE,
                    "ARTIFACT_WRITE_FAILED",
                    "Could not retain artifact",
                    source="artifacts",
                ) from error

    def _write_manifest(self) -> None:
        temporary = f".manifest_{uuid.uuid4().hex}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._directory_fd)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump([item.model_dump(mode="json") for item in self._items.values()], stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, "artifacts.json", src_dir_fd=self._directory_fd, dst_dir_fd=self._directory_fd)
            os.fsync(self._directory_fd)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=self._directory_fd)

    def temporary_path(self, suffix: str = ".zip") -> Path:
        if suffix not in {".zip", ".json"}:
            raise ValueError("unsupported staging suffix")
        self._check_open()
        return self.directory / f".pending_{uuid.uuid4().hex}{suffix}"

    async def import_file(self, path: Path, media_type: str, label: str) -> ArtifactInfo:
        if path.parent != self.directory:
            raise RuntimeFailure(ExitCode.INVALID_INPUT, "ARTIFACT_PATH", "Staging file is outside artifact storage")
        try:
            data = await anyio.to_thread.run_sync(self._read_file, path.name, self._max_bytes)
            return await self.save(data, media_type, label)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(path.name, dir_fd=self._directory_fd)

    async def read(self, artifact_id: str) -> tuple[ArtifactInfo, bytes]:
        return await anyio.to_thread.run_sync(self._read, artifact_id)

    def _read(self, artifact_id: str) -> tuple[ArtifactInfo, bytes]:
        with self._lock:
            self._check_open()
            item = self._items.get(artifact_id)
            if item is None:
                raise RuntimeFailure(ExitCode.INVALID_INPUT, "ARTIFACT_NOT_FOUND", "Unknown artifact reference")
            data = self._read_file(item.file, self._max_bytes)
            if len(data) != item.size or hashlib.sha256(data).hexdigest() != item.sha256:
                raise RuntimeFailure(ExitCode.ENVIRONMENT_UNAVAILABLE, "ARTIFACT_CHANGED", "Artifact integrity changed")
            return item, data

    def _read_file(self, filename: str, limit: int) -> bytes:
        try:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._directory_fd)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                    raise RuntimeFailure(
                        ExitCode.ENVIRONMENT_UNAVAILABLE, "ARTIFACT_LIMIT", "Artifact size/type rejected"
                    )
                data = stream.read(limit + 1)
                if len(data) > limit:
                    raise RuntimeFailure(ExitCode.ENVIRONMENT_UNAVAILABLE, "ARTIFACT_LIMIT", "Artifact size rejected")
                return data
        except OSError as error:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "ARTIFACT_READ_FAILED",
                "Could not read artifact",
                source="artifacts",
            ) from error

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE, "ARTIFACT_STORE_CLOSED", "Artifact storage is closed"
            )

    async def aclose(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                os.close(self._directory_fd)
