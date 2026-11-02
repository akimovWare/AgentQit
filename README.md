# AgentQit

[![Validate](https://github.com/akimovWare/AgentQit/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/akimovWare/AgentQit/actions/workflows/ci.yml)

Application testing with AI agents.

AgentQit exposes typed browser actions and reviewed backend operations through CLI
and local MCP. The agent selects checks from requirements; the runtime validates
inputs, enforces execution limits, and returns observations with evidence.
UI, API, database, and messaging observations can share a task journal.

CPython 3.12, 3.13, and 3.14; macOS and Linux.

## Install

With uv installed:

```sh
uv tool install --python 3.12 'agentqit[browser]>=0.1,<0.2'
qa browser install --headless-only
qa workspace init --directory ./my-qa --name sample
```

For backend-only use, omit `[browser]` and the browser provisioning command.
For a reproducible installation, pin a reviewed version such as `==0.1.0`.

The workspace contains your operation YAML, profile, and credential references.
Configure reviewed operations, QA endpoints, and an explicit credential provider
using the [workspace guide](docs/workspaces.md). Ordinary integrations require
no Python code or runtime checkout; reusable catalogs and custom composition can
use independent product plugins.

Configure your MCP host to launch:

```sh
agentqit-mcp --workspace /absolute/path/to/my-qa
```

Interactive browser sessions use MCP. CLI commands provide backend execution,
discovery, planning, diagnostics, and configuration. See
[installation and updates](docs/distribution.md) for package sources and upgrades.

## Try Orders Lab

The synthetic example provides a local UI/API and a verification task:

```sh
git clone https://github.com/akimovWare/AgentQit.git
cd AgentQit
uv sync --frozen --extra browser
uv run qa browser install --headless-only
uv run python examples/orders/src/qa_product_orders/demo.py
```

Open `http://127.0.0.1:8765`. Point your MCP host at the checkout's
`.venv/bin/agentqit-mcp` with `--workspace /absolute/path/to/AgentQit/examples/orders`.
Follow the [Orders guide](examples/orders/README.md) for mutation authorization,
expected behavior, and the deliberate frontend defect.

## Capabilities

- HTTP, PostgreSQL, Kafka, S3, and Kubernetes operations declared in YAML.
- Bounded catalog search, typed input descriptions, and offline planning.
- Interactive browser navigation, page observations, scoped element actions, and screenshots.
- Isolated browser contexts, shared evidence journals, and private PNG/trace artifacts.
- Explicit credential sources, recursive JSON redaction, TLS validation, and mutation admission.
- Local workspaces and installable product plugins.

A completed operation is an execution fact. The agent compares observations with
requirements to assess business behavior. Unknown write outcomes require state
inspection before another attempt. Plugins execute trusted code; browser contexts
provide task isolation rather than an OS sandbox. Read [security boundaries](SECURITY.md).

## Guides

| Use | Guide |
| --- | --- |
| Configure YAML and authentication | [Local workspaces](docs/workspaces.md) |
| Install and update | [Distribution](docs/distribution.md), [compatibility](docs/compatibility.md) |
| Connect an application with an agent | [Agent integration](docs/agent-integration.md) |
| Package an integration | [Plugin setup](docs/getting-started.md), [plugin API](docs/plugin-api.md) |
| Use browser sessions and evidence | [Browser runtime](docs/browser-runtime.md) |
| Interpret results | [Result contract](docs/result-contract.md) |
| Understand the components | [Architecture](docs/architecture.md) |
| Compare interface payloads | [CLI and MCP](docs/interface-comparison.md) |
| Contribute or release | [Contributing](CONTRIBUTING.md), [verification](docs/verification.md), [releases](docs/releases.md) |
| Review development priorities | [Roadmap](docs/roadmap.md) |

One Python distribution, `agentqit`, provides `qa`, `agentqit-mcp`, and the runtime.
Browser support is optional; binaries are provisioned explicitly.

[MIT](LICENSE) · [Support](SUPPORT.md) · [Community guidelines](CODE_OF_CONDUCT.md)
