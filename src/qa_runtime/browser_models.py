from __future__ import annotations

from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def http_origin(value: str) -> str:
    if "\\" in value or any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("browser URLs must not contain whitespace or backslashes")
    parsed = urlsplit(value)
    if parsed.netloc.endswith(":"):
        raise ValueError("browser URL port must not be empty")
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("browser targets require an HTTP(S) origin without credentials")
    port = parsed.port
    if port is not None and not 0 < port <= 65535:
        raise ValueError("browser URL port must be valid")
    host = parsed.hostname.lower()
    host = f"[{host}]" if ":" in host else host.encode("idna").decode("ascii")
    suffix = f":{port}" if port is not None and port != (443 if parsed.scheme == "https" else 80) else ""
    return f"{parsed.scheme}://{host}{suffix}"


def safe_observation_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            return "<redacted-url>"
        query = urlencode([(name, "***") for name, _ in parse_qsl(parsed.query, max_num_fields=100)])
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))[:4096]
    except ValueError:
        return "<invalid-url>"


class BrowserSettings(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", hide_input_in_errors=True, allow_inf_nan=False)

    allowed_origins: list[str] = Field(alias="allowedOrigins", min_length=1, max_length=32)
    browser: Literal["chromium", "firefox", "webkit"] = "chromium"
    headless: bool = True
    action_timeout_seconds: float = Field(default=20, gt=0, le=120, alias="actionTimeoutSeconds", strict=True)
    max_pages: int = Field(default=4, ge=1, le=16, alias="maxPages", strict=True)
    max_elements: int = Field(default=100, ge=1, le=500, alias="maxElements", strict=True)
    max_text_characters: int = Field(default=12000, ge=100, le=50000, alias="maxTextCharacters", strict=True)
    viewport_width: int = Field(default=1280, ge=320, le=1920, alias="viewportWidth", strict=True)
    viewport_height: int = Field(default=720, ge=240, le=1080, alias="viewportHeight", strict=True)
    trace: bool = False
    credential_refs: list[str] = Field(default_factory=list, alias="credentialRefs", max_length=64)

    @field_validator("allowed_origins")
    @classmethod
    def normalize_origins(cls, values: list[str]) -> list[str]:
        result = []
        for value in values:
            parsed = urlsplit(value)
            if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                raise ValueError("allowedOrigins must contain origins without paths, queries, or fragments")
            result.append(http_origin(value))
        return sorted(set(result))


class SessionSettings(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", allow_inf_nan=False)

    max_active: int = Field(default=4, ge=1, le=16, strict=True, alias="maxActive")
    max_total: int = Field(default=128, ge=1, le=1024, strict=True, alias="maxTotal")
    max_operations: int = Field(default=200, ge=1, le=2000, strict=True, alias="maxOperations")
    lifetime_seconds: float = Field(default=900, gt=0, le=3600, strict=True, alias="lifetimeSeconds")
    idle_seconds: float = Field(default=300, gt=0, le=3600, strict=True, alias="idleSeconds")
    max_artifact_bytes: int = Field(
        default=10 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024, strict=True, alias="maxArtifactBytes"
    )
    max_total_artifact_bytes: int = Field(
        default=50 * 1024 * 1024, ge=1024, le=200 * 1024 * 1024, strict=True, alias="maxTotalArtifactBytes"
    )


class BrowserCommand(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", hide_input_in_errors=True)

    action: Literal["observe", "navigate", "click", "fill", "select", "screenshot"]
    page_id: str | None = Field(default=None, alias="pageId", min_length=1, max_length=80)
    snapshot_id: str | None = Field(default=None, alias="snapshotId", min_length=1, max_length=80)
    element_ref: str | None = Field(default=None, alias="elementRef", min_length=1, max_length=80)
    url: str | None = Field(default=None, max_length=4096)
    value: str | None = Field(default=None, max_length=8192, repr=False)
    credential_ref: str | None = Field(default=None, alias="credentialRef", min_length=1, max_length=256)
    offset: int = Field(default=0, ge=0, le=10000, strict=True)
    dialog_policy: Literal["dismiss", "accept"] = Field(default="dismiss", alias="dialogPolicy")

    @model_validator(mode="after")
    def validate_action(self) -> BrowserCommand:
        if self.action != "observe" and self.offset:
            raise ValueError("offset is only supported for observations")
        if self.action in {"observe", "screenshot"} and self.dialog_policy != "dismiss":
            raise ValueError("dialog policy belongs to consequential actions")
        if self.action == "navigate":
            if self.url is None:
                raise ValueError("navigate requires url")
            http_origin(self.url)
        elif self.url is not None:
            raise ValueError("url is only supported for navigation")
        if self.action in {"click", "fill", "select"}:
            if self.element_ref is None or self.snapshot_id is None:
                raise ValueError("element actions require a reference and its snapshot")
        elif self.element_ref is not None or self.snapshot_id is not None:
            raise ValueError("element references are only supported for element actions")
        if self.action == "fill":
            if (self.value is None) == (self.credential_ref is None):
                raise ValueError("fill requires exactly one text value or credential reference")
        elif self.credential_ref is not None:
            raise ValueError("credential references are only supported for fill")
        if self.action == "select":
            if self.value is None:
                raise ValueError("select requires value")
        elif self.action != "fill" and self.value is not None:
            raise ValueError("value is only supported for fill/select")
        return self


class BrowserAction(BrowserCommand):
    action: Literal["navigate", "click", "fill", "select"]
