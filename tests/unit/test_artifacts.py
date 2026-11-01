import os
import stat

import pytest

from qa_runtime.artifacts import ArtifactStore
from qa_runtime.errors import RuntimeFailure

pytestmark = pytest.mark.anyio


async def test_artifacts_are_private_scoped_integrity_checked_and_not_path_inputs(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts", "session_example", max_bytes=100, max_total_bytes=200)
    try:
        item = await store.save(b"synthetic image", "image/png", "screenshot")
        assert item.session_id == "session_example" and item.private and not item.sanitized
        assert stat.S_IMODE((store.directory / item.file).stat().st_mode) == 0o600
        assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
        assert (await store.read(item.id))[1] == b"synthetic image"
        with pytest.raises(RuntimeFailure, match="Unknown artifact"):
            await store.read("../outside")
        (store.directory / item.file).write_bytes(b"changed")
        with pytest.raises(RuntimeFailure) as caught:
            await store.read(item.id)
        assert caught.value.code == "ARTIFACT_CHANGED"
    finally:
        await store.aclose()


async def test_artifact_limits_and_symlink_rejection(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts", "session_example", max_bytes=10, max_total_bytes=15)
    try:
        item = await store.save(b"12345678", "image/png", "first")
        for data in [b"12345678901", b"abcdefgh"]:
            with pytest.raises(RuntimeFailure) as caught:
                await store.save(data, "image/png", "too large")
            assert caught.value.code == "ARTIFACT_LIMIT"
        outside = tmp_path / "outside"
        outside.write_bytes(b"outside")
        (store.directory / item.file).unlink()
        os.symlink(outside, store.directory / item.file)
        with pytest.raises(RuntimeFailure) as caught:
            await store.read(item.id)
        assert caught.value.code == "ARTIFACT_READ_FAILED"
    finally:
        await store.aclose()


async def test_failed_manifest_write_rolls_back_partial_retention(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "artifacts", "session_example", max_bytes=10, max_total_bytes=15)

    def fail():
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(store, "_write_manifest", fail)
    try:
        with pytest.raises(RuntimeFailure):
            await store.save(b"image", "image/png", "screenshot")
        assert store.list() == []
        assert list(store.directory.glob("artifact_*")) == []
    finally:
        await store.aclose()
