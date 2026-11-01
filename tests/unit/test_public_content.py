import importlib.util
import re
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "public_content", Path(__file__).resolve().parents[2] / "tools/check_public_content.py"
)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


def test_flags_user_paths_without_printing_the_value():
    path = "/" + "Users/" + "example-user/project"
    issues = CHECK.findings("README.md", path)
    assert issues == [{"path": "README.md", "line": 1, "category": "local-user-path"}]
    assert path not in str(issues)


def test_flags_unknown_hosts_and_real_email_domains():
    text = "https://" + "unreviewed-host.org" + "\n" + "person" + "@" + "unreviewed-host.org"
    assert {item["category"] for item in CHECK.findings("docs/setup.md", text)} == {
        "unreviewed-url-host",
        "non-example-email",
    }


def test_allows_reserved_examples_and_loopback():
    text = "https://api.example.invalid\nhttp://127.0.0.1:8765\nhttps://vault.example\nfixture@example.invalid"
    assert CHECK.findings("docs/setup.md", text) == []
    assert CHECK.findings("tests/unit/test_url.py", "http://bad:bad") == []


def test_local_denylist_is_redacted():
    pattern = re.compile("hidden" + "-project-name", re.I)
    text = "Some hidden" + "-project-name here"
    issues = CHECK.findings("src/demo.py", text, (pattern,))
    assert issues == [{"path": "src/demo.py", "line": 1, "category": "restricted-literal"}]
    assert pattern.pattern not in str(issues)
