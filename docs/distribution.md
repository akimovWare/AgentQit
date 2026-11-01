# Installation and updates

**Status:** public source preview. Wheel and sdist installation are verified.
The PyPI package is not published yet. CI validates the project; package publication
is disabled by default and requires a separate maintainer instruction.

## The product users install

AgentQit is an application distributed as one Python package, `agentqit`. It installs
the `qa` CLI, the `agentqit-mcp` local server, and their shared runtime. Users configure
YAML and credential references; ordinary integrations require no Python code,
runtime changes, source checkout, or local build.

Local MCP over stdio is the primary interactive interface. The MCP host launches
a persistent process that owns browser sessions. CLI commands support setup,
discovery, planning, diagnostics, and backend invocation. Both use the same runtime.
A shared HTTP service and a supported Python embedding SDK are separate products
and compatibility commitments.

GitHub hosts the source and release notes. PyPI will host versioned installable
artifacts. A release consists of a wheel and source distribution built from the
same source version. See [PyPA's command-line packaging guide](https://packaging.python.org/en/latest/guides/creating-command-line-tools/).

## First installation

Use uv's isolated tool environment. The following commands become usable after
the first public release:

```sh
# Application with interactive browser support.
uv tool install --python 3.12 'agentqit[browser]>=0.1,<0.2'

# Explicitly provision matching Chromium headless binaries.
qa browser install --headless-only

# Create a separate configuration directory; do not edit the installation.
qa workspace init --directory ./my-service-qa --name my-service
```

For backend-only use, install `'agentqit>=0.1,<0.2'` and omit browser
provisioning. The initial minor-version range keeps normal upgrades within the
0.1.x compatibility promise. Adopt a new minor series explicitly after reading
its migration notes. Installing the base package does not download browsers. Start with
macOS or Linux; see [verified support](verification.md) for current evidence.

uv installs application executables on `PATH` and isolates their dependencies from
other Python projects. If needed, use `uv tool update-shell` as directed by uv.
See [uv's tool guide](https://docs.astral.sh/uv/guides/tools/).

Keep user configuration separate:

```text
my-service-qa/
  agentqit.yaml      # Workspace manifest: profile, catalogs, auth, browser policy
  profile.yaml       # QA endpoints and credential-provider configuration
  operations/        # Reviewed operation declarations
```

The profile may be named `qa.yaml`: rename it and set `profile: qa.yaml` in the
manifest. Configure credentials through an explicit provider such as environment
variables or Vault; YAML contains references, never secret values. See
[local workspaces](workspaces.md) for the full configuration and offline checks.

Connect the MCP host to `agentqit-mcp` with arguments
`--workspace /absolute/path/to/my-service-qa`. Configure browser origins and other
[browser settings](browser-runtime.md) before browser use. The generated workspace
is an offline draft, not a working integration with an arbitrary service.

## Installing a preview build

The supplied local wheel installs the same application without a source checkout:

```sh
uv tool install --python 3.12 /absolute/path/to/agentqit-0.1.0-py3-none-any.whl
```

For browser support, use the local wheel with its extra:

```sh
uv tool install --python 3.12 'agentqit[browser] @ file:///absolute/path/to/agentqit-0.1.0-py3-none-any.whl'
qa browser install --headless-only
```

These local-file installations do not track future PyPI releases. After publication,
reinstall from the index to opt into index-based upgrades. Keep the workspace outside
the tool environment in either case.

## Updates

Publishing a release makes it available on PyPI. It does not replace existing
installations. Users choose when to update:

```sh
uv tool upgrade agentqit

# Repeat when the installed Playwright version changes.
qa browser install --headless-only
```

Finish active tasks and close the MCP process before upgrading, then restart it.
An already running process continues using its loaded code. Workspace YAML and
credentials are outside the installation and are not overwritten by this workflow.
Playwright versions require matching browser binaries; see
[Playwright's browser documentation](https://playwright.dev/python/docs/browsers).

uv respects the version constraints used at installation. A version pinned with
`==` stays pinned during a normal upgrade. To select a specific published version
or roll back, reinstall with its exact version, for example:

```sh
uv tool install --force --python 3.12 'agentqit[browser]==0.1.0'
qa browser install --headless-only
```

That version must exist on the index. Keep the browser extra when reinstalling a
browser-enabled application. A rollback also requires workspace configuration
compatible with that version. CI integrations should pin reviewed versions and
update them deliberately.

Automatic background installation, an update-check command, and update notifications
are not implemented. The recommended initial behavior is an explicit upgrade through
the package manager, without replacing code during an agent's active task.

## When a plugin is useful

Local YAML catalogs and built-in auth cover ordinary integrations. A plugin is useful
for sharing a catalog across teams or adding custom auth/TLS composition or operation
handlers. It is an independent Python distribution, not a patch to AgentQit.
Install it in the same isolated environment as the application:

```sh
uv tool install --python 3.12 --with YOUR_PLUGIN 'agentqit[browser]>=0.1,<0.2'
```

`YOUR_PLUGIN` is a placeholder for the approved plugin distribution, not a required
AgentQit dependency. An unrelated Python environment is not visible to the installed
application. Plugin version constraints must remain compatible with AgentQit; see
[the plugin API](plugin-api.md). Plugins execute trusted Python code.

## Release delivery

The [release guide](releases.md) defines local checks, the prepared manual build/publish
workflow, disabled-by-default publication, and later owner-authorized activation.
The [compatibility policy](compatibility.md) defines patch updates, schema changes,
plugin pins, and explicit migrations. Installation and updates never rewrite workspaces.

Container images, native installers, and a separate runtime distribution can follow
demonstrated demand. The initial distribution keeps one installation and release
path, with MIT licensing and public dependency sources.
