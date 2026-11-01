from __future__ import annotations

import ast
from pathlib import Path

from qa_runtime.security import SecretRedactor

ROOT = Path(__file__).parents[2]


def imports(package: str) -> set[str]:
    result = set()
    for path in (ROOT / "src" / package).rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                result.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                result.add(node.module)
    return result


def test_core_does_not_import_interfaces_or_products():
    assert not any(
        name.split(".")[0] in {"qa_cli", "qa_mcp"} or name.startswith("qa_product_") for name in imports("qa_runtime")
    )


def test_cli_does_not_import_any_specific_product():
    assert not any(name.startswith("qa_product_") for name in imports("qa_cli"))


def test_generic_auth_token_keys_are_redacted_recursively():
    assert SecretRedactor().redact({"teamAuthToken": "hidden", "nested": {"auth-token": "hidden"}}) == {
        "teamAuthToken": "***",
        "nested": {"auth-token": "***"},
    }
