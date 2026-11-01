from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass
from threading import RLock
from typing import Any

from qa_runtime.contracts import CommandResult

SENSITIVE_KEYS = {
    "accesstoken",
    "apikey",
    "apitoken",
    "authorization",
    "clientsecret",
    "cookie",
    "credential",
    "credentials",
    "authtoken",
    "idtoken",
    "keystorepassword",
    "password",
    "passwordhash",
    "passphrase",
    "privatekey",
    "proxyauthorization",
    "refreshtoken",
    "secret",
    "secretkey",
    "sessiontoken",
    "setcookie",
    "token",
    "truststorepassword",
}


@dataclass(frozen=True, repr=False, slots=True)
class SecretValue:
    _value: str

    def __post_init__(self) -> None:
        if not self._value:
            raise ValueError("secret value must not be empty")

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SecretValue(***)"


class SecretRedactor:
    def __init__(self) -> None:
        self._values: set[str] = set()
        self._lock = RLock()

    def register(self, secret: SecretValue | str) -> None:
        value = secret.reveal() if isinstance(secret, SecretValue) else secret
        if value:
            with self._lock:
                self._values.add(value)

    def redact(self, value: Any) -> Any:
        if isinstance(value, SecretValue):
            return "***"
        if isinstance(value, Mapping):
            if value.get("encoding") == "base64" and isinstance(value.get("data"), str):
                try:
                    original = base64.b64decode(value["data"], validate=True)
                except (ValueError, binascii.Error):
                    pass
                else:
                    masked = self.redact(original)
                    if masked != original:
                        value = dict(value)
                        value["data"] = base64.b64encode(masked).decode("ascii")
                        value["redacted"] = True
            sensitive_named_value = self._sensitive_key(value.get("name", "")) and "value" in value
            return {
                self.redact_text(key) if isinstance(key, str) else key: (
                    "***"
                    if self._sensitive_key(key) or (sensitive_named_value and key == "value")
                    else self.redact(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact(item) for item in value)
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, bytes):
            with self._lock:
                secrets = sorted(self._values, key=len, reverse=True)
            for secret in secrets:
                value = value.replace(secret.encode("utf-8"), b"***")
            return value
        return value

    def redact_text(self, value: str) -> str:
        with self._lock:
            secrets = sorted(self._values, key=len, reverse=True)
        for secret in secrets:
            value = value.replace(secret, "***")
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except (ValueError, RecursionError):
                pass
            else:
                masked = self.redact(parsed)
                if masked != parsed:
                    value = json.dumps(masked, ensure_ascii=False)
        return value

    @staticmethod
    def _sensitive_key(key: Any) -> bool:
        normalized = "".join(character for character in str(key).lower() if character.isalnum())
        return normalized in SENSITIVE_KEYS or normalized.endswith("authtoken")


def redact_command_result(result: CommandResult, redactor: SecretRedactor | None = None) -> CommandResult:
    """Sanitize every free-form public field without changing protocol enums or numbers."""
    selected = redactor if redactor is not None else SecretRedactor()
    updates = {
        name: selected.redact(getattr(result, name))
        for name in ("command", "run_id", "environment", "result", "evidence", "warnings")
    }
    if result.error is not None:
        updates["error"] = result.error.model_copy(
            update={
                name: selected.redact(getattr(result.error, name))
                for name in ("code", "message", "source", "operation", "details", "hint", "documentation")
            }
        )
    if result.correlation is not None:
        updates["correlation"] = result.correlation.model_copy(
            update={
                name: selected.redact_text(getattr(result.correlation, name)) for name in ("trace_id", "traceparent")
            }
        )
    return result.model_copy(update=updates)
