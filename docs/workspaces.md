# Local workspaces

A workspace connects an ordinary backend without building or installing a Python
product package. Packaged plugins remain available for reusable integrations and
custom composition. Both paths use the existing compiler and runtime.

## First local checks

With AgentQit installed (see [distribution](distribution.md)):

```sh
qa workspace init --directory ./sample-qa --name sample
qa workspace list --workspace ./sample-qa
qa workspace describe items.list --workspace ./sample-qa
qa workspace doctor items.list --workspace ./sample-qa
qa workspace plan items.list --workspace ./sample-qa
```

For source development, run `uv sync --frozen` in the checkout and
prefix these commands with `uv run`. A source checkout is a development workflow,
not a requirement for an installed application.

These commands use a reserved `.invalid` address and perform no backend calls.
`init` requires a new directory. Replace the sample declaration and profile with
reviewed contracts and an authorized QA target before invoking anything.

`agentqit.yaml` references local files:

```yaml
apiVersion: agentqit/v1
name: sample
profile: profile.yaml
registry: [operations]
limits:
  operationTimeoutSeconds: 60
```

References must stay within the workspace directory, including resolved symlinks.
The profile must be a `.yaml` file. Multiple registry files/directories are allowed.
An optional `product` selects an installed plugin for special composition; local
`auth` must not conflict with its providers.

`init` creates `profile.yaml`. You can rename it to `qa.yaml` and set
`profile: qa.yaml` in `agentqit.yaml`; the filename is not fixed. Keep the workspace
outside the installed tool environment so application upgrades do not replace it.

## Exact HTTP response decimals

Set `httpResponseDecimals: true` at the top level of a profile to read fractional
and exponent-form JSON numbers in every standard HTTP response as `Decimal`.
CLI JSON, MCP results and saved evidence represent these values as exact decimal
strings. JSON integers remain integers. Profiles without this setting retain
the existing floating-point behavior.

## Explicit environment credentials

The profile stores variable names and references, not secret values:

```yaml
environment: unconfigured
services:
  api:
    baseUrl: https://api.example.invalid
    authRef: api-token
credentials:
  provider: env
  bindings:
    sample/api-token: SAMPLE_API_TOKEN
```

Add the auth provider to `agentqit.yaml`:

```yaml
auth:
  api-token:
    kind: bearer
    credentialRef: sample/api-token
```

For an API-key header, use `kind: api-key` and `headerName: X-API-Key`.
Supply the environment value through your approved secret-management workflow;
never put it in a command argument or tracked file. Values are retrieved only
during execution and registered for redaction. Missing mappings/values fail closed.
An environment source never falls back to Vault or a helper.

Vault profiles remain supported with their explicit provider configuration.
`doctor` and `plan` validate locally without retrieving credentials.

## PostgreSQL connections

Use a single host (including a DNS alias or load balancer) or a comma-separated
list of hosts in `databases.<name>.dsn`. Each host can specify its own port.
Keep database credentials in the configured credential source:

```yaml
databases:
  orders:
    dsn: postgresql://db-a.example.invalid:5432,db-b.example.invalid:5433/orders?target_session_attrs=read-write
    usernameRef: orders/db/username
    passwordRef: orders/db/password
```

With `target_session_attrs=read-write`, asyncpg selects a server that accepts
writes when it opens a connection. Without that option, normal driver defaults
apply. A DNS alias can route to the writer if the balancer is configured for it
and its hostname passes TLS verification. `targetServerType` is a JDBC option.

## MCP

Launch the local server using the installed application:

```sh
agentqit-mcp --workspace /absolute/path/to/sample-qa
```

Configure the MCP host to launch that executable with those arguments. It speaks
stdio and keeps protocol stdout clean. An equivalent command is
`qa mcp --workspace /absolute/path/to/sample-qa`. If the host does not inherit your
shell's `PATH`, configure the absolute path to the installed executable. For source
development, use the checkout's `.venv/bin/agentqit-mcp` instead.

Tools provide bounded search, operation descriptions, offline plans, a read-only
observation entry point, and explicit invocation. All return the
[common result envelope](result-contract.md); protocol errors mark failed commands
as MCP tool errors as well. A successful observation is not a business verdict.

Mutation is closed by default. An explicitly authorized operator must enable the
server's `--allow-mutation` capability; the call must also opt in, and the profile
must have exact `environment: qa`. A tool argument alone cannot grant permission.
For CLI workspace invocation, the operator's explicit flag supplies that capability.

The overall workspace execution budget covers authorization and transport I/O.
An uncertain write timeout returns `execution_outcome: unknown` with
`retry_advice: read_state_first`. Cleanup problems are warnings and do not erase a
known operation result.


## MCP discovery size

A backend workspace advertises 10 tools: six catalog/execution tools and four task/
evidence tools. Configuring `browser` adds `observe_page`, `browser_action`, and
`screenshot`, for 13 tools in total. The set is fixed when the process starts;
restart it after changing capabilities. Browser sessions require the browser extra
and explicitly provisioned binaries as well as workspace configuration.

Operation declarations are catalog data, not separate MCP tools. The agent searches
bounded metadata and requests a selected operation's description before planning
and execution. Adding catalog operations does not grow the tool definitions.
See [interface comparison](interface-comparison.md) for measured payloads and host limits.
