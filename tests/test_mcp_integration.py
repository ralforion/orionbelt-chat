"""End-to-end coverage of the tool-call → tool-result path.

Layer 1 (``test_tool_results.py``) pins the extraction in isolation. This layer
drives the real thing: a genuine MCP server subprocess over stdio, a genuine
Pydantic AI agent run, and the same ``is_call_tools_node`` → ``node.stream``
loop the app uses — so a change in how Pydantic AI emits tool events fails here
even if the event shape itself is unchanged.

No API key and no network: ``FunctionModel`` scripts the LLM side, so the only
subprocess is the fake MCP server in ``fake_mcp_server.py``.
"""

import sys
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset, StdioTransport
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from orionbelt_chat.app import _MCP_ERROR_PHRASES, _tool_result_fields

SERVER = Path(__file__).parent / "fake_mcp_server.py"

# Spawning a Python subprocess and completing the MCP handshake is the slow
# part; keep it well clear of the app's own timeouts on a loaded CI runner.
TIMEOUT = 60


def _toolset() -> MCPToolset:
    return MCPToolset(
        StdioTransport(command=sys.executable, args=[str(SERVER)]),
        read_timeout=TIMEOUT,
    )


def _script_one_call(tool_name: str, args: dict):
    """A model that calls `tool_name` once, then answers with plain text."""

    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        called = any(
            isinstance(part, ToolCallPart)
            for message in messages
            if isinstance(message, ModelResponse)
            for part in message.parts
        )
        if called:
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args)])

    return FunctionModel(model_fn)


async def _run_and_collect(tool_name: str, args: dict) -> list[FunctionToolResultEvent]:
    """Drive one agent turn, mirroring the app's node/event loop."""
    toolset = _toolset()
    agent = Agent(model=_script_one_call(tool_name, args), toolsets=[toolset])

    results: list[FunctionToolResultEvent] = []
    async with agent:
        async with agent.iter("go") as agent_run:
            async for node in agent_run:
                if Agent.is_call_tools_node(node):
                    async with node.stream(agent_run.ctx) as stream:
                        async for event in stream:
                            if isinstance(event, FunctionToolResultEvent):
                                results.append(event)
    return results


class TestRealToolResults:
    """The fake server really runs, so these exercise the whole path."""

    async def test_text_result(self):
        (event,) = await _run_and_collect("echo", {"text": "hello from mcp"})
        result = _tool_result_fields(event)
        assert result.tool_name == "echo"
        assert result.call_id
        assert "hello from mcp" in result.text

    async def test_structured_result(self):
        (event,) = await _run_and_collect("row_count", {"table": "orders"})
        result = _tool_result_fields(event)
        assert "orders" in result.text
        assert "42" in result.text

    async def test_binary_result_is_split_out(self):
        # The case that motivated `content` falling back to str(part.content):
        # an image-only result must still yield something displayable.
        (event,) = await _run_and_collect("render_chart", {})
        result = _tool_result_fields(event)
        assert result.binaries, "expected the PNG to be split out as binary content"
        assert result.binaries[0].media_type == "image/png"
        assert result.content != ""

    async def test_upstream_error_trips_the_reconnect_check(self):
        (event,) = await _run_and_collect("flaky", {})
        result = _tool_result_fields(event)
        assert any(phrase in result.content for phrase in _MCP_ERROR_PHRASES)


class TestEventOrdering:
    """The handler assumes a call event precedes its result, keyed by call id."""

    async def test_call_and_result_share_a_call_id(self):
        toolset = _toolset()
        agent = Agent(
            model=_script_one_call("echo", {"text": "x"}),
            toolsets=[toolset],
        )

        calls: list[str] = []
        results: list[str] = []
        async with agent:
            async with agent.iter("go") as agent_run:
                async for node in agent_run:
                    if Agent.is_call_tools_node(node):
                        async with node.stream(agent_run.ctx) as stream:
                            async for event in stream:
                                if isinstance(event, FunctionToolCallEvent):
                                    calls.append(event.part.tool_call_id)
                                elif isinstance(event, FunctionToolResultEvent):
                                    results.append(_tool_result_fields(event).call_id)

        assert calls and calls == results, (
            "tool_steps is keyed by call id — a mismatch orphans the step in the UI"
        )


