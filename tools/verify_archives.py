"""Check public wheel/sdist contents without accessing services or credentials."""

from __future__ import annotations

import argparse
import configparser
import email
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

FORBIDDEN_PARTS = {".git", ".agentqit", ".qa-cli", ".runs", ".codex", ".venv", "__pycache__"}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo", ".jks", ".key", ".pem", ".crt"}


def validate_paths(names: list[str]) -> None:
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or FORBIDDEN_PARTS.intersection(path.parts):
            raise ValueError(f"Unexpected archive member: {name}")
        if any(
            part == "reports" and index + 1 < len(path.parts) and path.parts[index + 1].startswith("20")
            for index, part in enumerate(path.parts)
        ):
            raise ValueError(f"Local acceptance report in archive: {name}")
        if path.suffix in FORBIDDEN_SUFFIXES or path.name == ".env" or path.name.startswith(".env."):
            raise ValueError(f"Operational state/trust material in archive: {name}")


def verify(directory: Path, source: Path) -> dict:
    expected_version = tomllib.loads((source / "pyproject.toml").read_text())["project"]["version"]
    wheels = list(directory.glob("agentqit-*.whl"))
    sources = list(directory.glob("agentqit-*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise ValueError("Expected exactly one AgentQit wheel and one sdist")
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()
        validate_paths(names)
        packages = {name.split("/", 1)[0] for name in names if ".dist-info" not in name}
        if packages != {"qa_runtime", "qa_cli", "qa_mcp"}:
            raise ValueError(f"Unexpected runtime packages: {packages}")
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = email.message_from_bytes(wheel.read(metadata_name))
        if metadata["Name"] != "agentqit" or metadata["License-Expression"] != "MIT":
            raise ValueError("Unexpected distribution identity or license")
        if metadata["Version"] != expected_version:
            raise ValueError("Wheel version differs from the source project")
        if wheels[0].name != f"agentqit-{expected_version}-py3-none-any.whl":
            raise ValueError("Unexpected wheel filename/version")
        entries = configparser.ConfigParser()
        entries.read_string(wheel.read(metadata_name.replace("METADATA", "entry_points.txt")).decode())
        if "qa_runtime.products" in entries:
            raise ValueError("Runtime wheel must not bundle a product plugin")
        expected_scripts = {"qa": "qa_cli.app:main", "agentqit-mcp": "qa_mcp.__main__:main"}
        if dict(entries["console_scripts"]) != expected_scripts:
            raise ValueError("Unexpected installed commands")
        if not any(name.endswith("/licenses/LICENSE") for name in names):
            raise ValueError("Wheel is missing the project license")
    with tarfile.open(sources[0]) as archive:
        members = archive.getmembers()
        validate_paths([member.name for member in members])
        if any(member.issym() or member.islnk() for member in members):
            raise ValueError("Source distribution must not contain symbolic/hard links")
        prefix = members[0].name.split("/", 1)[0]
        names = {member.name.removeprefix(prefix + "/") for member in members}
        for required in ["pyproject.toml", "LICENSE", "README.md", "tools/verify_wheel.py", "docs/workspaces.md"]:
            if required not in names:
                raise ValueError(f"Source distribution is missing {required}")
        project_file = archive.extractfile(prefix + "/pyproject.toml")
        project = tomllib.loads(project_file.read().decode())
        if project["project"]["name"] != "agentqit" or project["project"]["version"] != expected_version:
            raise ValueError("Unexpected sdist project/version")
        if sources[0].name != f"agentqit-{expected_version}.tar.gz":
            raise ValueError("Unexpected sdist filename/version")
        package_info = email.message_from_bytes(archive.extractfile(prefix + "/PKG-INFO").read())
        if package_info["Version"] != expected_version:
            raise ValueError("Source metadata version differs from the project")
        for member in members:
            if not member.isfile():
                continue
            relative = member.name.removeprefix(prefix + "/")
            local = source / relative
            if local.is_file() and local.read_bytes() != archive.extractfile(member).read():
                raise ValueError(f"Source distribution differs from source: {relative}")
        for module in (source / "src").rglob("*.py"):
            relative = module.relative_to(source).as_posix()
            if relative not in names:
                raise ValueError(f"Source distribution is missing source module: {relative}")
    lock = tomllib.loads((source / "uv.lock").read_text())
    for package in lock["package"]:
        registry = package.get("source", {}).get("registry")
        if registry is not None and registry.rstrip("/") != "https://pypi.org/simple":
            raise ValueError("Lockfile depends on a nonpublic package index")
    return {"wheel": wheels[0].name, "sdist": sources[0].name, "public_index_only": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    print(verify(args.directory, args.source))


if __name__ == "__main__":
    main()
