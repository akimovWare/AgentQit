from __future__ import annotations

from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field

from qa_mcp.rendering import SessionId, read_result, tool_result
from qa_runtime.application import WorkspaceApplication

_LOCAL = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def register_session_tools(server: MCPServer) -> None:
    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False))
    async def open_session(ctx: Context[WorkspaceApplication], browser: bool = False) -> CallToolResult:
        """Open a task and journal. Browser starts blank; opening does not navigate or authorize writes."""
        return tool_result(await ctx.request_context.lifespan_context.open_session(browser=browser))

    @server.tool(annotations=_LOCAL)
    def read_evidence(
        session_id: SessionId,
        ctx: Context[WorkspaceApplication],
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
        detail: bool = False,
    ) -> CallToolResult:
        """Read task evidence summaries; request detail selectively. A pending attempt never authorizes a repeat."""
        app = ctx.request_context.lifespan_context
        return read_result(
            "session evidence",
            lambda: app.sessions.evidence(
                session_id,
                limit=limit,
                offset=offset,
                detail=detail,
            ),
        )

    @server.tool(annotations=_LOCAL)
    def session_info(session_id: SessionId, ctx: Context[WorkspaceApplication]) -> CallToolResult:
        """Inspect task state, retained local artifact references, and cleanup warnings."""
        app = ctx.request_context.lifespan_context
        return read_result("session info", lambda: app.sessions.info(session_id))

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True))
    async def close_session(session_id: SessionId, ctx: Context[WorkspaceApplication]) -> CallToolResult:
        """Close browser state and finalize local traces; retain test records and evidence for inspection."""
        return tool_result(await ctx.request_context.lifespan_context.close_session(session_id))
