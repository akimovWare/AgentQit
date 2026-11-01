"""Check a tracked source snapshot for local identity and operational data.

An optional local denylist can supplement generic checks. Its values and source
path are never included in output. This check does not replace a secret scanner
or a maintainer's review of code, assets, and the history selected for publication.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

PUBLIC_HOSTS = {
    "github.com",
    "docs.github.com",
    "pypi.org",
    "files.pythonhosted.org",
    "docs.astral.sh",
    "packaging.python.org",
    "docs.pypi.org",
    "playwright.dev",
    "json-schema.org",
    "ts.sdk.modelcontextprotocol.io",
    "s3.amazonaws.com",
}
LOCAL_PATH = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+(?:/|\b)|[A-Za-z]:\\Users\\[A-Za-z0-9._-]+")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
URL = re.compile(r"https?://[^\s\"\'<>`\\]+")
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")
FORBIDDEN = {".git", ".venv", ".agentqit", ".qa-cli", ".runs", ".codex", "__pycache__"}


def example_host(host: str) -> bool:
    host = host.lower()
    return host in {"localhost", "example.com", "example.org", "example.net"} or host.endswith(
        (".invalid", ".test", ".example", ".example.com", ".example.org", ".example.net")
    )


def findings(path: str, text: str, forbidden: tuple[re.Pattern, ...] = ()) -> list[dict]:
    result = []
    for number, line in enumerate(text.splitlines(), 1):
        categories = set()
        if LOCAL_PATH.search(line):
            categories.add("local-user-path")
        if PRIVATE_KEY.search(line):
            categories.add("private-key-material")
        if any(
            not example_host(match.group(1)) and match.group(1) != "users.noreply.github.com"
            for match in EMAIL.finditer(line)
        ):
            categories.add("non-example-email")
        if any(pattern.search(line) for pattern in forbidden):
            categories.add("restricted-literal")
        for raw in URL.findall(line):
            try:
                host = urlsplit(raw.rstrip(").,;")).hostname
            except ValueError:
                continue
            if not host or host in PUBLIC_HOSTS or example_host(host):
                continue
            try:
                if ipaddress.ip_address(host).is_loopback:
                    continue
            except ValueError:
                # Deliberately malformed single-label URL fixtures exercise validation.
                if path.startswith("tests/") and "." not in host:
                    continue
            categories.add("unreviewed-url-host")
        result.extend({"path": path, "line": number, "category": category} for category in sorted(categories))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--denylist", type=Path, help="Local JSON array of restricted literals; never commit this file")
    args = parser.parse_args()
    root = args.root.resolve()
    forbidden = ()
    if args.denylist:
        values = json.loads(args.denylist.read_text())
        if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
            raise SystemExit("The local denylist must be an array of nonempty strings")
        forbidden = tuple(
            re.compile(r"(?<![A-Za-z0-9_])" + re.escape(value) + r"(?![A-Za-z0-9_])", re.I) for value in values
        )
    names = subprocess.check_output(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
    )
    issues = []
    checked = 0
    for raw in names.split(b"\0"):
        if not raw:
            continue
        name = raw.decode()
        path = root / name
        if not path.exists():
            continue
        parts = Path(name).parts
        if (
            path.is_symlink()
            or FORBIDDEN.intersection(parts)
            or path.suffix in {".pem", ".key", ".crt", ".jks", ".pyc"}
            or Path(name).name.startswith(".env")
        ):
            issues.append({"path": name, "category": "operational-file"})
            continue
        if any(
            part == "reports" and index + 1 < len(parts) and parts[index + 1].startswith("20")
            for index, part in enumerate(parts)
        ):
            issues.append({"path": name, "category": "local-report"})
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            issues.append({"path": name, "category": "unreviewed-binary"})
            continue
        checked += 1
        issues.extend(findings(name, text, forbidden))
    print(json.dumps({"checked_files": checked, "findings": issues}, sort_keys=True))
    if issues:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
