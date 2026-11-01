from __future__ import annotations

import os
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import anyio
import pytest

from qa_runtime.adapters.vault import VaultCliCredentialSource, vault_environment
from qa_runtime.contracts import ExitCode
from qa_runtime.credential_config import VaultCredentialConfig, VaultSecretBinding
from qa_runtime.errors import RuntimeFailure

pytestmark = pytest.mark.anyio


def _config(**overrides: Any) -> VaultCredentialConfig:
    fields: dict[str, Any] = {
        "address": "https://vault.example.test:8200",
        "bindings": {"service/password": VaultSecretBinding(mount="team/kv", path="qa/backend", field="password")},
    }
    fields.update(overrides)
    return VaultCredentialConfig(**fields)


def _executable(tmp_path: Path, body: str) -> Path:
    executable = tmp_path / "fake-vault"
    executable.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    executable.chmod(0o700)
    return executable


class _Environment(Mapping[str, str]):
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.reads: list[str] = []

    def __getitem__(self, key: str) -> str:
        self.reads.append(key)
        return self.values[key]

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("must not copy or enumerate the parent environment")

    def __len__(self) -> int:
        return len(self.values)


async def test_vault_session_is_lazy_and_preserves_exact_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _executable(tmp_path, "import sys; sys.stdout.write('  secret\\nsecond line\\n')")
    env = _Environment({"HOME": str(tmp_path), "VAULT_TOKEN": "unrelated-user-token"})
    started: list[dict[str, Any]] = []
    original = anyio.open_process

    async def capture(command: list[str], **kwargs: Any):
        started.append({"command": command, **kwargs})
        return await original(command, **kwargs)

    monkeypatch.setattr(anyio, "open_process", capture)
    source = VaultCliCredentialSource(_config(), executable=executable, env=env)
    assert env.reads == []
    assert started == []

    secret = await source.get("service/password")

    assert secret.reveal() == "  secret\nsecond line\n"
    assert "secret" not in repr(secret)
    assert "VAULT_TOKEN" not in env.reads
    assert started[0]["env"] == {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "VAULT_ADDR": "https://vault.example.test:8200",
    }
    assert started[0]["command"] == [
        str(executable),
        "kv",
        "get",
        "-address=https://vault.example.test:8200",
        "-tls-skip-verify=false",
        "-disable-redirects=true",
        "-mount=team/kv",
        "-field=password",
        "qa/backend",
    ]


async def test_vault_token_env_is_explicit_and_connection_comes_only_from_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _executable(tmp_path, "import sys; sys.stdout.write('service-value')")
    env = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "VAULT_ADDR": "http://wrong.test",
        "VAULT_NAMESPACE": "wrong",
        "VAULT_SKIP_VERIFY": "true",
        "VAULT_AGENT_ADDR": "http://wrong.test",
        "VAULT_PROXY_ADDR": "http://wrong.test",
        "VAULT_CLIENT_KEY": "/wrong/key",
        "VAULT_CACERT": "/wrong/ca",
        "VAULT_HEADERS": "X-Private:private-header",
        "VAULT_LOG_LEVEL": "trace",
        "VAULT_FORMAT": "json",
        "VAULT_CONFIG_PATH": "/wrong/config",
        "HTTPS_PROXY": "http://wrong.test",
        "http_proxy": "http://wrong.test",
        "SSL_CERT_FILE": "/wrong/ca",
        "HCP_CACHE_TEST_MODE": "true",
        "ANOTHER_SERVICE_TOKEN": "private-unrelated-token",
    }
    config = _config(
        auth_mode="token-env",
        namespace="division/team",
        ca_bundle=tmp_path / "trusted.pem",
        bindings={
            "service/password": VaultSecretBinding(mount="kv", path="qa/backend", field="-field=literal", version=3)
        },
    )
    started: list[dict[str, Any]] = []
    original = anyio.open_process

    async def capture(command: list[str], **kwargs: Any):
        started.append({"command": command, **kwargs})
        return await original(command, **kwargs)

    monkeypatch.setattr(anyio, "open_process", capture)
    source = VaultCliCredentialSource(config, executable=executable, env=env)
    # Token injection happens after construction, including in service integrations.
    env["VAULT_TOKEN"] = "synthetic-ci-token"
    assert (await source.get("service/password")).reveal() == "service-value"
    assert started[0]["env"] == {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "VAULT_TOKEN": "synthetic-ci-token",
        "VAULT_ADDR": "https://vault.example.test:8200",
        "VAULT_NAMESPACE": "division/team",
        "VAULT_CACERT": str(tmp_path / "trusted.pem"),
    }
    argv = started[0]["command"]
    assert "-namespace=division/team" in argv
    assert f"-ca-cert={tmp_path / 'trusted.pem'}" in argv
    assert argv[-4:] == ["-mount=kv", "-field=-field=literal", "-version=3", "qa/backend"]
    assert "synthetic-ci-token" not in repr(argv)


