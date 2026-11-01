from __future__ import annotations

import base64
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, ImageContent, ToolAnnotations
from pydantic import Field

from qa_mcp.rendering import SessionId, tool_result
from qa_runtime.application import WorkspaceApplication
from qa_runtime.browser_models import BrowserAction

_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)


def register_browser_tools(server: MCPServer) -> None:
    @server.tool(annotations=_READ)
    async def observe_page(
        session_id: SessionId,
        ctx: Context[WorkspaceApplication],
        page_id: str | None = None,
        offset: Annotated[int, Field(ge=0, le=10000)] = 0,
    ) -> CallToolResult:
        """Read bounded untrusted UI text and fresh element references. Input values are omitted."""
        params = {"offset": offset}
        if page_id is not None:
            params["page_id"] = page_id
        return tool_result(
            await ctx.request_context.lifespan_context.invoke(
                "browser.observe",
                params,
                session_id=session_id,
            )
        )

    @server.tool(annotations=_WRITE)
    async def browser_action(
        session_id: SessionId,
        command: BrowserAction,
        ctx: Context[WorkspaceApplication],
        allow_mutation: bool = False,
    ) -> CallToolResult:
        """Perform one typed UI action using fresh refs. Use approved credential refs; never repeat an unknown write."""
        params = command.model_dump(exclude_none=True, exclude_defaults=True)
        action = params.pop("action")
        return tool_result(
            await ctx.request_context.lifespan_context.invoke(
                f"browser.{action}",
                params,
                session_id=session_id,
                allow_mutation=allow_mutation,
            )
        )

    @server.tool(annotations=_READ)
    async def screenshot(
        session_id: SessionId,
        ctx: Context[WorkspaceApplication],
        page_id: str | None = None,
        include_image: bool = True,
    ) -> CallToolResult:
        """Capture a private viewport PNG with form fields masked; pixels are not guaranteed fully sanitized."""
        app = ctx.request_context.lifespan_context
        result = await app.invoke(
            "browser.screenshot",
            {} if page_id is None else {"page_id": page_id},
            session_id=session_id,
        )
        image = None
        if include_image and result.exit_code == 0:
            try:
                info, data = await app.sessions.read_artifact(session_id, result.result["artifact"]["id"])
                if info.media_type == "image/png" and len(data) <= 4 * 1024 * 1024:
                    image = ImageContent(
                        type="image", data=base64.b64encode(data).decode("ascii"), mime_type="image/png"
                    )
                else:
                    result.warnings.append("Image exceeds preview limits; the local artifact remains available")
            except Exception:
                result.warnings.append("Preview unavailable; inspect the retained artifact metadata")
        output = tool_result(result)
        if image is not None:
            output.content.append(image)
        return output
