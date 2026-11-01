"""Verify release source and archive identity, then write a checksum manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

from packaging.version import Version
from verify_archives import verify
from verify_wheel import verify_wheel


def git(source: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", "-C", str(source), *arguments], text=True).strip()


def validate_source(source: Path, version: str, *, require_tag: bool) -> dict:
    parsed = Version(version)
    if str(parsed) != version or parsed.local is not None or parsed.epoch:
        raise ValueError("Use a canonical public version, such as 0.1.0 or 0.2.0rc1")
    project = tomllib.loads((source / "pyproject.toml").read_text())["project"]
    if version != project["version"]:
        raise ValueError("Requested version differs from pyproject.toml")
    changelog = (source / "CHANGELOG.md").read_text()
    if f"## {version} - " not in changelog:
        raise ValueError("The version requires a matching changelog entry")
    if git(source, "status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("Commit all intended changes before preparing a release")
    revision = git(source, "rev-parse", "HEAD")
    example = source / "examples/orders/pyproject.toml"
    if example.is_file():
        dependencies = tomllib.loads(example.read_text())["project"]["dependencies"]
        if f"agentqit=={version}" not in dependencies:
            raise ValueError("Update the example's exact runtime dependency for this release")
    if require_tag:
        dated_entry = re.search(rf"^## {re.escape(version)} - (\d{{4}}-\d{{2}}-\d{{2}})$", changelog, re.MULTILINE)
        if dated_entry is None:
            raise ValueError("Date the changelog entry before tagging a public release")
        date.fromisoformat(dated_entry.group(1))
        tag_revision = git(source, "rev-parse", f"refs/tags/v{version}^{{commit}}")
        if tag_revision != revision:
            raise ValueError("The release tag must identify the checked-out source commit")
    return {"version": version, "revision": revision, "tag_verified": require_tag}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--require-tag", action="store_true")
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    try:
        result = validate_source(source, args.version, require_tag=args.require_tag)
        directory = args.directory.resolve()
        result["archives"] = verify(directory, source)
        artifacts = {
            path.name for path in directory.iterdir() if path.suffix == ".whl" or path.name.endswith(".tar.gz")
        }
        expected = {result["archives"]["wheel"], result["archives"]["sdist"]}
        if artifacts != expected:
            raise ValueError("The release directory must contain only the selected wheel and sdist")
        with redirect_stdout(sys.stderr):
            verify_wheel(directory / result["archives"]["wheel"], source)
        result["files"] = {
            path.name: {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
            for path in sorted(directory.iterdir())
            if path.suffix == ".whl" or path.name.endswith(".tar.gz")
        }
        result["publication_performed"] = False
        (directory / "release-manifest.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Release check failed: {error}") from error


if __name__ == "__main__":
    main()