@pytest.mark.parametrize("token", [None, "", "   ", "invalid\ntoken", "invalid\x00token"])
async def test_token_env_never_falls_back_to_local_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str | None
) -> None:
    def forbidden(*args: Any, **kwargs: Any):
        raise AssertionError("missing token must fail before a child starts")

    monkeypatch.setattr(anyio, "open_process", forbidden)
    env = {"HOME": str(tmp_path)}
    (tmp_path / ".vault-token").write_text("unrelated-session-token")
    if token is not None:
        env["VAULT_TOKEN"] = token
    with pytest.raises(RuntimeFailure) as raised:
        await VaultCliCredentialSource(_config(auth_mode="token-env"), env=env).get("service/password")
    assert raised.value.code == "VAULT_TOKEN_REQUIRED"
    assert "unrelated-session-token" not in str(raised.value)


async def test_unbound_reference_fails_before_environment_or_process_access(tmp_path: Path) -> None:
    env = _Environment({})
    with pytest.raises(RuntimeFailure) as raised:
        await VaultCliCredentialSource(_config(), executable=tmp_path / "absent", env=env).get("private-ref")
    assert raised.value.code == "VAULT_BINDING_NOT_FOUND"
    assert "private-ref" not in str(raised.value)
    assert env.reads == []


@pytest.mark.parametrize("auth_mode", ["session", "token-env"])
async def test_hcp_address_override_is_refused_without_reading_cache(tmp_path: Path, auth_mode: str) -> None:
    cache = tmp_path / ".config/hcp/hvd/hvd_proxy_config.json"
    cache.parent.mkdir(parents=True)
    # Even a broken symlink is not a safe basis for a pinned Vault destination.
    cache.symlink_to(tmp_path / "missing-sensitive-cache")
    with pytest.raises(RuntimeFailure) as raised:
        vault_environment(_config(auth_mode=auth_mode), {"HOME": str(tmp_path), "VAULT_TOKEN": "ci-token"})
    assert raised.value.code == "VAULT_SESSION_UNSAFE"
    assert cache.is_symlink()


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("import time; time.sleep(10)", "VAULT_TIMEOUT"),
        ("import sys; sys.stdout.buffer.write(b'\\xff')", "VAULT_CREDENTIAL_INVALID"),
        ("import sys; sys.stdout.write('x' * 65537)", "VAULT_CREDENTIAL_INVALID"),
        ("pass", "VAULT_CREDENTIAL_INVALID"),
        (
            "import sys; sys.stderr.write('stderr-secret' * 100000); sys.stdout.write('stdout-secret'); sys.exit(1)",
            "VAULT_READ_FAILED",
        ),
    ],
)
async def test_vault_failures_are_bounded_and_do_not_disclose_output(tmp_path: Path, body: str, code: str) -> None:
    timeout = 0.2 if code == "VAULT_TIMEOUT" else 5
    source = VaultCliCredentialSource(
        _config(timeout_seconds=timeout),
        executable=_executable(tmp_path, body),
        env={"HOME": str(tmp_path)},
    )
    with pytest.raises(RuntimeFailure) as raised:
        await source.get("service/password")
    assert raised.value.code == code
    assert raised.value.exit_code == ExitCode.AUTHENTICATION_FAILED
    assert raised.value.retryable == (code == "VAULT_TIMEOUT")
    result = raised.value.as_error_item().model_dump_json()
    assert "stdout-secret" not in result
    assert "stderr-secret" not in result
    assert "service/password" not in result


async def test_vault_accepts_exact_output_limit_and_discards_large_stderr(tmp_path: Path) -> None:
    source = VaultCliCredentialSource(
        _config(),
        executable=_executable(
            tmp_path, "import sys; sys.stderr.write('diagnostic' * 100000); sys.stdout.write('x' * 65536)"
        ),
        env={"HOME": str(tmp_path)},
    )
    assert len((await source.get("service/password")).reveal()) == 65536


async def test_vault_missing_executable_is_normalized(tmp_path: Path) -> None:
    source = VaultCliCredentialSource(
        _config(), executable=tmp_path / "absent-sensitive-path", env={"HOME": str(tmp_path)}
    )
    with pytest.raises(RuntimeFailure) as raised:
        await source.get("service/password")
    assert raised.value.code == "VAULT_UNAVAILABLE"
    assert "absent-sensitive-path" not in str(raised.value)


async def test_cancellation_kills_and_reaps_vault_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ready = anyio.Event()
    children = []
    original = anyio.open_process

    async def capture(*args: Any, **kwargs: Any):
        process = await original(*args, **kwargs)
        children.append(process)
        ready.set()
        return process

    monkeypatch.setattr(anyio, "open_process", capture)
    source = VaultCliCredentialSource(
        _config(),
        executable=_executable(tmp_path, "import time; time.sleep(30)"),
        env={"HOME": str(tmp_path)},
    )
    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(source.get, "service/password")
            await ready.wait()
            group.cancel_scope.cancel()
    assert len(children) == 1
    assert children[0].returncode is not None
    if os.name == "posix":
        with pytest.raises(ProcessLookupError):
            os.kill(children[0].pid, 0)
