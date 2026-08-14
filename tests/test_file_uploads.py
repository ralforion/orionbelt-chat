"""Tests for src.file_uploads."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic_ai import ModelRetry

import src.file_uploads as file_uploads
from src.file_uploads import (
    HANDLE_PREFIX,
    INLINE_THRESHOLD,
    JSON_MODIFIER,
    MAX_UPLOAD_BYTES,
    PREVIEW_LINES,
    UploadedFile,
    augment_message,
    build_upload_notice,
    detect_kind,
    drain_pending_uploads,
    process_tool_call,
    register_uploads,
    session_uploads,
    substitute_handles,
)

OBML_YAML = """version: "1.0"
dataObjects:
  - name: orders
dimensions:
  - name: order_date
measures:
  - name: revenue
"""

TURTLE = """@prefix ex: <http://example.org/> .
ex:Order a owl:Class .
"""


@pytest.fixture
def mock_session():
    """Stand in for ``cl.user_session`` with a plain dict."""
    store: dict = {}
    session = SimpleNamespace(get=store.get, set=store.__setitem__)
    with patch.object(file_uploads.cl, "user_session", session):
        yield store


def _upload(name="model.yaml", content=OBML_YAML, kind="OBSL/OBML semantic model (YAML)"):
    return UploadedFile(handle_name=name, original_name=name, kind=kind, content=content)


def _registry(*uploads):
    return {u.handle_name: u for u in uploads}


class TestDetectKind:
    def test_obml_yaml_by_content(self):
        assert detect_kind("export.yaml", OBML_YAML) == "OBSL/OBML semantic model (YAML)"

    def test_plain_yaml_without_obml_keys(self):
        assert detect_kind("config.yaml", "host: localhost\nport: 5432\n") == "YAML document"

    def test_osi_yaml_wins_over_obml(self):
        content = "semantic_model:\n  name: sales\ndimensions: []\nmeasures: []\n"
        assert detect_kind("sales.yaml", content) == "OSI semantic model (YAML)"

    def test_turtle_by_extension(self):
        assert detect_kind("schema.ttl", TURTLE) == "RDF ontology (Turtle)"

    def test_turtle_by_content_without_extension(self):
        assert detect_kind("schema", TURTLE) == "RDF ontology (Turtle)"

    def test_rdfxml_by_content_without_extension(self):
        assert detect_kind("onto", '<?xml version="1.0"?><rdf:RDF/>') == "RDF ontology (RDF/XML)"

    def test_obml_json(self):
        content = '{"dataObjects": [], "dimensions": [], "measures": []}'
        assert detect_kind("model.json", content) == "OBSL/OBML semantic model (JSON)"

    def test_plain_json(self):
        assert detect_kind("data.json", '{"rows": []}') == "JSON document"

    def test_unknown_extension_is_text(self):
        assert detect_kind("notes.log", "some log line") == "text file"


class TestRegisterUploads:
    def test_reads_content_from_path(self, tmp_path, mock_session):
        path = tmp_path / "schema.ttl"
        path.write_text(TURTLE, encoding="utf-8")
        element = SimpleNamespace(name="schema.ttl", path=str(path), content=None)

        uploads = register_uploads([element])

        assert len(uploads) == 1
        assert uploads[0].handle == f"{HANDLE_PREFIX}schema.ttl"
        assert uploads[0].content == TURTLE
        assert uploads[0].kind == "RDF ontology (Turtle)"

    def test_reads_inline_bytes(self, mock_session):
        element = SimpleNamespace(name="model.yaml", path=None, content=OBML_YAML.encode())
        uploads = register_uploads([element])
        assert uploads[0].content == OBML_YAML

    def test_sanitizes_name_into_handle(self, mock_session):
        element = SimpleNamespace(name="my model (v2).yaml", path=None, content=b"a: 1\n")
        uploads = register_uploads([element])
        assert uploads[0].handle_name == "my_model_v2_.yaml"
        assert uploads[0].original_name == "my model (v2).yaml"

    def test_skips_binary(self, mock_session):
        element = SimpleNamespace(name="logo.png", path=None, content=b"\x89PNG\x00\xff\xfe")
        assert register_uploads([element]) == []

    def test_skips_oversized(self, mock_session):
        element = SimpleNamespace(
            name="huge.yaml", path=None, content=b"x" * (MAX_UPLOAD_BYTES + 1)
        )
        assert register_uploads([element]) == []

    def test_skips_oversized_str_content(self, mock_session):
        # str content took an early return that skipped the size check.
        element = SimpleNamespace(name="huge.yaml", path=None, content="x" * (MAX_UPLOAD_BYTES + 1))
        assert register_uploads([element]) == []

    def test_oversized_str_measured_in_bytes_not_characters(self, mock_session):
        # Just under the limit in characters, over it once encoded.
        element = SimpleNamespace(name="huge.yaml", path=None, content="é" * MAX_UPLOAD_BYTES)
        assert register_uploads([element]) == []

    def test_accepts_str_content_within_the_limit(self, mock_session):
        element = SimpleNamespace(name="model.yaml", path=None, content=OBML_YAML)
        assert register_uploads([element])[0].content == OBML_YAML

    def test_skips_empty(self, mock_session):
        element = SimpleNamespace(name="blank.yaml", path=None, content=b"   \n")
        assert register_uploads([element]) == []

    def test_skips_oversized_on_disk_without_reading_it(self, tmp_path, mock_session):
        path = tmp_path / "huge.ttl"
        path.write_bytes(b"x" * (MAX_UPLOAD_BYTES + 1))
        element = SimpleNamespace(name="huge.ttl", path=str(path), content=None)
        assert register_uploads([element]) == []

    def test_skips_unreadable_path(self, tmp_path, mock_session):
        element = SimpleNamespace(name="gone.ttl", path=str(tmp_path / "missing.ttl"), content=None)
        assert register_uploads([element]) == []

    def test_none_elements(self, mock_session):
        assert register_uploads(None) == []

    def test_reupload_replaces_previous_version(self, mock_session):
        register_uploads([SimpleNamespace(name="m.yaml", path=None, content=b"version: 1\n")])
        register_uploads([SimpleNamespace(name="m.yaml", path=None, content=b"version: 2\n")])
        assert mock_session["uploaded_files"]["m.yaml"].content == "version: 2\n"

    def test_registry_accumulates_across_messages(self, mock_session):
        register_uploads([SimpleNamespace(name="a.yaml", path=None, content=b"a: 1\n")])
        register_uploads([SimpleNamespace(name="b.ttl", path=None, content=TURTLE.encode())])
        assert set(mock_session["uploaded_files"]) == {"a.yaml", "b.ttl"}


class TestHandleCollisions:
    def test_distinct_names_sanitizing_alike_get_distinct_handles(self, mock_session):
        register_uploads([SimpleNamespace(name="my model.yaml", path=None, content=b"a: 1\n")])
        register_uploads([SimpleNamespace(name="my@model.yaml", path=None, content=b"b: 2\n")])

        handles = set(mock_session["uploaded_files"])
        assert handles == {"my_model.yaml", "my_model_2.yaml"}

    def test_each_collided_handle_resolves_to_its_own_file(self, mock_session):
        register_uploads([SimpleNamespace(name="my model.yaml", path=None, content=b"a: 1\n")])
        register_uploads([SimpleNamespace(name="my@model.yaml", path=None, content=b"b: 2\n")])

        registry = session_uploads()
        assert substitute_handles("@upload:my_model.yaml", registry) == "a: 1\n"
        assert substitute_handles("@upload:my_model_2.yaml", registry) == "b: 2\n"

    def test_notice_advertises_the_allocated_handle(self, mock_session):
        register_uploads([SimpleNamespace(name="my model.yaml", path=None, content=b"a: 1\n")])
        added = register_uploads(
            [SimpleNamespace(name="my@model.yaml", path=None, content=b"b: 2\n")]
        )

        assert added[0].handle == "@upload:my_model_2.yaml"
        assert "@upload:my_model_2.yaml" in build_upload_notice(added)

    def test_same_name_still_reuses_its_handle(self, mock_session):
        register_uploads([SimpleNamespace(name="my model.yaml", path=None, content=b"a: 1\n")])
        added = register_uploads(
            [SimpleNamespace(name="my model.yaml", path=None, content=b"a: 2\n")]
        )

        assert added[0].handle_name == "my_model.yaml"
        assert list(mock_session["uploaded_files"]) == ["my_model.yaml"]

    def test_three_way_collision(self, mock_session):
        for name in ("a b.ttl", "a@b.ttl", "a#b.ttl"):
            register_uploads([SimpleNamespace(name=name, path=None, content=f"{name}\n".encode())])

        assert set(mock_session["uploaded_files"]) == {"a_b.ttl", "a_b_2.ttl", "a_b_3.ttl"}

    def test_collision_within_a_single_batch(self, mock_session):
        register_uploads(
            [
                SimpleNamespace(name="my model.yaml", path=None, content=b"a: 1\n"),
                SimpleNamespace(name="my@model.yaml", path=None, content=b"b: 2\n"),
            ]
        )
        assert set(mock_session["uploaded_files"]) == {"my_model.yaml", "my_model_2.yaml"}

    def test_same_basename_twice_in_one_batch_stays_reachable(self, mock_session):
        # A multi-file pick can carry two files with the same basename from
        # different folders; both must keep their own handle.
        added = register_uploads(
            [
                SimpleNamespace(name="m.yaml", path=None, content=b"first: 1\n"),
                SimpleNamespace(name="m.yaml", path=None, content=b"second: 2\n"),
            ]
        )

        assert [u.handle_name for u in added] == ["m.yaml", "m_2.yaml"]
        registry = session_uploads()
        assert substitute_handles("@upload:m.yaml", registry) == "first: 1\n"
        assert substitute_handles("@upload:m_2.yaml", registry) == "second: 2\n"

    def test_same_basename_in_one_batch_gives_the_notice_distinct_handles(self, mock_session):
        added = register_uploads(
            [
                SimpleNamespace(name="m.yaml", path=None, content=b"first: 1\n"),
                SimpleNamespace(name="m.yaml", path=None, content=b"second: 2\n"),
            ]
        )
        notice = build_upload_notice(added)

        assert len({u.handle for u in added}) == 2
        assert "@upload:m.yaml" in notice
        assert "@upload:m_2.yaml" in notice

    def test_later_batch_with_the_same_name_still_replaces(self, mock_session):
        # Across batches the same filename means a new version, not a new file.
        register_uploads([SimpleNamespace(name="m.yaml", path=None, content=b"v: 1\n")])
        added = register_uploads([SimpleNamespace(name="m.yaml", path=None, content=b"v: 2\n")])

        assert [u.handle_name for u in added] == ["m.yaml"]
        assert list(session_uploads()) == ["m.yaml"]
        assert substitute_handles("@upload:m.yaml", session_uploads()) == "v: 2\n"

    def test_suffixed_handle_is_reused_on_reupload(self, mock_session):
        register_uploads([SimpleNamespace(name="my model.yaml", path=None, content=b"a: 1\n")])
        register_uploads([SimpleNamespace(name="my@model.yaml", path=None, content=b"b: 1\n")])
        added = register_uploads(
            [SimpleNamespace(name="my@model.yaml", path=None, content=b"b: 2\n")]
        )

        assert added[0].handle_name == "my_model_2.yaml"
        assert set(session_uploads()) == {"my_model.yaml", "my_model_2.yaml"}

    def test_extensionless_names_collide_safely(self, mock_session):
        register_uploads([SimpleNamespace(name="my model", path=None, content=b"a: 1\n")])
        register_uploads([SimpleNamespace(name="my@model", path=None, content=b"b: 2\n")])

        assert set(mock_session["uploaded_files"]) == {"my_model", "my_model_2"}


class TestJsonModifier:
    def test_converts_yaml_to_json(self):
        registry = _registry(_upload())
        result = substitute_handles(f"@upload:model.yaml{JSON_MODIFIER}", registry)
        assert json.loads(result)["dataObjects"] == [{"name": "orders"}]

    def test_plain_handle_stays_verbatim_yaml(self):
        registry = _registry(_upload())
        assert substitute_handles("@upload:model.yaml", registry) == OBML_YAML

    def test_json_source_passes_through_json(self):
        upload = _upload("model.json", '{"dataObjects": [{"name": "orders"}]}')
        result = substitute_handles(f"@upload:model.json{JSON_MODIFIER}", _registry(upload))
        assert json.loads(result) == {"dataObjects": [{"name": "orders"}]}

    def test_result_is_loadable_by_the_server_side_json_loads(self):
        # `load_model(model=<str>)` runs json.loads on the argument — that is
        # the call this modifier exists to make work for a YAML upload.
        registry = _registry(_upload())
        args = {"model": f"@upload:model.yaml{JSON_MODIFIER}", "dedup": True}
        substituted = substitute_handles(args, registry)
        assert isinstance(json.loads(substituted["model"]), dict)

    def test_unquoted_date_becomes_an_iso_string(self):
        # PyYAML resolves `since: 2026-01-01` to datetime.date, which json
        # cannot serialise — this used to raise a bare TypeError.
        upload = _upload("model.yaml", "dataObjects: []\nsince: 2026-01-01\n")
        result = substitute_handles(f"@upload:model.yaml{JSON_MODIFIER}", _registry(upload))
        assert json.loads(result)["since"] == "2026-01-01"

    def test_timestamp_becomes_an_iso_string(self):
        upload = _upload("model.yaml", "dataObjects: []\nat: 2026-01-01T10:00:00Z\n")
        result = substitute_handles(f"@upload:model.yaml{JSON_MODIFIER}", _registry(upload))
        assert json.loads(result)["at"].startswith("2026-01-01T10:00:00")

    def test_yaml_set_becomes_an_array(self):
        upload = _upload("model.yaml", "dataObjects: []\ntags: !!set {a: null}\n")
        result = substitute_handles(f"@upload:model.yaml{JSON_MODIFIER}", _registry(upload))
        assert json.loads(result)["tags"] == ["a"]

    def test_binary_becomes_base64(self):
        upload = _upload("model.yaml", "dataObjects: []\nblob: !!binary aGk=\n")
        result = substitute_handles(f"@upload:model.yaml{JSON_MODIFIER}", _registry(upload))
        assert json.loads(result)["blob"] == "aGk="

    def test_quoted_date_is_untouched(self):
        upload = _upload("model.yaml", 'dataObjects: []\nsince: "2026-01-01"\n')
        result = substitute_handles(f"@upload:model.yaml{JSON_MODIFIER}", _registry(upload))
        assert json.loads(result)["since"] == "2026-01-01"

    def test_result_is_always_valid_json(self):
        upload = _upload(
            "model.yaml",
            "dataObjects: []\nsince: 2026-01-01\nat: 2026-01-01T10:00:00Z\ntags: !!set {a: null}\n",
        )
        result = substitute_handles(f"@upload:model.yaml{JSON_MODIFIER}", _registry(upload))
        assert isinstance(json.loads(result), dict)

    def test_recursive_alias_raises_model_retry(self):
        # `a: &a [*a]` is valid YAML and parses fine, but json.dumps raises
        # ValueError rather than TypeError on the resulting cycle.
        upload = _upload("model.yaml", "a: &a [*a]\n")
        with pytest.raises(ModelRetry) as exc:
            substitute_handles(f"@upload:model.yaml{JSON_MODIFIER}", _registry(upload))
        assert "model.yaml" in str(exc.value)

    def test_malformed_yaml_raises_model_retry(self):
        upload = _upload("broken.yaml", "a:\n  - b\n - c\n")
        with pytest.raises(ModelRetry) as exc:
            substitute_handles(f"@upload:broken.yaml{JSON_MODIFIER}", _registry(upload))
        assert "broken.yaml" in str(exc.value)

    def test_scalar_document_raises_model_retry(self):
        upload = _upload("notes.txt", "just a sentence")
        with pytest.raises(ModelRetry) as exc:
            substitute_handles(f"@upload:notes.txt{JSON_MODIFIER}", _registry(upload))
        assert "JSON object or array" in str(exc.value)

    def test_unknown_handle_with_modifier_still_reports_missing(self):
        registry = _registry(_upload())
        with pytest.raises(ModelRetry) as exc:
            substitute_handles(f"@upload:other.yaml{JSON_MODIFIER}", registry)
        assert "No uploaded file matches" in str(exc.value)

    def test_notice_documents_the_modifier(self):
        assert JSON_MODIFIER in build_upload_notice([_upload()])


class TestPendingQueue:
    def test_drains_registered_uploads(self, mock_session):
        register_uploads([SimpleNamespace(name="m.yaml", path=None, content=OBML_YAML.encode())])
        pending = drain_pending_uploads()
        assert [u.handle_name for u in pending] == ["m.yaml"]

    def test_drain_clears_the_queue(self, mock_session):
        register_uploads([SimpleNamespace(name="m.yaml", path=None, content=OBML_YAML.encode())])
        drain_pending_uploads()
        assert drain_pending_uploads() == []

    def test_accumulates_until_drained(self, mock_session):
        register_uploads([SimpleNamespace(name="a.yaml", path=None, content=b"a: 1\n")])
        register_uploads([SimpleNamespace(name="b.ttl", path=None, content=TURTLE.encode())])
        assert [u.handle_name for u in drain_pending_uploads()] == ["a.yaml", "b.ttl"]

    def test_reupload_supersedes_queued_entry(self, mock_session):
        register_uploads([SimpleNamespace(name="m.yaml", path=None, content=b"version: 1\n")])
        register_uploads([SimpleNamespace(name="m.yaml", path=None, content=b"version: 2\n")])
        pending = drain_pending_uploads()
        assert len(pending) == 1
        assert pending[0].content == "version: 2\n"

    def test_handle_stays_usable_after_draining(self, mock_session):
        register_uploads([SimpleNamespace(name="m.yaml", path=None, content=OBML_YAML.encode())])
        drain_pending_uploads()
        assert substitute_handles("@upload:m.yaml", session_uploads()) == OBML_YAML


class TestUploadedFile:
    def test_size_label_bytes(self):
        assert _upload(content="abc").size_label == "3 B"

    def test_size_label_kilobytes(self):
        assert _upload(content="x" * 2048).size_label == "2.0 KB"

    def test_size_label_megabytes(self):
        assert _upload(content="x" * (2 * 1024 * 1024)).size_label == "2.0 MB"

    def test_preview_truncates_to_line_limit(self):
        upload = _upload(content="\n".join(f"line {i}" for i in range(100)))
        preview = upload.preview()
        assert preview.count("\n") <= PREVIEW_LINES
        assert preview.endswith("…")

    def test_short_file_is_inlinable(self):
        assert _upload(content=OBML_YAML).is_inlinable is True

    def test_large_file_is_not_inlinable(self):
        assert _upload(content="x" * (INLINE_THRESHOLD + 1)).is_inlinable is False


class TestBuildUploadNotice:
    def test_empty(self):
        assert build_upload_notice([]) == ""

    def test_small_file_inlined_in_full(self):
        notice = build_upload_notice([_upload()])
        assert "@upload:model.yaml" in notice
        assert "Full content" in notice
        assert "dataObjects:" in notice

    def test_large_file_shows_preview_only(self):
        big = "\n".join(f"key_{i}: value" for i in range(500))
        notice = build_upload_notice([_upload(content=big)])
        assert "Preview" in notice
        assert "Full content" not in notice
        assert len(notice) < len(big)

    def test_states_the_handle_convention(self):
        notice = build_upload_notice([_upload()])
        assert "verbatim" in notice

    def test_augment_message_appends_notice(self):
        result = augment_message("load this", [_upload()])
        assert result.startswith("load this")
        assert "@upload:model.yaml" in result

    def test_augment_message_without_uploads_is_unchanged(self):
        assert augment_message("hello", []) == "hello"

    def test_augment_message_with_empty_text(self):
        assert augment_message("  ", [_upload()]).startswith("## Uploaded files")


class TestSubstituteHandles:
    def test_whole_argument(self):
        registry = _registry(_upload())
        args = {"osi_yaml": "@upload:model.yaml"}
        assert substitute_handles(args, registry) == {"osi_yaml": OBML_YAML}

    def test_embedded_in_larger_string(self):
        registry = _registry(_upload())
        result = substitute_handles("before @upload:model.yaml after", registry)
        assert result == f"before {OBML_YAML} after"

    def test_nested_structures(self):
        registry = _registry(_upload())
        args = {"queries": [{"model": "@upload:model.yaml"}], "execute": True}
        assert substitute_handles(args, registry)["queries"][0]["model"] == OBML_YAML

    def test_leaves_other_values_untouched(self):
        registry = _registry(_upload())
        args = {"dedup": True, "limit": 5, "name": "orders", "extra": None}
        assert substitute_handles(args, registry) == args

    def test_case_insensitive_match(self):
        registry = _registry(_upload())
        assert substitute_handles("@upload:MODEL.YAML", registry) == OBML_YAML

    def test_stem_match_when_extension_dropped(self):
        registry = _registry(_upload())
        assert substitute_handles("@upload:model", registry) == OBML_YAML

    def test_ambiguous_stem_is_rejected(self):
        registry = _registry(_upload("model.yaml"), _upload("model.json", content="{}"))
        with pytest.raises(ModelRetry):
            substitute_handles("@upload:model", registry)

    def test_unknown_handle_raises_model_retry_listing_available(self):
        registry = _registry(_upload())
        with pytest.raises(ModelRetry) as exc:
            substitute_handles("@upload:other.yaml", registry)
        assert "@upload:model.yaml" in str(exc.value)

    def test_multiple_handles_in_one_call(self):
        registry = _registry(_upload(), _upload("schema.ttl", TURTLE, "RDF ontology (Turtle)"))
        args = {"a": "@upload:model.yaml", "b": "@upload:schema.ttl"}
        assert substitute_handles(args, registry) == {"a": OBML_YAML, "b": TURTLE}


class TestProcessToolCall:
    async def test_substitutes_before_calling(self, mock_session):
        register_uploads(
            [SimpleNamespace(name="model.yaml", path=None, content=OBML_YAML.encode())]
        )
        seen = {}

        async def call_tool(name, args):
            seen["name"] = name
            seen["args"] = args
            return "ok"

        result = await process_tool_call(
            None, call_tool, "load_model", {"osi_yaml": "@upload:model.yaml"}
        )

        assert result == "ok"
        assert seen["name"] == "load_model"
        assert seen["args"] == {"osi_yaml": OBML_YAML}

    async def test_passes_through_when_no_uploads(self, mock_session):
        async def call_tool(name, args):
            return args

        args = {"model_id": "m1"}
        assert await process_tool_call(None, call_tool, "describe_model", args) == args
