"""Tests for the EU AI Act Art. 50(2) marking of AI-generated output.

Art. 50(2) requires generated content to be marked in a machine-readable
format and detectable as artificially generated.  These tests pin the marking
to every channel by which content leaves the app — downloads, charts, images
and the rendered page — so a refactor cannot silently drop it, and pin the
guarantee that marking never corrupts the payload it describes.
"""

import json
import struct
import zlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import app
from src import provenance
from src.chart_renderer import _apply_defaults
from src.file_downloads import (
    extract_downloads_from_response,
    extract_downloads_from_tool_results,
)

REPO_ROOT = Path(app.__file__).resolve().parent

# Comfortably over MIN_DOWNLOAD_SIZE so the extractors emit a file.
PADDING = "x" * 250


@pytest.fixture(autouse=True)
def _mock_chainlit_context():
    """Provide a fake Chainlit context so cl.File can be instantiated."""
    from chainlit.context import context_var

    mock_ctx = MagicMock()
    mock_ctx.session.thread_id = "test-thread"
    mock_ctx.session.id = "test-session"
    mock_ctx.session.files = {}
    token = context_var.set(mock_ctx)
    yield
    context_var.reset(token)


@pytest.fixture
def record():
    return provenance.provenance_record("openrouter/anthropic/claude-sonnet-5")


def _fenced(lang: str, body: str) -> str:
    return f"```{lang}\n{body}\n```"


def _files_by_name(files) -> dict:
    return {f.name: f for f in files}


class TestProvenanceRecord:
    def test_declares_content_as_ai_generated(self, record):
        assert record["aiGenerated"] is True

    def test_uses_the_iptc_digital_source_type_vocabulary(self, record):
        # The interoperable term Art. 50(2) marking is expected to speak.
        assert record["digitalSourceType"].endswith("/trainedAlgorithmicMedia")

    def test_names_the_producing_application_and_version(self, record):
        assert record["producer"].startswith("OrionBelt Chat v")

    def test_names_the_model(self, record):
        assert record["model"] == "openrouter/anthropic/claude-sonnet-5"

    def test_omits_the_model_when_unknown(self):
        # Better to say nothing than to name the wrong model.
        assert "model" not in provenance.provenance_record()

    def test_timestamp_is_timezone_aware_iso(self, record):
        assert record["generatedAt"].endswith("+00:00")

    def test_notice_line_is_human_readable_and_single_line(self, record):
        line = provenance.notice_line(record)
        assert "AI-generated content" in line
        assert "\n" not in line


class TestTextMarking:
    @pytest.mark.parametrize(
        ("ext", "marker"),
        [(".ttl", "#"), (".sparql", "#"), (".yaml", "#"), (".sql", "--")],
    )
    def test_comment_header_uses_the_native_syntax(self, ext, marker, record):
        marked = provenance.mark_text("@prefix ex: <http://e/> .", ext, record)
        assert marked.startswith(f"{marker} AI-generated content")

    def test_payload_survives_marking_intact(self, record):
        body = "SELECT * FROM orders;"
        assert body in provenance.mark_text(body, ".sql", record)

    def test_unknown_extension_is_left_alone(self, record):
        body = "some prose"
        assert provenance.mark_text(body, ".txt", record) == body


class TestXmlMarking:
    def test_comment_follows_the_xml_declaration(self, record):
        # A declaration is only well-formed as the very first construct, so
        # the header cannot simply be prepended.
        marked = provenance.mark_text('<?xml version="1.0"?>\n<a/>', ".xml", record)
        assert marked.startswith('<?xml version="1.0"?>')
        assert marked.index("<!--") < marked.index("<a/>")

    def test_comment_leads_when_there_is_no_declaration(self, record):
        marked = provenance.mark_text("<a/>", ".rdf", record)
        assert marked.startswith("<!-- AI-generated content")


class TestJsonMarking:
    def test_object_gains_a_provenance_key(self, record):
        marked = json.loads(provenance.mark_text('{"a": 1}', ".json", record))
        assert marked["_provenance"]["aiGenerated"] is True
        assert marked["a"] == 1

    def test_json_ld_uses_the_prov_vocabulary(self, record):
        doc = '{"@context": {"ex": "http://e/"}, "ex:name": "n"}'
        marked = json.loads(provenance.mark_text(doc, ".jsonld", record))
        assert marked["@context"]["prov"] == "http://www.w3.org/ns/prov#"
        assert marked["prov:wasGeneratedBy"]["aiGenerated"] is True

    def test_array_document_is_not_reshaped(self, record):
        # Wrapping a top-level array would change how consumers parse it.
        doc = "[1, 2, 3]"
        assert json.loads(provenance.mark_text(doc, ".json", record)) == [1, 2, 3]

    def test_unparseable_json_is_passed_through(self, record):
        doc = "{not json"
        assert provenance.mark_text(doc, ".json", record) == doc


