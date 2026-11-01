# Agent instructions

## Project

AgentQit is a public source preview. Read [the roadmap](docs/roadmap.md),
[compatibility](docs/compatibility.md), and [the release guide](docs/releases.md) before changing code.
Package publication requires an explicit maintainer instruction. Source changes
use pull requests and the required CI checks.

## Architecture

- `qa_runtime` owns contracts, compilation, execution, ports, and adapters.
- `qa_cli` owns argument parsing, configuration selection, and rendering.
- Product plugins own their catalogs, topology, auth/TLS composition, and guides.
- Core must not import CLI or product packages. CLI discovers products through
  the `qa_runtime.products` entry point group.
- Add ordinary operations in a workspace or plugin YAML catalog. Add Python only for
  composition or a capability that existing handlers cannot express.
- Keep `compile()` synchronous and free of network and credential access.
  Execution and I/O ports are asynchronous; close resources with `aclose()`.
- Browser support uses [explicit task sessions](docs/browser-runtime.md),
  a browser port, and an adapter. Compilation does not launch or provision browsers.

## Behavior and evidence

- Requirements and canonical contracts define expectations. Service code helps
  explain implementation; it does not establish the expected business result.
- Return facts. Business assertions and verdicts belong to the caller.
- Preserve the `CommandResult` envelope and documented exit codes. JSON stdout
  contains exactly one document; diagnostics belong on stderr.
- Do not introduce executable scenarios, arbitrary SQL/topic inputs, shell,
  expression evaluation, or templates that execute code.
- `admission` is discovery metadata; it is not a separate authorization gate.

## Data and execution

- Use synthetic examples, reserved `.invalid` hostnames, and fake ports in tests.
- Keep system-specific contracts, source revisions, endpoints, records, and
  credentials out of the reusable runtime and public examples.
- Profiles contain nonsecret configuration and credential references only.
  Secrets enter through `CredentialSource` and use the shared recursive redactor.
- Keep TLS chain and hostname validation enabled.
- Keep authenticated browser state out of Git. JSON redaction does not sanitize
  screenshots or trace archives; handle those through scoped artifact policy.
- Live execution requires a user task naming the target environment and scope.
- Mutations require explicit user authorization, `--allow-mutation`, and exact
  environment `qa`. MCP additionally requires operator-granted host capability;
  a tool argument cannot grant it. Enforce admission before transport I/O.
- After authorized QA, report created record IDs and effects. Keep those records
  unless deletion was requested; restore temporary faults and locks.

## Changes

- Write public documentation and generated instructions in English.
- Update behavior, focused tests, and affected guides together.
- Normalize public errors through `RuntimeFailure` and the appropriate `ExitCode`.
- Change dependencies in `pyproject.toml` through uv; never hand-edit `uv.lock`.
- Start with focused tests, then run lint, format, and the full offline suite.
  Packaging changes also require a build, archive inspection, and installation
  checks outside the checkout.
- Include relevant validation results and supported scope with each change.

For a downstream plugin, follow [the integration workflow](docs/agent-integration.md).


Browser-marked acceptance uses owned synthetic loopback fixtures and provisioned
binaries. Run `AGENTQIT_BROWSER_TESTS=1 uv run pytest` after explicit browser setup.
The standard suite runs without browser binaries. Supported platforms and test
scope are documented in [verification](docs/verification.md).
