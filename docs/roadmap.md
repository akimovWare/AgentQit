# Roadmap

AgentQit is a public source preview. The initial Python distribution provides the
CLI, local stdio MCP, and a shared execution runtime. PyPI publication is a separate
release step; see [the release guide](releases.md).

## Available in the preview

- Local YAML workspaces, explicit environment/Vault credentials, and header auth.
- Reviewed HTTP, PostgreSQL, Kafka, S3, and Kubernetes operations.
- Bounded operation discovery and offline input planning through CLI and MCP.
- Agent-controlled Playwright through task-owned MCP sessions.
- Isolated browser contexts, scoped element references, and private evidence artifacts.
- Shared backend/UI journals and explicit uncertain-write outcomes.
- Independently packaged synthetic Orders UI/API example.
- Versioned configuration schemas, plugin contracts, and package validation.

## Before a package release

- Require successful public CI for the selected source revision.
- Confirm package-name ownership, release metadata, and the reporting channel.
- Activate the protected publication workflow only after maintainer authorization.

## Candidate improvements

- Richer operation discovery, descriptions, and terminology matching.
- Independent agent evaluations across CLI and MCP hosts.
- Optional dependency groups for transport-specific installation sizes.
- A browser session owner for CLI if real usage justifies the additional interface.
- Broader platform coverage and a supported embedding API when concrete users need them.

These are candidate directions, not delivery dates or supported capabilities.
Requirements and caller reasoning remain separate from deterministic execution;
there is no built-in business-success evaluator or automatic scenario runner.
