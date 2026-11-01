"""Measure real CLI/MCP workflow payloads against an owned loopback backend.

This is reproducible context accounting, not an autonomous LLM or billing benchmark.
No external application, model API, credentials, or browser is contacted.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import threading
from pathlib import Path
from uuid import uuid4

import anyio
import tiktoken
from mcp import Client
from typer.testing import CliRunner

from qa_cli.app import app
from qa_mcp.server import create_server
from qa_runtime.version import VERSION

ROOT = Path(__file__).resolve().parents[1]


def compact(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def model_definition(tool) -> dict:
    # A reference model-facing representation, not a promise about any host's wrapper.
    return {"name": tool.name, "description": tool.description, "parameters": tool.input_schema}


async def benchmark(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    encoding = tiktoken.get_encoding("o200k_base")

    def tokens(value):
        return len(encoding.encode(value if isinstance(value, str) else compact(value)))

    fixture = ROOT / "examples/orders/src/qa_product_orders"
    spec = importlib.util.spec_from_file_location("orders_context_fixture", fixture / "demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backend = module.create_server(0, data_file=output / "synthetic-records.json")
    worker = threading.Thread(target=backend.serve_forever, daemon=True)
    worker.start()
    origin = f"http://127.0.0.1:{backend.server_port}"
    workspace = output / "workspace"
    workspace.mkdir()
    manifest = {"apiVersion": "agentqit/v1", "name": "benchmark", "registry": ["operations.yaml"]}
    manifest_path = workspace / "agentqit.yaml"
    manifest_path.write_text(compact(manifest))
    (workspace / "profile.yaml").write_text(compact({"environment": "qa", "services": {"orders": {"baseUrl": origin}}}))
    catalog = json.loads((fixture / "operations.yaml").read_text())
    catalog_path = workspace / "operations.yaml"
    catalog_path.write_text(compact(catalog))
    server = create_server(workspace, mutations_allowed=True)
    definitions = await server.list_tools()
    by_name = {tool.name: model_definition(tool) for tool in definitions}
    initial = tokens(list(by_name.values())) + tokens(server.instructions)
    browser_manifest = {**manifest, "browser": {"allowedOrigins": [origin]}}
    manifest_path.write_text(compact(browser_manifest))
    browser_definitions = await create_server(workspace).list_tools()
    manifest_path.write_text(compact(manifest))
    # Catalog growth must not become model-facing tool-schema growth.
    expanded = {
        **catalog,
        "operations": catalog["operations"]
        + [
            {
                "id": f"items.read-{index}",
                "purpose": "Read a sample item",
                "config": {"service": "orders", "method": "GET", "path": "/api/orders"},
            }
            for index in range(500)
        ],
    }
    catalog_path.write_text(compact(expanded))
    expanded_definitions = await create_server(workspace).list_tools()
    assert [model_definition(tool) for tool in expanded_definitions] == list(by_name.values())
    catalog_path.write_text(compact(catalog))
    report = {
        "version": VERSION,
        "tokenizer": "o200k_base",
        "autonomous_llm_evaluation": False,
        "method": "Actual Typer CLI and in-memory MCP SDK calls; reference model-facing serialization; no image tasks",
        "startup": {
            "backend_tools": len(definitions),
            "browser_tools": len(browser_definitions),
            "backend_definition_tokens": initial,
            "browser_definition_tokens": tokens([model_definition(tool) for tool in browser_definitions])
            + tokens(server.instructions),
            "catalog_operations": len(catalog["operations"]),
            "expanded_operations": len(expanded["operations"]),
            "catalog_growth_changes_definitions": False,
        },
        "workflows": [],
    }
    runner = CliRunner()
    help_transcript = []
    for arguments in [["--help"], ["workspace", "--help"], ["workspace", "invoke", "--help"]]:
        result = await anyio.to_thread.run_sync(lambda arguments=arguments: runner.invoke(app, arguments, color=False))
        assert result.exit_code == 0, result.output
        help_transcript.append({"command": ["qa", *arguments], "output": result.stdout})
    help_tokens = sum(tokens(item["command"]) + tokens(item["output"]) for item in help_transcript)
    report["startup"]["cli_help_tokens"] = help_tokens
    transcripts = {"cli_help": help_transcript, "workflows": []}
    try:
        async with Client(server) as client:
            for task in ["discover-and-read", "invalid-input", "blocked-write", "create-read-cancel"]:
                task_result = {"task": task, "interfaces": {}}
                for interface in ["cli", "mcp"]:
                    calls = []

                    async def call(name, arguments, expected=0, *, interface=interface, calls=calls):
                        if interface == "mcp":
                            response = await client.call_tool(name, arguments)
                            payload = response.structured_content
                            text = "\n".join(item.text for item in response.content if item.type == "text")
                            request = {"name": name, "arguments": arguments}
                            # Text is the compatibility rendering. Hosts may additionally include structuredContent.
                            duplicate = tokens(payload)
                        else:
                            operation = arguments.get("operation_id")
                            command = {
                                "search_operations": "list",
                                "describe_operation": "describe",
                                "plan_operation": "plan",
                                "observe_operation": "invoke",
                                "invoke_operation": "invoke",
                            }[name]
                            args = ["workspace", command]
                            if operation:
                                args.append(operation)
                            args += ["--workspace", str(workspace)]
                            if name == "search_operations":
                                args += ["--query", arguments.get("query", ""), "--limit", "5"]
                            for key, value in arguments.get("inputs", {}).items():
                                args += ["--param", f"{key}={compact(value)}"]
                            if arguments.get("allow_mutation"):
                                args.append("--allow-mutation")
                            response = await anyio.to_thread.run_sync(lambda: runner.invoke(app, args, color=False))
                            text = response.stdout
                            payload = json.loads(text)
                            request = ["qa", *args]
                            duplicate = 0
                            assert response.exit_code == expected, text
                        assert payload["exit_code"] == expected, payload
                        calls.append(
                            {
                                "tool": name,
                                "request": request,
                                "response": text,
                                "request_tokens": tokens(request),
                                "response_tokens": tokens(text),
                                "additional_structured_tokens_if_host_duplicates": duplicate,
                            }
                        )
                        return payload.get("result")

                    if task == "discover-and-read":
                        await call("search_operations", {"query": "orders.list", "limit": 5})
                        await call("describe_operation", {"operation_id": "orders.list"})
                        await call("plan_operation", {"operation_id": "orders.list"})
                        observed = await call("observe_operation", {"operation_id": "orders.list"})
                        assert observed["status_code"] == 200
                    elif task == "invalid-input":
                        await call("describe_operation", {"operation_id": "orders.get"})
                        await call(
                            "plan_operation", {"operation_id": "orders.get", "inputs": {"order-id": "invalid"}}, 2
                        )
                    elif task == "blocked-write":
                        arguments = {
                            "operation_id": "orders.create",
                            "inputs": {"title": "Blocked sample", "idempotency-key": "blocked-sample"},
                        }
                        await call("describe_operation", {"operation_id": "orders.create"})
                        await call("plan_operation", arguments)
                        await call("invoke_operation", arguments, 7)
                    else:
                        arguments = {
                            "operation_id": "orders.create",
                            "inputs": {"title": f"{interface} context sample", "idempotency-key": str(uuid4())},
                        }
                        await call("describe_operation", {"operation_id": "orders.create"})
                        await call("plan_operation", arguments)
                        created = await call("invoke_operation", {**arguments, "allow_mutation": True})
                        identifier = created["body"]["id"]
                        observed = await call(
                            "observe_operation", {"operation_id": "orders.get", "inputs": {"order-id": identifier}}
                        )
                        assert observed["body"]["status"] == "CREATED"
                        await call(
                            "invoke_operation",
                            {
                                "operation_id": "orders.cancel",
                                "allow_mutation": True,
                                "inputs": {"order-id": identifier, "idempotency-key": str(uuid4())},
                            },
                        )
                        observed = await call(
                            "observe_operation", {"operation_id": "orders.get", "inputs": {"order-id": identifier}}
                        )
                        assert observed["body"]["status"] == "CANCELLED"
                    interaction = sum(item["request_tokens"] + item["response_tokens"] for item in calls)
                    used = list(dict.fromkeys(item["tool"] for item in calls))
                    task_result["interfaces"][interface] = {
                        "calls": len(calls),
                        "interaction_tokens": interaction,
                        "cold_eager_tokens": interaction + (initial if interface == "mcp" else help_tokens),
                        "used_definition_tokens": tokens([by_name[name] for name in used]) if interface == "mcp" else 0,
                        "additional_structured_tokens_if_host_duplicates": sum(
                            item["additional_structured_tokens_if_host_duplicates"] for item in calls
                        ),
                        "expected_outcome_verified": True,
                    }
                    transcripts["workflows"].append({"task": task, "interface": interface, "calls": calls})
                report["workflows"].append(task_result)
    finally:
        backend.shutdown()
        backend.server_close()
        worker.join(timeout=5)
    for name, data in [("summary.json", report), ("transcripts.json", transcripts)]:
        path = output / name
        path.write_text(json.dumps(data, indent=2) + "\n")
        path.chmod(0o600)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="New local directory for summaries and retained records"
    )
    args = parser.parse_args()
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["NO_COLOR"] = "1"
    os.environ["COLUMNS"] = "100"
    print(json.dumps(anyio.run(benchmark, args.output.resolve())))


if __name__ == "__main__":
    main()
