"""MCP elicitation: schema parsing, validation, URL vetting, and real round trips.

The round trips run against `fake_mcp_server.py` over stdio, so the server's
`ctx.elicit` reaches the handler through a genuine MCP session and the answer
the tool reports is the answer that crossed the wire. The UI is replaced by a
scripted `prompt`, which is exactly the seam `ask_user` plugs into in the app.
"""

import sys
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset, StdioTransport
from pydantic_ai.messages import FunctionToolResultEvent

from orionbelt_chat.mcp_config import ServerDef
from orionbelt_chat.mcp_elicitation import (
    UnsupportedSchemaError,
    inspect_url,
    make_elicitation_handler,
    parse_form_schema,
    validate_form,
)
from orionbelt_chat.mcp_servers import _make_server
from orionbelt_chat.settings import settings
from tests.test_mcp_integration import SERVER, TIMEOUT, _script_one_call


class TestParseFormSchema:
    """Every schema shape the 2026-07-28 elicitation spec allows."""

    def test_string_with_format_and_lengths(self):
        [f] = parse_form_schema(
            {
                "type": "object",
                "properties": {
                    "email": {"type": "string", "format": "email", "minLength": 3, "title": "Email"}
                },
                "required": ["email"],
            }
        )
        assert f["kind"] == "string" and f["format"] == "email" and f["minLength"] == 3
        assert f["title"] == "Email" and f["required"] is True

    def test_title_falls_back_to_name_and_default_is_kept(self):
        [f] = parse_form_schema({"properties": {"n": {"type": "integer", "default": 5}}})
        assert f["title"] == "n" and f["default"] == 5 and f["required"] is False

    def test_plain_and_titled_single_select(self):
        plain, titled = parse_form_schema(
            {
                "properties": {
                    "a": {"type": "string", "enum": ["Red", "Green"]},
                    "b": {"type": "string", "oneOf": [{"const": "#F00", "title": "Red"}]},
                }
            }
        )
        assert plain["kind"] == "enum" and plain["options"][0] == {"value": "Red", "label": "Red"}
        assert titled["options"] == [{"value": "#F00", "label": "Red"}]

    def test_plain_and_titled_multi_select(self):
        plain, titled = parse_form_schema(
            {
                "properties": {
                    "a": {"type": "array", "items": {"type": "string", "enum": ["x", "y"]}},
                    "b": {
                        "type": "array",
                        "maxItems": 1,
                        "items": {"anyOf": [{"const": "x", "title": "Ex"}]},
                    },
                }
            }
        )
        assert plain["kind"] == titled["kind"] == "multi_enum"
        assert titled["options"] == [{"value": "x", "label": "Ex"}] and titled["maxItems"] == 1

    @pytest.mark.parametrize(
        "prop",
        [
            {"type": "object", "properties": {}},
            {"type": "array", "items": {"type": "string"}},
            {"type": "string", "format": "password"},
        ],
    )
    def test_anything_beyond_the_flat_subset_is_refused(self, prop):
        with pytest.raises(UnsupportedSchemaError):
            parse_form_schema({"properties": {"x": prop}})


class TestValidateForm:
    FIELDS = parse_form_schema(
        {
            "properties": {
                "name": {"type": "string", "maxLength": 5},
                "age": {"type": "integer", "minimum": 18},
                "ok": {"type": "boolean"},
                "when": {"type": "string", "format": "date"},
                "tags": {"type": "array", "items": {"enum": ["a", "b"]}, "maxItems": 1},
            },
            "required": ["name", "age"],
        }
    )

    def test_browser_strings_are_coerced_and_blanks_dropped(self):
        content, errors = validate_form(self.FIELDS, {"name": " Ann ", "age": "30", "when": ""})
        assert errors == {}
        # `ok` is an optional boolean nobody touched, so it is left out entirely
        # rather than sent as false — see TestBooleanFields.
        assert content == {"name": "Ann", "age": 30}

    def test_each_constraint_reports_on_its_own_field(self):
        _, errors = validate_form(
            self.FIELDS, {"name": "toolong", "age": "17", "when": "31/12/2026", "tags": ["a", "b"]}
        )
        assert set(errors) == {"name", "age", "when", "tags"}

    def test_missing_required_fields(self):
        _, errors = validate_form(self.FIELDS, {})
        assert errors == {"name": "Required", "age": "Required"}

    def test_integer_rejects_fractions(self):
        _, errors = validate_form(self.FIELDS, {"name": "a", "age": "20.5"})
        assert "age" in errors

    @pytest.mark.parametrize("raw", [float("nan"), float("inf"), "1e309", "nan"])
    def test_non_finite_numbers_are_refused(self, raw):
        """They pass every range check (NaN compares false, infinity has no bound)
        and `json.dumps` writes them as bare NaN/Infinity, which is not JSON."""
        fields = parse_form_schema({"properties": {"n": {"type": "number", "minimum": 0}}})
        content, errors = validate_form(fields, {"n": raw})
        assert content == {} and errors == {"n": "Enter a finite number"}

    def test_a_number_too_large_to_convert_is_refused_not_raised(self):
        fields = parse_form_schema({"properties": {"i": {"type": "integer"}}})
        assert validate_form(fields, {"i": 10**400}) == ({}, {"i": "Enter a number"})


class TestBooleanFields:
    """`False` is an answer; a box nobody touched is not."""

    FIELDS = parse_form_schema(
        {
            "properties": {"opt": {"type": "boolean"}, "req": {"type": "boolean"}},
            "required": ["req"],
        }
    )

    def test_untouched_optional_box_is_left_out(self):
        assert validate_form(self.FIELDS, {})[0] == {"req": False}

    def test_explicit_false_is_kept(self):
        assert validate_form(self.FIELDS, {"opt": False})[0] == {"opt": False, "req": False}

    def test_ticked_box_is_sent(self):
        assert validate_form(self.FIELDS, {"opt": True})[0]["opt"] is True


