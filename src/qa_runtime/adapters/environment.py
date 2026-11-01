from __future__ import annotations

import os
from collections.abc import Mapping

from qa_runtime.contracts import ExitCode
from qa_runtime.credential_config import EnvironmentCredentialConfig
from qa_runtime.errors import RuntimeFailure
from qa_runtime.security import SecretValue


class EnvironmentCredentialSource:
    """Resolve only explicitly mapped names, lazily and without provider fallback."""

    def __init__(self, config: EnvironmentCredentialConfig, env: Mapping[str, str] | None = None) -> None:
        self._bindings = dict(config.bindings)
        self._env = os.environ if env is None else env

    async def get(self, reference: str) -> SecretValue:
        variable = self._bindings.get(reference)
        value = self._env.get(variable) if variable is not None else None
        if not value:
            raise RuntimeFailure(
                ExitCode.AUTHENTICATION_FAILED,
                "ENV_CREDENTIAL_UNAVAILABLE",
                "The requested credential has no configured nonempty environment value",
                source="credentials",
                execution_outcome="not_sent",
            )
        return SecretValue(value)
