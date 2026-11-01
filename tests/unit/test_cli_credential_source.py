from __future__ import annotations

import sys
from pathlib import Path

import anyio
import pytest

from qa_cli.app import _credential_source
from qa_runtime.adapters.credential_helper import SubprocessCredentialSource, UnavailableCredentialSource
from qa_runtime.errors import RuntimeFailure


@pytest.mark.anyio
async def test_cli_without_explicit_source_does_not_use_macos_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    async def forbidden(*args, **kwargs):
        raise AssertionError("unconfigured credentials must not start an OS credential helper")

    monkeypatch.setattr(anyio, "open_process", forbidden)
    source = _credential_source(None)
    assert isinstance(source, UnavailableCredentialSource)
    with pytest.raises(RuntimeFailure) as raised:
        await source.get("service/credential")
    assert raised.value.code == "CREDENTIAL_SOURCE_NOT_CONFIGURED"


@pytest.mark.anyio
async def test_cli_uses_explicit_credential_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    helper = tmp_path / "helper"
    helper.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "assert sys.argv[1:] == ['get', 'service/credential']\n"
        "sys.stdout.write('synthetic-service-secret')\n",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    source = _credential_source(helper)
    assert isinstance(source, SubprocessCredentialSource)
    assert (await source.get("service/credential")).reveal() == "synthetic-service-secret"
