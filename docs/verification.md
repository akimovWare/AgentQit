# Verification

Tests use fake transports, generated test certificates, and owned synthetic
loopback applications. Application-environment access and real credentials are
not required.

## Development checks

```sh
uv sync --frozen --extra browser
uv run ruff check src tests tools examples/orders/src
uv run ruff format --check src tests tools examples/orders/src
uv run pre-commit run --all-files
uv run python tools/check_public_content.py
uv run python tools/export_contracts.py --check
uv run qa browser install --headless-only
AGENTQIT_BROWSER_TESTS=1 uv run pytest
```

Browser tests require explicit provisioning. They cover context isolation,
stale and changed references, credential input, screenshots, traces, native
dialogs, and UI/API observations. Orders Lab supplies correct and deliberately
defective UI behavior. Business assertions belong to the acceptance tests.

The standard suite runs without browser binaries. CI covers Python 3.12/3.13 on
macOS/Linux, plus Chromium, interface-payload, and publication-content checks.
See [GitHub Actions](https://github.com/akimovWare/AgentQit/actions/workflows/ci.yml)
for results on each commit. Windows is unsupported by the POSIX journal/artifact
implementation. Model reasoning and independent LLM evaluations are outside the
runtime test suite.

## Package checks

```sh
uv build
uv run python tools/verify_wheel.py dist
uv run python tools/verify_archives.py dist
uv run twine check --strict dist/*.whl dist/*.tar.gz
uv build examples/orders
```

Archive checks validate distribution identity, public dependency sources, source
content, and exclusion of operational files. Schema snapshots and a frozen v1
consumer workspace cover configuration/result compatibility. Release-gate tests
cover source/version/tag consistency and stale-archive rejection.

## Installed application

Use a fresh environment outside the checkout. Run `tools/smoke_installed.py` with
isolated Python (`-I`) and a new `--output` directory. It verifies package import
origins, version entry points, schema export, CLI JSON planning, and MCP stdio.
`--with-example --browser` also exercises the independently installed Orders wheel,
UI creation, API observation, PNG capture, and trace finalization.

CLI parsing checks reuse the installed application; subprocess checks cover entry
points and stdio. Startup budgets and runtime execution deadlines are separate.
Synthetic records and artifact summaries remain in the output directory.

Provisioning uses public package/browser downloads. Configure trusted proxy/CA
settings through the relevant tool's supported options. Keep TLS verification
active. Local reports, trust material, credentials, journals, and caches remain
outside version control and distribution archives.

See [interface accounting](interface-comparison.md) and [release checks](releases.md).
