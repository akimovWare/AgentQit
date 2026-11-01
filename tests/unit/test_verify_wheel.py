from __future__ import annotations

import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "verify_wheel.py"
spec = importlib.util.spec_from_file_location("qa_verify_wheel", SCRIPT)
assert spec is not None and spec.loader is not None
verification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verification)


def _source(directory: Path, products=("alpha",), *, setup=True, git=True):
    directory.mkdir()
    packages = [f"qa_product_{product}" for product in products] or ["qa_library"]
    paths = ", ".join(f'"src/{package}"' for package in packages)
    entries = {product: f"qa_product_{product}:product_plugin" for product in products}
    declared = "\n".join(f'{name} = "{value}"' for name, value in entries.items())
    (directory / "pyproject.toml").write_text(
        '[project]\nname = "sample-qa"\nversion = "1.0"\n'
        f'[project.entry-points."qa_runtime.products"]\n{declared}\n'
        f"[tool.hatch.build.targets.wheel]\npackages = [{paths}]\n",
        encoding="utf-8",
    )
    (directory / ".gitignore").write_text("*.local-cert\n__pycache__/\n", encoding="utf-8")
    for package in packages:
        root = directory / "src" / package
        root.mkdir(parents=True)
        if not products:
            (root / "__init__.py").write_text("", encoding="utf-8")
            continue
        product = package.removeprefix("qa_product_")
        setup_argument = ", setup=ProductSetup(profile_template=root / 'profiles' / 'example.yaml')" if setup else ""
        (root / "__init__.py").write_text(
            "from pathlib import Path\nfrom qa_runtime.ports.product import ProductPlugin, ProductSetup\n"
            "def product_plugin():\n    root = Path(__file__).parent\n"
            f"    return ProductPlugin('{product}', root / 'registry'{setup_argument})\n",
            encoding="utf-8",
        )
        (root / "registry").mkdir()
        (root / "registry" / "read.yaml").write_text(
            "apiVersion: qa.runtime/v1\nkind: OperationRegistry\noperations:\n"
            f"- id: {product}.inspect\n  config: {{service: sample, method: GET, path: /state}}\n",
            encoding="utf-8",
        )
        (root / "profiles").mkdir()
        (root / "profiles" / "example.yaml").write_text(
            "environment: qa\nservices:\n  sample:\n    baseUrl: https://sample.invalid\n", encoding="utf-8"
        )
        (root / "descriptor.bin").write_bytes(b"descriptor fixture")
        (root / "private.local-cert").write_bytes(b"local trust fixture must be excluded")
    if git:
        subprocess.run(["git", "init", "-q", str(directory)], check=True, capture_output=True)
    return entries


def _wheel(directory: Path, target: Path, entries: dict[str, str], *, omit=(), replace=None, add=None):
    with zipfile.ZipFile(target, "w") as archive:
        for source in (directory / "src").rglob("*"):
            if not source.is_file() or source.suffix == ".local-cert":
                continue
            name = source.relative_to(directory / "src").as_posix()
            if name not in omit:
                archive.writestr(name, (replace or {}).get(name, source.read_bytes()))
        for name, value in (add or {}).items():
            archive.writestr(name, value)
        archive.writestr("sample_qa-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: sample_qa\nVersion: 1.0\n")
        archive.writestr(
            "sample_qa-1.0.dist-info/entry_points.txt",
            "[qa_runtime.products]\n" + "\n".join(f"{name} = {value}" for name, value in entries.items()) + "\n",
        )
    return target


@pytest.mark.parametrize(("products", "setup"), [(("alpha", "beta"), True), (("alpha",), False), ((), False)])
def test_verifies_arbitrary_products_optional_setup_and_runtime_only_wheel(tmp_path, capsys, products, setup):
    source = tmp_path / "source"
    entries = _source(source, products, setup=setup)
    wheel = _wheel(source, tmp_path / "sample.whl", entries)

    verification.verify_wheel(wheel, source)

    assert f"{len(products)} product entry points from extracted wheel" in capsys.readouterr().out


@pytest.mark.parametrize("asset", ["registry/read.yaml", "profiles/example.yaml", "descriptor.bin"])
def test_git_inventory_detects_missing_arbitrary_data_assets(tmp_path, asset):
    source = tmp_path / "source"
    entries = _source(source)
    relative = f"qa_product_alpha/{asset}"
    wheel = _wheel(source, tmp_path / "sample.whl", entries, omit=(relative,))

    with pytest.raises(verification.VerificationError, match="missing source package files") as raised:
        verification.verify_wheel(wheel, source)

    assert relative in str(raised.value)