class TestConfiguredServer:
    """The feature a user actually wants: a server the app did not ship with.

    Goes through the real config path — a YAML file, `get_mcp_servers_named()`,
    the transport factory — rather than constructing a toolset directly, so it
    covers the wiring an end user depends on.
    """

    @staticmethod
    def _config(tmp_path, extra=""):
        path = tmp_path / "mcp_servers.yaml"
        path.write_text(
            "servers:\n"
            "  - name: My Own Server\n"
            f"    command: {sys.executable}\n"
            f'    args: ["{SERVER}"]\n' + extra,
            encoding="utf-8",
        )
        return path

    def test_third_party_server_is_configurable(self, tmp_path, monkeypatch):
        from orionbelt_chat import mcp_config
        from orionbelt_chat.mcp_servers import get_mcp_servers_named

        monkeypatch.setattr(mcp_config, "config_path", lambda: self._config(tmp_path))
        names = [name for name, _ in get_mcp_servers_named()]
        assert "My Own Server" in names

    async def test_configured_server_answers_a_tool_call(self, tmp_path, monkeypatch):
        from orionbelt_chat import mcp_config
        from orionbelt_chat.mcp_servers import get_mcp_servers_named

        monkeypatch.setattr(mcp_config, "config_path", lambda: self._config(tmp_path))
        toolset = dict(get_mcp_servers_named())["My Own Server"]

        agent = Agent(model=_script_one_call("echo", {"text": "configured!"}), toolsets=[toolset])
        results = []
        async with agent:
            async with agent.iter("go") as agent_run:
                async for node in agent_run:
                    if Agent.is_call_tools_node(node):
                        async with node.stream(agent_run.ctx) as stream:
                            async for event in stream:
                                if isinstance(event, FunctionToolResultEvent):
                                    results.append(_tool_result_fields(event))

        assert results, "the configured server produced no tool result"
        assert "configured!" in results[0].text

    def test_sampling_flag_is_honoured(self, tmp_path, monkeypatch):
        from orionbelt_chat import mcp_config
        from orionbelt_chat.mcp_servers import servers_using_sampling

        monkeypatch.setattr(
            mcp_config, "config_path", lambda: self._config(tmp_path, "    sampling: true\n")
        )
        assert "My Own Server" in servers_using_sampling()


def _script_calls(*calls: tuple[str, dict]):
    """A model that makes *calls* one per turn, then answers; records its instructions."""
    seen: list[str | None] = []

    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info.instructions)
        made = sum(
            isinstance(part, ToolCallPart)
            for message in messages
            if isinstance(message, ModelResponse)
            for part in message.parts
        )
        if made < len(calls):
            tool_name, args = calls[made]
            return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args)])
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(model_fn), seen


async def _skill_results(toolset: MCPToolset, *calls: tuple[str, dict]) -> list[str]:
    """Discover the fake server's skills, then run *calls* against them; the result texts."""
    from orionbelt_chat.skills import discover_server_skills, skills_toolset

    found, _ = await discover_server_skills("Fake", toolset)
    model, _seen = _script_calls(*calls)
    agent = Agent(model=model, toolsets=[toolset, skills_toolset(found)])
    texts: list[str] = []
    async with agent.iter("go") as agent_run:
        async for node in agent_run:
            if Agent.is_call_tools_node(node):
                async with node.stream(agent_run.ctx) as stream:
                    async for event in stream:
                        if isinstance(event, FunctionToolResultEvent):
                            texts.append(_tool_result_fields(event).text)
    return texts


