# Security

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/akimovWare/AgentQit/security/advisories/new)
to contact the maintainers. Include the affected version, a minimal synthetic
reproduction, expected impact, and any relevant redacted evidence. Do not disclose
unpatched vulnerabilities or sensitive data in public issues.

The project is a preview. Security fixes target the current preview branch and
announced releases; there is no long-term support or response-time guarantee.

## Trust boundaries

- The operator selects a trusted workspace/catalog and any installed Python plugins.
  A plugin is trusted code, not a sandboxed extension.
- The MCP server is a local stdio process. It is not a shared remote/multi-user service.
- Mutation requires host authorization, caller opt-in, and exact `environment: qa`.
  These guardrails do not prove the identity or permissions of a remote environment.
- Browser contexts isolate task cookies/storage. They do not roll back backend data.
- Browser HTTP/WebSocket targets are restricted by configured origins. This is not
  a general OS/network sandbox or a guarantee against native browser vulnerabilities.
- Credentials use explicit references. Browser input has its own approved-reference
  subset; a backend credential is not automatically available for UI input.
- JSON redaction does not sanitize arbitrary pixels or an opaque trace archive.
  Screenshot previews are sent to the configured MCP client when requested; artifacts
  remain local/private by default. Review them before any further sharing.

Only synthetic or explicitly authorized QA targets should be used. Production access,
untrusted executable plugins, and a public remote endpoint are outside this release's scope.