def test_compares_asset_bytes_and_rejects_unexpected_ignored_files(tmp_path):
    source = tmp_path / "source"
    entries = _source(source)
    wheel = _wheel(
        source, tmp_path / "changed.whl", entries, replace={"qa_product_alpha/descriptor.bin": b"different descriptor"}
    )
    with pytest.raises(verification.VerificationError, match="differs from source"):
        verification.verify_wheel(wheel, source)

    wheel = _wheel(
        source, tmp_path / "extra.whl", entries, add={"qa_product_alpha/private.local-cert": b"unwanted fixture"}
    )
    with pytest.raises(verification.VerificationError, match="unexpected package files"):
        verification.verify_wheel(wheel, source)


def test_entry_points_are_compared_with_source_metadata(tmp_path):
    source = tmp_path / "source"
    _source(source)
    wheel = _wheel(source, tmp_path / "sample.whl", {})
    with pytest.raises(verification.VerificationError, match="entry points differ"):
        verification.verify_wheel(wheel, source)


def test_product_entry_point_may_return_an_instance_directly(tmp_path):
    source = tmp_path / "source"
    entries = _source(source)
    module = source / "src" / "qa_product_alpha" / "__init__.py"
    module.write_text(module.read_text() + "\nproduct_plugin = product_plugin()\n", encoding="utf-8")
    verification.verify_wheel(_wheel(source, tmp_path / "sample.whl", entries), source)


def test_static_project_version_is_compared_with_wheel_metadata(tmp_path):
    source = tmp_path / "source"
    entries = _source(source)
    metadata = source / "pyproject.toml"
    metadata.write_text(metadata.read_text().replace('version = "1.0"', 'version = "2.0"'), encoding="utf-8")
    with pytest.raises(verification.VerificationError, match="version differs"):
        verification.verify_wheel(_wheel(source, tmp_path / "sample.whl", entries), source)


def test_uncommitted_source_deletion_is_respected_and_stale_wheel_file_is_rejected(tmp_path):
    source = tmp_path / "source"
    entries = _source(source)
    removed = source / "src" / "qa_product_alpha" / "descriptor.bin"
    subprocess.run(["git", "-C", str(source), "add", str(removed)], check=True, capture_output=True)
    removed.unlink()
    verification.verify_wheel(_wheel(source, tmp_path / "current.whl", entries), source)
    stale = _wheel(
        source, tmp_path / "stale.whl", entries, add={"qa_product_alpha/descriptor.bin": b"descriptor fixture"}
    )
    with pytest.raises(verification.VerificationError, match="unexpected package files"):
        verification.verify_wheel(stale, source)


@pytest.mark.parametrize("asset", ["registry/read.yaml", "profiles/example.yaml"])
def test_declared_assets_remain_required_without_git_inventory(tmp_path, asset, capsys):
    source = tmp_path / "source"
    entries = _source(source, git=False)
    wheel = _wheel(source, tmp_path / "sample.whl", entries, omit=(f"qa_product_alpha/{asset}",))
    with pytest.raises(verification.VerificationError):
        verification.verify_wheel(wheel, source)

    assert "no Git source inventory" in capsys.readouterr().err


def test_import_origin_check_rejects_fallback_to_editable_runtime(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[project]\nname="sample-qa"\n[tool.hatch.build.targets.wheel]\npackages=["src/qa_runtime"]\n',
        encoding="utf-8",
    )
    (source / "src" / "qa_runtime").mkdir(parents=True)
    (source / "src" / "qa_runtime" / "marker.txt").write_text("fixture", encoding="utf-8")
    wheel = _wheel(source, tmp_path / "sample.whl", {})

    with pytest.raises(verification.VerificationError, match="outside the extracted wheel"):
        verification.verify_wheel(wheel, source)


def test_cli_keeps_wheel_directory_interface_with_explicit_source_root(tmp_path):
    source = tmp_path / "source"
    entries = _source(source)
    distribution = tmp_path / "dist"
    distribution.mkdir()
    _wheel(source, distribution / "sample.whl", entries)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(distribution), "--source-root", str(source)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "from extracted wheel" in result.stdout
