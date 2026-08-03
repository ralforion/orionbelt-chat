"""EU AI Act Art. 50(2) marking of AI-generated output.

Art. 50(2) requires the provider of an AI system that generates synthetic
content to mark that output in a machine-readable format, detectable as
artificially generated.  Marking must be effective, interoperable and robust
"as far as technically feasible" for the content type.

This module owns the one provenance record every output channel states, and
the per-format helpers that embed it:

- text payloads (TTL, SPARQL, SQL, YAML, XML/RDF) — native comment header
- JSON / JSON-LD — a ``_provenance`` object, or ``prov:wasGeneratedBy``
- CSV / TSV — no safe comment syntax; the caller ships a sidecar record
- PNG — XMP packet carrying IPTC ``DigitalSourceType``
- Plotly figures — ``meta`` block plus a visible caption

Text *watermarking* (e.g. SynthID-Text) is deliberately not attempted: it
needs logit-level access this app does not have across OpenRouter/Ollama/
Anthropic/OpenAI, and zero-width marker characters would corrupt the SQL and
Turtle users copy out of the chat.  See docs/AI_ACT.md.
"""

import json
import logging
import struct
import zlib
from datetime import UTC, datetime
from html import escape
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

logger = logging.getLogger(__name__)

try:
    APP_VERSION = _pkg_version("orionbelt-chat")
except PackageNotFoundError:  # running from a source checkout
    APP_VERSION = "unknown"

PRODUCER = "OrionBelt Chat"

# IPTC DigitalSourceType vocabulary — the interoperable term for content
# created by a generative model.  Recognised by C2PA and XMP-aware readers.
TRAINED_ALGORITHMIC_MEDIA = "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"

# Human-readable half of the marking, kept identical across every channel.
NOTICE = "AI-generated content"

# Comment syntax per file extension.  Extensions absent here have no comment
# form that is safe to inject (CSV/TSV) or need structural marking (JSON).
_LINE_COMMENT: dict[str, str] = {
    ".ttl": "#",
    ".sparql": "#",
    ".yaml": "#",
    ".yml": "#",
}
_SQL_COMMENT = {".sql"}
_XML_LIKE = {".xml", ".rdf"}
_JSON_LIKE = {".json", ".jsonld"}
_NO_INLINE_MARKING = {".csv", ".tsv"}


