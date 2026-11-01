from __future__ import annotations

import os
import sys
import time
import uuid
from collections.abc import Sequence
from typing import Any

from typer.core import TyperGroup

try:  # Use Typer's vendored Click when available.
    from typer import _click as click
except ImportError:  # pragma: no cover - standalone Click import
    import click

from qa_cli.renderers import render
from qa_cli.settings import DEFAULT_OUTPUT
from qa_runtime.contracts import CommandResult, ErrorItem, ExitCode


def _requested_output(args: Sequence[str]) -> str:
    selected = os.environ.get("QA_CLI_OUTPUT", DEFAULT_OUTPUT)
    for index, argument in enumerate(args):
        if argument == "--":
            break
        if argument.startswith("--output="):
            selected = argument.partition("=")[2]
        elif argument == "--output" and index + 1 < len(args) and not args[index + 1].startswith("--"):
            selected = args[index + 1]
    return selected


def _usage_error(error: click.exceptions.ClickException) -> ErrorItem:
    # Public parser errors include parameter names and help, excluding input values.
    details: dict[str, Any] = {}
    if isinstance(error, click.exceptions.MissingParameter):
        code = "MISSING_ARGUMENT"
        message = "a required command argument or option value is missing"
    elif isinstance(error, click.exceptions.NoSuchOption):
        code = "UNKNOWN_OPTION"
        message = "the command received an unknown option"
    elif isinstance(error, click.exceptions.BadParameter):
        code = "INVALID_ARGUMENT"
        message = "a command argument or option has an invalid value"
    else:
        code = "INVALID_COMMAND"
        message = "invalid command syntax"
    parameter = getattr(error, "param", None)
    if parameter is not None and parameter.name:
        details["parameter"] = parameter.name
    context = getattr(error, "ctx", None)
    command_path = context.command_path if context is not None else "qa"
    return ErrorItem(
        code=code,
        message=message,
        source="cli",
        details=details or None,
        hint=f"Inspect {command_path} --help for the accepted arguments and options.",
    )


class StructuredTyperGroup(TyperGroup):
    """Keep parser failures inside the same result contract as command failures."""

    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        **extra: Any,
    ) -> Any:
        arguments = list(sys.argv[1:] if args is None else args)
        options = arguments[: arguments.index("--")] if "--" in arguments else arguments
        selected_output = _requested_output(arguments)
        if "--help" in options or selected_output == "human":
            return super().main(
                args=arguments,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=standalone_mode,
                **extra,
            )

        started = time.monotonic_ns()
        if selected_output not in {"json", "human"}:
            error = ErrorItem(
                code="INVALID_OUTPUT",
                message="--output and QA_CLI_OUTPUT must be 'json' or 'human'",
                source="cli",
            )
        else:
            try:
                result = super().main(
                    args=arguments,
                    prog_name=prog_name,
                    complete_var=complete_var,
                    standalone_mode=False,
                    **extra,
                )
            except click.exceptions.ClickException as caught:
                error = _usage_error(caught)
            else:
                if standalone_mode:
                    raise SystemExit(result if isinstance(result, int) else 0)
                return result
        render(
            CommandResult.failure(
                command="cli",
                run_id=f"run_{uuid.uuid4().hex}",
                duration_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
                exit_code=ExitCode.INVALID_INPUT,
                error=error,
            ),
            "json",
        )
        if standalone_mode:
            raise SystemExit(ExitCode.INVALID_INPUT)
        return ExitCode.INVALID_INPUT
