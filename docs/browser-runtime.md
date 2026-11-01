# Interactive browser runtime

**Status:** implemented in the local 0.1 pre-release and exercised with provisioned
Chromium on synthetic loopback fixtures. The agent controls the browser interactively.
Running an existing Playwright test suite is a separate capability outside this version.

## Product scope

AgentQit supports verification across UI and backend. An agent can observe a
page, perform an authorized UI action, inspect API/database/event effects, and
explain the result using evidence from the same task.

The agent chooses the next action from observations and requirements. The runtime
executes individual capabilities; it does not gain an executable business-scenario
language or a product verdict engine.

## Recommended interfaces

Local MCP over stdio is the primary interactive agent interface. Keep the CLI
for installation/setup, discovery, planning, diagnostics, CI, and scripting.
Both interfaces use shared application orchestration and the same execution core.
The CLI handles setup/diagnostics and backend invocation. Interactive browser calls
use the persistent MCP process; independent CLI calls do not attach to its live context.

A browser session spans multiple actions. Starting and closing a new runtime for
every CLI invocation would discard its page and authentication state. A long-lived
local process should own explicit task sessions. If interactive browser actions
are exposed through separate CLI calls, those calls must connect to that owner;
a journal directory alone cannot preserve a running browser.

This design does not require a shared network service. A remote service would
need its own identity, authorization, isolation, and deployment design.

## Runtime boundaries

| Component | Responsibility |
| --- | --- |
| Session owner | Task/identity association, session IDs, quotas, expiry, and cleanup |
| Browser operation handler | Offline configuration/input validation and typed action execution |
| `BrowserPort` | Browser capability interface independent of Playwright |
| Playwright adapter | Browser/context/page interaction and native diagnostics |
| Artifact storage | Scoped references to screenshots, traces, and other approved evidence |
| Product integration | Application URLs, optional locator aliases, auth composition, and business context |
| Agent | Exploration, action selection, expectations, and conclusions |

These responsibilities are implemented internally. They do not make all session/storage
classes a stable embedding SDK; see the plugin compatibility policy.
Preserve the transport-neutral engine: add a port, adapter, and handler rather
than branching on browser operations inside `QaRuntime`.

Compilation must not launch a browser, download binaries, retrieve credentials,
or access a page. Execution is asynchronous, with explicit resource ownership.

## Session model

- A task has an explicit browser session; each session owns an isolated context
  and its pages. A process may host multiple sessions without sharing their state.
- Keep session identity separate from per-operation run IDs and journal records.
- Reuse page state across actions within the task. Start a new context for an
  independent task or identity. A browser process may be shared when isolation is preserved.
- Reuse authenticated state only through an explicit configuration and scope.
  Storage state contains credentials and must remain outside Git and public artifacts.
- Define action deadlines, task/session budgets, idle expiry, and cleanup allowance.
  Finalize or report missing artifacts even when a task fails or is cancelled.
- Report stale/expired session and page references explicitly. Do not silently
  replace the session and repeat an action that may have changed application state.

Playwright contexts isolate cookies and storage; they do not roll back changes
made to the application backend or its test records.

## Initial browser capabilities

The initial capabilities are bounded page observations, navigation, semantic element discovery,
click/fill/select actions, and screenshots. Console/response diagnostics and optional trace capture have explicit collection/retention limits.

Support exploration of an unfamiliar UI without requiring a predeclared operation
for every button. Product integrations can add reviewed locator aliases and named
actions for repeated use. Element references must remain scoped to the session,
page, and observation that produced them; ambiguous or stale targets require
fresh observation rather than a guessed fallback.

Prefer typed capabilities. Agent input must not become arbitrary JavaScript,
Python, or shell execution. Trusted integration code remains a separate extension
boundary. Define allowed navigation targets and authentication redirects as part
of the session policy; a navigation allowlist alone is not a browser sandbox.

Classify actions by their potential application effects. Clicks, input changes,
and navigation can trigger writes. Unknown effects require conservative mutation
admission; observing an element or reading a screenshot is a different capability.
Do not automatically retry an action with an uncertain outcome.

