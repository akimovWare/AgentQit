# Changelog

## 0.1.1 - 2026-11-11

- Fix Vault credential-source construction for workspace CLI and MCP operations.
  Offline plans remain credential-free; execution honors the application's
  explicitly supplied credential environment.

## 0.1.0 - 2026-11-02

- CPython 3.12, 3.13, and 3.14 support on macOS and Linux.
- Product-neutral runtime and CLI with public dependency sources.
- Local YAML workspaces and independently installed product plugins.
- Local stdio MCP with bounded discovery, plans, observations, invocation, and checkpoints.
- Interactive Playwright task sessions, scoped snapshot references, typed actions,
  input masking, approved credential refs, screenshots, and private traces.
- Shared UI/backend journals, expiry/session/operation limits, and uncertain-outcome handling.
- Explicit environment credentials and standard Bearer/API-key header composition.
- Synthetic Orders UI/API, a separate plugin distribution, and deliberate frontend fault acceptance.
- Agent/contributor guides, CI, archive inspection, and isolated install checks.
- Workspace-aware MCP discovery: backend task/evidence tools remain available;
  browser-only tools appear only when browser capabilities are configured.
- Installed-version commands, workspace schema export, frozen v1 consumer checks,
  reviewed schema snapshots, and a documented compatibility/upgrade policy.
- Reproducible CLI/MCP context accounting against synthetic local backend workflows.
- Manual release preparation, source/tag/version checks, retained artifact hashes,
  strict package metadata validation, and protected Trusted Publishing.