class TestInspectUrl:
    def test_https_url_is_clean(self):
        view = inspect_url("https://Auth.Example.com:8443/connect?x=1")
        assert view["host"] == "auth.example.com" and view["warnings"] == []
        # The highlighted part is the URL's own text, not the lowercased host.
        assert view["url_parts"] == ["https://", "Auth.Example.com", ":8443/connect?x=1"]

    def test_userinfo_spoof_highlights_the_real_host(self):
        view = inspect_url("https://bank.com@evil.example/login")
        assert view["url_parts"][1] == "evil.example"
        assert any("before '@'" in w for w in view["warnings"])

    def test_punycode_and_plain_http_are_flagged(self):
        assert len(inspect_url("http://xn--pple-43d.com/").get("warnings")) == 2

    def test_localhost_http_is_not_flagged(self):
        assert inspect_url("http://localhost:3000/cb")["warnings"] == []

    @pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///etc/passwd", "/relative"])
    def test_non_web_targets_are_refused(self, url):
        with pytest.raises(ValueError):
            inspect_url(url)


# ── Round trips over a real MCP session ────────────────────


class ScriptedPrompt:
    """Stands in for `ask_user`: records each view and replays scripted answers."""

    def __init__(self, *answers: dict[str, Any] | None):
        self.answers = list(answers)
        self.views: list[dict[str, Any]] = []

    async def __call__(self, view: dict[str, Any]) -> dict[str, Any] | None:
        self.views.append(view)
        return self.answers.pop(0)


async def _call(tool: str, prompt: ScriptedPrompt) -> str:
    """Have a scripted agent call `tool`; return what the tool reported back."""
    toolset = MCPToolset(
        StdioTransport(command=sys.executable, args=[str(SERVER)]),
        read_timeout=TIMEOUT,
        elicitation_handler=make_elicitation_handler("fake", prompt),
    )
    agent = Agent(model=_script_one_call(tool, {}), toolsets=[toolset])
    async with agent:
        async with agent.iter("go") as run:
            async for node in run:
                if Agent.is_call_tools_node(node):
                    async with node.stream(run.ctx) as stream:
                        async for event in stream:
                            if isinstance(event, FunctionToolResultEvent):
                                return str(event.part.content)
    raise AssertionError("tool never returned")


class TestFormRoundTrip:
    async def test_accept_sends_typed_content(self):
        prompt = ScriptedPrompt({"action": "accept", "content": {"table": "orders", "limit": "5"}})
        assert await _call("ask_query_options", prompt) == "accept {'table': 'orders', 'limit': 5}"
        [view] = prompt.views
        assert view["server"] == "fake" and view["message"] == "Which table, and how many rows?"
        assert [f["name"] for f in view["fields"]] == ["table", "limit"]
        assert view["values"] == {"limit": 10}  # the schema default pre-fills the form

    async def test_invalid_answer_is_asked_again_with_the_error(self):
        prompt = ScriptedPrompt(
            {"action": "accept", "content": {"table": "orders", "limit": "500"}},
            {"action": "accept", "content": {"table": "orders", "limit": "50"}},
        )
        assert await _call("ask_query_options", prompt) == "accept {'table': 'orders', 'limit': 50}"
        assert prompt.views[1]["errors"] == {"limit": "Must be at most 100"}
        assert prompt.views[1]["values"]["limit"] == "500"  # the user's input is kept

    async def test_decline_reaches_the_server(self):
        assert await _call("ask_query_options", ScriptedPrompt({"action": "decline"})) == "decline"

    async def test_no_answer_is_a_cancel_not_an_error(self):
        assert await _call("ask_query_options", ScriptedPrompt(None)) == "cancel"


class TestUrlRoundTrip:
    async def test_consent_is_reported_as_accept(self):
        prompt = ScriptedPrompt({"action": "accept"})
        assert await _call("connect_warehouse", prompt) == "accept"
        [view] = prompt.views
        assert view["mode"] == "url"
        assert view["url"] == "https://auth.example.com/connect?session=abc"
        assert view["url_parts"][1] == "auth.example.com"

    async def test_decline(self):
        assert await _call("connect_warehouse", ScriptedPrompt({"action": "decline"})) == "decline"


class TestWiring:
    """Installing the handler is what advertises the capability to a server."""

    @staticmethod
    def _callback(prompt, monkeypatch, allow: bool):
        monkeypatch.setattr(settings, "mcp_allow_elicitation", allow)
        server = _make_server(ServerDef(name="s", endpoint="https://x/mcp"), None, prompt)
        return server.client._session_kwargs.get("elicitation_callback")

    def test_every_server_gets_a_handler_when_a_prompt_is_given(self, monkeypatch):
        assert self._callback(ScriptedPrompt(), monkeypatch, allow=True) is not None

    def test_kill_switch_removes_it(self, monkeypatch):
        assert self._callback(ScriptedPrompt(), monkeypatch, allow=False) is None

    def test_no_prompt_no_handler(self, monkeypatch):
        assert self._callback(None, monkeypatch, allow=True) is None


def test_seeding_ships_the_custom_element(tmp_path):
    """`cli._seed` used to copy only top-level files, which would drop `elements/`."""
    from orionbelt_chat.cli import _seed

    _seed(tmp_path)
    assert (tmp_path / "public" / "elements" / "McpElicitation.jsx").is_file()
