# Compatibility policy

Package updates and configuration migrations are separate actions.
AgentQit preserves workspace files during installation, startup, planning, and
upgrade.

## Python and platforms

Supported interpreters are standard CPython 3.12, 3.13, and 3.14 on macOS and
Linux. CI validates the offline suite, package build, and isolated installation
on both platforms for each version. Chromium acceptance runs on Linux for all
three versions. Free-threaded Python, PyPy, and Windows are outside the tested
support matrix. See [verification](verification.md).

## Versioned contracts

| Surface | Current contract | Promise |
| --- | --- | --- |
| Workspace manifest | `apiVersion: agentqit/v1` | Existing valid v1 manifests keep their meaning in 0.1.x |
| Profile | Referenced by a v1 workspace | The reviewed v1 profile fields/defaults follow the same policy |
| Operation catalog | `apiVersion: qa.runtime/v1` | Existing declarations remain accepted in 0.1.x |
| CLI/MCP results | `schema_version: "1.0"` | Envelope fields, meanings, exit codes, and uncertain-outcome semantics remain compatible in 0.1.x |
| Product plugins | `qa.product/v1` and the [documented Python surface](plugin-api.md) | Pin the exact tested AgentQit version |
| Local session/journal state | Internal | Finish and close tasks before upgrading; active sessions are not portable across versions |

Profile filenames are user choices. `qa.yaml` and the generated `profile.yaml` use
the same schema. Credentials remain in their explicitly selected external source.

## Package versions

Use PEP 440 versions: for example `0.1.0`, `0.1.1`, and `0.2.0rc1`.

- A patch release (`0.1.0` to `0.1.1`) fixes behavior while preserving these contracts.
  Bug fixes can change incorrect observations; document changes in the changelog.
- During 0.x, a minor release can change a documented extension interface. Publish
  the changes, tested plugin versions, and migration instructions before adoption.
- An incompatible YAML change requires a new schema identifier, even during 0.x.
  Do not change the meaning of an existing field under the same identifier.
- Additive optional fields need schema review. Consumers should tolerate documented
  additive result fields; producers preserve existing required fields and types.
- A security fix can intentionally reject a previously accepted unsafe configuration.
  Explain the rejection and a safe migration explicitly.

Unknown schema versions and extra configuration fields fail closed. There is no
implicit downgrade, coercion to an unsupported schema, or automatic file migration.
The profile has no independent version tag; its compatibility is governed by its
workspace/catalog contract and the package policy above.

## Reviewing a contract change

The checked-in [JSON Schemas](schemas/) are a review baseline. They include public
YAML aliases and the result envelope. They are not a replacement for compilation
or behavioral validation.

```sh
qa workspace schema
uv run python tools/export_contracts.py --check
```

CI checks both these snapshots and a frozen v1 consumer workspace. To refresh
snapshots after reviewing an intentional compatible addition:

```sh
uv run python tools/export_contracts.py
```

Review the schema diff, document the change, and keep the old consumer fixture.
For an incompatible revision, add a new baseline and explicit migration guidance
instead of replacing the old contract silently. Schemas cannot prove preservation
of runtime semantics; behavioral tests remain necessary.

## Patch 0.1.1

Vault-backed workspace CLI and MCP operations construct the credential source
correctly and honor an explicitly supplied application credential environment.
Existing profiles, operation catalogs, and result schemas remain compatible;
offline planning does not retrieve credentials.

## Upgrades and rollback

Finish tasks, stop MCP, back up the workspace, read release notes, and update the
application separately. Re-provision browser binaries if Playwright changed, then
restart the host. Use `qa --version` or `agentqit-mcp --version` to identify the build.

For an incompatible change, migration instructions must identify changed fields,
provide before/after YAML, describe the offline validation command, and explain
rollback. Migration tools must write a new file or require explicit opt-in and a backup.
The v1 schema has no migration command.

The package-index installation range is `>=0.1,<0.2`, so normal upgrades
stay within 0.1.x compatibility. Change that constraint explicitly to adopt a new
minor series after reading its migration notes. CI should pin a reviewed version.
Plugins use exact runtime pins or an explicitly tested compatible range. See
[installation and updates](distribution.md).