class TestServerSkills:
    """Skills a server publishes as `skill://` resources, read over a real session."""

    async def test_discovers_the_published_skills_only(self):
        from orionbelt_chat.skills import discover_server_skills

        async with _toolset() as toolset:
            found, errors = await discover_server_skills("Fake", toolset)
        assert [s.name for s in found] == ["fake-skill", "folded-skill"]
        assert all(s.server_name == "Fake" for s in found)
        assert errors == []

    async def test_a_folded_description_is_read_from_the_file(self):
        # FastMCP before 4 lists `description: >-` as ">-"; the catalog needs the text.
        from orionbelt_chat.skills import discover_server_skills

        async with _toolset() as toolset:
            found, _ = await discover_server_skills("Fake", toolset)
        folded = next(s for s in found if s.name == "folded-skill")
        assert folded.description == "Folded description over two lines."

    async def test_load_skill_and_a_reference_over_mcp(self):
        from orionbelt_chat.skills import LOAD_SKILL_TOOL, READ_SKILL_FILE_TOOL

        async with _toolset() as toolset:
            loaded, reference = await _skill_results(
                toolset,
                (LOAD_SKILL_TOOL, {"name": "fake-skill"}),
                (READ_SKILL_FILE_TOOL, {"name": "fake-skill", "path": "references/guide.md"}),
            )
        assert loaded.startswith('<skill name="fake-skill">\n# Fake skill')
        assert "description:" not in loaded
        # Listed from the manifest, without SKILL.md itself.
        assert loaded.endswith("- references/guide.md")
        assert reference == "Guide: use row_count."

    async def test_a_skill_uri_from_server_instructions_is_understood(self):
        from orionbelt_chat.skills import LOAD_SKILL_TOOL, READ_SKILL_FILE_TOOL

        async with _toolset() as toolset:
            loaded, reference = await _skill_results(
                toolset,
                (LOAD_SKILL_TOOL, {"name": "skill://fake-skill/SKILL.md"}),
                (
                    READ_SKILL_FILE_TOOL,
                    {"name": "fake-skill", "path": "skill://fake-skill/references/guide.md"},
                ),
            )
        assert loaded.startswith('<skill name="fake-skill">')
        assert reference == "Guide: use row_count."

    async def test_a_missing_file_is_an_answer_not_a_failed_run(self):
        from orionbelt_chat.skills import READ_SKILL_FILE_TOOL

        async with _toolset() as toolset:
            (text,) = await _skill_results(
                toolset, (READ_SKILL_FILE_TOOL, {"name": "fake-skill", "path": "references/x.md"})
            )
        assert text.startswith("Error: could not read skill://fake-skill/references/x.md")

    async def test_climbing_out_is_refused_before_the_server_is_asked(self):
        from orionbelt_chat.skills import READ_SKILL_FILE_TOOL

        async with _toolset() as toolset:
            (text,) = await _skill_results(
                toolset, (READ_SKILL_FILE_TOOL, {"name": "fake-skill", "path": "../other/SKILL.md"})
            )
        assert "outside the skill 'fake-skill'" in text


class TestServerInstructions:
    """What a server sends at initialization, passed on to the model by name."""

    async def test_reaches_the_model_under_the_server_name(self, monkeypatch):
        from orionbelt_chat import agent as agent_module
        from orionbelt_chat import mcp_servers

        monkeypatch.setattr(mcp_servers, "servers_with_instructions", lambda: frozenset({"Fake"}))
        model, seen = _script_calls()
        monkeypatch.setattr(agent_module, "resolve_model", lambda provider, name: model)

        async with _toolset() as toolset:
            forwarded = mcp_servers.server_instructions([("Fake", toolset)])
            agent = agent_module.make_agent(
                "openrouter", "any", toolsets=[toolset], server_instructions=forwarded
            )
            await agent.run("go")

        assert forwarded == [("Fake", "Fake server instructions: call echo before row_count.")]
        assert seen and "### Fake\n\nFake server instructions" in (seen[0] or "")

    async def test_a_server_that_was_never_entered_has_none(self, monkeypatch):
        from orionbelt_chat import mcp_servers

        monkeypatch.setattr(mcp_servers, "servers_with_instructions", lambda: frozenset({"Fake"}))
        assert mcp_servers.server_instructions([("Fake", _toolset())]) == []


class TestServerOptOuts:
    """`skills: false` and `instructions: false` in mcp_servers.yaml."""

    def test_both_default_on_and_can_be_switched_off(self, tmp_path, monkeypatch):
        from orionbelt_chat import mcp_config, mcp_servers

        config = TestConfiguredServer._config(
            tmp_path,
            "  - name: Quiet Server\n"
            f"    command: {sys.executable}\n"
            f'    args: ["{SERVER}"]\n'
            "    skills: false\n"
            "    instructions: 'no'\n",
        )
        monkeypatch.setattr(mcp_config, "config_path", lambda: config)
        assert "My Own Server" in mcp_servers.servers_with_skills()
        assert "My Own Server" in mcp_servers.servers_with_instructions()
        assert "Quiet Server" not in mcp_servers.servers_with_skills()
        assert "Quiet Server" not in mcp_servers.servers_with_instructions()

    async def test_switched_off_instructions_are_not_read(self, tmp_path, monkeypatch):
        from orionbelt_chat import mcp_config, mcp_servers

        config = TestConfiguredServer._config(tmp_path, "    instructions: false\n")
        monkeypatch.setattr(mcp_config, "config_path", lambda: config)
        async with _toolset() as toolset:
            assert mcp_servers.server_instructions([("My Own Server", toolset)]) == []
