# Design decisions

## One runtime, two interfaces

CLI and local stdio MCP share configuration loading, compilation, authorization,
execution, and result contracts. CLI is useful for terminal agents, setup, and CI.
A persistent MCP process owns interactive browser sessions. Neither interface is
assumed to have lower total context use; see [the measurement](interface-comparison.md).

## Catalog data instead of per-operation tools

YAML operations are catalog entries. The agent searches bounded metadata, describes
one operation, plans exact inputs, and executes within its authorized task. Adding
operations does not add CLI commands or MCP tool definitions. Search currently
matches substrings in IDs and purposes; semantic retrieval is a candidate improvement.

## Configuration before plugins

Ordinary integrations use a local workspace. A plugin is useful for reusable catalogs,
custom auth/TLS composition, or new handler kinds. Plugins are trusted Python code.
The supported extension surface and compatibility promises are documented separately.

## Explicit browser ownership

An interactive task keeps one browser context across actions. References belong to
an observation and page; changed or stale targets require another observation.
Screenshots and traces have their own privacy policy because JSON redaction cannot
sanitize arbitrary pixels or opaque archives.

## Facts and requirements

An HTTP response or completed action is an observation. The caller compares it with
independently supplied requirements. Service implementation alone is not the expected
business result. Unknown write outcomes require state inspection before a retry.

## Release scope

Mutations require explicit capability, per-call opt-in, and exact `environment: qa`.
Browser sessions use MCP in this version. Shared remote service deployment, Windows,
and a general embedding SDK are outside the supported preview scope.
