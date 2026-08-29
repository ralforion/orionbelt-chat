"""Detect Mermaid diagram content in MCP tool results.

Scans tool return parts for Mermaid syntax (erDiagram, flowchart, etc.)
and returns the diagram text for client-side rendering via Mermaid.js.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Matches the start of valid Mermaid diagram types
_MERMAID_START = re.compile(
    r"^\s*(?:%%\{init:|erDiagram|graph\s|flowchart\s|sequenceDiagram|classDiagram"
    r"|stateDiagram|gantt|pie|gitgraph|journey|mindmap|timeline|sankey|xychart|block)",
    re.MULTILINE,
)


def is_mermaid(text: str) -> bool:
    """Return True if *text* looks like a Mermaid diagram definition."""
    return bool(_MERMAID_START.search(text))


def extract_mermaid_from_tool_results(result_messages: list) -> list[str]:
    """Return Mermaid diagram strings found in tool return parts."""
    diagrams: list[str] = []
    for msg in result_messages:
        for part in getattr(msg, "parts", []):
            if type(part).__name__ != "ToolReturnPart":
                continue
            raw = getattr(part, "content", "")
            text = _extract_text(raw)
            if text and is_mermaid(text):
                diagrams.append(text.strip())
                logger.info(
                    "Mermaid diagram found in tool %s (%d chars)",
                    getattr(part, "tool_name", "?"),
                    len(text),
                )
    return diagrams


def _extract_text(raw) -> str | None:
    """Pull text from a tool return value (str or dict with payload key)."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        for key in ("content", "diagram", "result", "data", "text", "body"):
            val = raw.get(key)
            if isinstance(val, str) and len(val) > 10:
                return val
    return None
