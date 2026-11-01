# Releases

Source repository: `akimovWare/AgentQit`. Package publication is maintainer-controlled
and disabled by default.

## Version and metadata

`pyproject.toml` defines the installed version. CLI and MCP read distribution metadata.
For a release, update the version, changelog, example's exact runtime dependency,
compatibility notes, and affected fixtures. Update dependencies through uv.

The build backend is pinned. Strict Twine validation checks the emitted package
metadata. [Twine release notes](https://github.com/pypa/twine/blob/main/docs/changelog.rst)
describe supported metadata versions.

| Field | Value |
| --- | --- |
| GitHub repository | `akimovWare/AgentQit` |
| Default branch | `main` |
| PyPI project | `agentqit` |
| Summary | `Application testing tools for AI agents` |
| License | MIT |
| Index description | `README.pypi.md` |
| Trusted Publisher owner | `akimovWare` |
| Trusted Publisher repository | `AgentQit` |
| Trusted Publisher workflow | `release.yml` |
| Trusted Publisher environment | `pypi` |

`README.md` is the repository overview; `README.pypi.md` is the package-index
page. Keep their installation instructions and availability accurate. The index
page uses absolute documentation URLs. Verify that its rendered Markdown and the
built wheel description match. Include both files in the sdist.

## Validate a release

Use an empty artifact directory and a new benchmark output directory:

```sh
uv sync --frozen --extra browser
uv run qa browser install --headless-only
uv run ruff check src tests tools examples/orders/src
uv run ruff format --check src tests tools examples/orders/src
uv run pre-commit run --all-files
uv run python tools/check_public_content.py
uv run python tools/export_contracts.py --check
AGENTQIT_BROWSER_TESTS=1 uv run pytest
uv run python tools/benchmark_interfaces.py --output .agentqit/context-review
uv build
uv run python tools/verify_wheel.py dist
uv run python tools/verify_archives.py dist
uv run twine check --strict dist/*.whl dist/*.tar.gz
```

Complete [fresh installation checks](verification.md) outside the checkout, then
commit the release source and verify identity:

```sh
uv run python tools/release_check.py dist --version 0.1.0
```

The check requires a clean source tree and matching version/changelog metadata.
It validates wheel source bytes/import origins, sdist contents, archive identity,
and dependency sources, then writes `dist/release-manifest.json` with the source
revision, sizes, and SHA-256 hashes. Stale same-version artifacts are rejected.
`--require-tag` also requires a dated changelog entry and a matching `vVERSION` tag.
Published versions are immutable; changed artifacts require a new version.

## GitHub workflow

`.github/workflows/release.yml` runs only by manual dispatch. `publish: false`
executes the CI matrix, builds and checks artifacts, and retains them as a workflow
artifact. Commit/tag pushes do not trigger package publication.

Upload requires all of:

- Dispatch with `publish: true`.
- `AGENTQIT_PUBLISH_ENABLED` set to exactly `true`.
- Dispatch from the matching version tag.
- Approval in the protected `pypi` environment.
- A matching PyPI Trusted Publisher.

The publish job verifies retained hashes and uploads the wheel and sdist without
rebuilding. OIDC supplies publishing credentials. The workflow creates neither a
version tag nor a GitHub release. Duplicate uploads fail for investigation;
`skip-existing` is disabled.

## Publish a package

1. Confirm PyPI name ownership, metadata, successful CI for the source revision,
   and the private reporting route in `SECURITY.md`.
2. Configure the protected environment and PyPI Trusted Publisher. Keep the
   publication variable `false` until upload is authorized.
3. Run a dry dispatch and review artifacts/hashes. Set package availability in
   both README files, date the changelog, and commit through a pull request.
4. Tag the validated commit with `vVERSION` and validate that exact revision.
5. Enable publishing and dispatch from the tag after maintainer authorization.
6. Check PyPI rendering and a fresh index installation. Publish GitHub release
   notes with installation, compatibility, and upgrade information.

References: [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/),
[PyPA release workflows](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/).
