"""Tests for orionbelt_chat.mcp_servers."""

from unittest.mock import patch

import pytest
from pydantic_ai.mcp import StdioTransport, StreamableHttpTransport
from pydantic_ai.models.test import TestModel

from orionbelt_chat.mcp_config import ServerDef
from orionbelt_chat.mcp_servers import _is_url, _make_server, get_mcp_servers_named


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


def _def(**kwargs) -> ServerDef:
    return ServerDef(name=kwargs.pop("name", "Test Server"), **kwargs)


class TestMakeServer:
    def test_url_creates_streamable_http(self):
        server = _make_server(_def(endpoint="http://localhost:8080/mcp"), None)
        assert isinstance(server.client.transport, StreamableHttpTransport)

    def test_path_creates_stdio(self):
        server = _make_server(_def(endpoint="/opt/mcp-server", module="my_module"), None)
        assert isinstance(server.client.transport, StdioTransport)

    def test_stdio_runs_module_via_uv(self):
        server = _make_server(_def(endpoint="/opt/mcp-server", module="my_module"), None)
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
        server = _make_server(
            _def(endpoint="http://localhost:8080/mcp", sampling=True), TestModel()
        )
        capabilities = server.client._session_kwargs["sampling_capabilities"]
        assert capabilities.tools is not None

    def test_not_advertised_when_sampling_disabled(self):
        server = _make_server(_def(endpoint="http://localhost:8080/mcp"), None)
        session_kwargs = server.client._session_kwargs
        assert session_kwargs.get("sampling_callback") is None
        assert session_kwargs.get("sampling_capabilities") is None


@pytest.fixture
def mock_settings():
    """Patched settings with concrete defaults.

    The timeout must be a real number: FastMCP coerces it to a timedelta at
    construction time, so a bare MagicMock raises TypeError.

    Both modules are patched because the endpoint variables are read in
    `mcp_config` while the timeouts are read in `mcp_servers`; `config_path` is
    stubbed so a stray mcp_servers.yaml in the working directory cannot leak
    into these assertions.
    """
    with (
        patch("orionbelt_chat.mcp_servers.settings") as server_settings,
        patch("orionbelt_chat.mcp_config.settings") as config_settings,
        patch("orionbelt_chat.mcp_config.config_path", return_value=None),
    ):
        server_settings.mcp_request_timeout_seconds = 300
        server_settings.mcp_allow_sampling = False
        config_settings.analytics_server_dir = ""
        config_settings.semantic_layer_server_dir = ""
        config_settings.mcp_servers_file = ""
        yield config_settings


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


class TestSamplingIsPerServer:
    """MCP_ALLOW_SAMPLING is the global switch; `sampling:` is the per-server one.

    Without both, attaching a third-party server would hand it a route back to
    the user's LLM budget purely because sampling was enabled for OrionBelt.
    """

    def test_server_without_the_flag_gets_no_sampling(self):
        server = _make_server(_def(endpoint="https://x/mcp", sampling=False), TestModel())
        kwargs = server.client._session_kwargs
        assert kwargs.get("sampling_callback") is None
        assert kwargs.get("sampling_capabilities") is None

    def test_only_declared_servers_receive_the_model(self):
        defs = [
            ServerDef(name="Quiet", endpoint="https://a/mcp", sampling=False),
            ServerDef(name="Sampler", endpoint="https://b/mcp", sampling=True),
        ]
        with (
            patch("orionbelt_chat.mcp_servers.load_server_defs", return_value=(defs, [])),
            patch("orionbelt_chat.mcp_servers._resolve_sampling_model", return_value=TestModel()),
        ):
            got = {
                name: server.client._session_kwargs.get("sampling_callback") is not None
                for name, server in get_mcp_servers_named()
            }
        assert got == {"Quiet": False, "Sampler": True}
