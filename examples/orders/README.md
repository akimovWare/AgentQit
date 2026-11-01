# Orders Lab

A synthetic loopback-only UI/API for AgentQit. The runtime has no knowledge of
this application's order rules. Expectations live in
[requirements.md](src/qa_product_orders/requirements.md); the catalog is pinned to
[the included contract](src/qa_product_orders/openapi.json).

## Start locally

From the AgentQit repository, with Python 3.12+ and uv:

```sh
uv sync --frozen --extra browser
uv run qa browser install --headless-only
uv run python examples/orders/src/qa_product_orders/demo.py
```

Open `http://127.0.0.1:8765`. The server binds only to loopback. Its CLI retains
synthetic records in `.agentqit/orders-demo/records.json`; delete them only when
you intentionally want a fresh demonstration.

The supplied [workspace](agentqit.yaml) points to this local service. It can be
inspected without network access:

```sh
uv run qa workspace list --workspace examples/orders
uv run qa workspace doctor orders.get --workspace examples/orders
uv run qa workspace plan orders.create --workspace examples/orders \
  --param title=Sample --param idempotency-key=sample-delivery
```

## Connect an agent

Configure your MCP host to launch the checkout's `.venv/bin/agentqit-mcp` with:

```text
--workspace /absolute/path/to/agentqit/examples/orders
```

This default is read-only. For a task explicitly authorizing synthetic writes in
this local demo, the operator may additionally enable the server's mutation flag.
The agent still opts in per action. The provided `environment: qa` is the existing
runtime gate for this synthetic target; it does not assert that a remote QA system
is being used.

Example task for an agent:

> Verify order creation and cancellation against the packaged requirements. Use
> only the local Orders Lab workspace. This task authorizes creating and cancelling
> one synthetic order. Capture baseline, operate the UI, observe the exact order
> through the API, and compare visible state with requirements. Keep the created
> record and report its ID, status, evidence references, and any limitations.

The agent opens a browser-enabled session, navigates within the declared origin,
and observes the page. Element actions use references from the latest observation.
After each action, observe again. Use the same session ID for API operations so
their facts are saved with the browser evidence. A successful click is not proof
that the order reached its expected state.

Use `screenshot` for a masked viewport image and local PNG reference. Closing the
session finalizes its private Playwright trace. The session's journal and artifacts
remain under this workspace's `.agentqit/runs/`. Do not commit or publicly share
auth state or unreviewed traces/screenshots.

## Deliberate frontend defect

Restart the demo with `--defect-ui` to display stale CREATED status after a
successful backend cancellation. The requirement remains CANCELLED in both UI
and API. An agent should identify the discrepancy from independent observations.
This mode demonstrates a test subject defect, not a runtime failure.

## Independent plugin package

`agentqit-orders` is also a separate distribution exposing the standard
`qa_runtime.products` entry point. The local workspace does not require its
installation. To test packaged plugin discovery in the development environment:

```sh
uv pip install --python .venv/bin/python --no-deps -e examples/orders
uv run qa product list --output json
uv run qa product validate --product orders --output json
```

For release verification, build/install this example's wheel alongside AgentQit
in a fresh environment outside both source directories. Browser dependencies and
binaries remain an explicit optional installation.
