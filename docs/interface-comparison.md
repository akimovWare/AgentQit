# CLI and MCP context costs

AgentQit offers both interfaces on the same runtime. A terminal-capable agent can
use CLI discovery and JSON output. An MCP host can use typed tools and persistent
browser sessions. Choose based on the host and task; neither interface guarantees
lower total token use or better reasoning.

## Reproduce the measurement

```sh
uv sync --frozen
uv run python tools/benchmark_interfaces.py --output .agentqit/context-review
```

Use a new output directory. The command starts an owned synthetic Orders backend
on loopback, executes real CLI parsing/rendering and SDK MCP calls, verifies the
observed outcomes, and retains private JSON transcripts, a summary, and synthetic
records. It does not call a model API or external application. The tokenizer may
need its first public vocabulary download; subsequent runs can use its local cache.

The four workflows are:

1. Search, describe, plan, and read the order list.
2. Describe an operation and reject an invalid UUID before execution.
3. Plan a write and reject its invocation without mutation opt-in.
4. Describe/plan creation, create a synthetic order, read it, cancel it, and read
   the final state. Each interface uses a different idempotency key; created records
   remain in the local fixture's state file.

## Accounting model

Use `o200k_base` as a fixed reference tokenizer. Numbers count selected text payloads,
not billed usage or an exact model prompt. CLI runs through Typer's `CliRunner`;
MCP runs through the SDK's in-memory transport. Installed-process checks are separate.
No process-start latency or network-framing performance is inferred from this test.

- CLI interaction includes the serialized command arguments and actual JSON stdout.
- MCP interaction includes the tool name/arguments and text result. AgentQit also
  supplies `structuredContent` for programmatic clients. The summary counts its
  potential additional cost separately if a host includes both representations.
- Cold CLI includes one root help, workspace help, and invoke help. A warm session
  can reuse that knowledge. The existing terminal tool's schema and host prompts
  are outside this application-specific accounting.
- Cold eager MCP includes all model-facing tool definitions and server instructions
  once, followed by the workflow's calls/results. A host may use a different wrapper
  or choose different metadata. Do not add this startup cost again for every action.
- `used_definition_tokens` reports only definitions for tools used in the workflow.
  It is not a measured deferred-loading host: search costs, selection, and host/model
  support are deliberately not estimated.

Browser schemas are measured without launching a browser. Browser interaction itself
is not compared with CLI because this version's browser sessions are owned by MCP.
There is no CLI browser daemon in 0.1. Screenshot/image tokens, reasoning, hidden
host instructions, prompt caching, tool-choice quality, and task completion by an
independent LLM are outside the measurement. Paths, identifiers, and dependency
versions cause small changes between runs.

## Local reference measurement

Measured on macOS with the 0.1.0 candidate on 2026-11-01, using the accounting
above. Every expected outcome passed. Values are reference text tokens.

Startup: CLI help **821**; backend MCP definitions and instructions
**1074**; browser-enabled MCP **1735**.

| Workflow | CLI interaction | MCP interaction | CLI cold total | MCP eager cold total |
| --- | ---: | ---: | ---: | ---: |
| discover-and-read | 919 | 802 | 1740 | 1876 |
| invalid-input | 569 | 503 | 1390 | 1577 |
| blocked-write | 881 | 863 | 1702 | 1937 |
| create-read-cancel | 2111 | 1968 | 2932 | 3042 |

If all four workflows share discovery in one session, the reference totals are
**5301 CLI** and **5210 MCP**. Cold columns instead treat each task as a fresh session.
This is not a universal winning ratio. The CLI example includes absolute workspace
paths; a caller can shorten its command representation. A host that includes both
MCP text and structured results pays additional context shown separately in the
summary. Browser schemas add 661 reference tokens here; catalog growth adds none.

The evidence supports keeping both interfaces and bounded discovery. It does not
establish which interface a specific autonomous agent will use more accurately.

A separate Debian 12 aarch64 container run passed all four workflows and measured
identical startup definitions. Its shared-session totals were **4993 CLI** and
**5230 MCP**. Shorter workspace paths changed the CLI payloads enough to reverse
the small ordering seen on macOS. This is why neither result is a universal savings
claim. Both runs verified unchanged definitions at 504 catalog operations.

## Discovery and release checks

A backend workspace exposes 10 tools. Enabling browser configuration adds three,
for 13 total. The benchmark verifies that adding 500 catalog operations leaves
all MCP tool definitions unchanged. Operation names and inputs are retrieved through
bounded search and selected descriptions, not added to the initial tool list.

CI runs these workflows and retains the numeric summary. Regression checks verify
outcomes and catalog independence; they do not enforce a claimed CLI/MCP winning ratio.
The [compatibility policy](compatibility.md) and [release guide](releases.md) cover
stable configuration and the separate publication process.

## Interface choice

Use CLI for a terminal agent that already knows how to discover commands and filter
JSON locally. MCP is useful for hosts with structured tool integration and for the
current long-lived browser workflow. Keep responses bounded in either interface.
A host that loads every connected tool eagerly pays a larger startup cost; a host
with deferred discovery may avoid part of it. Verify the actual host behavior before
making a token-efficiency promise to users.
