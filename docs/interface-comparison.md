# CLI and MCP

CLI and local stdio MCP use the same compiler, execution core, and result contract.
CLI supports terminal agents, setup, diagnostics, backend invocation, and CI.
MCP provides structured tools and persistent interactive browser sessions.

## Discovery

A backend workspace exposes 10 MCP tools. Browser configuration adds three,
for 13 total. Operations are catalog data: the agent searches bounded metadata,
describes an operation, plans its inputs, and executes it by ID. Catalog growth
does not add tool definitions or CLI commands.

Search matches substrings in operation IDs and purposes. Parameters and source
metadata are returned by `describe_operation`. Business values come from the
user's task, canonical contracts, and authorized observations.

## Context use

CLI help and command results enter the agent's context as requested. An MCP host
can expose tool definitions eagerly or use its own discovery mechanism. The host
also controls how text and structured results are presented to the model.

Keep search results and observations bounded in either interface. Request details
selectively. Schema size, output duplication, command paths, images, and host
instructions all affect context use; interface choice depends on the client and task.

## Payload benchmark

```sh
uv sync --frozen
uv run python tools/benchmark_interfaces.py --output .agentqit/context-review
```

Use a new output directory. The command starts a synthetic Orders backend on
loopback, verifies CLI/MCP outcomes, and writes summaries, transcripts, and retained
records. The reference tokenizer is `o200k_base`; its vocabulary is cached after
the first download.

Workflows cover discovery/read, invalid UUID rejection, write admission, and
creation/read/cancellation. An expanded catalog checks that adding 500 operations
leaves MCP definitions unchanged. Browser schemas are counted without launching
a browser.

| Metric | Included |
| --- | --- |
| CLI interaction | Serialized command arguments and actual JSON stdout |
| MCP interaction | Tool name/arguments and text results |
| Cold CLI | Interaction plus root, workspace, and invoke help |
| Cold eager MCP | Interaction plus all tool definitions and server instructions once |
| `used_definition_tokens` | Definitions of tools used in the workflow |
| Structured duplication | Additional payload if a host includes both text and `structuredContent` |

CLI calls use Typer's test runner; MCP calls use the SDK's in-memory transport.
Installed-process checks are described in [verification](verification.md).
The accounting excludes host prompts, reasoning, image tokens, billing, process
latency, and independent model performance. `used_definition_tokens` excludes
host discovery overhead and is not a deferred-client measurement.

CI runs the workflows and retains the summary for each revision. Timing, paths,
identifiers, serialization, and dependencies affect the values between runs.
