"""Enable the MCP `sampling.tools` capability in pydantic-ai's MCP client.

When given a `sampling_model`, pydantic-ai builds its FastMCP client with a
bare `SamplingCapability()` (`pydantic_ai.mcp.MCPToolset.__init__`), leaving
`sampling.tools` unadvertised. Servers then reject sampling calls that carry
tools (`mcp/server/validation.py:55`) and fall back to a manual review path.

FastMCP accepts a `sampling_capabilities` session kwarg, so we upgrade it in
place on the already-constructed client. This deliberately leaves
pydantic-ai's own sampling callback and message handler untouched — the
latter drives `MCPToolset` cache invalidation and is skipped entirely if a
pre-built client is passed to `MCPToolset` instead.
"""

from typing import Any

from mcp.types import SamplingCapability, SamplingToolsCapability
from pydantic_ai.mcp import MCPToolset

_SAMPLING_TOOLS = SamplingCapability(tools=SamplingToolsCapability())


def enable_sampling_tools(toolset: MCPToolset[Any]) -> MCPToolset[Any]:
    """Advertise `sampling.tools` on `toolset`, if it does sampling at all.

    No-op when sampling is disabled (no callback installed), so we never
    advertise a capability we cannot fulfill. Returns `toolset` for chaining.
    """
    # `_session_kwargs` is private; FastMCP exposes no read path for the
    # already-wrapped sampling callback, and re-setting it via the public
    # `set_sampling_callback()` would double-wrap it.
    session_kwargs = toolset.client._session_kwargs
    if session_kwargs.get("sampling_callback") is None:
        return toolset
    session_kwargs["sampling_capabilities"] = _SAMPLING_TOOLS
    return toolset
