# Verification

All automated checks use fake transports, generated local certificates, or owned
synthetic loopback applications. No private credentials or QA network are required.

```sh
uv sync --frozen --extra browser
uv run ruff check src tests tools examples/orders/src
uv run ruff format --check src tests tools examples/orders/src
uv run pre-commit run --all-files
uv run python tools/export_contracts.py --check
uv run qa browser install --headless-only
AGENTQIT_BROWSER_TESTS=1 uv run pytest
uv build
uv run python tools/verify_wheel.py dist
uv run python tools/verify_archives.py dist
uv run twine check --strict dist/*.whl dist/*.tar.gz
uv build examples/orders
```

Provisioning uses public Playwright downloads and is an explicit setup action.
If a trusted proxy/CA setup is required, configure it for the installed Playwright
environment; certificate verification must remain enabled.

Browser-marked checks are opt-in when binaries are provisioned. They exercise
context isolation, stale/changed references, password input handling, screenshots,
traces, and UI/API observations through MCP. The Orders fixture includes correct
and deliberately defective UI behavior. Business assertions live in acceptance
tests outside the runtime.

Build/install validation must run outside the source checkout using a fresh
environment. `tools/smoke_installed.py` checks package import origins, plain local
workspace planning, actual stdio communication, and optional independently installed
Orders/browser behavior. Run it with isolated Python (`-I`) and an unused output
directory. It creates only synthetic records and retains summaries/artifacts there.

The GitHub workflow covers Python 3.12/3.13 on macOS/Linux and a Linux
Chromium job. A local check is not evidence that those remote jobs have run; the
source preview is public. Record the actual tested environment and results.
Windows is not supported by this version's POSIX journal/artifact implementation.

There is no external LLM evaluation in the automated suite. It verifies the protocol,
execution mechanisms, documented interaction path, and known fixture outcomes.
Independent user/agent evaluation remains valuable before expanding release scope.


Public CI action references follow the [official uv integration guide](https://docs.astral.sh/uv/guides/integration/github/).
Package publication is disabled by default in the manual release workflow.


Release preparation also checks frozen consumer configuration, schema snapshots,
version identity, dirty-source/tag rejection, and stale wheel rejection. The
[release guide](releases.md) separates a local candidate from later remote activation.
The [interface benchmark](interface-comparison.md) measures actual workflow payloads
without calling a model API or claiming autonomous reasoning quality.

Installed checks use bounded startup budgets for cold imports. CLI parsing checks
reuse the installed application in one process; separate subprocess checks verify
both entry points, a JSON plan, and actual MCP stdio. This is installation coverage,
not a claim about startup latency. Runtime execution budgets remain independent.


## Verified preview capabilities

Local verification covered the complete runtime suite on macOS and Linux with
provisioned Chromium, plus focused release-gate checks. Fresh wheel and sdist
installations verified exported schemas, CLI JSON planning, and actual MCP stdio
without source-path fallback. The independent Orders wheel exercised UI creation,
API observation, screenshots, and finalized traces through an installed application.

CI results on each commit are the current public evidence. Tests use synthetic
fixtures only. Local reports, records, journals, trust material, and provisioning
caches stay outside version control and distribution archives.
