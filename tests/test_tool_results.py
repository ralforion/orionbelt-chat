"""Tests for the tool-result extraction in the streaming handler.

These exist because of a bug that shipped: Pydantic AI renamed
``FunctionToolResultEvent.result`` to ``.part``, and the app kept reading
``.result``. Every tool result raised ``AttributeError`` at runtime, and
nothing noticed — the code sat ~15 levels deep inside ``on_message``, so no
test reached it, and the CI smoke check never sends a message. The extraction
now lives in ``_tool_result_fields``, and these tests fail on the next rename.
"""

import json

import pytest
from pydantic_ai.messages import (
    BinaryContent,
    FunctionToolResultEvent,
    RetryPromptPart,
    ToolReturnPart,
)

from orionbelt_chat.app import _tool_result_fields

PNG = BinaryContent(data=b"\x89PNG\r\n\x1a\n", media_type="image/png")


def _event(content, tool_name="query_semantic_layer", call_id="call-1"):
    return FunctionToolResultEvent(
        part=ToolReturnPart(tool_name=tool_name, content=content, tool_call_id=call_id)
    )


class TestEventShape:
    """Pins the pydantic-ai attribute names the handler depends on."""

    def test_reads_the_part_field(self):
        result = _tool_result_fields(_event("42 rows"))
        assert result.text == "42 rows"
        assert result.call_id == "call-1"
        assert result.tool_name == "query_semantic_layer"

    def test_result_attribute_is_gone_upstream(self):
        # The regression guard: if a future pydantic-ai reintroduces `.result`,
        # or this assertion starts failing the other way, the helper above is
        # the one place to update.
        assert not hasattr(_event("x"), "result")

    def test_handles_a_retry_prompt_part(self):
        # The union arm the happy path forgets: a failed tool call arrives as a
        # RetryPromptPart, whose tool_name is optional.
        event = FunctionToolResultEvent(
            part=RetryPromptPart(content="try again", tool_name=None, tool_call_id="call-9")
        )
        result = _tool_result_fields(event)
        assert result.call_id == "call-9"
        assert result.tool_name is None
        assert "try again" in result.content


class TestContentSplitting:
    def test_plain_text(self):
        result = _tool_result_fields(_event("hello"))
        assert result.text == "hello"
        assert result.binaries == []
        assert result.content == "hello"

    def test_dict_is_json_serialised(self):
        result = _tool_result_fields(_event({"rows": 3}))
        assert json.loads(result.text) == {"rows": 3}

    def test_binary_only_yields_no_text(self):
        result = _tool_result_fields(_event(PNG))
        assert result.text == ""
        assert result.binaries == [PNG]

    def test_binary_only_still_has_display_content(self):
        # `content` falls back to the raw repr so the step and the log line are
        # never empty for an image-only result.
        result = _tool_result_fields(_event(PNG))
        assert result.content != ""

    def test_mixed_text_and_binary(self):
        result = _tool_result_fields(_event(["chart follows", PNG]))
        assert result.text == "chart follows"
        assert result.binaries == [PNG]


class TestReconnectDetection:
    """The branch keys its MCP-reconnect decision off `content`."""

    @pytest.mark.parametrize(
        "phrase",
        ["Session terminated", "session expired", "McpError", "Connection refused"],
    )
    def test_error_phrases_survive_into_content(self, phrase):
        from orionbelt_chat.app import _MCP_ERROR_PHRASES

        result = _tool_result_fields(_event(f"upstream said: {phrase}"))
        assert any(p in result.content for p in _MCP_ERROR_PHRASES)

    def test_ordinary_result_does_not_trip_reconnect(self):
        from orionbelt_chat.app import _MCP_ERROR_PHRASES

        result = _tool_result_fields(_event("SELECT returned 12 rows"))
        assert not any(p in result.content for p in _MCP_ERROR_PHRASES)
