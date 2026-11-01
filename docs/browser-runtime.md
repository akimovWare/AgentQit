# Interactive browser runtime

Agents control Playwright through local stdio MCP. Browser and backend calls share
the runtime's input validation, mutation admission, and result contract. The caller
selects actions and interprets observations against requirements.

## Sessions

The MCP process owns browser contexts across actions. Each browser-enabled task session starts with
an empty, isolated context and owns its pages, references, journal, and artifacts.
Independent CLI calls do not attach to a live browser context.

| Component | Responsibility |
| --- | --- |
| Session owner | Session IDs, contexts, quotas, expiry, journals, and cleanup |
| Browser handler | Offline validation and typed operation execution |
| `BrowserPort` | Transport-independent browser capabilities |
| Playwright adapter | Browser/context/page interaction and diagnostics |
| Artifact store | Private files with scoped IDs and integrity metadata |
| Workspace | Allowed origins, credential references, and resource limits |
| Caller | Requirements, action selection, authorized scope, and conclusions |

Compilation is synchronous and offline. Browser provisioning, launch, credential
resolution, and page access occur outside compilation. Execution is asynchronous;
resources close on session expiry, explicit closure, or server shutdown.
Session/storage implementation classes are outside the [plugin API](plugin-api.md).

Context cookies and storage stay within the task. Browser authentication uses
explicitly permitted credential references. Authenticated storage is not ordinary
evidence, and context closure does not roll back backend records.

## Configuration

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

Origins scope browser HTTP/WebSocket requests. Service workers are blocked.
Configure extra origins for required resources and authentication redirects.
This restriction is a browser policy rather than an OS/network sandbox.

Credential references must be mapped by the workspace's provider and listed in
`browser.credentialRefs`. A backend credential is not automatically eligible for
browser input. Separate workspaces/processes provide separate credential scopes.

Task lifetime, idle expiry, action deadlines, operation count, session counts,
and artifact quotas are independent limits.

## Tools and actions

MCP exposes `open_session`, `observe_page`, `browser_action`, `screenshot`,
`session_info`, `read_evidence`, and `close_session`. Backend observations,
invocations, and checkpoints accept the same optional session ID.

1. Open a session with `browser: true`.
2. Navigate within configured origins.
3. Observe the page and use the returned page/snapshot/element references.
4. Perform one typed action, then observe again.
5. Correlate UI and backend observations through exact business IDs.
6. Close the session to release contexts and finalize traces.

`observe_page` returns bounded text and element metadata. `offset` pages through
element candidates. Input values are omitted, and page content is untrusted data.
Each observation invalidates the page's previous references. Detached or
semantically changed targets require another observation.

`browser_action` accepts navigation, click, fill, and select commands. These actions
can change application state and require operator capability, per-call mutation
opt-in, and exact `environment: qa`. No agent-provided JavaScript, Python, or shell
execution is accepted. Password fields require approved credential references;
plain fill values are masked in plans and journals.

Native dialogs default to dismissal with a diagnostic. An authorized action can
request `dialog_policy: accept`. An action acknowledgment does not establish a
backend business outcome. Unknown outcomes require state inspection before retrying.
Expired sessions and stale references return explicit errors.

Iframes, uploads, drag/drop, arbitrary scripting, and execution of existing
Playwright suites are unsupported.

## Evidence and artifacts

Browser and backend observations use the [common result envelope](result-contract.md).
The journal summarizes evidence by default; request record detail selectively.
Large records stay local rather than entering the model context without bounds.

Artifact manifests contain opaque IDs, MIME type, byte size, SHA-256, session/run
association, local filename, and privacy/sanitization metadata. Screenshots can
include a bounded native MCP image preview. Form controls are masked;
`sanitized: false` reflects that arbitrary pixels are not guaranteed clean.

Traces are private local ZIP files finalized on close or expiry. Recording can
use additional temporary SDK storage before final quota validation. JSON secret
redaction does not sanitize pixels or opaque archives. Keep artifacts local until
reviewed for disclosure, and retain created QA record IDs and effects in the report.

## Installation

```sh
qa browser install --headless-only
```

The installed application needs the `browser` extra. Provisioning downloads binaries
matched to the installed Playwright version. Backend-only use and offline compilation
need no browser package or binaries.

Chromium headless is covered by browser acceptance. Firefox, WebKit, and headed
mode require matching binaries and are outside that test coverage. The
[Orders example](../examples/orders/README.md) supplies a synthetic UI/API and
correct/defective UI modes for verification.

References: [contexts](https://playwright.dev/python/docs/browser-contexts),
[authentication](https://playwright.dev/python/docs/auth),
[traces](https://playwright.dev/python/docs/trace-viewer),
[provisioning](https://playwright.dev/python/docs/browsers),
[stdio](https://ts.sdk.modelcontextprotocol.io/v2/serving/stdio).
