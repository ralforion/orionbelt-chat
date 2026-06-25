"""Detect downloadable content in responses and tool results.

Scans LLM response code blocks and MCP tool results for content that
can be offered as file downloads (TTL ontologies, JSON, CSV, SQL, etc.).
"""

import logging
import re

import chainlit as cl

logger = logging.getLogger(__name__)

# Language hint → (file extension, MIME type)
DOWNLOAD_TYPES: dict[str, tuple[str, str]] = {
    "turtle": (".ttl", "text/turtle"),
    "ttl": (".ttl", "text/turtle"),
    "sparql": (".sparql", "application/sparql-query"),
    "json": (".json", "application/json"),
    "jsonld": (".jsonld", "application/ld+json"),
    "csv": (".csv", "text/csv"),
    "tsv": (".tsv", "text/tab-separated-values"),
    "sql": (".sql", "application/sql"),
    "yaml": (".yaml", "text/yaml"),
    "yml": (".yaml", "text/yaml"),
    "xml": (".xml", "application/xml"),
    "rdf": (".rdf", "application/rdf+xml"),
    "obml": (".yaml", "text/yaml"),
}

# Auto-detection patterns for tool result content (prefix → type key)
_CONTENT_SIGNATURES: list[tuple[str, str]] = [
    ("@prefix ", "ttl"),
    ("@base ", "ttl"),
    ("PREFIX ", "sparql"),
    ("SELECT ", "sql"),
    ("<?xml ", "xml"),
]

# Minimum content size (chars) to offer as a downloadable file
MIN_DOWNLOAD_SIZE = 200

# Fenced code blocks: ```lang\n…content…\n```
_CODE_BLOCK_RE = re.compile(r"```(\w+)\s*\n(.*?)```", re.DOTALL)


def extract_downloads_from_response(response_text: str) -> list[cl.File]:
    """Extract fenced code blocks with known file types as downloadable File elements."""
    files: list[cl.File] = []
    seen = 0
    for match in _CODE_BLOCK_RE.finditer(response_text):
        lang = match.group(1).lower()
        content = match.group(2)

        if lang not in DOWNLOAD_TYPES:
            continue
        if len(content.strip()) < MIN_DOWNLOAD_SIZE:
            continue

        ext, mime = DOWNLOAD_TYPES[lang]
        seen += 1
        name = f"download{ext}" if seen == 1 else f"download_{seen}{ext}"

        files.append(
            cl.File(name=name, content=content.encode("utf-8"), mime=mime, display="inline")
        )
        logger.info("Download from code block: %s (%d chars, %s)", name, len(content), lang)

    return files


def extract_downloads_from_tool_results(result_messages: list) -> list[cl.File]:
    """Scan tool return parts for content that looks like a downloadable file.

    Handles both bare strings and dicts (e.g. ``{'success': True, 'content': '...'}``).
    """
    files: list[cl.File] = []
    seen = 0
    for msg in result_messages:
        for part in getattr(msg, "parts", []):
            if type(part).__name__ != "ToolReturnPart":
                continue

            raw = getattr(part, "content", "")
            text = _extract_text(raw)
            if not text or len(text) < MIN_DOWNLOAD_SIZE:
                continue

            file_type = _detect_type(text)
            if not file_type:
                continue

            ext, mime = DOWNLOAD_TYPES[file_type]
            seen += 1
            tool_name = getattr(part, "tool_name", "download")
            name = f"{tool_name}{ext}" if seen == 1 else f"{tool_name}_{seen}{ext}"

            files.append(
                cl.File(name=name, content=text.encode("utf-8"), mime=mime, display="inline")
            )
            logger.info("Download from tool result: %s (%d chars, %s)", name, len(text), tool_name)

    return files


def _extract_text(raw) -> str | None:
    """Pull the best text candidate from a tool return value.

    Tool results may be a bare string, or a dict like
    ``{'success': True, 'content': '@prefix ...'}`` where the actual
    file payload is in a nested field.
    """
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        # Try common payload keys in priority order
        for key in ("content", "data", "result", "text", "body", "ontology"):
            val = raw.get(key)
            if isinstance(val, str) and len(val) >= MIN_DOWNLOAD_SIZE:
                return val
    return None


def _detect_type(content: str) -> str | None:
    """Auto-detect file type from content prefix."""
    stripped = content.lstrip()
    for prefix, type_key in _CONTENT_SIGNATURES:
        if stripped.startswith(prefix):
            return type_key
    return None