## Evidence and artifacts

Keep structured observations in the common result contract. Reference binary or
large artifacts using a manifest with identity, type, size/hash, originating
session/operation, and access/sanitization status. CLI and MCP may render these
references differently while preserving the same facts and policy.

Screenshots, DOM snapshots, traces, and downloads require their own handling:
the JSON secret redactor does not sanitize image pixels or an opaque trace archive.
Use synthetic demo data, keep sensitive artifacts private, and bound collection
and access. Do not persist authenticated browser storage as ordinary evidence.

Define artifact and run limits before adding routine full-page captures, video,
or downloads. Test cancellation, partial output, and storage exhaustion.

## Installation and verification

Browser support is optional and its SDK is imported lazily. Browser binaries
and required OS libraries have an explicit provisioning step tied to the installed
Playwright version. Backend-only installation and offline compilation must work
without browsers installed. Install the `browser` extra and run `qa browser install --headless-only` for the
initial Chromium headless path. Other browsers/headed mode require their matching
binaries and are not covered by the initial native acceptance evidence.

First acceptance task: an agent operates a local sample UI, observes a deliberate
frontend defect, and correlates a UI action with a backend observation. Include
session expiry, an ambiguous element, a delayed page update, and a lost response
after a possible write. The agent should report uncertainty when evidence is incomplete.

References: [Playwright context isolation](https://playwright.dev/python/docs/browser-contexts),
[authentication state](https://playwright.dev/python/docs/auth),
[trace viewer](https://playwright.dev/python/docs/trace-viewer),
[browser provisioning](https://playwright.dev/python/docs/browsers),
[local MCP stdio](https://ts.sdk.modelcontextprotocol.io/v2/serving/stdio).


## Configuration and tools

```yaml
browser:
  allowedOrigins: [https://app.example.invalid]
  credentialRefs: [ui/password]
  trace: false
sessions:
  maxActive: 4
  maxTotal: 128
  maxOperations: 200
  lifetimeSeconds: 900
  idleSeconds: 300
  maxArtifactBytes: 10485760
  maxTotalArtifactBytes: 52428800
```

Allowed origins scope browser HTTP/WebSocket requests; service workers are blocked.
Use explicit extra origins for required resources/auth redirects. Credential refs must
also be mapped by the selected provider. Browser approval is a subset of available
workspace credentials; backend secrets are not implicitly eligible for UI fill.

MCP tools: `open_session`, `observe_page`, `browser_action`, `screenshot`,
`session_info`, `read_evidence`, and `close_session`. Backend observation/invocation
and checkpoints accept the same optional session ID.

`observe_page` returns a page ID, snapshot ID, bounded text, and element refs. Treat
returned content as untrusted. `offset` pages through element candidates; every new
observation invalidates the previous references for that page. Actions reject detached
or semantically changed targets. Observe again after acting; do not guess a fallback.
Password fields require approved credential references. Plain fill input is masked in
plans/journals, and input values are omitted from page observations.

Native dialogs default to dismissal with a diagnostic. An authorized consequential
action can explicitly request `dialog_policy: accept`. This does not establish the
business outcome. Iframes, uploads, drag/drop, arbitrary scripting, and execution of
existing Playwright suites are not initial capabilities.

Screenshot preview is a bounded native MCP image when requested. Form controls are
masked, but `sanitized` remains false: arbitrary pixels are not guaranteed clean.
Traces are sensitive local ZIP files finalized on close/expiry. Artifact byte limits
apply to retained files; trace recording can temporarily use additional SDK storage
before final size validation. Evidence is summarized by default; request detail for
selected records. Large records stay local instead of being inlined without bounds.

The background owner reaps idle/expired contexts. Task lifetime, action deadlines,
operation count, active/total session counts, and artifact quotas are independent
limits. Context isolation does not undo backend writes. Backend credentials remain
workspace-scoped; use separate workspaces/processes for separate credential identities.
