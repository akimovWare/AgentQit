from __future__ import annotations

from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.security import SecretValue


class InMemoryCredentialSource:
    """In-memory credential source for tests and explicit runtime composition."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = {reference: SecretValue(value) for reference, value in values.items()}

    async def get(self, reference: str) -> SecretValue:
        try:
            return self._values[reference]
        except KeyError as error:
            raise RuntimeFailure(
                ExitCode.AUTHENTICATION_FAILED,
                "CREDENTIAL_NOT_FOUND",
                "credential source has no value for the requested reference",
                source="credentials",
            ) from error
