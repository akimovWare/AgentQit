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

For the unpublished source preview, run `uv sync --frozen` in the checkout and
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

## MCP

Launch the local server using the installed application:

```sh
agentqit-mcp --workspace /absolute/path/to/sample-qa
```

Configure the MCP host to launch that executable with those arguments. It speaks
stdio and keeps protocol stdout clean. An equivalent command is
`qa mcp --workspace /absolute/path/to/sample-qa`. If the host does not inherit your
shell's `PATH`, configure the absolute path to the installed executable. For the
source preview, use the checkout's `.venv/bin/agentqit-mcp` instead.

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
and execution. Adding catalog operations does not grow the initial tool schemas.
See [interface comparison](interface-comparison.md) for measured payloads and host limits.
