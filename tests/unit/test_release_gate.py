"""Release preparation must reject dirty source, stale archives, and a wrong tag."""

from __future__ import annotations

import importlib.util
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    spec = importlib.util.spec_from_file_location("release_gate", ROOT / "tools/release_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def source(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True)

    git("init", "-q")
    (tmp_path / "pyproject.toml").write_text('[project]\nname="agentqit"\nversion="0.1.0"\n')
    (tmp_path / "CHANGELOG.md").write_text("## 0.1.0 - 2026-11-01\n")
    git("add", ".")
    git("-c", "user.name=Release Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    return tmp_path, git


def test_release_rejects_dirty_tree_and_version_mismatch(gate, source):
    directory, _ = source
    assert gate.validate_source(directory, "0.1.0", require_tag=False)["tag_verified"] is False
    with pytest.raises(ValueError, match="differs"):
        gate.validate_source(directory, "0.2.0", require_tag=False)
    (directory / "forgotten.txt").write_text("uncommitted change")
    with pytest.raises(ValueError, match="Commit all"):
        gate.validate_source(directory, "0.1.0", require_tag=False)


def test_release_requires_matching_existing_tag(gate, source):
    directory, git = source
    with pytest.raises(subprocess.CalledProcessError):
        gate.validate_source(directory, "0.1.0", require_tag=True)
    git("tag", "v0.1.0")
    assert gate.validate_source(directory, "0.1.0", require_tag=True)["tag_verified"] is True
    (directory / "CHANGELOG.md").write_text("## 0.1.0 - 2026-11-01\nMore detail\n")
    git("add", ".")
    git("-c", "user.name=Release Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "later")
    with pytest.raises(ValueError, match="tag must identify"):
        gate.validate_source(directory, "0.1.0", require_tag=True)


def test_release_rejects_stale_wheel(gate, source):
    directory, _ = source
    artifacts = directory / "dist"
    artifacts.mkdir()
    (artifacts / "agentqit-0.1.0.tar.gz").touch()
    with zipfile.ZipFile(artifacts / "agentqit-0.0.1-py3-none-any.whl", "w") as archive:
        for package in ["qa_runtime", "qa_cli", "qa_mcp"]:
            archive.writestr(f"{package}/__init__.py", "")
        archive.writestr(
            "agentqit-0.0.1.dist-info/METADATA", "Name: agentqit\nVersion: 0.0.1\nLicense-Expression: MIT\n"
        )
    with pytest.raises(ValueError, match="Wheel version differs"):
        gate.verify(artifacts, directory)


def test_archive_rejects_local_acceptance_reports(gate):
    from verify_archives import validate_paths

    with pytest.raises(ValueError, match="Local acceptance report"):
        validate_paths(["agentqit-0.1.0/docs/products/orders/reports/2026-11-01-session/index.html"])
