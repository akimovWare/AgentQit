# Architecture

AgentQit separates requirements and caller reasoning from deterministic execution.

```text
Requirements + canonical contracts
                |
          Human / agent
                |
        local MCP / qa CLI
                |
   WorkspaceApplication / SessionManager
                |
      CatalogCompiler -> QaRuntime
                |           |
        Product plugin  Operation handlers
        catalog / setup     |
        auth / guides   Ports -> adapters -> UI / backend
```

| Component | Owns |
| --- | --- |
| `qa_runtime` | Contracts, compilation, execution, lifecycle, security, ports, adapters |
| `qa_cli` | Argument parsing, configuration selection, rendering, authoring commands |
| `qa_mcp` | Stdio tools, protocol rendering, and application lifecycle |
| Workspace | Profile references, local catalogs, auth composition, browser/session policy |
| Product plugin | Reusable catalog, optional composition, setup, guides |
| Session owner | Browser contexts, expiry, quotas, artifacts, task journal |
| Caller | Requirements, authorized scope, check selection, business conclusions |

## Execution

`CatalogCompiler` validates inputs, configuration, resource references, and bindings
synchronously without transport I/O or credential lookup. `QaRuntime` executes the
compiled operation through asynchronous handlers and ports. `aclose()` releases
owned resources.

The engine is transport-neutral. Handlers compile typed requests and execute them
through adapters. Core imports neither CLI nor application-specific product code.
CLI discovers plugins through `qa_runtime.products`; a versioned `ProductPlugin`
provides its catalog and runtime components.

Ordinary integrations use local YAML workspaces. Independent product packages
supply reusable catalogs or custom auth/TLS/handler composition. Plugins execute
trusted Python code. See [the plugin API](plugin-api.md).

## Discovery

Operations are catalog entries. Search returns bounded metadata; descriptions
provide the chosen operation's parameters and bindings. Catalog entries do not
become separate CLI commands or MCP tools. Search matches IDs and purposes.
See [CLI/MCP behavior](interface-comparison.md).

## Browser sessions and evidence

The MCP process owns browser contexts across actions. Each task has an explicit
session; references are scoped to its page and observation. Browser and backend
operations can append observations to the same journal. The CLI executes independent
backend operations; it does not attach to live browser contexts.

JSON redaction, browser artifact privacy, and workspace credential scope are
separate controls. Context isolation preserves task boundaries and does not undo
backend writes. See [browser behavior](browser-runtime.md) and [security](../SECURITY.md).

## Contracts

Profiles and catalogs use strict schemas and documented YAML aliases. Inputs have
typed validation and bounded resource bindings. CLI and MCP share `CommandResult`,
error codes, and uncertain-outcome semantics. Mutation admission precedes I/O;
credentials use explicit providers, recursive redaction, and validated TLS.

The caller compares observations with independently supplied requirements. A
transport response or action acknowledgment is not a business verdict. Unknown
write outcomes require state inspection before retrying.

[Compatibility](compatibility.md) defines the supported schemas and extension surface.
