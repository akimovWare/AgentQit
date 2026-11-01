from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_PROFILE = "qa"
DEFAULT_OUTPUT = "json"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QA_CLI_", extra="ignore")

    profile: str = DEFAULT_PROFILE
    profiles_dir: Path = Field(default_factory=lambda: Path.home() / ".qa-cli" / "profiles")
    product: str | None = None
    registry_path: Path | None = None
    credential_helper: Path | None = None
    vault_executable: Path = Path("vault")
    output: str = DEFAULT_OUTPUT
