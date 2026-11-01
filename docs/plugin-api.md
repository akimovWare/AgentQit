# Plugin API and compatibility

The distribution is `agentqit`; the command remains `qa`, and runtime imports remain
`qa_runtime`. Product packages register in `qa_runtime.products` and return a
`ProductPlugin` with API version `qa.product/v1`.

The initial supported extension surface is:

- `qa_runtime.ports.product`: plugin, guide, setup, components, prerequisites, and
  declared resource/auth dependencies.
- `qa_runtime.ports`: transport, handler, credentials, auth, and TLS protocols.
- `qa_runtime.models`: documented profile/operation/input/request/context models
  needed by those interfaces.
- `RuntimeFailure`, `ExitCode`, and the common result contract.
- The compiler/registry interfaces used by generated offline integration tests.

Internal CLI functions, MCP implementation classes, concrete session storage internals,
and private underscore-prefixed methods are not an embedding SDK contract.

This is a 0.1 pre-release. Scaffolds and the bundled example pin the runtime version
that supplies `qa`. Keep the dependency, schema snapshot, and integration tests aligned.
See [the compatibility policy](compatibility.md). Compatible plugin dependency ranges
will be introduced only after testing those ranges;
the plugin API tag by itself is not proof that every internal Python import is stable.

Local workspaces need no installed product package. An optional workspace `product`
loads an installed plugin's composition, catalog, and guides; local header auth must
not conflict with that plugin. The shared application provides the browser handler
when browser settings are configured.

`ResourceBinding(section="none", config_field="")` declares a handler whose resources
are task-owned rather than selected from profile services/clusters. Its explicit auth
dependencies still participate in onboarding. Unknown custom bindings do not gain
implicit access to new profile fields.

`InputRedactor.redact_input(plan, params)` can supply a safe input representation
for plans/journals without changing actual execution input. Browser fill masks plain
text values in these representations; credentials use references and lazy resolution.

Independent integration validation must install the built plugin with the runtime
in the same environment. A source-path import is useful during development but
does not prove that packaged catalogs, contracts, guides, and assets are complete.
