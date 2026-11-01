from __future__ import annotations

import importlib.util
import sys
import time
from datetime import UTC, datetime
from enum import StrEnum
from importlib.metadata import version
from typing import Annotated

import anyio
import typer

from qa_cli.parsing import StructuredTyperGroup
from qa_cli.renderers import render
from qa_runtime.application import failure_result, local_result
from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure

browser_app = typer.Typer(
    help="Explicit browser provisioning; offline checks never install browsers.", cls=StructuredTyperGroup
)


class BrowserName(StrEnum):
    CHROMIUM = "chromium"
    FIREFOX = "firefox"
    WEBKIT = "webkit"


@browser_app.command("install")
def install(
    browser: Annotated[BrowserName, typer.Option("--browser")] = BrowserName.CHROMIUM,
    headless_only: Annotated[bool, typer.Option("--headless-only")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    output: Annotated[str, typer.Option("--output")] = "json",
) -> None:
    """Provision browser binaries using the Playwright version installed in this exact Python environment."""
    started_at, started = datetime.now(UTC), time.monotonic()

    async def provision():
        if importlib.util.find_spec("playwright") is None:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "BROWSER_DEPENDENCY_MISSING",
                "Install agentqit[browser] first",
                source="browser",
            )
        command = [sys.executable, "-m", "playwright", "install", browser.value, "--no-progress"]
        if headless_only:
            command.append("--only-shell")
        if dry_run:
            command.append("--dry-run")
        with anyio.fail_after(600):
            completed = await anyio.run_process(command, check=False)
        if completed.returncode:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "BROWSER_INSTALL_FAILED",
                "Browser provisioning failed",
                source="browser",
                details={"return_code": completed.returncode},
                hint="Run Python's playwright module in the same environment for private installer diagnostics",
            )
        return local_result(
            "browser install",
            {
                "browser": browser.value,
                "playwright_version": version("playwright"),
                "headless_only": headless_only,
                "dry_run": dry_run,
            },
        )

    try:
        result = anyio.run(provision)
    except TimeoutError:
        result = failure_result(
            "browser install",
            RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "BROWSER_INSTALL_TIMEOUT",
                "Browser provisioning exceeded its budget",
            ),
            started_at,
            started,
        )
    except Exception as error:
        result = failure_result("browser install", error, started_at, started)
    render(result, output)
    if result.exit_code:
        raise typer.Exit(result.exit_code)
