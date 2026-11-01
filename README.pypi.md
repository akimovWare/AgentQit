# AgentQit

Application testing tools for AI agents.

AgentQit gives an agent typed browser actions, reviewed backend operations, and
observations with evidence. The agent chooses checks from requirements and compares
the returned facts with expected behavior. CLI and local stdio MCP share one runtime.

**Status:** public source preview on GitHub. The PyPI package is not published yet;
index-installation commands become available after the first package release.

## Install

With uv installed, on macOS or Linux:

```sh
uv tool install --python 3.12 'agentqit[browser]>=0.1,<0.2'
qa browser install --headless-only
qa workspace init --directory ./my-qa --name sample
```

For backend-only use, install `'agentqit>=0.1,<0.2'` and omit browser provisioning.
The generated workspace is an offline draft. Configure reviewed operation YAML,
your QA endpoints, and explicit credential references before execution. Ordinary
integrations use YAML; reusable catalogs and custom composition can use plugins.

## Connect an agent

Configure your MCP host to launch:

```sh
agentqit-mcp --workspace /absolute/path/to/my-qa
```

Interactive browser sessions use MCP. Backend operations are also available through
CLI. Writes require operator authorization, caller opt-in, and the exact QA
configuration described in the guides. Browser actions use scoped references and
artifacts remain private/local by default.

## Update

```sh
uv tool upgrade agentqit
```

The installation constraint keeps upgrades within the compatible 0.1 series.
Finish tasks and stop MCP before upgrading, then restart it. Re-provision browsers
when Playwright changes. User YAML and credential sources stay separate from the
application installation.

## Guides

- [Project overview and source](https://github.com/akimovWare/AgentQit)
- [Workspace and authentication setup](https://github.com/akimovWare/AgentQit/blob/main/docs/workspaces.md)
- [Integration instructions for coding agents](https://github.com/akimovWare/AgentQit/blob/main/docs/agent-integration.md)
- [Browser sessions and evidence](https://github.com/akimovWare/AgentQit/blob/main/docs/browser-runtime.md)
- [Installation, updates, and rollback](https://github.com/akimovWare/AgentQit/blob/main/docs/distribution.md)
- [Compatibility policy](https://github.com/akimovWare/AgentQit/blob/main/docs/compatibility.md)
- [Security boundaries](https://github.com/akimovWare/AgentQit/blob/main/SECURITY.md)

A completed operation is an execution fact, not a business verdict. Unknown write
outcomes require state inspection before retrying. Plugins execute trusted code;
this local runtime is not a shared remote service or an OS sandbox.

[MIT license](https://github.com/akimovWare/AgentQit/blob/main/LICENSE).
