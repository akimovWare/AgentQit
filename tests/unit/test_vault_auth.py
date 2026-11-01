from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from qa_runtime.adapters.vault_auth import login_oidc
from qa_runtime.credential_config import VaultCredentialConfig
from qa_runtime.errors import RuntimeFailure

pytestmark = pytest.mark.anyio


def executable(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "vault"
    path.write_text(f"#!{sys.executable}\n{body}\n")
    path.chmod(0o700)
    return path


async def test_oidc_login_uses_explicit_profile_and_never_prints_child_output(tmp_path, capfd):
    captured = tmp_path / "arguments.json"
    command = executable(
        tmp_path,
        "import json, os, sys\nfrom pathlib import Path\n"
        f"Path({str(captured)!r}).write_text(json.dumps([sys.argv[1:], dict(os.environ)]))\n"
        "print('sensitive-session-token')\nprint('private diagnostic', file=sys.stderr)",
    )
    config = VaultCredentialConfig(address="https://vault.example.invalid", auth_path="employees", auth_role="qa")
    await login_oidc(config, command, env={"HOME": str(tmp_path), "VAULT_TOKEN": "do-not-inherit"})
    args, child = json.loads(captured.read_text())
    assert args[0] == "login"
    assert {"-method=oidc", "-path=employees", "-no-print", "role=qa", "-tls-skip-verify=false"} <= set(args)
    assert "VAULT_TOKEN" not in child
    assert "do-not-inherit" not in json.dumps(args)
    assert capfd.readouterr() == ("", "")


@pytest.mark.parametrize("config", [{}, {"authMode": "token-env", "authPath": "employees"}])
async def test_login_requires_explicit_session_auth_path(config, tmp_path):
    with pytest.raises(RuntimeFailure) as error:
        await login_oidc(VaultCredentialConfig(address="https://vault.example.invalid", **config), tmp_path / "absent")
    assert error.value.code == "VAULT_LOGIN_NOT_CONFIGURED"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("import sys; print('secret-token', file=sys.stderr); sys.exit(1)", "VAULT_LOGIN_FAILED"),
        ("import time; time.sleep(20)", "VAULT_LOGIN_TIMEOUT"),
    ],
)
async def test_login_failures_are_bounded_and_safe(tmp_path, capfd, body, code):
    config = VaultCredentialConfig(address="https://vault.example.invalid", auth_path="employees")
    with pytest.raises(RuntimeFailure) as error:
        await login_oidc(config, executable(tmp_path, body), 1, env={"HOME": str(tmp_path)})
    assert error.value.code == code
    assert "secret-token" not in str(error.value)
    assert capfd.readouterr() == ("", "")
