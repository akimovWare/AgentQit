# AgentQit

Application testing tools for AI agents.

AgentQit exposes typed browser actions, reviewed backend operations, and observations
with evidence. The agent selects checks from requirements and compares the returned
facts with expected behavior. CLI and local stdio MCP share one execution runtime.

CPython 3.12, 3.13, and 3.14; macOS and Linux.

## Install

With uv installed:

```sh
uv tool install --python 3.12 'agentqit[browser]>=0.1,<0.2'
qa browser install --headless-only
qa workspace init --directory ./my-qa --name sample
```

For backend-only use, omit `[browser]` and browser provisioning. Pin a reviewed
version such as `==0.1.0` for a reproducible installation.

Configure the workspace with reviewed operation YAML, your QA endpoints, and
explicit credential references. Ordinary integrations use YAML; reusable catalogs
and custom composition can use plugins.

## Connect an agent

Configure your MCP host to launch:

```sh
agentqit-mcp --workspace /absolute/path/to/my-qa
```

Interactive browser sessions use MCP. CLI also supports backend execution,
discovery, planning, and diagnostics. Writes require operator authorization,
caller opt-in, and `environment: qa`. Browser actions use scoped references;
evidence artifacts remain local and private by default.

## Update

```sh
uv tool upgrade agentqit
```

Updates follow the source and version constraints used at installation. Finish
tasks and stop MCP before upgrading, then restart it. Re-provision browser binaries
when Playwright changes. User YAML and credentials are separate from the application.

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
