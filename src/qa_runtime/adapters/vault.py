from __future__ import annotations

import os
import signal
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from subprocess import DEVNULL

import anyio
from anyio.abc import Process

from qa_runtime.contracts import ExitCode
from qa_runtime.credential_config import VaultCredentialConfig
from qa_runtime.errors import RuntimeFailure
from qa_runtime.security import SecretValue

_MAX_SECRET_BYTES = 64 * 1024
_CLEANUP_TIMEOUT_SECONDS = 2
_HCP_CONFIG = Path(".config/hcp/hvd/hvd_proxy_config.json")


def _failure(code: str, message: str, *, retryable: bool = False) -> RuntimeFailure:
    return RuntimeFailure(ExitCode.AUTHENTICATION_FAILED, code, message, source="credentials", retryable=retryable)


def vault_connection_args(config: VaultCredentialConfig) -> list[str]:
    """Build fixed connection flags without I/O or credential discovery."""
    args = [
        f"-address={config.address}",
        "-tls-skip-verify=false",
        "-disable-redirects=true",
    ]
    if config.namespace is not None:
        args.append(f"-namespace={config.namespace}")
    if config.ca_bundle is not None:
        args.append(f"-ca-cert={config.ca_bundle}")
    return args


def vault_environment(config: VaultCredentialConfig, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Prepare the child environment at execution time, never during compilation.

    Session mode deliberately uses the local Vault token helper, ignoring VAULT_TOKEN.
    Token mode requires its explicit token before starting Vault, preventing helper
    fallback. Only PATH and HOME are inherited; proxies and ambient Vault/TLS/HCP
    settings are not supported. A trusted custom token helper must work with this
    environment. The configured address cannot be replaced by a cached HCP session.
    """
    parent = os.environ if env is None else env
    child = {"PATH": parent.get("PATH", "/usr/bin:/bin"), "VAULT_ADDR": config.address}
    if config.namespace is not None:
        child["VAULT_NAMESPACE"] = config.namespace
    if config.ca_bundle is not None:
        child["VAULT_CACERT"] = str(config.ca_bundle)
    if config.auth_mode == "token-env":
        token = parent.get("VAULT_TOKEN", "")
        if not token.strip() or any(character.isspace() or ord(character) < 32 for character in token):
            raise _failure("VAULT_TOKEN_REQUIRED", "token-env mode requires a nonempty VAULT_TOKEN")
        child["VAULT_TOKEN"] = token

    # go-homedir, used by Vault, can otherwise discover an implicit OS home.
    home = parent.get("HOME") or str(Path.home())
    if not Path(home).is_absolute() or "\x00" in home:
        raise _failure("VAULT_SESSION_UNSAFE", "Vault requires an absolute local HOME directory")
    child["HOME"] = home
    try:
        (Path(home) / _HCP_CONFIG).lstat()
    except FileNotFoundError:
        pass
    except OSError:
        raise _failure("VAULT_SESSION_UNSAFE", "Vault HCP session configuration could not be checked") from None
    else:
        # Vault applies this file after -address and may replace the destination.
        # Refuse even an empty file or symlink; never read or alter HCP credentials.
        raise _failure(
            "VAULT_SESSION_UNSAFE", "a cached Vault HCP connection can override the configured Vault address"
        )
    return child


async def close_vault_process(process: Process, *, abort: bool) -> None:
    """Close/reap a child; cancellation also terminates its POSIX helper process group."""
    with (
        suppress(Exception, anyio.get_cancelled_exc_class()),
        anyio.move_on_after(_CLEANUP_TIMEOUT_SECONDS, shield=True),
    ):
        if abort or process.returncode is None:
            with suppress(ProcessLookupError):
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
        await process.aclose()


class VaultCliCredentialSource:
    """Read explicitly bound KV fields through an installed, trusted Vault CLI.

    Construction only copies configuration. No token or secret is cached by this
    adapter. The shared Vault login session is managed by Vault's token helper.
    """

    def __init__(
        self,
        config: VaultCredentialConfig,
        *,
        executable: Path = Path("vault"),
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config.model_copy(deep=True)
        self._executable = executable
        self._env = env

    async def get(self, reference: str) -> SecretValue:
        binding = self._config.bindings.get(reference)
        if binding is None:
            raise _failure("VAULT_BINDING_NOT_FOUND", "no Vault binding is configured for this credential reference")
        args = [str(self._executable), "kv", "get", *vault_connection_args(self._config)]
        args.extend([f"-mount={binding.mount}", f"-field={binding.field}"])
        if binding.version is not None:
            args.append(f"-version={binding.version}")
        args.append(binding.path)
        child_env = vault_environment(self._config, self._env)
        try:
            with anyio.fail_after(self._config.timeout_seconds):
                process = await anyio.open_process(
                    args,
                    stdin=DEVNULL,
                    stderr=DEVNULL,
                    env=child_env,
                    start_new_session=os.name == "posix",
                )
                completed = False
                try:
                    output = bytearray()
                    assert process.stdout is not None
                    async for chunk in process.stdout:
                        if len(output) + len(chunk) > _MAX_SECRET_BYTES:
                            raise _failure("VAULT_CREDENTIAL_INVALID", "Vault returned an oversized credential value")
                        output.extend(chunk)
                    returncode = await process.wait()
                    completed = True
                finally:
                    await close_vault_process(process, abort=not completed)
        except TimeoutError:
            raise _failure("VAULT_TIMEOUT", "Vault did not respond in time", retryable=True) from None
        except (OSError, ValueError):
            raise _failure("VAULT_UNAVAILABLE", "Vault CLI could not be started or read") from None
        if returncode != 0:
            raise _failure("VAULT_READ_FAILED", "Vault could not read the configured credential field")
        try:
            # -field emits the value without an added newline. Preserve significant
            # whitespace and multiline credentials instead of silently rewriting them.
            value = output.decode("utf-8")
        except UnicodeDecodeError:
            raise _failure("VAULT_CREDENTIAL_INVALID", "Vault returned a non-text credential value") from None
        if not value:
            raise _failure("VAULT_CREDENTIAL_INVALID", "Vault returned an empty credential value")
        return SecretValue(value)
