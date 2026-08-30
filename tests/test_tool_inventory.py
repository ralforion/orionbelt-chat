"""Tests for the MCP tool inventory shown at the start of a chat.

The config format takes arbitrary third-party servers, so a session can open
with servers whose tools nobody in the room has memorised. The greeting names
them; these pin what it says about them.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orionbelt_chat import app


def _tool(name: str, description: str = "") -> SimpleNamespace:
    return SimpleNamespace(name=name, description=description)


class TestServerLine:
    """The bullet naming each server in the greeting.

    When tools were listed, the whole label is the element's name, because that
    is what Chainlit turns into the clickable pill — a code span around it would
    split the pill in two.
    """

    def test_pill_carries_name_and_count(self):
        assert app._server_line("DeepWiki", [_tool("a"), _tool("b")], False) == (
            "- DeepWiki (2 tools)"
        )

    def test_singular(self):
        assert app._server_line("S", [_tool("a")], False) == "- S (1 tool)"

    def test_sampling_note_follows_the_pill(self):
        assert app._server_line("S", [_tool("a")], True) == "- S (1 tool) — uses sampling"

    def test_empty_server_says_so_and_stays_plain(self):
        # No tools means no element, so nothing to click — keep the code span.
        assert app._server_line("S", [], False) == "- `S` (no tools)"

    def test_failed_listing_is_distinct_from_empty(self):
        assert app._server_line("S", None, False) == "- `S` (tools unavailable)"

    def test_never_listed_says_nothing_about_tools(self):
        # Callers from before the inventory existed must not gain a claim.
        assert app._server_line("S", app._UNLISTED, False) == "- `S`"


class TestToolElements:
    @staticmethod
    def _elements(tools_by_server):
        """`cl.Text` reads the Chainlit context at construction, so stub it."""
        with (
            patch.object(app.cl, "user_session") as user_session,
            patch.object(app.cl, "Text", side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
        ):
            user_session.get.return_value = tools_by_server
            return app._tool_elements()

    def test_one_element_per_server_named_like_its_pill(self):
        elements = self._elements({"DeepWiki": [_tool("a"), _tool("b")], "Context7": [_tool("c")]})
        assert [e.name for e in elements] == ["DeepWiki (2 tools)", "Context7 (1 tool)"]
        assert all(e.display == "side" for e in elements)

    def test_servers_without_tools_get_no_element(self):
        assert self._elements({"Broken": None, "Empty": []}) == []

    def test_element_names_match_the_server_lines(self):
        # The pill only appears if the element name occurs verbatim in the body.
        tools = [_tool("a"), _tool("b")]
        assert app._tool_element_name("DeepWiki", tools) in app._server_line(
            "DeepWiki", tools, True
        )


class TestToolInventoryMarkdown:
    def test_name_and_description(self):
        markdown = app._tool_inventory_markdown([_tool("ask_question", "Ask about a repo.")])
        assert markdown == "- `ask_question` — Ask about a repo."

    def test_description_is_optional(self):
        assert app._tool_inventory_markdown([_tool("fetch")]) == "- `fetch`"

    def test_only_the_first_line_is_kept(self):
        # MCP descriptions carry argument docs and examples below the summary.
        markdown = app._tool_inventory_markdown(
            [_tool("search", "Search the web.\n\nArgs:\n  query: the query")]
        )
        assert markdown == "- `search` — Search the web."

    def test_long_description_is_truncated(self):
        markdown = app._tool_inventory_markdown([_tool("t", "x" * 400)])
        assert markdown.endswith("…")
        assert len(markdown) < 200

    def test_one_bullet_per_tool(self):
        markdown = app._tool_inventory_markdown([_tool("a", "A."), _tool("b", "B.")])
        assert markdown.splitlines() == ["- `a` — A.", "- `b` — B."]


class TestCollectTools:
    def test_lists_each_server(self):
        server = MagicMock()
        server.list_tools = AsyncMock(return_value=[_tool("a"), _tool("b")])
        tools = asyncio.run(app._collect_tools([("DeepWiki", server)]))
        assert [t.name for t in tools["DeepWiki"]] == ["a", "b"]

    def test_a_failed_listing_does_not_lose_the_other_servers(self):
        ok = MagicMock()
        ok.list_tools = AsyncMock(return_value=[_tool("a")])
        broken = MagicMock()
        broken.list_tools = AsyncMock(side_effect=RuntimeError("nope"))
        tools = asyncio.run(app._collect_tools([("Good", ok), ("Bad", broken)]))
        assert tools["Bad"] is None
        assert [t.name for t in tools["Good"]] == ["a"]

    def test_a_slow_server_does_not_hold_up_the_greeting(self):
        async def never(*_args, **_kwargs):
            await asyncio.sleep(3600)

        slow = MagicMock()
        slow.list_tools = never
        with patch.object(app, "_TOOL_LIST_TIMEOUT", 0.01):
            tools = asyncio.run(app._collect_tools([("Slow", slow)]))
        assert tools["Slow"] is None


class TestServerListCounts:
    """`_update_mcp_info` folds the count into the line naming each server."""

    @pytest.fixture
    def session(self):
        store = {}
        with (
            patch.object(app.cl, "user_session") as user_session,
            patch.object(app, "servers_using_sampling", return_value=frozenset()),
            patch.object(app, "get_mcp_server_errors", return_value=[]),
            patch.object(app, "get_sampling_model_label", return_value=None),
        ):
            user_session.set.side_effect = store.__setitem__
            user_session.get.side_effect = lambda k, d=None: store.get(k, d)
            yield store

    def test_count_appears_beside_the_name(self, session):
        app._update_mcp_info(["DeepWiki"], None, {"DeepWiki": [_tool("a"), _tool("b")]})
        assert "- DeepWiki (2 tools)" in session["mcp_info"]

    def test_sampling_note_survives_alongside_the_count(self, session):
        with patch.object(app, "servers_using_sampling", return_value=frozenset({"S"})):
            app._update_mcp_info(["S"], None, {"S": [_tool("a")]})
        assert "- S (1 tool) — uses sampling" in session["mcp_info"]

    def test_no_tool_map_leaves_the_line_as_it_was(self, session):
        # Callers that never listed tools must not get "(tools unavailable)".
        app._update_mcp_info(["S"])
        assert "- `S`\n" in session["mcp_info"] + "\n"
        assert "tools" not in session["mcp_info"]


class TestCollectToolsConcurrency:
    """The greeting waits on this listing, so servers are asked in parallel.

    Serially, a handful of servers that are up but slow to answer would each
    add their own timeout to how long the user stares at nothing.
    """

    def test_servers_are_listed_concurrently(self):
        started, finished = [], []

        def slow_server(name):
            async def list_tools():
                started.append(name)
                await asyncio.sleep(0.15)
                finished.append(name)
                return [_tool("a")]

            server = MagicMock()
            server.list_tools = list_tools
            return server

        pairs = [(n, slow_server(n)) for n in ("A", "B", "C")]
        tools = asyncio.run(app._collect_tools(pairs))

        # Every listing had begun before the first one came back.
        assert len(started) == 3
        assert started == ["A", "B", "C"]
        assert finished and set(tools) == {"A", "B", "C"}

    def test_one_slow_server_does_not_add_its_timeout_to_the_others(self):
        async def hangs():
            await asyncio.sleep(3600)

        def quick():
            async def list_tools():
                return [_tool("a")]

            server = MagicMock()
            server.list_tools = list_tools
            return server

        stuck = MagicMock()
        stuck.list_tools = hangs
        pairs = [("Stuck", stuck), ("Stuck2", MagicMock(list_tools=hangs)), ("Quick", quick())]

        async def run():
            loop = asyncio.get_running_loop()
            start = loop.time()
            tools = await app._collect_tools(pairs)
            return tools, loop.time() - start

        with patch.object(app, "_TOOL_LIST_TIMEOUT", 0.2):
            tools, elapsed = asyncio.run(run())

        # Two stuck servers, one timeout — not two.
        assert elapsed < 0.5
        assert tools["Stuck"] is None and tools["Stuck2"] is None
        assert [t.name for t in tools["Quick"]] == ["a"]

    def test_configured_order_is_preserved(self):
        def server(n):
            async def list_tools():
                await asyncio.sleep(0.05 if n == "First" else 0)
                return [_tool("a")]

            return MagicMock(list_tools=list_tools)

        pairs = [("First", server("First")), ("Second", server("Second"))]
        # The slow one is first in the config, so it must stay first in the list.
        assert list(asyncio.run(app._collect_tools(pairs))) == ["First", "Second"]


class TestRefreshStatusMessage:
    """The greeting is the one message that stays on screen all session.

    A model switch or a reconnect changes what is connected, so leaving it on
    the old listing means stale counts and pills that open the previous tools.
    """

    @pytest.fixture
    def session(self):
        store = {"provider": "openrouter", "model": "some-model", "mcp_info": "Connected: none"}
        with (
            patch.object(app.cl, "user_session") as user_session,
            patch.object(app.cl, "Text", side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
        ):
            user_session.set.side_effect = store.__setitem__
            user_session.get.side_effect = lambda k, d=None: store.get(k, d)
            yield store

    def test_rewrites_content_and_elements(self, session):
        session["mcp_tools"] = {"DeepWiki": [_tool("a"), _tool("b")]}
        status = MagicMock()
        status.update = AsyncMock()
        session["status_msg"] = status

        asyncio.run(app._refresh_status_message())

        assert [e.name for e in status.elements] == ["DeepWiki (2 tools)"]
        assert "Connected: none" in status.content
        assert "some-model" in status.content
        status.update.assert_awaited_once()

    def test_no_status_message_is_not_an_error(self, session):
        # Init failed, or the greeting was never sent.
        asyncio.run(app._refresh_status_message())
