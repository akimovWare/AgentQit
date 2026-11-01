# AgentQit

[![Validate](https://github.com/akimovWare/AgentQit/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/akimovWare/AgentQit/actions/workflows/ci.yml)

Application testing with AI agents.

AgentQit gives an agent typed browser capabilities and reviewed backend operations.
The agent chooses checks from requirements; the runtime validates input, enforces
execution guardrails, and returns observations with evidence. UI and API/database/
messaging observations can share a task journal.

**Status:** public source preview. CLI, local MCP, interactive Playwright sessions,
and the synthetic Orders example are implemented. The PyPI package is not published yet.
See [verification](docs/verification.md) and [the roadmap](docs/roadmap.md).

## Install and configure

The user installs an application, then maintains a separate workspace containing
YAML catalogs, a profile, and credential references. Ordinary integrations require
no Python code or runtime checkout. Reusable catalogs and custom composition can
be supplied as independent plugins.

After a public PyPI release, the intended installation is:

```sh
uv tool install --python 3.12 'agentqit[browser]>=0.1,<0.2'
qa browser install --headless-only
qa workspace init --directory ./my-qa --name sample
```

Use `'agentqit>=0.1,<0.2'` for backend-only work. The initial range keeps
normal upgrades within the compatible 0.1 series. The generated workspace
is an offline draft; configure your reviewed operations, QA endpoints, and credential
references before execution. Then point your MCP host at `agentqit-mcp` with
`--workspace /absolute/path/to/my-qa`.

Upgrade the installed application with `uv tool upgrade agentqit`. Your workspace
stays separate from the installation. See [installation and updates](docs/distribution.md)
and [workspace configuration](docs/workspaces.md). **These index-installation commands
are for the future release; no public package is published yet.**

## Install the source preview

Install the application directly from the public repository with uv:

```sh
uv tool install --python 3.12 'agentqit[browser] @ git+https://github.com/akimovWare/AgentQit.git@main'
qa browser install --headless-only
qa workspace init --directory ./my-qa --name sample
```

Preview installs track the selected Git source. Pin a reviewed commit for repeatable
installs; switch to the versioned PyPI package when it becomes available.
See [workspace setup](docs/workspaces.md) for YAML and credential configuration.

## Try the synthetic example from source

With Python 3.12+ and uv, on macOS or Linux:

```sh
git clone https://github.com/akimovWare/AgentQit.git
cd AgentQit
uv sync --frozen --extra browser
uv run qa browser install --headless-only
uv run python examples/orders/src/qa_product_orders/demo.py
```

Orders Lab runs at `http://127.0.0.1:8765`. Connect an MCP host to the checkout's
`.venv/bin/agentqit-mcp` with arguments:

```text
--workspace /absolute/path/to/agentqit/examples/orders
```

This launches a local stdio process. Writes remain disabled until the operator
explicitly authorizes them. Follow [the Orders guide](examples/orders/README.md)
for the agent task, synthetic write scope, expected behavior, and deliberate defect.

Backend-only use needs no browser package or binaries:

```sh
uv sync --frozen
uv run qa workspace init --directory ../my-qa --name sample
uv run qa workspace list --workspace ../my-qa
uv run qa workspace doctor items.list --workspace ../my-qa
uv run qa workspace plan items.list --workspace ../my-qa
```

The generated `.invalid` endpoint is an offline draft. Supply reviewed operations,
authorized QA endpoints, and explicit credential references before execution.

## What is included

- A CLI and local MCP interface sharing one execution core.
- HTTP, PostgreSQL, Kafka, S3, and Kubernetes operations declared in YAML.
- Interactive browser navigation, observations, scoped element actions, and screenshots.
- Task-isolated browser contexts, evidence journals, and private screenshot/trace artifacts.
- Local catalogs for ordinary integrations; installable product plugins for reuse/custom composition.
- Explicit credential providers, recursive JSON redaction, TLS validation, and mutation admission.

A completed operation is not a business verdict. The agent compares observations
with independently supplied requirements. Unknown write outcomes require inspecting
state before any repeat. Plugins are trusted code; this local runtime is not a sandbox
or a shared remote service. Read [security boundaries](SECURITY.md).

## Guides

| Goal | Guide |
| --- | --- |
| Connect a backend without packaging | [Local workspaces](docs/workspaces.md) |
| Install the application and understand updates | [Distribution](docs/distribution.md) |
| Add a reusable product integration | [Getting started](docs/getting-started.md), [plugin API](docs/plugin-api.md) |
| Give implementation work to an agent | [Agent integration guide](docs/agent-integration.md) |
| Use browser sessions and artifacts | [Browser runtime](docs/browser-runtime.md) |
| Understand results and uncertain outcomes | [Result contract](docs/result-contract.md) |
| Understand the components and tradeoffs | [Architecture](docs/architecture.md), [design decisions](docs/design-decisions.md) |
| Compare CLI/MCP context costs | [Interface comparison](docs/interface-comparison.md) |
| Upgrade safely or prepare a release | [Compatibility](docs/compatibility.md), [release guide](docs/releases.md) |
| Develop, build, and validate | [Contributing](CONTRIBUTING.md), [verification](docs/verification.md), [agent rules](AGENTS.md) |

## Distribution

One Python distribution, `agentqit`, provides `qa`, `agentqit-mcp`, and the runtime.
Browser support is optional and its binaries are provisioned explicitly. Backend-only
installation does not download browsers. Application catalogs, topology, and custom
composition stay in workspaces or independent product packages.

[MIT](LICENSE). See [contributing](CONTRIBUTING.md), [support](SUPPORT.md), and
[community guidelines](CODE_OF_CONDUCT.md). Third-party dependencies retain their own licenses.
