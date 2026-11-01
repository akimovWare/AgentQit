# Contributing

Start with [the roadmap](docs/roadmap.md) and [architecture](docs/architecture.md).
Please follow the [community guidelines](CODE_OF_CONDUCT.md).

Keep changes small and explain the problem, resulting behavior, and validation.
For a new operation, prefer a workspace or product YAML catalog. Change the runtime when a reusable
capability is missing across systems.

The development workflow is:

```sh
uv sync --frozen
uv run ruff check src tests tools examples/orders/src
uv run ruff format --check src tests tools examples/orders/src
uv run pre-commit run --all-files
uv run python tools/check_public_content.py
uv run python tools/export_contracts.py --check
uv run pytest
uv build
uv run python tools/verify_wheel.py dist
uv run python tools/verify_archives.py dist
uv run twine check --strict dist/*.whl dist/*.tar.gz
```

Tests use fake ports and owned synthetic fixtures. They must run without access
to an application environment or real credentials.

Public interface changes include the JSON envelope, exit codes, operation schema,
plugin API, configuration aliases, and CLI arguments. Cover relevant boundaries
and update their documentation in the same change.

Packaging validation must inspect both wheel and source distribution, install
from built artifacts outside the checkout, and exercise an independently built
example plugin. Run focused tests first; repeat broader checks when changes or
failures justify it.

Write documentation, issue descriptions, and pull requests in English. Include
only synthetic data in reproducible examples. Review [AGENTS.md](AGENTS.md) before
using an agent to contribute.

Native browser acceptance and clean-install validation are described in [verification](docs/verification.md).

Read [compatibility](docs/compatibility.md) before changing a public contract and
[the release guide](docs/releases.md) before preparing a version. Publication is
disabled by default and requires a separate owner instruction.

Before sharing changes, review the full diff and selected history for identifying
information and operational data. Publication checks flag developer-local paths,
non-example email addresses, unreviewed URL hosts, and operational files. Optional
local denylists stay outside version control. Never add sensitive literals as test
fixtures or scanner rules in this repository.
