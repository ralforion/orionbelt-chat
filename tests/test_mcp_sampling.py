"""Tests for src.mcp_sampling and the app's sampling wrapper.

The wrapper is a regression guard: it reaches into pydantic-ai/FastMCP
internals, so a version bump can silently change the shape it depends on.
"""

from unittest.mock import patch

from pydantic_ai.mcp import MCPToolset, StdioTransport
from pydantic_ai.models.test import TestModel

from src.mcp_sampling import (
    enable_sampling_tools,
    get_sampling_callback,
    set_sampling_callback,
)


def _toolset(sampling: bool) -> MCPToolset:
    return enable_sampling_tools(
        MCPToolset(
            StdioTransport(command="uv", args=["run"]),
            read_timeout=300,
            sampling_model=TestModel() if sampling else None,
        )
    )


class TestSamplingCallbackAccessors:
    def test_get_returns_callback_when_sampling_on(self):
        assert get_sampling_callback(_toolset(sampling=True)) is not None

    def test_get_returns_none_when_sampling_off(self):
        assert get_sampling_callback(_toolset(sampling=False)) is None

    def test_set_replaces_callback(self):
        toolset = _toolset(sampling=True)

        async def replacement(context, params):  # pragma: no cover - never called
            return None

        set_sampling_callback(toolset, replacement)
        assert get_sampling_callback(toolset) is replacement

    def test_set_preserves_sampling_tools_capability(self):
        """FastMCP's public set_sampling_callback() would reset capabilities,
        dropping sampling.tools. Ours must not."""
        toolset = _toolset(sampling=True)

        async def replacement(context, params):  # pragma: no cover - never called
            return None

        set_sampling_callback(toolset, replacement)
        capabilities = toolset.client._session_kwargs["sampling_capabilities"]
        assert capabilities.tools is not None


class TestWrapSamplingForChainlit:
    """Regression: the wrapper read `server._sampling_callback`, a v1
    MCPServer attribute that MCPToolset does not have. It ran before the
    connect try/except, so it broke chat startup outright."""

    def test_wraps_without_error_when_sampling_on(self):
        from app import _wrap_sampling_for_chainlit

        toolset = _toolset(sampling=True)
        before = get_sampling_callback(toolset)
        _wrap_sampling_for_chainlit(toolset, "OrionBelt Analytics")
        after = get_sampling_callback(toolset)

        assert after is not None
        assert after is not before, "callback should have been replaced"

    def test_no_op_when_sampling_off(self):
        from app import _wrap_sampling_for_chainlit

        toolset = _toolset(sampling=False)
        _wrap_sampling_for_chainlit(toolset, "OrionBelt Analytics")
        assert get_sampling_callback(toolset) is None

    def test_survives_real_get_mcp_servers_named(self):
        """End-to-end shape check against what the app actually iterates."""
        from app import _wrap_sampling_for_chainlit
        from src.mcp_servers import get_mcp_servers_named

        with patch("src.mcp_servers.settings") as mock_settings:
            mock_settings.mcp_request_timeout_seconds = 300
            mock_settings.mcp_allow_sampling = False
            mock_settings.analytics_server_dir = "http://localhost:8001/mcp"
            mock_settings.semantic_layer_server_dir = ""
            for name, server in get_mcp_servers_named():
                _wrap_sampling_for_chainlit(server, name)
