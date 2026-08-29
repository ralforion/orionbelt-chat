"""Chart rendering for FastMCP Apps ui:// resources.

Detects ui:// URIs in MCP tool results, fetches the resource HTML,
extracts Plotly figure data, and renders it natively via Chainlit.
"""

import json
import logging
import re
from typing import ClassVar

from chainlit.element import Element, ElementType
from pydantic.dataclasses import dataclass

from orionbelt_chat.provenance import mark_figure, provenance_record

logger = logging.getLogger(__name__)

UI_URI_PATTERN = re.compile(r"ui://[^\s\"']+")
CHART_JSON_URI_PATTERN = re.compile(r"ui://\S*chart-json/[^\s\"']+")


@dataclass
class PlotlyChart(Element):
    """Lightweight Plotly element — sends raw JSON to the Chainlit frontend
    which already bundles Plotly.js.  No plotly Python package required."""

    type: ClassVar[ElementType] = "plotly"

    def __post_init__(self):
        self.mime = "application/json"
        super().__post_init__()


def _extract_plotly_json(text: str) -> str | None:
    """Extract a Plotly figure dict from text and return it as a JSON string.

    Strategies (tried in order):
    1. JSON object with ``data`` or ``traces`` key (standard Plotly figure dict)
    2. ``Plotly.newPlot(el, data, layout)`` call in HTML/JS
    3. JSON array of trace objects (bare traces)

    Normalises to ``{"data": [...], "layout": {...}}`` for Plotly.js.
    """
    # Strategy 1: JSON object with data/traces key
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = json.JSONDecoder().raw_decode(text, match.start())
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        traces = obj.get("traces") or obj.get("data")
        if traces and isinstance(traces, list):
            return json.dumps({"data": traces, "layout": obj.get("layout", {})})

    # Strategy 2: Plotly.newPlot(element, data, layout) in JS/HTML
    newplot_match = re.search(r"Plotly\.newPlot\s*\(\s*['\"]?\w+['\"]?\s*,\s*", text)
    if newplot_match:
        rest = text[newplot_match.end() :]
        try:
            data, end = json.JSONDecoder().raw_decode(rest)
            if isinstance(data, list):
                # Try to find layout after the data array
                layout = {}
                after_data = rest[end:].lstrip()
                if after_data.startswith(","):
                    after_data = after_data[1:].lstrip()
                    try:
                        layout_obj, _ = json.JSONDecoder().raw_decode(after_data)
                        if isinstance(layout_obj, dict):
                            layout = layout_obj
                    except (json.JSONDecodeError, ValueError):
                        pass
                return json.dumps({"data": data, "layout": layout})
        except (json.JSONDecodeError, ValueError):
            pass

    # Strategy 3: bare JSON array of trace objects
    for match in re.finditer(r"\[", text):
        try:
            arr, _ = json.JSONDecoder().raw_decode(text, match.start())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(arr, list) and arr and isinstance(arr[0], dict):
            # Looks like trace objects if they have type/x/y/values etc.
            first = arr[0]
            if any(k in first for k in ("type", "x", "y", "values", "labels", "z")):
                return json.dumps({"data": arr, "layout": {}})

    return None


def _apply_defaults(fig: dict, record: dict | None = None) -> str:
    """Apply display defaults and the AI Act Art. 50(2) marking, return JSON."""
    layout = fig.setdefault("layout", {})
    layout.setdefault("autosize", True)
    layout.setdefault("width", 900)
    layout.setdefault("height", 550)
    margin = layout.setdefault("margin", {})
    margin.setdefault("t", 40)
    # Room below the plot for the provenance caption mark_figure adds.
    margin.setdefault("b", 80)
    layout.setdefault("legend", {})
    fig.setdefault("config", {"displaylogo": False})
    mark_figure(fig, record or provenance_record())
    return json.dumps(fig)


async def render_chart_if_present(
    tool_result_text: str,
    mcp_server,
    record: dict | None = None,
) -> PlotlyChart | None:
    """Render a Plotly chart from MCP tool result text.

    Tries the ``chart-json`` resource URI first (direct JSON, no parsing),
    then falls back to fetching the ``ui://`` HTML resource and extracting
    the Plotly figure data.

    ``record`` is the AI Act Art. 50(2) provenance marking applied to the
    figure; one is built per call when the caller does not supply it.
    """
    if not hasattr(mcp_server, "read_resource"):
        logger.warning("Server %s has no read_resource method", type(mcp_server).__name__)
        return None

    # Fast path: fetch JSON resource directly (no HTML parsing)
    json_match = CHART_JSON_URI_PATTERN.search(tool_result_text)
    if json_match:
        json_uri = json_match.group(0)
        try:
            resource_content = await mcp_server.read_resource(json_uri)
            content_str = (
                str(resource_content) if not isinstance(resource_content, str) else resource_content
            )
            fig = json.loads(content_str)
            if isinstance(fig, dict) and "data" in fig:
                fig_json = _apply_defaults(fig, record)
                logger.info("Plotly JSON from chart-json resource (%d chars)", len(fig_json))
                return PlotlyChart(name="chart", content=fig_json, display="inline")
        except Exception as e:
            logger.debug("chart-json resource fetch failed, falling back to HTML: %s", e)

    # Fallback: fetch ui:// HTML resource and extract Plotly data
    match = UI_URI_PATTERN.search(tool_result_text)
    if not match:
        return None

    # Find the first non-JSON ui:// URI (the HTML chart resource)
    uri = None
    for m in UI_URI_PATTERN.finditer(tool_result_text):
        if "chart-json/" not in m.group(0):
            uri = m.group(0)
            break
    if not uri:
        return None
    logger.info("Chart URI detected: %s (server: %s)", uri, type(mcp_server).__name__)

    try:
        resource_content = await mcp_server.read_resource(uri)
        logger.info(
            "Resource fetched (%s, %d chars): %.300s",
            type(resource_content).__name__,
            len(str(resource_content)),
            str(resource_content)[:300],
        )
        content_str = (
            str(resource_content) if not isinstance(resource_content, str) else resource_content
        )
        extracted = _extract_plotly_json(content_str)
        if extracted:
            fig = json.loads(extracted)
            fig_json = _apply_defaults(fig, record)
            logger.info("Plotly JSON extracted from HTML (%d chars)", len(fig_json))
            return PlotlyChart(name="chart", content=fig_json, display="inline")
        else:
            logger.warning("No Plotly JSON found in resource content")
    except Exception as e:
        logger.warning("Failed to render chart from %s: %s", uri, e, exc_info=True)

    return None
