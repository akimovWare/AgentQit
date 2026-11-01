"""Check installed CLI/MCP entry points, import origins, and synthetic UI/API behavior."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from importlib.metadata import version
from pathlib import Path

import anyio
from mcp import Client, StdioServerParameters


async def command(*args, subprocess_check: bool = False) -> dict:
    def invoke():
        if subprocess_check:
            env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
            completed = subprocess.run(
                [str(Path(sys.executable).parent / "qa"), *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
                env=env,
            )
            return completed.returncode, completed.stdout
        from typer.testing import CliRunner

        from qa_cli.app import app

        completed = CliRunner().invoke(app, list(args), color=False)
        return completed.exit_code, completed.stdout

    code, stdout = await anyio.to_thread.run_sync(invoke)
    if code:
        raise AssertionError(f"Installed command failed: {args[0]}")
    payload = json.loads(stdout)
    assert len(stdout.splitlines()) == 1 and payload["exit_code"] == 0
    return payload["result"]


async def tool(client, name, args):
    result = await client.call_tool(name, args)
    assert not result.is_error, result.structured_content
    return result.structured_content["result"]


async def verify(output: Path, *, with_example: bool, browser: bool) -> dict:
    import qa_cli
    import qa_mcp
    import qa_runtime

    origins = {module.__name__: str(Path(module.__file__).resolve()) for module in [qa_runtime, qa_cli, qa_mcp]}
    assert all(Path(value).is_relative_to(Path(sys.prefix).resolve()) for value in origins.values())
    output.mkdir(parents=True, exist_ok=False)
    summary = {"installed_origins": origins, "source_fallback": False}
    expected_version = f"AgentQit {version('agentqit')}"
    for executable in ["qa", "agentqit-mcp"]:
        result = await anyio.to_thread.run_sync(
            lambda executable=executable: subprocess.run(
                [str(Path(sys.executable).parent / executable), "--version"],
                capture_output=True,
                text=True,
                check=True,
                timeout=180,
            )
        )
        assert result.stdout.strip() == expected_version
    summary["version_commands"] = expected_version
    assert (await command("workspace", "schema"))["schema"]["properties"]["apiVersion"]["const"] == "agentqit/v1"
    assert "properties" in (await command("registry", "schema"))["schema"]
    expected = ["orders"] if with_example else []
    assert (await command("product", "list"))["products"] == expected
    plain = output / "plain"
    await command("workspace", "init", "--directory", str(plain), "--name", "installed")
    assert (await command("workspace", "plan", "items.list", "--workspace", str(plain), subprocess_check=True))[
        "network_checked"
    ] is False
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-I", "-m", "qa_mcp", "--workspace", str(plain)],
        cwd=output,
    )
    async with Client(parameters, read_timeout_seconds=180) as client:
        tool_names = {item.name for item in (await client.list_tools()).tools}
        assert len(tool_names) == 10 and "browser_action" not in tool_names
        summary["backend_tool_count"] = len(tool_names)
        assert (await tool(client, "plan_operation", {"operation_id": "items.list"}))["network_checked"] is False
    summary["stdio_plan"] = True
    if with_example:
        import qa_product_orders

        origin = Path(qa_product_orders.__file__).resolve()
        assert origin.is_relative_to(Path(sys.prefix).resolve())
        summary["example_origin"] = str(origin)
        assert (await command("product", "validate", "--product", "orders"))["valid"] is True
    if browser:
        assert with_example
        from qa_product_orders.demo import create_server

        fixture = output / "demo"
        fixture.mkdir()
        state_file = output / "synthetic-records.json"
        server = create_server(0, data_file=state_file)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        (fixture / "agentqit.yaml").write_text(
            json.dumps(
                {
                    "apiVersion": "agentqit/v1",
                    "name": "installed-orders",
                    "product": "orders",
                    "browser": {"allowedOrigins": [origin], "trace": True},
                }
            )
        )
        (fixture / "profile.yaml").write_text(
            json.dumps(
                {
                    "environment": "qa",
                    "services": {"orders": {"baseUrl": origin}},
                }
            )
        )
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-I", "-m", "qa_mcp", "--workspace", str(fixture), "--allow-mutation"],
            cwd=output,
            env={"NO_PROXY": "127.0.0.1,localhost"},
        )
        try:
            async with Client(parameters, read_timeout_seconds=180) as client:
                sid = (await tool(client, "open_session", {"browser": True}))["session_id"]
                await tool(
                    client,
                    "browser_action",
                    {
                        "session_id": sid,
                        "allow_mutation": True,
                        "command": {"action": "navigate", "url": origin},
                    },
                )
                observed = await tool(client, "observe_page", {"session_id": sid})
                field = next(item for item in observed["elements"] if item["name"].strip() == "Order title")
                await tool(
                    client,
                    "browser_action",
                    {
                        "session_id": sid,
                        "allow_mutation": True,
                        "command": {
                            "action": "fill",
                            "snapshot_id": observed["snapshot_id"],
                            "element_ref": field["ref"],
                            "value": "Installed wheel sample",
                        },
                    },
                )
                observed = await tool(client, "observe_page", {"session_id": sid})
                button = next(item for item in observed["elements"] if item["name"].strip() == "Create order")
                await tool(
                    client,
                    "browser_action",
                    {
                        "session_id": sid,
                        "allow_mutation": True,
                        "command": {
                            "action": "click",
                            "snapshot_id": observed["snapshot_id"],
                            "element_ref": button["ref"],
                        },
                    },
                )
                for _ in range(30):
                    result = await tool(client, "observe_operation", {"session_id": sid, "operation_id": "orders.list"})
                    if result["body"]["orders"]:
                        break
                    await anyio.sleep(0.05)
                order = result["body"]["orders"][0]
                assert order["title"] == "Installed wheel sample" and order["status"] == "CREATED"
                await tool(client, "screenshot", {"session_id": sid})
                closed = await tool(client, "close_session", {"session_id": sid})
                assert any(item["media_type"] == "application/zip" for item in closed["artifacts"])
                summary["browser"] = {
                    "session_id": sid,
                    "order": order,
                    "artifacts": closed["artifacts"],
                    "journal": closed["journal"],
                }
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (output / "summary.json").chmod(0o600)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--with-example", action="store_true")
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()
    if Path.cwd().name == "agentqit":
        raise SystemExit("Run this check outside the source checkout")

    async def execute():
        with anyio.fail_after(900):
            return await verify(args.output.resolve(), with_example=args.with_example, browser=args.browser)

    print(json.dumps(anyio.run(execute)))


if __name__ == "__main__":
    main()
