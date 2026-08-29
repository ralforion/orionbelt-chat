"""Tests for declarative MCP server configuration."""

from unittest.mock import patch

import pytest

from orionbelt_chat.mcp_config import (
    McpConfigError,
    ServerDef,
    config_path,
    load_from_file,
    load_server_defs,
    parse_server,
)


def _write(tmp_path, text: str):
    path = tmp_path / "mcp_servers.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def env(tmp_path):
    """Settings with no built-in servers and no config file, unless a test says so."""
    with patch("orionbelt_chat.mcp_config.settings") as settings:
        settings.analytics_server_dir = ""
        settings.semantic_layer_server_dir = ""
        settings.mcp_servers_file = ""
        yield settings


class TestParseServer:
    def test_http_endpoint(self):
        defn = parse_server({"name": "W", "endpoint": "https://x.example/mcp"}, "cfg")
        assert defn.is_url
        assert defn.name == "W"
        assert defn.sampling is False

    def test_directory_endpoint_with_module(self):
        defn = parse_server({"name": "A", "endpoint": "../a", "module": "a_mod"}, "cfg")
        assert not defn.is_url
        assert defn.module == "a_mod"

    def test_arbitrary_command(self):
        defn = parse_server(
            {"name": "FS", "command": "npx", "args": ["-y", "server-fs", "/data"]}, "cfg"
        )
        assert defn.command == "npx"
        assert defn.args == ("-y", "server-fs", "/data")

    def test_sampling_flag(self):
        defn = parse_server({"name": "S", "endpoint": "https://x/mcp", "sampling": True}, "cfg")
        assert defn.sampling is True

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (True, True),
            (False, False),
            # A template tool that quotes YAML scalars must not flip the meaning:
            # bool("false") is True, which would silently grant LLM access.
            ("true", True),
            ("false", False),
            ("TRUE", True),
            ("no", False),
            ("on", True),
            (None, False),
        ],
    )
    def test_sampling_accepts_only_real_booleans(self, value, expected):
        entry = {"name": "S", "endpoint": "https://x/mcp"}
        if value is not None:
            entry["sampling"] = value
        assert parse_server(entry, "cfg").sampling is expected

    def test_sampling_rejects_anything_else(self):
        with pytest.raises(McpConfigError, match="must be true or false"):
            parse_server({"name": "S", "endpoint": "https://x/mcp", "sampling": "maybe"}, "cfg")

    def test_env_is_stringified(self):
        defn = parse_server({"name": "E", "command": "x", "env": {"PORT": 8080}}, "cfg")
        assert defn.env == {"PORT": "8080"}


class TestParseServerRejects:
    """Ambiguity is refused rather than guessed at — a server that starts on the
    wrong transport is much harder to diagnose than one that will not load."""

    def test_missing_name(self):
        with pytest.raises(McpConfigError, match="'name' is required"):
            parse_server({"endpoint": "https://x/mcp"}, "cfg")

    def test_neither_endpoint_nor_command(self):
        with pytest.raises(McpConfigError, match="either 'endpoint' or 'command'"):
            parse_server({"name": "X"}, "cfg")

    def test_both_endpoint_and_command(self):
        with pytest.raises(McpConfigError, match="not both"):
            parse_server({"name": "X", "endpoint": "https://x/mcp", "command": "y"}, "cfg")

    def test_directory_without_module(self):
        with pytest.raises(McpConfigError, match="needs 'module'"):
            parse_server({"name": "X", "endpoint": "../somewhere"}, "cfg")

    def test_module_with_http_endpoint(self):
        with pytest.raises(McpConfigError, match="does not apply to an HTTP endpoint"):
            parse_server({"name": "X", "endpoint": "https://x/mcp", "module": "m"}, "cfg")

    def test_args_without_command(self):
        with pytest.raises(McpConfigError, match="only applies to a 'command' server"):
            parse_server({"name": "X", "endpoint": "https://x/mcp", "args": ["a"]}, "cfg")

    def test_not_a_mapping(self):
        with pytest.raises(McpConfigError, match="must be a mapping"):
            parse_server("just a string", "cfg")

    def test_args_not_a_list_of_strings(self):
        with pytest.raises(McpConfigError, match="list of strings"):
            parse_server({"name": "X", "command": "y", "args": [1, 2]}, "cfg")


