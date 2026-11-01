# Connect your backend

For an ordinary backend, start with [a local workspace](workspaces.md). The workflow
below creates a separately installable plugin for reuse or custom composition.
`RUNTIME_CHECKOUT` means this AgentQit checkout with its dependencies installed.

You need Python 3.12+, uv, canonical contracts for your backend, and a clear first
operation. Start with a read operation and synthetic data.

## Create a plugin

From `RUNTIME_CHECKOUT`, after dependencies have been installed:

```sh
uv run qa product init orders --directory ../qa-product-orders --output json
uv pip install --python .venv/bin/python --no-deps -e ../qa-product-orders
uv run qa product list --output json
```

The scaffold creates a separate package with an empty registry, agent instructions,
a schema snapshot, and an offline compilation test. It does not invent operations,
authentication, or a working QA profile. Its runtime dependency is pinned to the
installed distribution that supplied the `qa` command.

## Add one operation

Ask an agent to follow [the integration guide](agent-integration.md). Supply the
canonical contract, its revision, requirements, and relevant service sources.
Keep the plugin's operations and auth/TLS composition in its own package.

For a local OpenAPI contract, create a separate draft:

```sh
uv run qa product import-openapi \
  --source ../contracts/orders/openapi.yaml \
  --contract-root ../contracts \
  --directory ../orders-draft --service orders --output json
```

Read the import report, including every skipped operation. Review wire names,
constraints, provenance, and response sensitivity before moving a declaration
into the plugin. Import success is not proof of completeness or correctness.

## Validate locally

```sh
uv run qa product validate --product orders --output json
uv run pytest ../qa-product-orders/tests
```

The plugin's offline profile must declare every resource needed by its operations.
Use fake ports and `.invalid` addresses. A JSON schema validates structure;
compilation checks references and bindings; request/response tests check behavior.

## Prepare a QA profile

Provide a nonsecret `ProductSetup` template, or pass an explicit template:

```sh
uv run qa config requirements --product orders --operation orders.get \
  --template ../team-qa.yaml
uv run qa config init --product orders --operation orders.get \
  --profile first --template ../team-qa.yaml
uv run qa doctor orders.get --product orders --profile first
uv run qa plan orders.get --product orders --profile first \
  --param orderId=sample-order-123
```

Replace `orders.get` and `orderId` with the actual reviewed operation and input.
`doctor` without `--network` and `plan` perform local checks. They may validate
local trust files but do not access QA or retrieve credentials.

Live invocation is a separate authorized task. Successful execution returns facts;
the caller still compares them with the expected behavior.
