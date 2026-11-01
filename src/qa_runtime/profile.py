from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import ValidationError

from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import Profile

ENV_PLACEHOLDER = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)}")


class ProfileLoader:
    def __init__(self, profiles_dir: Path, env: Mapping[str, str] | None = None) -> None:
        self._profiles_dir = profiles_dir
        self._env = os.environ if env is None else env

    def load(self, profile_name: str) -> Profile:
        path = self._profiles_dir / f"{profile_name}.yaml"
        if not path.is_file():
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "PROFILE_NOT_FOUND",
                f"profile '{profile_name}' not found at {path}",
                source="profile",
            )
        try:
            raw = path.read_text(encoding="utf-8")
            resolved = ENV_PLACEHOLDER.sub(self._replace_env(profile_name), raw)
            return Profile.model_validate(yaml.safe_load(resolved))
        except RuntimeFailure:
            raise
        except ValidationError as error:
            # Pydantic's default text includes rejected input, which can contain
            # accidentally pasted credentials before a redactor knows them.
            issues = "; ".join(
                f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
                for issue in error.errors(include_input=False, include_context=False, include_url=False)
            )
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "PROFILE_INVALID",
                f"profile '{profile_name}' is invalid: {issues}",
                source="profile",
            ) from error
        except (OSError, UnicodeError, yaml.YAMLError, TypeError) as error:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "PROFILE_INVALID",
                f"profile '{profile_name}' must contain readable UTF-8 YAML matching the profile schema",
                source="profile",
            ) from error

    def _replace_env(self, profile_name: str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            value = self._env.get(name)
            if value is None:
                raise RuntimeFailure(
                    ExitCode.INVALID_INPUT,
                    "PROFILE_INVALID",
                    f"profile '{profile_name}' references environment variable '{name}' which is not set",
                    source="profile",
                )
            return value

        return replace
