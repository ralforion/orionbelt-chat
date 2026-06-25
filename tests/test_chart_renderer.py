"""Tests for src.chart_renderer."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.chart_renderer import (
    UI_URI_PATTERN,
    PlotlyChart,
    _extract_plotly_json,
    render_chart_if_present,
)


@pytest.fixture(autouse=True)
def _mock_chainlit_context():
    """Provide a fake Chainlit context so PlotlyChart can be instantiated."""
    from chainlit.context import context_var

    mock_ctx = MagicMock()
    mock_ctx.session.thread_id = "test-thread"
    mock_ctx.session.id = "test-session"
    mock_ctx.session.files = {}
    token = context_var.set(mock_ctx)
    yield
    context_var.reset(token)


class TestUiUriPattern:
    def test_matches_ui_uri(self):
        text = "Chart available at ui://chart/sales-2024 for review"
        match = UI_URI_PATTERN.search(text)
        assert match is not None
        assert match.group(0) == "ui://chart/sales-2024"

    def test_no_match(self):
        assert UI_URI_PATTERN.search("no chart here") is None

    def test_stops_at_whitespace(self):
        text = "ui://chart/1 extra text"
        match = UI_URI_PATTERN.search(text)
        assert match.group(0) == "ui://chart/1"

    def test_stops_at_quote(self):
        text = '"ui://chart/1"'
        match = UI_URI_PATTERN.search(text)
        assert match.group(0) == "ui://chart/1"


class TestExtractPlotlyJson:
    def test_data_key(self):
        text = json.dumps({"data": [{"type": "bar", "x": [1], "y": [2]}], "layout": {"title": "T"}})
        result = _extract_plotly_json(text)
        assert result is not None
        obj = json.loads(result)
        assert obj["data"][0]["type"] == "bar"
        assert obj["layout"]["title"] == "T"

    def test_traces_key(self):
        text = json.dumps({"traces": [{"x": [1], "y": [2]}], "layout": {}})
        result = _extract_plotly_json(text)
        assert result is not None
        obj = json.loads(result)
        assert len(obj["data"]) == 1

    def test_embedded_in_html(self):
        html = '<html><body><script>var fig = {"data": [{"type": "scatter"}], "layout": {}};</script></body></html>'
        result = _extract_plotly_json(html)
        assert result is not None
        obj = json.loads(result)
        assert obj["data"][0]["type"] == "scatter"

    def test_plotly_newplot(self):
        js = 'Plotly.newPlot("chart", [{"type": "bar", "x": [1], "y": [2]}], {"title": "T"})'
        result = _extract_plotly_json(js)
        assert result is not None
        obj = json.loads(result)
        assert obj["data"][0]["type"] == "bar"
        assert obj["layout"]["title"] == "T"

    def test_bare_trace_array(self):
        text = '[{"type": "bar", "x": [1, 2], "y": [3, 4]}]'
        result = _extract_plotly_json(text)
        assert result is not None
        obj = json.loads(result)
        assert len(obj["data"]) == 1

    def test_no_match(self):
        assert _extract_plotly_json("just plain text") is None

    def test_non_plotly_json(self):
        text = json.dumps({"name": "test", "value": 42})
        assert _extract_plotly_json(text) is None


class TestRenderChartIfPresent:
    @pytest.mark.asyncio
    async def test_no_uri_returns_none(self):
        server = AsyncMock()
        result = await render_chart_if_present("no chart", server)
        assert result is None

    @pytest.mark.asyncio
    async def test_with_uri_returns_plotly_chart(self):
        server = AsyncMock()
        fig = json.dumps({"data": [{"type": "bar", "x": [1], "y": [2]}], "layout": {}})
        server.read_resource = AsyncMock(return_value=f"<html><script>{fig}</script></html>")
        result = await render_chart_if_present("see ui://chart/sales", server)
        assert isinstance(result, PlotlyChart)
        assert result.type == "plotly"
        server.read_resource.assert_called_once_with("ui://chart/sales")

    @pytest.mark.asyncio
    async def test_server_error_returns_none(self):
        server = AsyncMock()
        server.read_resource = AsyncMock(side_effect=Exception("not found"))
        result = await render_chart_if_present("see ui://chart/sales", server)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_read_resource_returns_none(self):
        server = object()  # no read_resource attribute
        result = await render_chart_if_present("see ui://chart/sales", server)
        assert result is None

    @pytest.mark.asyncio
    async def test_unparseable_resource_returns_none(self):
        server = AsyncMock()
        server.read_resource = AsyncMock(return_value="<html>no plotly data</html>")
        result = await render_chart_if_present("see ui://chart/sales", server)
        assert result is None
