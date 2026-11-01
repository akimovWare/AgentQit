from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from subprocess import DEVNULL

import anyio

from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.security import SecretValue


class UnavailableCredentialSource:
    async def get(self, reference: str) -> SecretValue:
        raise RuntimeFailure(
            ExitCode.AUTHENTICATION_FAILED,
            "CREDENTIAL_SOURCE_NOT_CONFIGURED",
            "configure profile credentials for Vault or explicitly select a trusted credential helper",
            source="credentials",
        )


class SubprocessCredentialSource:
    """Reads one secret from a trusted executable using `helper get <reference>`."""

    def __init__(self, executable: Path, *, timeout_seconds: float = 30) -> None:
        self._executable = executable
        self._timeout_seconds = timeout_seconds

    async def get(self, reference: str) -> SecretValue:
        try:
            with anyio.fail_after(self._timeout_seconds):
                async with await anyio.open_process(
                    [str(self._executable), "get", reference],
                    stdin=DEVNULL,
                    stderr=DEVNULL,
                    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                ) as process:
                    try:
                        output = bytearray()
                        assert process.stdout is not None
                        async for chunk in process.stdout:
                            if len(output) + len(chunk) > 64 * 1024:
                                raise self._invalid("credential helper returned an oversized value")
                            output.extend(chunk)
                        returncode = await process.wait()
                    finally:
                        if process.returncode is None:
                            with suppress(ProcessLookupError):
                                process.kill()
        except TimeoutError as error:
            raise RuntimeFailure(
                ExitCode.AUTHENTICATION_FAILED,
                "CREDENTIAL_HELPER_TIMEOUT",
                "credential helper did not respond in time",
                source="credentials",
                retryable=True,
            ) from error
        except OSError as error:
            raise RuntimeFailure(
                ExitCode.AUTHENTICATION_FAILED,
                "CREDENTIAL_HELPER_UNAVAILABLE",
                "credential helper could not be started",
                source="credentials",
            ) from error
        if returncode != 0:
            raise RuntimeFailure(
                ExitCode.AUTHENTICATION_FAILED,
                "CREDENTIAL_HELPER_REJECTED",
                "credential helper rejected the requested reference",
                source="credentials",
            )
        try:
            value = output.decode("utf-8").strip()
        except UnicodeDecodeError as error:
            raise self._invalid("credential helper returned a non-text value") from error
        if not value:
            raise self._invalid("credential helper returned no value for the requested reference")
        if "\n" in value or "\r" in value:
            raise self._invalid("credential helper must return a single line without log messages")
        return SecretValue(value)

    @staticmethod
    def _invalid(message: str) -> RuntimeFailure:
        return RuntimeFailure(
            ExitCode.AUTHENTICATION_FAILED, "CREDENTIAL_HELPER_INVALID", message, source="credentials"
        )
