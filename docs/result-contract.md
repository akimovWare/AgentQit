# Result contract

CLI JSON output is exactly one `CommandResult` document. Its `schema_version` is
`1.0`. Diagnostics must not corrupt stdout. The same facts and errors are reused
by application adapters; MCP protocol framing is a separate transport layer.
`--help` and `--version` produce discovery text rather than operation results.

## Execution and observations

The outer `status` describes execution: `success`, `failure`, `timeout`, `blocked`,
or `partial`. It does not establish whether a business requirement is satisfied.
An HTTP 500 received from a backend can be a successful observation with
`result.status_code: 500`. The agent compares that fact with the task's expectation.

Stable fields include `command`, `exit_code`, `run_id`, `environment`, `started_at`,
`finished_at`, `duration_ms`, `result`, `error`, `correlation`, `evidence`, and
`warnings`. JSON rendering may omit null fields. Correlation includes a W3C trace
ID and traceparent when execution generates them.

| Exit code | Meaning |
| --- | --- |
| 0 | Execution completed |
| 2 | Invalid input or configuration |
| 3 | Assertion failure category reserved by the contract |
| 4 | Environment or transport unavailable |
| 5 | Authentication or authorization failure |
| 6 | Internal error |
| 7 | Execution blocked |
| 8 | Partial-result category |

Do not infer a business pass/fail from an exit code alone. Timeout errors may use
an environment/authentication exit category and `status: timeout`.

## Error and mutation outcomes

`ErrorItem` includes a machine-readable `code`, safe `message`, optional `source`,
`operation`, `http_status`, `details`, `hint`, `documentation`, and retry metadata.
Unexpected exceptions are normalized without exposing arbitrary exception text.

`execution_outcome` is one of:

- `not_sent`: the effect was not sent, including admission/input rejection.
- `confirmed`: a transport outcome is known; inspect the facts before concluding.
- `unknown`: a mutation might have taken effect despite a missing response.

`retry_advice` can be `read_state_first`, `same_key_only`, `safe`, or `do_not_retry`.
Unknown mutation outcomes are not automatically retryable. A durable pending
journal record is also not proof that repeating an action is safe.

Mutation admission happens before I/O. Mutating operations require explicit
`--allow-mutation` and exact `environment: qa`; catalog admission labels remain
discovery metadata rather than an independent authorization system.

The workspace/MCP application also has an operator-controlled mutation capability.
A caller's `allow_mutation` argument cannot enable it. The workspace budget wraps
authorization and execution; an uncertain write timeout remains `unknown` and
requires state inspection. The separate read-only MCP tool rejects mutating IDs.

## Transport facts

- HTTP returns `status_code`, public/redacted `headers`, and `body`. A configured
  `responseBodyFields` allowlist is fail-closed; an incompatible response shape
  produces a `projection_error` rather than exposing the original body.
- Database observations return declared, bounded rows and counts. Empty results
  are observations; the caller establishes whether absence is expected.
- Kafka publishing reports topic/partition/offset. Consumption reports records
  and bounded observation diagnostics. Offset observations expose the committed
  **next** offset; a missing offset is null, distinct from zero.
- S3 and Kubernetes results preserve bounded transport facts and diagnostics.
  Consult the selected operation schema for fields and limits.

All free-form result and error fields pass through recursive secret redaction.
Binary representations and strings containing JSON also require redaction. A
redacted or incomplete observation may not support the desired conclusion.

## Browser and task evidence

The result envelope version is 1.0. Browser observations carry session-scoped
page/snapshot/element references, bounded text, truncation markers, diagnostics,
and `content_trust: untrusted`. Navigation, click, fill, and select are mutating;
observe and screenshot are read-only capabilities. A successful action acknowledgment
is not a claim about backend effects.

Session IDs identify task state; `run_id` identifies an individual operation. The
execution context carries the optional task session without adding transport logic
to the engine. Backend/browser calls using that session share a durable journal;
returned `evidence` contains retained record paths. Cancellation can leave a pending
attempt whose outcome must be investigated.

Artifact metadata includes opaque ID, session ID, optional originating run ID,
media type, size, SHA-256, label, local file name, `private`, and `sanitized`.
Screenshots return metadata; MCP can additionally attach a bounded PNG content block.
Session traces are local sensitive archives and may lack a single originating run.
JSON redaction does not sanitize arbitrary pixels or trace archives.

Closed/expired sessions and stale targets return explicit errors. The session manager never silently
recreates browser state and repeats the previous action. Cleanup failures are warnings
and can make session closure partial without replacing earlier business observations.
