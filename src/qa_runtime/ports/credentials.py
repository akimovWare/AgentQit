from __future__ import annotations

from typing import Protocol

from qa_runtime.security import SecretValue


class CredentialSource(Protocol):
    async def get(self, reference: str) -> SecretValue: ...