def provenance_record(model_label: str | None = None) -> dict:
    """Build the provenance record embedded in every marked output.

    ``model_label`` is the provider/model that produced the content, e.g.
    ``"openrouter/anthropic/claude-sonnet-5"``.  Omitted when the content
    came from a tool rather than the model itself.
    """
    record = {
        "aiGenerated": True,
        "digitalSourceType": TRAINED_ALGORITHMIC_MEDIA,
        "producer": f"{PRODUCER} v{APP_VERSION}",
        "generatedAt": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if model_label:
        record["model"] = model_label
    return record


def notice_line(record: dict) -> str:
    """One-line human-readable rendering of a provenance record."""
    parts = [NOTICE, record["producer"]]
    if record.get("model"):
        parts.append(f"model: {record['model']}")
    parts.append(record["generatedAt"])
    return " · ".join(parts)


def mark_text(content: str, ext: str, record: dict) -> str:
    """Return ``content`` with a provenance header for the given extension.

    Unknown or comment-less extensions are returned unchanged — marking must
    never corrupt the payload it describes.
    """
    if ext in _JSON_LIKE:
        return _mark_json(content, ext, record)
    if ext in _XML_LIKE:
        return _mark_xml(content, record)
    if ext in _SQL_COMMENT:
        return _prepend_comment(content, "--", record)
    if ext in _LINE_COMMENT:
        return _prepend_comment(content, _LINE_COMMENT[ext], record)
    return content


def needs_sidecar(ext: str) -> bool:
    """True when ``ext`` has no safe inline comment form (CSV/TSV).

    A leading ``#`` line breaks strict CSV parsers, so those payloads ship an
    adjacent ``.prov.json`` record instead of an in-band header.
    """
    return ext in _NO_INLINE_MARKING


def sidecar_bytes(record: dict, subject: str) -> bytes:
    """Serialise a provenance record as a standalone sidecar document.

    ``subject`` names the file the record describes, so the sidecar is
    self-describing and two sidecars in one turn are never byte-identical —
    callers deduplicate download elements by content.
    """
    return json.dumps({"subject": subject, **record}, indent=2).encode("utf-8")


def _prepend_comment(content: str, marker: str, record: dict) -> str:
    """Prepend a comment header using ``marker`` as the line-comment token."""
    return f"{marker} {notice_line(record)}\n{content}"


def _mark_xml(content: str, record: dict) -> str:
    """Insert an XML comment, after the declaration when one is present.

    An XML declaration is only well-formed as the very first construct in the
    document, so the header cannot simply be prepended.
    """
    comment = f"<!-- {notice_line(record)} -->"
    stripped = content.lstrip()
    if stripped.startswith("<?xml"):
        end = content.find("?>")
        if end != -1:
            split = end + 2
            return f"{content[:split]}\n{comment}{content[split:]}"
    return f"{comment}\n{content}"


def _mark_json(content: str, ext: str, record: dict) -> str:
    """Embed the record in a JSON document, preserving its shape.

    Only mappings can carry the key — a top-level array is returned unchanged
    rather than being wrapped, which would change how consumers parse it.
    """
    try:
        doc = json.loads(content)
    except json.JSONDecodeError:
        logger.debug("Unparseable JSON download left unmarked (%d chars)", len(content))
        return content

    if not isinstance(doc, dict):
        return content

    if ext == ".jsonld" and isinstance(doc.get("@context"), dict):
        doc["@context"].setdefault("prov", "http://www.w3.org/ns/prov#")
        doc["prov:wasGeneratedBy"] = record
    else:
        doc["_provenance"] = record

    return json.dumps(doc, indent=2)


def xmp_packet(record: dict) -> str:
    """Build an XMP packet asserting the content is model-generated.

    Record values reach the packet as XML attributes, so they are escaped —
    a model or producer name is free text and must not be able to break the
    metadata it is embedded in.
    """
    model = escape(record.get("model", ""), quote=True)
    producer = escape(record["producer"], quote=True)
    source_type = escape(record["digitalSourceType"], quote=True)
    created = escape(record["generatedAt"], quote=True)
    return (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about=""'
        ' xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/"'
        ' xmlns:xmp="http://ns.adobe.com/xap/1.0/"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
        f' Iptc4xmpExt:DigitalSourceType="{source_type}"'
        f' xmp:CreatorTool="{producer}"'
        f' xmp:CreateDate="{created}"'
        f' dc:creator="{model}">'
        f'<dc:description><rdf:Alt><rdf:li xml:lang="x-default">{NOTICE}'
        "</rdf:li></rdf:Alt></dc:description>"
        "</rdf:Description></rdf:RDF></x:xmpmeta>"
        '<?xpacket end="w"?>'
    )


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_XMP_KEYWORD = b"XML:com.adobe.xmp"


def mark_png(data: bytes, record: dict) -> bytes:
    """Return the PNG with an XMP iTXt chunk describing its provenance.

    Non-PNG bytes and PNGs that already carry an XMP packet are returned
    unchanged.  Written by hand rather than via an imaging library so the
    marking adds no runtime dependency.
    """
    if not data.startswith(_PNG_SIGNATURE):
        return data
    if _XMP_KEYWORD in data:
        return data

    # Chunk layout after the signature: length(4) type(4) payload crc(4).
    # IHDR must come first, so the XMP chunk is inserted directly after it.
    ihdr_len = struct.unpack(">I", data[8:12])[0]
    insert_at = 8 + 12 + ihdr_len

    # iTXt payload: keyword\0 compression_flag compression_method
    #               language_tag\0 translated_keyword\0 text
    payload = _XMP_KEYWORD + b"\x00\x00\x00\x00\x00" + xmp_packet(record).encode("utf-8")
    chunk = (
        struct.pack(">I", len(payload))
        + b"iTXt"
        + payload
        + struct.pack(">I", zlib.crc32(b"iTXt" + payload) & 0xFFFFFFFF)
    )
    return data[:insert_at] + chunk + data[insert_at:]


def mark_figure(fig: dict, record: dict) -> None:
    """Mark a Plotly figure in place.

    Two markings, because they survive different export paths: ``meta`` rides
    along in the figure JSON the frontend receives, while the caption is the
    only part that survives the modebar's client-side PNG export.
    """
    meta = fig.setdefault("meta", {})
    if isinstance(meta, dict):
        meta.setdefault("provenance", record)

    layout = fig.setdefault("layout", {})
    annotations = layout.setdefault("annotations", [])
    if not isinstance(annotations, list):
        return
    if any(isinstance(a, dict) and a.get("name") == "ai-provenance" for a in annotations):
        return

    annotations.append(
        {
            "name": "ai-provenance",
            "text": f"{NOTICE} · {record['producer']}",
            "showarrow": False,
            "xref": "paper",
            "yref": "paper",
            "x": 1,
            "y": -0.12,
            "xanchor": "right",
            "yanchor": "top",
            "font": {"size": 10, "color": "#888888"},
        }
    )
