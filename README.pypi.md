# AgentQit

**Give your coding agent a reusable QA workspace for your application.**

Connect the browser, APIs, databases, and events in one place. Define your
application's operations once, reuse them across agent sessions, and collect
facts that show what happened beyond the UI.

**Playwright · HTTP · PostgreSQL · Kafka · S3 · Kubernetes**

Works with your existing coding agent through CLI or local MCP. Python 3.12,
3.13, and 3.14; macOS and Linux. MIT licensed.

## Why AgentQit

- **Load project knowledge on demand.** The agent searches your operation catalog
  and requests the selected operation's parameter schema. Endpoints, reviewed SQL,
  resource bindings, and usage guides live in your workspace and can be reused
  across tasks and agents.
- **Use credential references in prompts and YAML.** The runtime resolves secret
  values from configured providers, including environment variables and Vault,
  and applies redaction to structured results.
- **Keep one agent connection as the catalog grows.** A fixed set of MCP tools
  discovers and invokes operations by ID. Adding YAML operations adds neither
  MCP tool definitions nor CLI commands.
- **Follow a check across layers.** Use a task session to keep browser observations,
  backend results, IDs, screenshots, and traces together in one evidence journal.

Your team maintains the workspace alongside the application. The agent chooses
checks and compares observations with your requirements; AgentQit executes
operations and returns facts.

## Catch a UI/API mismatch

> Cancel an order. Verify that both the UI and API show CANCELLED, and show the evidence.

The browser can confirm what the user sees. A declared API operation can inspect
the same order. Put both observations in one task session. The agent compares
the two states against your requirements and keeps the evidence together.

The included [Orders Lab](https://github.com/akimovWare/AgentQit/blob/main/examples/orders/README.md) makes this concrete: its
`--defect-ui` mode deliberately keeps showing CREATED after the API has cancelled
an order. It runs locally with synthetic data.

## Fast track

### 1. Install and create a workspace

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) once, then:

```sh
uv tool install --python 3.14 'agentqit[browser]>=0.1,<0.2'
qa browser install --headless-only
qa workspace init --directory ./my-qa --name sample
```

For backend-only use, omit `[browser]` and browser provisioning. Choose a new
directory for `workspace init`; it creates sample YAML for the next step.

### 2. Give your agent the setup guide

Use a coding agent with terminal and filesystem access. Copy this task:

```text
Read https://github.com/akimovWare/AgentQit/blob/main/docs/agent-integration.md
and follow the linked workspace and browser guides.

Set up AgentQit for this project in ./my-qa using the installed qa command.
Reuse the initialized workspace. Ask me for the first useful check, expected
behavior, canonical contracts, QA endpoints, my agent host, and credential references.
Configure the workspace YAML and CLI/MCP connection. Validate offline, show the
changes and gaps, and prepare the first check.
Ask for an authorized execution scope before making live calls.
```

