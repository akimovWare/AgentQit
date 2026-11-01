from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, TypeAdapter, field_validator

_HTTP_URL = TypeAdapter(AnyHttpUrl)


def _single_line(value: str) -> str:
    if not value.strip() or any(
        unicodedata.category(character).startswith("C") or character in "\u2028\u2029" for character in value
    ):
        raise ValueError("value must be a nonempty single-line name without control characters")
    return value


def _vault_path(value: str) -> str:
    """Validate path segments, including encoded forms, without rewriting a Vault name."""
    decoded = value
    for _ in range(16):
        _single_line(decoded)
        parts = decoded.split("/")
        if any(character in decoded for character in "\\?#") or any(
            not part or part in {".", ".."} or part.startswith("-") or part != part.strip() for part in parts
        ):
            raise ValueError("Vault paths must use safe relative segments without options or traversal")
        expanded = unquote(decoded, errors="strict")
        if expanded == decoded:
            return value
        decoded = expanded
    raise ValueError("Vault path encoding is too deeply nested")


class VaultSecretBinding(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", hide_input_in_errors=True)

    mount: str
    path: str
    field: str
    version: int | None = Field(default=None, gt=0, strict=True)

    @field_validator("mount", "path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _vault_path(value)

    @field_validator("field")
    @classmethod
    def validate_field(cls, value: str) -> str:
        # The adapter passes this name in one -field=<value> argument.
        return _single_line(value)


class EnvironmentCredentialConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    provider: Literal["env"]
    bindings: dict[str, str] = Field(default_factory=dict)

    @field_validator("bindings")
    @classmethod
    def validate_bindings(cls, values: dict[str, str]) -> dict[str, str]:
        for reference, variable in values.items():
            _single_line(reference)
            if reference != reference.strip() or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", variable) is None:
                raise ValueError("bindings require trimmed references and explicit environment variable names")
        return values


class VaultCredentialConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", hide_input_in_errors=True)

    provider: Literal["vault-cli"] = "vault-cli"
    address: str
    auth_mode: Literal["session", "token-env"] = Field(default="session", alias="authMode")
    namespace: str | None = None
    ca_bundle: Path | None = Field(default=None, alias="caBundle")
    auth_path: str | None = Field(default=None, alias="authPath")
    auth_role: str | None = Field(default=None, alias="authRole")
    timeout_seconds: float = Field(default=30, gt=0, le=120, allow_inf_nan=False, alias="timeoutSeconds")
    bindings: dict[str, VaultSecretBinding] = Field(default_factory=dict)

    @field_validator("address")
    @classmethod
    def validate_address(cls, value: str) -> str:
        try:
            _single_line(value)
            if any(character.isspace() for character in value):
                raise ValueError("invalid address")
            _HTTP_URL.validate_python(value)
            parsed = urlsplit(value)
            valid_port = parsed.port is None or 0 < parsed.port <= 65535
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or "?" in value
                or "#" in value
                or not valid_port
                or parsed.netloc.endswith(":")
            ):
                raise ValueError("invalid address")
        except ValueError as error:
            raise ValueError(
                "Vault address must be an HTTPS URL with a hostname and valid port, "
                "without credentials, query or fragment"
            ) from error
        return value

    @field_validator("namespace", "auth_path")
    @classmethod
    def validate_optional_path(cls, value: str | None) -> str | None:
        return _vault_path(value) if value is not None else None

    @field_validator("auth_role")
    @classmethod
    def validate_role(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _single_line(value)
        # Vault login parses role=<value> as K=V data: @ reads a file, - reads
        # stdin, and \@ changes the literal value rather than preserving it.
        if value == "-" or value.startswith(("@", "\\@")):
            raise ValueError("authRole must be a literal name without file or stdin substitution")
        return value

    @field_validator("ca_bundle")
    @classmethod
    def validate_ca_bundle(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        _single_line(str(value))
        path = value.expanduser()
        if not path.is_absolute():
            raise ValueError("caBundle must contain an absolute local path")
        return path

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def reject_boolean_timeout(cls, value):
        if isinstance(value, bool):
            raise ValueError("timeoutSeconds must be a finite number")
        return value

    @field_validator("bindings")
    @classmethod
    def validate_references(cls, values: dict[str, VaultSecretBinding]) -> dict[str, VaultSecretBinding]:
        for reference in values:
            _single_line(reference)
        return values
