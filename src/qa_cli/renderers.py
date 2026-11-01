from __future__ import annotations

from rich.console import Console
from rich.pretty import Pretty

from qa_runtime.contracts import CommandResult


def render(result: CommandResult, output: str) -> None:
    if output == "json":
        print(result.model_dump_json(by_alias=True, exclude_none=True))
        return
    if output != "human":
        raise ValueError("output must be 'json' or 'human'")
    console = Console()
    console.print(f"[{result.status}] {result.command} ({result.duration_ms} ms)", markup=False)
    if result.result is not None:
        console.print(Pretty(result.result))
    if result.error is not None:
        console.print(f"{result.error.code}: {result.error.message}", style="red", markup=False, highlight=False)
    for warning in result.warnings:
        console.print(f"Warning: {warning}", style="yellow", markup=False, highlight=False)
    if result.evidence:
        console.print("Saved evidence:", style="bold", markup=False)
        for path in result.evidence:
            console.print(f"  {path}", markup=False, highlight=False)
