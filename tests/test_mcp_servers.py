"""Tests for src.mcp_servers."""

from unittest.mock import patch

import pytest
from pydantic_ai.mcp import StdioTransport, StreamableHttpTransport
from pydantic_ai.models.test import TestModel

from src.mcp_servers import _is_url, _make_server, get_mcp_servers_named


class TestIsUrl:
    def test_http(self):
        assert _is_url("http://localhost:8080") is True

    def test_https(self):
        assert _is_url("https://api.example.com/mcp") is True

    def test_local_path(self):
        assert _is_url("/home/user/mcp-server") is False

    def test_relative_path(self):
        assert _is_url("../mcp-server") is False

    def test_empty(self):
        assert _is_url("") is False


class TestMakeServer:
    def test_url_creates_streamable_http(self):
        server = _make_server("http://localhost:8080/mcp", "my_module", None)
        assert isinstance(server.client.transport, StreamableHttpTransport)

    def test_path_creates_stdio(self):
        server = _make_server("/opt/mcp-server", "my_module", None)
        assert isinstance(server.client.transport, StdioTransport)

    def test_stdio_runs_module_via_uv(self):
        server = _make_server("/opt/mcp-server", "my_module", None)
        transport = server.client.transport
        assert transport.command == "uv"
        assert transport.args == [
            "run",
            "--directory",
            "/opt/mcp-server",
            "python",
            "-m",
            "my_module",
        ]


class TestSamplingToolsCapability:
    """`sampling.tools` must be advertised, else servers reject sampling calls
    carrying tools. Pydantic-AI only ever sets a bare `SamplingCapability()`."""

    def test_advertised_when_sampling_model_present(self):
        server = _make_server("http://localhost:8080/mcp", "my_module", TestModel())
        capabilities = server.client._session_kwargs["sampling_capabilities"]
        assert capabilities.tools is not None

    def test_not_advertised_when_sampling_disabled(self):
        server = _make_server("http://localhost:8080/mcp", "my_module", None)
        session_kwargs = server.client._session_kwargs
        assert session_kwargs.get("sampling_callback") is None
        assert session_kwargs.get("sampling_capabilities") is None


@pytest.fixture
def mock_settings():
    """Patched settings with concrete defaults.

    The timeout must be a real number: FastMCP coerces it to a timedelta at
    construction time, so a bare MagicMock raises TypeError.
    """
    with patch("src.mcp_servers.settings") as settings:
        settings.mcp_request_timeout_seconds = 300
        settings.mcp_allow_sampling = False
        settings.analytics_server_dir = ""
        settings.semantic_layer_server_dir = ""
        yield settings


class TestGetMcpServersNamed:
    def test_empty_config_returns_empty(self, mock_settings):
        assert get_mcp_servers_named() == []

    def test_one_configured(self, mock_settings):
        mock_settings.analytics_server_dir = "http://localhost:8001/mcp"
        result = get_mcp_servers_named()
        assert len(result) == 1
        name, server = result[0]
        assert name == "OrionBelt Analytics"
        assert isinstance(server.client.transport, StreamableHttpTransport)

    def test_both_configured(self, mock_settings):
        mock_settings.analytics_server_dir = "http://localhost:8001/mcp"
        mock_settings.semantic_layer_server_dir = "/opt/semantic-layer"
        assert len(get_mcp_servers_named()) == 2

    def test_returns_name_server_pairs(self, mock_settings):
        mock_settings.analytics_server_dir = "/opt/analytics"
        mock_settings.semantic_layer_server_dir = "/opt/semantic"
        names = [n for n, _ in get_mcp_servers_named()]
        assert names == ["OrionBelt Analytics", "OrionBelt Semantic Layer"]
