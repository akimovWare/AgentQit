from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentQit local MCP server (stdio)")
    from qa_runtime.version import VERSION

    parser.add_argument("--version", action="version", version=f"AgentQit {VERSION}")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument(
        "--allow-mutation", action="store_true", help="Host authorization for explicitly requested QA writes"
    )
    args = parser.parse_args()
    try:
        from qa_mcp.server import create_server

        create_server(args.workspace, mutations_allowed=args.allow_mutation).run(transport="stdio")
    except ImportError:
        print("AgentQit dependencies are incomplete; reinstall the package", file=sys.stderr)
        raise SystemExit(2) from None
    except Exception:
        # Before serving, stdout must remain clean for protocol framing.
        print("AgentQit MCP startup failed; validate the workspace with the CLI", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
