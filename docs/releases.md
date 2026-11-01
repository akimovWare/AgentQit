# Preparing and delivering a release

The public source repository is `akimovWare/AgentQit`. Package publication is
separate and remains disabled until the maintainer explicitly authorizes it.

## Local release candidate

The build backend is pinned to the verified Hatchling version. Twine 7 supports
the emitted [core metadata 2.5](https://github.com/pypa/twine/blob/main/docs/changelog.rst); keep artifact validation enabled when updating tools.
Keep `pyproject.toml` as the single source of the installed version. CLI and MCP
read the installed distribution metadata. For a new version, update the changelog,
the example's exact runtime dependency, compatibility notes, and any affected fixtures.
Regenerate dependency metadata through uv, never by editing `uv.lock` manually.

```sh
uv sync --frozen --extra browser
uv run ruff check src tests tools examples/orders/src
uv run ruff format --check src tests tools examples/orders/src
uv run pre-commit run --all-files
uv run python tools/export_contracts.py --check
AGENTQIT_BROWSER_TESTS=1 uv run pytest
uv run python tools/benchmark_interfaces.py --output .agentqit/context-review
uv build
uv run python tools/verify_wheel.py dist
uv run python tools/verify_archives.py dist
uv run twine check --strict dist/*.whl dist/*.tar.gz
```

Browser acceptance assumes explicit provisioning with `qa browser install --headless-only`.
Use a new benchmark directory on every run; it retains synthetic records and transcripts.
Inspect its [measurement limits](interface-comparison.md), not just the token totals.
Build into an empty `dist` directory, or a new output directory, to avoid stale artifacts.
Complete fresh wheel/sdist installation checks outside the checkout as described in
[verification](verification.md). Finish and commit intended source changes before the
identity check:

```sh
uv run python tools/release_check.py dist --version 0.1.0
```

This command checks clean source, version/changelog consistency, archive identity,
wheel source bytes and import origins, sdist source contents, and public dependencies.
An older artifact with the same version is not sufficient. It writes `dist/release-manifest.json` with the source
revision, sizes, and SHA-256 checksums. It never creates a tag or uploads files.
For a future tagged release, `--require-tag` also requires a dated changelog entry
and `v0.1.0` to name the checked-out commit. An existing package version must never be reused for different
artifacts; issue a new version instead.

## Prepared GitHub workflow

`.github/workflows/release.yml` has only a manual `workflow_dispatch` trigger.
Pushing a commit/tag or creating a GitHub release does not invoke publication.
Its default `publish: false` runs the reusable validation matrix, builds and checks
artifacts, and retains them as a workflow artifact. It performs no package upload.

Publication additionally requires all of:

- A manual dispatch with `publish: true`.
- Repository variable `AGENTQIT_PUBLISH_ENABLED` set to exactly `true`.
- Dispatch from the version's tag, matching the source and requested version.
- A protected `pypi` environment with a required reviewer configured by the owner.
- A PyPI Trusted Publisher bound to the exact personal repository, `release.yml`,
  and `pypi` environment.

The publish job receives OIDC permission, downloads the already verified artifacts,
checks their retained hashes, and uploads only the wheel and sdist. It does not
rebuild source, use a long-lived PyPI token, create a GitHub release, or push a tag.
A failed duplicate upload must be investigated; `skip-existing` is not enabled.
References: [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/),
[PyPA's release workflow guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/).

The workflow has been prepared for review; local syntax validation is distinct from
an actual GitHub run. Remote CI must succeed before the first public release.

## Page content and maintainer fields

| Field | Prepared value |
| --- | --- |
| GitHub owner / repository | `akimovWare/AgentQit` |
| Default branch | `main` |
| GitHub About description | `Application testing tools for AI agents: interactive browser control, reviewed backend operations, and evidence.` |
| Suggested topics | `ai-agents`, `testing`, `qa`, `mcp`, `playwright`, `python`, `cli` |
| License | MIT |
| PyPI project / initial version | `agentqit` / `0.1.0` |
| PyPI summary | `Application testing tools for AI agents` |
| PyPI description | `README.pypi.md`, Markdown, with absolute documentation URLs |
| Trusted Publisher owner | `akimovWare` |
| Trusted Publisher repository | `AgentQit` |
| Trusted Publisher workflow | `release.yml` |
| Trusted Publisher environment | `pypi` |

`README.md` is the GitHub/local overview; `README.pypi.md` is the shorter index page.
Keep installation commands and release status aligned. PyPI renders descriptions
outside the repository, so its description uses absolute URLs rather than relative
Markdown links. See [the renderer's tracked limitation](https://github.com/pypa/readme_renderer/issues/163).
Before upload, verify that the built wheel's description matches `README.pypi.md`,
all its documentation links are absolute, and the file is present in the sdist.
Use CI/version badges after the corresponding public workflows/releases exist.

## First package release

1. Recheck PyPI name availability and confirm repository/package metadata. The
   package name is not reserved by publishing source to GitHub.
2. Confirm successful GitHub CI for the exact source revision. Private vulnerability
   reporting must be enabled and `SECURITY.md` must name the active reporting route.
3. Configure the protected `pypi` environment and pending PyPI Trusted Publisher.
   Leave `AGENTQIT_PUBLISH_ENABLED` set to `false` until upload is authorized.
4. Run a dry release dispatch and review the artifacts/checksums. Update package
   availability text in both README files, date the changelog, commit the changes,
   and create the approved version tag. Revalidate that exact tagged revision.
5. After authorization, enable publishing and dispatch from that tag. Verify PyPI
   metadata and a fresh index installation, then publish the GitHub release notes.

A source-preview push does not publish a package or create a version tag. A local
check does not establish ownership of an unreserved PyPI name or prove a remote job.
