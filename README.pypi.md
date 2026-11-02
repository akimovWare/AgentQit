# AgentQit

Give your AI agent tools to test a UI and follow the same action through APIs,
databases, messages, object storage, and Kubernetes observations. Configure your
project in YAML; the agent chooses checks and compares evidence with requirements.

CPython 3.12, 3.13, and 3.14; macOS and Linux.

## What your agent can do

Available after configuring your workspace and access:

| Integration | What the agent can do |
| --- | --- |
| Browser · Playwright via MCP | Navigate pages, fill forms, click/select controls, inspect page state, capture screenshots and traces |
| HTTP APIs | Call declared endpoints with typed inputs and inspect responses |
| PostgreSQL | Run reviewed, bounded queries to inspect records and state |
| Kafka | Publish test messages, observe events, and capture partition-offset checkpoints |
| S3 | List buckets/objects, inspect, upload, copy, and delete objects |
| Kubernetes | Inspect deployments, list pods, and read deployment logs |

The agent can search the catalog, inspect parameter schemas, and validate plans
offline. Local OpenAPI imports create read-only operation drafts for review.
Built-in HTTP auth supports Bearer and API-key headers, with credentials from
environment variables or Vault.

Combine UI and backend observations in one task journal. For example: submit a
form, find the resulting record through an API, and inspect its database state or
emitted event. You supply expected behavior and authorize the execution scope.

## Fast track

### 1. Install the application

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) once, then
download and install AgentQit from PyPI:

```sh
uv tool install --python 3.14 'agentqit[browser]>=0.1,<0.2'
qa browser install --headless-only
qa workspace init --directory ./my-qa --name sample
```

For backend-only use, omit `[browser]` and browser provisioning. Choose a new
directory for `workspace init`. The generated workspace contains sample YAML;
the next step connects it to your application.

### 2. Give your agent this setup task

Use a coding agent with terminal and filesystem access. Copy this message:

```text
Read https://github.com/akimovWare/AgentQit/blob/main/docs/agent-integration.md
and follow its linked workspace and browser guides.

Set up AgentQit for this project in ./my-qa using the installed qa command.
Use the initialized workspace. Ask me for missing requirements, the first check,
contract/source locations, QA endpoints, my agent host, and credential references.
Configure the workspace YAML and prepare my CLI/MCP connection.
Validate offline, report gaps, and show the first check ready to run.
Request an authorized execution scope before making live calls.
```

The agent asks for missing details and maintains `agentqit.yaml`, your profile
(`profile.yaml` or `qa.yaml`), and `operations/*.yaml`. Supply secret values through
your environment or Vault; give the agent credential references.

Backend operations work through CLI or local stdio MCP. Interactive browser
control uses MCP; have the agent configure your host to launch:

```sh
agentqit-mcp --workspace /absolute/path/to/my-qa
```

An execution-only MCP client needs this setup completed first. See the
[agent integration guide](https://github.com/akimovWare/AgentQit/blob/main/docs/agent-integration.md) for the full workflow.

### 3. Ask for checks and keep operations current

Give the agent a task and its expected behavior, for example:

> Verify this checkout change against these requirements and show the UI/API evidence.

Add or update operation steps through the same agent:

> Update the operation YAML in ./my-qa from this API contract. Show the changed
> inputs and effects, the YAML diff, and offline validation results.

These are workspace changes. Finish active sessions and restart MCP after
updating its workspace. New catalog operations use the same CLI/MCP entry points.
The [integration guide](https://github.com/akimovWare/AgentQit/blob/main/docs/agent-integration.md#maintain-an-integration) covers
catalog updates and validation.

Update the application with `uv tool upgrade agentqit`. Finish tasks and stop MCP
before upgrading, then restart it; provision browser binaries again if Playwright
changed. See [installation and updates](https://github.com/akimovWare/AgentQit/blob/main/docs/distribution.md) for version pins,
plugins, and rollback.

## Guides

- [Project overview](https://github.com/akimovWare/AgentQit)
- [Workspace and authentication setup](https://github.com/akimovWare/AgentQit/blob/main/docs/workspaces.md)
- [Agent integration](https://github.com/akimovWare/AgentQit/blob/main/docs/agent-integration.md)
- [Browser sessions and evidence](https://github.com/akimovWare/AgentQit/blob/main/docs/browser-runtime.md)
- [Installation and updates](https://github.com/akimovWare/AgentQit/blob/main/docs/distribution.md)
- [Compatibility](https://github.com/akimovWare/AgentQit/blob/main/docs/compatibility.md)
- [Security boundaries](https://github.com/akimovWare/AgentQit/blob/main/SECURITY.md)

A completed operation is an execution fact. Business conclusions belong to the
caller. Unknown write outcomes require state inspection before retrying.
Plugins execute trusted code; this runtime is a local tool, not a shared remote service.

[MIT license](https://github.com/akimovWare/AgentQit/blob/main/LICENSE).