class TestPngMarking:
    @staticmethod
    def _minimal_png() -> bytes:
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
        chunk = (
            struct.pack(">I", len(ihdr))
            + b"IHDR"
            + ihdr
            + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF)
        )
        end = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND"))
        return b"\x89PNG\r\n\x1a\n" + chunk + end

    def test_embeds_an_xmp_packet(self, record):
        marked = provenance.mark_png(self._minimal_png(), record)
        assert b"XML:com.adobe.xmp" in marked
        assert b"trainedAlgorithmicMedia" in marked

    def test_keeps_the_signature_and_ihdr_first(self, record):
        marked = provenance.mark_png(self._minimal_png(), record)
        assert marked.startswith(b"\x89PNG\r\n\x1a\n")
        assert marked[12:16] == b"IHDR"

    def test_chunk_crc_is_valid(self, record):
        marked = provenance.mark_png(self._minimal_png(), record)
        start = marked.index(b"iTXt") - 4
        length = struct.unpack(">I", marked[start : start + 4])[0]
        body = marked[start + 4 : start + 8 + length]
        crc = struct.unpack(">I", marked[start + 8 + length : start + 12 + length])[0]
        assert crc == zlib.crc32(body) & 0xFFFFFFFF

    def test_model_name_cannot_break_out_of_the_xmp(self, record):
        record["model"] = 'evil" rogue="1'
        packet = provenance.xmp_packet(record)
        assert 'rogue="1' not in packet
        assert "&quot;" in packet

    def test_non_png_bytes_are_untouched(self, record):
        jpeg = b"\xff\xd8\xff\xe0 not a png"
        assert provenance.mark_png(jpeg, record) == jpeg

    def test_marking_is_idempotent(self, record):
        once = provenance.mark_png(self._minimal_png(), record)
        assert provenance.mark_png(once, record) == once


class TestChartMarking:
    def test_figure_meta_carries_the_record(self, record):
        fig = json.loads(_apply_defaults({"data": [], "layout": {}}, record))
        assert fig["meta"]["provenance"]["aiGenerated"] is True

    def test_visible_caption_survives_png_export(self, record):
        # The modebar's client-side export keeps annotations but drops meta,
        # so the caption is the only marking on an exported chart image.
        fig = json.loads(_apply_defaults({"data": [], "layout": {}}, record))
        captions = [a["text"] for a in fig["layout"]["annotations"]]
        assert any("AI-generated content" in c for c in captions)

    def test_caption_is_not_duplicated_on_remarking(self, record):
        fig = {"data": [], "layout": {}}
        provenance.mark_figure(fig, record)
        provenance.mark_figure(fig, record)
        assert len(fig["layout"]["annotations"]) == 1

    def test_existing_annotations_are_preserved(self, record):
        fig = {"data": [], "layout": {"annotations": [{"text": "Q3"}]}}
        provenance.mark_figure(fig, record)
        assert fig["layout"]["annotations"][0]["text"] == "Q3"


class TestDownloadMarking:
    def test_code_block_download_is_marked(self, record):
        text = _fenced("sql", f"SELECT * FROM orders; {PADDING}")
        files = extract_downloads_from_response(text, record)
        body = _files_by_name(files)["download.sql"].content.decode()
        assert body.startswith("-- AI-generated content")

    def test_tool_result_download_is_marked(self, record):
        part = type("ToolReturnPart", (), {})()
        part.content = f"@prefix ex: <http://e/> . {PADDING}"
        part.tool_name = "get_ontology"
        msg = type("Msg", (), {"parts": [part]})()

        files = extract_downloads_from_tool_results([msg], record)
        body = _files_by_name(files)["get_ontology.ttl"].content.decode()
        assert body.startswith("# AI-generated content")

    def test_csv_payload_is_left_unmarked(self, record):
        # A leading comment line breaks strict CSV parsers, so the rows must
        # reach the user exactly as generated.
        rows = f"a,b\n1,2\n3,{PADDING}"
        files = extract_downloads_from_response(_fenced("csv", rows), record)
        assert _files_by_name(files)["download.csv"].content.decode().strip() == rows

    def test_csv_ships_a_provenance_sidecar_instead(self, record):
        rows = f"a,b\n1,2\n3,{PADDING}"
        files = extract_downloads_from_response(_fenced("csv", rows), record)
        sidecar = _files_by_name(files)["download.csv.prov.json"]
        assert json.loads(sidecar.content)["aiGenerated"] is True

    def test_each_sidecar_names_its_own_subject(self, record):
        # app.py dedupes by content, so identical sidecars would collapse and
        # leave one CSV unmarked.  Naming the subject keeps them distinct.
        text = _fenced("csv", f"a,b\n1,{PADDING}") + _fenced("csv", f"c,d\n2,{PADDING}")
        files = _files_by_name(extract_downloads_from_response(text, record))
        first = json.loads(files["download.csv.prov.json"].content)
        second = json.loads(files["download_2.csv.prov.json"].content)
        assert first["subject"] == "download.csv"
        assert second["subject"] == "download_2.csv"
        assert files["download.csv.prov.json"].content != files["download_2.csv.prov.json"].content

    def test_shared_record_marks_both_channels_identically(self, record):
        # app.py dedupes download elements by marked bytes, so one record per
        # turn must yield the same header — a per-call timestamp would not.
        body = f"SELECT * FROM orders; {PADDING}"
        part = type("ToolReturnPart", (), {})()
        part.content = body
        part.tool_name = "download"
        msg = type("Msg", (), {"parts": [part]})()

        from_block = extract_downloads_from_response(_fenced("sql", body), record)
        from_tool = extract_downloads_from_tool_results([msg], record)
        header = from_block[0].content.decode().splitlines()[0]
        assert header == from_tool[0].content.decode().splitlines()[0]


class TestMarkingInTheRenderedPage:
    def test_header_js_tags_the_document_as_ai_generated(self):
        text = (REPO_ROOT / "public" / "header.js").read_text(encoding="utf-8")
        assert 'flag.name = "ai-generated"' in text

    def test_header_js_tags_non_user_steps(self):
        text = (REPO_ROOT / "public" / "header.js").read_text(encoding="utf-8")
        assert 'el.dataset.aiGenerated = "true"' in text
        assert 'data-step-type") === "user_message"' in text

    def test_header_js_uses_the_iptc_vocabulary(self):
        text = (REPO_ROOT / "public" / "header.js").read_text(encoding="utf-8")
        assert "trainedAlgorithmicMedia" in text