The agent maintains `agentqit.yaml`, your profile (`profile.yaml` or `qa.yaml`),
and `operations/*.yaml`. You supply the application access and expected behavior.
Secret values go through the configured provider; share references with the agent.
Custom authentication or composition can use a [product plugin](https://github.com/akimovWare/AgentQit/blob/main/docs/plugin-api.md).

Backend operations work through CLI or MCP. Interactive browser control uses MCP;
have the agent configure your host to launch:

```sh
agentqit-mcp --workspace /absolute/path/to/my-qa
```

An execution-only MCP client uses a workspace that has already been configured.
The [integration guide](https://github.com/akimovWare/AgentQit/blob/main/docs/agent-integration.md) covers setup and host configuration.

### 3. Run checks and evolve the workspace

Give the agent a task with requirements:

> Verify this checkout change against these requirements. Show the UI/API evidence and any gaps.

Maintain operations through the same agent:

> Update the workspace operations from this API contract. Show the changed inputs,
> effects, YAML diff, and offline validation results.

Keep your application-specific knowledge in the workspace as the application
changes. Finish active sessions and restart MCP after updating workspace files.
New operations use the same discovery and invocation tools.

## What your agent can do

Available after configuring the workspace and access:

| Integration | Capabilities |
| --- | --- |
| Browser · Playwright via MCP | Navigate, fill forms, click/select controls, inspect page state, capture screenshots and traces |
| HTTP APIs | Call declared endpoints with typed inputs and inspect responses |
| PostgreSQL | Inspect records and state through reviewed, bounded queries |
| Kafka | Publish test messages, observe events, and capture partition-offset checkpoints |
| S3 | List buckets/objects, inspect, upload, copy, and delete objects |
| Kubernetes | Inspect deployments, list pods, and read deployment logs |

Discover operations, inspect schemas, and validate plans offline. Import local
OpenAPI contracts into read-only operation drafts for review. Built-in HTTP auth
supports Bearer and API-key headers; plugins can supply custom composition.

See [CLI and MCP](https://github.com/akimovWare/AgentQit/blob/main/docs/interface-comparison.md) for the fixed toolset and payload
benchmark. Actual context use depends on your agent host and the observations requested.

Try the synthetic [Orders Lab](https://github.com/akimovWare/AgentQit/blob/main/examples/orders/README.md) to see a UI/API disagreement using only local synthetic data.

## Updates and execution boundaries

Update the application with `uv tool upgrade agentqit`. Finish tasks and stop MCP
before upgrading, then restart it; provision browser binaries again if Playwright
changed. Workspace YAML and credential configuration remain separate from the
installed application. See [installation and updates](https://github.com/akimovWare/AgentQit/blob/main/docs/distribution.md).

Live checks require an authorized scope. Mutations additionally require caller
opt-in and `environment: qa`; MCP also requires an operator-granted host
capability. Inspect state before retrying a write with an unknown outcome. Browser contexts provide task isolation; plugins run trusted code.
Structured redaction does not sanitize arbitrary screenshots or trace archives.
Review artifacts before sharing. See [security boundaries](https://github.com/akimovWare/AgentQit/blob/main/SECURITY.md).

## Guides

| Use | Guide |
| --- | --- |
| Connect your application with an agent | [Agent integration](https://github.com/akimovWare/AgentQit/blob/main/docs/agent-integration.md) |
| Configure YAML and authentication | [Local workspaces](https://github.com/akimovWare/AgentQit/blob/main/docs/workspaces.md) |
| Install, upgrade, and pin versions | [Distribution](https://github.com/akimovWare/AgentQit/blob/main/docs/distribution.md), [compatibility](https://github.com/akimovWare/AgentQit/blob/main/docs/compatibility.md) |
| Package a reusable integration | [Plugin setup](https://github.com/akimovWare/AgentQit/blob/main/docs/getting-started.md), [plugin API](https://github.com/akimovWare/AgentQit/blob/main/docs/plugin-api.md) |
| Use browser sessions and evidence | [Browser runtime](https://github.com/akimovWare/AgentQit/blob/main/docs/browser-runtime.md) |
| Interpret observations | [Result contract](https://github.com/akimovWare/AgentQit/blob/main/docs/result-contract.md) |
| Understand or extend the runtime | [Architecture](https://github.com/akimovWare/AgentQit/blob/main/docs/architecture.md), [roadmap](https://github.com/akimovWare/AgentQit/blob/main/docs/roadmap.md) |
| Contribute or release | [Contributing](https://github.com/akimovWare/AgentQit/blob/main/CONTRIBUTING.md), [verification](https://github.com/akimovWare/AgentQit/blob/main/docs/verification.md), [releases](https://github.com/akimovWare/AgentQit/blob/main/docs/releases.md) |

One Python distribution, `agentqit`, provides `qa`, `agentqit-mcp`, and the runtime.
Browser support is optional; binaries are provisioned explicitly.

[MIT](https://github.com/akimovWare/AgentQit/blob/main/LICENSE) · [Support](https://github.com/akimovWare/AgentQit/blob/main/SUPPORT.md) · [Community guidelines](https://github.com/akimovWare/AgentQit/blob/main/CODE_OF_CONDUCT.md)
