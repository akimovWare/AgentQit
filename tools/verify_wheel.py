"""Compare a built wheel with declared source packages and inspect its product plugins.

The source project must declare its package directories in Hatch's wheel.packages.
Git supplies the complete source inventory (including new, nonignored files). For
an exported source tree without Git, only present wheel files and plugin-declared
registry/setup assets can be compared; a warning reports that narrower check.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import re
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any


class VerificationError(ValueError):
    pass


def _project(source_root: Path) -> tuple[dict[str, Any], dict[str, Path]]:
    document = tomllib.loads((source_root / "pyproject.toml").read_text(encoding="utf-8"))
    declared = document.get("tool", {}).get("hatch", {}).get("build", {}).get("targets", {}).get("wheel", {})
    paths = declared.get("packages")
    if not isinstance(paths, list) or not paths:
        raise VerificationError("source pyproject must declare Hatch wheel.packages")
    packages: dict[str, Path] = {}
    for value in paths:
        path = (source_root / value).resolve()
        if not path.is_relative_to(source_root) or not path.is_dir() or not path.name.isidentifier():
            raise VerificationError("each declared package must be an existing directory inside the source project")
        if path.name in packages:
            raise VerificationError("declared packages have duplicate destination names")
        packages[path.name] = path
    return document["project"], packages


def _source_path(relative: Path, packages: dict[str, Path]) -> Path:
    package = packages.get(relative.parts[0])
    if package is None:
        raise VerificationError(f"plugin resource is outside declared packages: {relative}")
    return package.joinpath(*relative.parts[1:])


def _git_inventory(source_root: Path, packages: dict[str, Path]) -> set[Path] | None:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(source_root),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
                "--",
                *(str(path.relative_to(source_root)) for path in packages.values()),
            ],
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode:
        return None
    files: set[Path] = set()
    for name in result.stdout.decode("utf-8").split("\0"):
        if not name:
            continue
        path = source_root / name
        if not path.is_file():
            # A tracked deletion is part of the working tree even before commit.
            continue
        for package_name, package in packages.items():
            if path.is_relative_to(package):
                files.add(Path(package_name) / path.relative_to(package))
                break
    return files


def _compare_package_files(extracted: Path, source_root: Path, packages: dict[str, Path]) -> None:
    installed = {
        path.relative_to(extracted) for name in packages for path in (extracted / name).rglob("*") if path.is_file()
    }
    expected = _git_inventory(source_root, packages)
    if expected is None:
        print(
            "warning: no Git source inventory; missing undeclared assets cannot be detected; "
            "registry/setup assets and files present in the wheel are still compared",
            file=sys.stderr,
        )
        expected = installed
    missing = sorted(expected - installed)
    unexpected = sorted(installed - expected)
    if missing:
        raise VerificationError("wheel is missing source package files: " + ", ".join(map(str, missing)))
    if unexpected:
        raise VerificationError("wheel contains unexpected package files: " + ", ".join(map(str, unexpected)))
    for relative in sorted(expected):
        source = _source_path(relative, packages)
        if not source.is_file() or source.read_bytes() != (extracted / relative).read_bytes():
            raise VerificationError(f"wheel file differs from source: {relative}")


def _relative_resource(path: Path, extracted: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(extracted):
        raise VerificationError("product resource points outside the extracted wheel")
    return resolved.relative_to(extracted)


def _inspect_extracted(extracted: Path, source_root: Path) -> None:
    project, packages = _project(source_root)
    sys.path.insert(0, str(extracted))
    distributions = tuple(importlib.metadata.distributions(path=[extracted]))
    if len(distributions) != 1:
        raise VerificationError("wheel must contain exactly one distribution metadata directory")
    distribution = distributions[0]

    def normalize(value: str) -> str:
        return re.sub(r"[-_.]+", "-", value).lower()

    if normalize(distribution.metadata["Name"]) != normalize(project["name"]):
        raise VerificationError("wheel distribution name differs from source project.name")
    if "version" in project and distribution.version != project["version"]:
        raise VerificationError("wheel distribution version differs from source project.version")
    declared = project.get("entry-points", {}).get("qa_runtime.products", {})
    entries = [entry for entry in distribution.entry_points if entry.group == "qa_runtime.products"]
    installed = {entry.name: entry.value for entry in entries}
    if len(installed) != len(entries) or installed != declared:
        raise VerificationError("wheel product entry points differ from source project metadata")

    # Check even a runtime-only wheel, which legitimately has no product entry point.
    for name in packages:
        importlib.import_module(name)
    for entry in entries:
        from qa_runtime.ports.product import PRODUCT_PLUGIN_API_VERSION, ProductPlugin
        from qa_runtime.registry import OperationRegistry

        provider = entry.load()
        plugin = provider() if callable(provider) else provider
        if not isinstance(plugin, ProductPlugin) or plugin.product_id != entry.name:
            raise VerificationError(f"invalid product plugin returned by entry point: {entry.name}")
        if plugin.api_version != PRODUCT_PLUGIN_API_VERSION:
            raise VerificationError(f"unsupported product API version: {entry.name}")
        registry_path = _relative_resource(plugin.registry_path, extracted)
        registry = OperationRegistry.load(plugin.registry_path)
        source_registry = OperationRegistry.load(_source_path(registry_path, packages))
        if [operation.model_dump() for operation in registry.operations()] != [
            operation.model_dump() for operation in source_registry.operations()
        ]:
            raise VerificationError(f"installed registry differs from source: {entry.name}")
        if plugin.setup is not None:
            template = _relative_resource(plugin.setup.profile_template, extracted)
            source_template = _source_path(template, packages)
            if not plugin.setup.profile_template.is_file() or not source_template.is_file():
                raise VerificationError(f"declared setup template is missing: {entry.name}")
            if plugin.setup.profile_template.read_bytes() != source_template.read_bytes():
                raise VerificationError(f"installed setup template differs from source: {entry.name}")

    # An editable installation must never conceal an omitted wheel module.
    for name, module in tuple(sys.modules.items()):
        if name.partition(".")[0] not in packages:
            continue
        origin = getattr(module, "__file__", None)
        if origin is not None and not Path(origin).resolve().is_relative_to(extracted):
            raise VerificationError(f"package module was imported outside the extracted wheel: {name}")
        for location in getattr(module, "__path__", ()):
            if not Path(location).resolve().is_relative_to(extracted):
                raise VerificationError(f"package search path points outside the extracted wheel: {name}")
    print(f"verified {len(packages)} source packages and {len(entries)} product entry points from extracted wheel")


def verify_wheel(target: Path, source_root: Path) -> None:
    source_root = source_root.resolve()
    target = target.resolve()
    if target.is_dir():
        wheels = tuple(target.glob("*.whl"))
        if len(wheels) != 1:
            raise VerificationError(f"expected exactly one wheel, found {len(wheels)}")
        target = wheels[0]
    if not target.is_file():
        raise VerificationError("wheel file was not found")
    _, packages = _project(source_root)
    with tempfile.TemporaryDirectory(prefix="qa-wheel-") as directory:
        extracted = Path(directory).resolve()
        with zipfile.ZipFile(target) as archive:
            archive.extractall(extracted)
        _compare_package_files(extracted, source_root, packages)
        checked = subprocess.run(
            [
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "--inspect-extracted",
                str(extracted),
                str(source_root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if checked.returncode:
            raise VerificationError(checked.stderr.strip() or "extracted wheel inspection failed")
        print(checked.stdout.strip())


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == "--inspect-extracted":
        _inspect_extracted(Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve())
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="wheel file or directory containing exactly one wheel")
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    options = parser.parse_args()
    verify_wheel(options.target, options.source_root)


if __name__ == "__main__":
    try:
        main()
    except VerificationError as error:
        raise SystemExit(str(error)) from None
    except Exception as error:
        raise SystemExit(f"wheel inspection failed ({type(error).__name__})") from None
