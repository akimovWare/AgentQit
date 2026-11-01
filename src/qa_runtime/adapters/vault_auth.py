from __future__ import annotations

import math
import os
from collections.abc import Mapping
from pathlib import Path
from subprocess import DEVNULL

import anyio

from qa_runtime.adapters.vault import close_vault_process, vault_connection_args, vault_environment
from qa_runtime.contracts import ExitCode
from qa_runtime.credential_config import VaultCredentialConfig
from qa_runtime.errors import RuntimeFailure


async def login_oidc(
    config: VaultCredentialConfig,
    executable: Path = Path("vault"),
    timeout_seconds: float = 120,
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Explicit browser login, separate from all compilation and credential reads."""
    if config.auth_mode != "session" or config.auth_path is None:
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "VAULT_LOGIN_NOT_CONFIGURED",
            "OIDC login requires credentials.authMode=session and credentials.authPath",
            source="credentials",
        )
    if not math.isfinite(timeout_seconds) or not 1 <= timeout_seconds <= 120:
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "VAULT_LOGIN_TIMEOUT_INVALID",
            "login timeout must be between 1 and 120 seconds",
            source="credentials",
        )
    args = [
        str(executable),
        "login",
        *vault_connection_args(config),
        "-method=oidc",
        f"-path={config.auth_path}",
        "-no-print",
        "abort_on_error=true",
    ]
    if config.auth_role is not None:
        args.append(f"role={config.auth_role}")
    child_env = vault_environment(config, env)
    # Linux desktop browser launchers need these non-secret session coordinates.
    parent = os.environ if env is None else env
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        if name in parent:
            child_env[name] = parent[name]
    try:
        with anyio.fail_after(timeout_seconds):
            process = await anyio.open_process(
                args,
                env=child_env,
                stdin=DEVNULL,
                stdout=DEVNULL,
                stderr=DEVNULL,
                start_new_session=os.name == "posix",
            )
            completed = False
            try:
                returncode = await process.wait()
                completed = True
            finally:
                await close_vault_process(process, abort=not completed)
    except TimeoutError:
        raise RuntimeFailure(
            ExitCode.AUTHENTICATION_FAILED,
            "VAULT_LOGIN_TIMEOUT",
            "Vault OIDC login did not complete in time; finish browser authentication and retry",
            source="credentials",
            retryable=True,
        ) from None
    except (OSError, ValueError):
        raise RuntimeFailure(
            ExitCode.AUTHENTICATION_FAILED,
            "VAULT_UNAVAILABLE",
            "Vault CLI could not be started",
            source="credentials",
        ) from None
    if returncode != 0:
        raise RuntimeFailure(
            ExitCode.AUTHENTICATION_FAILED,
            "VAULT_LOGIN_FAILED",
            "Vault OIDC login failed; check the auth path, browser availability and trusted CA configuration",
            source="credentials",
        )
