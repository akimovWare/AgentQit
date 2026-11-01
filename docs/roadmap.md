# Roadmap

## Supported capabilities

- Local YAML workspaces and explicit credential/auth providers.
- HTTP, PostgreSQL, Kafka, S3, and Kubernetes operations.
- Bounded discovery, typed input descriptions, and offline planning.
- Agent-controlled Playwright through task-owned MCP sessions.
- Context isolation, scoped references, evidence journals, and private artifacts.
- Versioned configuration schemas, plugin contracts, and package validation.

## Development priorities

- Richer operation descriptions and terminology matching.
- Independent agent evaluations across CLI and MCP hosts.
- Transport-specific dependency groups.
- A CLI browser-session interface.
- Broader platform coverage and an embedding API.

These items are outside the supported API. Scope and compatibility are defined
in [the architecture](architecture.md), [plugin API](plugin-api.md), and
[compatibility policy](compatibility.md). Package releases follow
[the release process](releases.md).
