from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import HttpRequest
from qa_runtime.ports.auth import AuthContext
from qa_runtime.ports.credentials import CredentialSource
from qa_runtime.security import SecretRedactor


class HeaderAuthConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", hide_input_in_errors=True)

    kind: Literal["bearer", "api-key"]
    credential_ref: str = Field(alias="credentialRef", min_length=1, max_length=256)
    header_name: str = Field(default="X-API-Key", alias="headerName")

    @field_validator("header_name")
    @classmethod
    def validate_header_name(cls, value: str) -> str:
        if re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", value) is None or value.lower() in {
            "host",
            "content-length",
            "connection",
            "transfer-encoding",
            "traceparent",
            "cookie",
            "set-cookie",
        }:
            raise ValueError("auth header must be a valid credential header, not transport metadata")
        return value


class HeaderAuthProvider:
    def __init__(self, config: HeaderAuthConfig, credentials: CredentialSource, redactor: SecretRedactor) -> None:
        self._config, self._credentials, self._redactor = config, credentials, redactor

    async def authorize(self, request: HttpRequest, context: AuthContext) -> HttpRequest:
        secret = await self._credentials.get(self._config.credential_ref)
        self._redactor.register(secret)
        value = secret.reveal()
        if any(ord(character) < 32 or ord(character) > 126 for character in value):
            raise RuntimeFailure(
                ExitCode.AUTHENTICATION_FAILED,
                "HEADER_CREDENTIAL_INVALID",
                "The configured credential cannot be encoded as an HTTP header",
                source="auth",
                execution_outcome="not_sent",
            )
        name = "Authorization" if self._config.kind == "bearer" else self._config.header_name
        value = f"Bearer {value}" if self._config.kind == "bearer" else value
        headers = {key: item for key, item in request.headers.items() if key.lower() != name.lower()}
        return request.model_copy(update={"headers": {**headers, name: value}})

    async def invalidate(self, context: AuthContext) -> None:
        pass

    async def aclose(self) -> None:
        pass