class TestLoadFromFile:
    def test_reads_multiple_servers(self, tmp_path):
        path = _write(
            tmp_path,
            """
            servers:
              - name: One
                endpoint: https://one.example/mcp
              - name: Two
                command: npx
                args: ["-y", "two"]
            """,
        )
        defs = load_from_file(path)
        assert [d.name for d in defs] == ["One", "Two"]

    def test_empty_file_is_not_an_error(self, tmp_path):
        assert load_from_file(_write(tmp_path, "")) == []

    def test_empty_servers_list(self, tmp_path):
        assert load_from_file(_write(tmp_path, "servers:\n")) == []

    def test_missing_servers_key(self, tmp_path):
        with pytest.raises(McpConfigError, match="top-level 'servers:' list"):
            load_from_file(_write(tmp_path, "other: 1"))

    def test_duplicate_names(self, tmp_path):
        text = "servers:\n  - {name: D, endpoint: 'https://a/mcp'}\n  - {name: D, command: x}\n"
        with pytest.raises(McpConfigError, match="duplicate server name"):
            load_from_file(_write(tmp_path, text))

    def test_malformed_yaml(self, tmp_path):
        with pytest.raises(McpConfigError):
            load_from_file(_write(tmp_path, "servers: [unclosed"))

    def test_missing_file(self, tmp_path):
        with pytest.raises(McpConfigError, match="no such file"):
            load_from_file(tmp_path / "absent.yaml")

    def test_error_names_the_offending_entry(self, tmp_path):
        path = _write(
            tmp_path,
            "servers:\n  - {name: Fine, endpoint: 'https://a/mcp'}\n  - {name: Broken}\n",
        )
        with pytest.raises(McpConfigError, match=r"\[1\] \(Broken\)"):
            load_from_file(path)


class TestConfigPath:
    def test_explicit_setting_wins(self, env, tmp_path):
        env.mcp_servers_file = str(tmp_path / "custom.yaml")
        assert config_path() == tmp_path / "custom.yaml"

    def test_explicit_missing_path_is_still_returned(self, env, tmp_path):
        # So a typo becomes a visible error rather than silence.
        env.mcp_servers_file = str(tmp_path / "typo.yaml")
        assert config_path() is not None

    def test_none_when_nothing_configured(self, env, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CHAINLIT_APP_ROOT", str(tmp_path))
        assert config_path() is None

    def test_found_in_working_directory(self, env, tmp_path, monkeypatch):
        _write(tmp_path, "servers: []")
        monkeypatch.chdir(tmp_path)
        assert config_path() == tmp_path / "mcp_servers.yaml"


class TestLoadServerDefs:
    def test_builtins_only(self, env):
        env.analytics_server_dir = "https://a.example/mcp"
        with patch("orionbelt_chat.mcp_config.config_path", return_value=None):
            defs, errors = load_server_defs()
        assert [d.name for d in defs] == ["OrionBelt Analytics"]
        assert errors == []

    def test_builtin_declares_sampling(self, env):
        env.analytics_server_dir = "https://a.example/mcp"
        env.semantic_layer_server_dir = "https://s.example/mcp"
        with patch("orionbelt_chat.mcp_config.config_path", return_value=None):
            defs, _ = load_server_defs()
        sampling = {d.name: d.sampling for d in defs}
        assert sampling == {"OrionBelt Analytics": True, "OrionBelt Semantic Layer": False}

    def test_file_servers_are_added_to_builtins(self, env, tmp_path):
        env.analytics_server_dir = "https://a.example/mcp"
        path = _write(tmp_path, "servers:\n  - {name: Extra, endpoint: 'https://e/mcp'}\n")
        with patch("orionbelt_chat.mcp_config.config_path", return_value=path):
            defs, errors = load_server_defs()
        assert [d.name for d in defs] == ["OrionBelt Analytics", "Extra"]
        assert errors == []

    def test_file_entry_overrides_a_builtin(self, env, tmp_path):
        env.analytics_server_dir = "https://from-env/mcp"
        path = _write(
            tmp_path,
            "servers:\n  - {name: OrionBelt Analytics, endpoint: 'https://from-file/mcp'}\n",
        )
        with patch("orionbelt_chat.mcp_config.config_path", return_value=path):
            defs, _ = load_server_defs()
        assert len(defs) == 1
        assert defs[0].endpoint == "https://from-file/mcp"

    def test_broken_file_reports_but_keeps_builtins(self, env, tmp_path):
        # A typo in the config must not take the working servers down with it.
        env.analytics_server_dir = "https://a.example/mcp"
        path = _write(tmp_path, "servers:\n  - {name: Broken}\n")
        with patch("orionbelt_chat.mcp_config.config_path", return_value=path):
            defs, errors = load_server_defs()
        assert [d.name for d in defs] == ["OrionBelt Analytics"]
        assert len(errors) == 1
        assert "Broken" in errors[0]


class TestServerDef:
    @pytest.mark.parametrize(
        ("endpoint", "expected"),
        [
            ("http://x/mcp", True),
            ("https://x/mcp", True),
            ("/opt/server", False),
            ("../server", False),
            ("", False),
        ],
    )
    def test_is_url(self, endpoint, expected):
        assert ServerDef(name="X", endpoint=endpoint).is_url is expected
