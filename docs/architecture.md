# Architecture

AgentQit separates task reasoning, execution, and system knowledge.

```text
Task requirements + canonical contracts
                  |
             Human / agent
                  |
         local MCP / qa CLI
                  |
     WorkspaceApplication / SessionManager
                  |
        CatalogCompiler -> QaRuntime
                  |             |
          Product plugin    Operation handlers
          catalog / setup       |
          auth / guides     Ports -> adapters -> UI / QA backend
```

The caller chooses checks and interprets the evidence. The CLI selects a product,
profile, and inputs. Compilation validates references and bindings before I/O.
The runtime executes the selected operation through asynchronous ports.

Interactive Playwright support provides frontend verification. It uses
an explicit session owner, `BrowserPort`, Playwright adapter, and artifact storage.
Browser state lives across actions within a task; per-operation CLI processes and
the existing journal do not provide that lifecycle. See [the browser design](browser-runtime.md).

Local MCP over stdio is the interactive entry point, sharing application orchestration
with the CLI. The session owner lives across actions and closes resources on task/server shutdown.

| Component | Owns |
| --- | --- |
| `qa_runtime` | Contracts, compiler, engine, lifecycle, security, ports, adapters |
| `qa_cli` | CLI, settings, rendering, product scaffolding, draft imports |
| Product plugin | Operation YAML, source metadata, setup, optional composition, guides |
| Session owner | Browser lifecycle, expiry, quotas, artifacts, task journal |
| Caller | Expectations, authorized scope, check selection, business verdict |

Plugins are discovered through `qa_runtime.products`. The versioned `ProductPlugin`
owns both registry and runtime components. Python packages provide code and assets;
the configuration selects installed plugins and resources.

The distribution contains the runtime, CLI, and MCP adapter. The independent
synthetic Orders plugin demonstrates integration. Applications can use local
workspaces or install a compatible plugin.

## Public contracts

- Synchronous compilation, asynchronous execution, and explicit resource cleanup.
- Strict profiles and registries, including existing YAML aliases.
- Typed input validation and resource bindings.
- One `CommandResult` JSON document and stable exit codes.
- Mutation gating before I/O, recursive secret redaction, and TLS validation.
- Facts and diagnostics without a runtime business evaluator.

The [plugin API](plugin-api.md) defines the supported Python extension surface.
The `qa_runtime` namespace identifies that surface; other implementation modules
are governed by the documented compatibility policy.
