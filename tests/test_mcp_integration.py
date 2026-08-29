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
