"""MCP sampling plumbing for pydantic-ai's `MCPToolset`.

Everything here reaches into the FastMCP client's private `_session_kwargs`.
It is centralised in this module so the rest of the codebase never has to,
and so there is one place to fix when pydantic-ai changes shape. All of it
must run *before* the session opens — `_session_kwargs` is consumed then.

`sampling.tools`: given a `sampling_model`, pydantic-ai builds its FastMCP
client with a bare `SamplingCapability()` (`pydantic_ai.mcp.MCPToolset`),
leaving `sampling.tools` unadvertised. Servers then reject sampling calls
carrying tools (`mcp/server/validation.py:55`) and fall back to a manual
review path. We upgrade the capability in place, deliberately leaving
pydantic-ai's own callback and message handler alone — the latter drives
`MCPToolset` cache invalidation and is skipped if a pre-built client is
passed to `MCPToolset` instead.

Note the MCP Sampling feature is deprecated as of protocol 2026-07-28
(SEP-2577) but stays for at least twelve months, so this remains live.
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
    session_kwargs = toolset.client._session_kwargs
    if session_kwargs.get("sampling_callback") is None:
        return toolset
    session_kwargs["sampling_capabilities"] = _SAMPLING_TOOLS
    return toolset


def get_sampling_callback(toolset: MCPToolset[Any]) -> Any | None:
    """Return the installed sampling callback, or None if sampling is off.

    The callback is FastMCP-wrapped: `(context, params)` awaitable returning a
    `CreateMessageResult` — or `ErrorData` rather than raising, since
    `create_sampling_callback` swallows handler exceptions.
    """
    return toolset.client._session_kwargs.get("sampling_callback")


def set_sampling_callback(toolset: MCPToolset[Any], callback: Any) -> None:
    """Replace the sampling callback. Must run before the session opens.

    Deliberately writes `_session_kwargs` rather than calling FastMCP's public
    `Client.set_sampling_callback()`, which would re-wrap an already-wrapped
    callback and reset `sampling_capabilities` (dropping `sampling.tools`).
    """
    toolset.client._session_kwargs["sampling_callback"] = callback
