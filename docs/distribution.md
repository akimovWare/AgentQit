# Installation and updates

The `agentqit` Python distribution provides the `qa` CLI, `agentqit-mcp` server,
and shared runtime. Configuration lives in a separate workspace containing YAML
catalogs, a profile, and credential references. Ordinary integrations require no
Python code or runtime checkout.

Supported interpreters are standard CPython 3.12, 3.13, and 3.14 on macOS and Linux.
The commands below select Python 3.12; use `--python 3.13` or `--python 3.14` to
select another supported version.

## Install from PyPI

With uv installed:

```sh
uv tool install --python 3.12 'agentqit[browser]>=0.1,<0.2'
qa browser install --headless-only
qa workspace init --directory ./my-service-qa --name my-service
```

For backend-only use, select `'agentqit>=0.1,<0.2'` and omit browser provisioning.
The version range accepts compatible 0.1.x updates. Adopt a new minor series
after reading its migration notes. CI integrations should pin a reviewed version.

uv installs executables on `PATH` and isolates dependencies from other projects.
`uv tool update-shell` adds its executable directory when needed. See
[uv tools](https://docs.astral.sh/uv/guides/tools/).

## Install from Git

With uv installed, on macOS or Linux:

```sh
uv tool install --python 3.12 'agentqit[browser] @ git+https://github.com/akimovWare/AgentQit.git@main'
qa browser install --headless-only
qa workspace init --directory ./my-service-qa --name my-service
```

For backend-only use, omit `[browser]` and browser provisioning. Use a commit SHA
instead of `main` to pin a source revision.

## Install a built wheel

```sh
uv tool install --python 3.12 /absolute/path/to/agentqit-0.1.0-py3-none-any.whl
```

For browser support:

```sh
uv tool install --python 3.12 'agentqit[browser] @ file:///absolute/path/to/agentqit-0.1.0-py3-none-any.whl'
qa browser install --headless-only
```

## Workspace

```text
my-service-qa/
  agentqit.yaml      # Profile, catalogs, auth, browser/session policy
  profile.yaml       # QA endpoints and credential-provider configuration
  operations/        # Reviewed operations
```

The profile can be named `qa.yaml` by setting `profile: qa.yaml` in the manifest.
Credentials come from an explicit provider such as environment variables or Vault;
YAML stores references rather than secret values. Follow [workspace setup](workspaces.md)
for declarations and offline checks.

Configure the MCP host to launch `agentqit-mcp` with
`--workspace /absolute/path/to/my-service-qa`. Browser use also requires
[configured origins and policy](browser-runtime.md).

## Updates

```sh
uv tool upgrade agentqit
```

uv respects the source and version constraints used at installation. Git and local
wheel installations do not track PyPI releases. Reinstall from the index to select
that source. An exact `==` constraint stays pinned during normal upgrades.

Finish tasks and stop MCP before updating, then restart it. Running processes keep
loaded code. Workspace YAML and credentials are outside the installation and are
not overwritten. When Playwright changes, provision matching binaries again:

```sh
qa browser install --headless-only
```

See [Playwright browser versions](https://playwright.dev/python/docs/browsers).
There is no background auto-updater or update-notification service.

## Pinning and rollback

To select or roll back to a specific published version:

```sh
uv tool install --force --python 3.12 'agentqit[browser]==0.1.0'
qa browser install --headless-only
```

The version must exist on the selected source. Rollback requires compatible
workspace configuration. See [compatibility](compatibility.md).

## Product plugins

Local catalogs and built-in auth cover ordinary integrations. Plugins package
reusable catalogs, custom auth/TLS composition, or operation handlers. They execute
trusted Python code and must share the application's environment:

```sh
uv tool install --python 3.12 --with YOUR_PLUGIN 'agentqit[browser]>=0.1,<0.2'
```

`YOUR_PLUGIN` is the plugin's distribution name. This index example requires the
specified packages to be published; local wheel paths are also accepted. Plugin
version constraints must match their tested runtime. See [the plugin API](plugin-api.md).

The [release guide](releases.md) covers build validation and maintainer-controlled
publication. [MIT](../LICENSE) covers AgentQit; dependencies retain their own licenses.
