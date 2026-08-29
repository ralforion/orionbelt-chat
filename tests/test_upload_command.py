"""Tests for the composer Upload button and the turn it drives."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chainlit.server import validate_file_mime_type
from chainlit.types import AskFileSpec

from orionbelt_chat import app
from orionbelt_chat.file_uploads import MAX_UPLOAD_MB, UPLOAD_ACCEPT, UploadedFile


@pytest.fixture
def composer():
    """Patch the Chainlit surface ``on_start`` touches, yielding the emitter."""
    context = MagicMock()
    context.emitter.set_commands = AsyncMock()
    with (
        patch.object(app.cl, "Message") as message_cls,
        patch.object(app.cl, "ChatSettings") as chat_settings,
        patch.object(app.cl, "user_session", MagicMock()),
        patch.object(app.cl, "context", context),
        patch.object(app, "build_chat_settings", MagicMock(return_value=[])),
        patch.object(app, "_init_agent", AsyncMock(return_value=True)),
    ):
        message_cls.return_value.send = AsyncMock()
        chat_settings.return_value.send = AsyncMock()
        yield context.emitter


class TestUploadCommand:
    def test_renders_as_a_composer_button(self):
        # `button` is what puts it next to the message input rather than
        # behind the slash-command picker.
        assert app.UPLOAD_COMMAND["button"] is True

    def test_does_not_stay_selected_after_use(self):
        # Persisting would re-open the file picker on every later message.
        assert app.UPLOAD_COMMAND["persistent"] is False
        assert app.UPLOAD_COMMAND["selected"] is False

    async def test_registered_on_chat_start(self, composer):
        await app.on_start()

        composer.set_commands.assert_awaited_once()
        commands = composer.set_commands.await_args.args[0]
        assert app.UPLOAD_COMMAND in commands

    async def test_registered_even_when_agent_fails_to_start(self, composer):
        # A dead MCP server or provider must not cost the user the button.
        with patch.object(app, "_init_agent", AsyncMock(return_value=False)):
            await app.on_start()

        composer.set_commands.assert_awaited_once()


class TestUploadAccept:
    """The picker's accept filter, checked against Chainlit's own validator.

    Chainlit requires the browser-reported content type to fnmatch the dict key
    *and* the filename to match an extension.  A concrete MIME key silently
    rejected every format the button exists for, so these assert the real
    server-side outcome rather than the shape of the config.
    """

    @staticmethod
    def _accepts(filename, content_type):
        spec = AskFileSpec(
            accept=UPLOAD_ACCEPT,
            max_size_mb=MAX_UPLOAD_MB,
            max_files=5,
            timeout=300,
            type="file",
            step_id="test",
        )
        upload = SimpleNamespace(filename=filename, content_type=content_type)
        try:
            validate_file_mime_type(upload, spec)
            return True
        except ValueError:
            return False

    @pytest.mark.parametrize(
        ("filename", "content_type"),
        [
            ("model.yaml", "text/yaml"),
            ("model.yaml", "application/x-yaml"),
            ("model.yml", ""),
            ("model.obml", None),
            ("model.obsl", None),
            ("model.json", "application/json"),
            ("model.jsonld", "application/ld+json"),
            ("schema.ttl", ""),
            ("schema.ttl", "text/turtle"),
            ("schema.turtle", None),
            ("schema.n3", None),
            ("schema.nt", None),
            ("onto.rdf", "application/rdf+xml"),
            ("onto.owl", "application/octet-stream"),
        ],
    )
    def test_intended_formats_are_accepted(self, filename, content_type):
        assert self._accepts(filename, content_type) is True

    @pytest.mark.parametrize(
        ("filename", "content_type"),
        [
            ("logo.png", "image/png"),
            ("report.pdf", "application/pdf"),
            ("data.csv", "text/csv"),
            ("archive.zip", "application/zip"),
        ],
    )
    def test_unrelated_formats_are_rejected(self, filename, content_type):
        assert self._accepts(filename, content_type) is False

    def test_every_advertised_extension_round_trips(self):
        # Nothing may sit in the list that the validator would then reject.
        for extension in next(iter(UPLOAD_ACCEPT.values())):
            assert self._accepts(f"model{extension}", None) is True, extension


class TestCollectUploads:
    @pytest.fixture
    def picker(self):
        """Patch the collaborators of ``_collect_uploads``, yielding the picker."""
        with (
            patch.object(app, "_prompt_for_upload", AsyncMock()) as prompt_for_upload,
            patch.object(app, "register_uploads", MagicMock(return_value=[])),
            patch.object(app, "drain_pending_uploads", MagicMock(return_value=[])),
            patch.object(app, "_announce_uploads", AsyncMock()),
        ):
            yield prompt_for_upload

    @staticmethod
    def _message(command=None, elements=None):
        return SimpleNamespace(content="load this", elements=elements or [], command=command)

    async def test_command_opens_the_file_picker(self, picker):
        await app._collect_uploads(self._message(command=app.UPLOAD_COMMAND["id"]))
        picker.assert_awaited_once()

    async def test_plain_message_does_not(self, picker):
        await app._collect_uploads(self._message())
        picker.assert_not_awaited()

    async def test_unrelated_command_does_not(self, picker):
        await app._collect_uploads(self._message(command="SomethingElse"))
        picker.assert_not_awaited()

    async def test_returns_message_unchanged_without_uploads(self, picker):
        assert await app._collect_uploads(self._message()) == "load this"

    async def test_appends_the_notice_for_queued_uploads(self, picker):
        upload = UploadedFile(
            handle_name="model.yaml",
            original_name="model.yaml",
            kind="OBSL/OBML semantic model (YAML)",
            content="dataObjects: []\n",
        )
        with (
            patch.object(app, "drain_pending_uploads", MagicMock(return_value=[upload])),
            patch.object(app, "_announce_uploads", AsyncMock()) as announce,
        ):
            prompt = await app._collect_uploads(self._message())

        assert prompt.startswith("load this")
        assert "@upload:model.yaml" in prompt
        announce.assert_awaited_once_with([upload])

    async def test_picker_runs_before_the_registry_is_drained(self, picker):
        """A file picked this turn must make it into this turn's prompt."""
        order = []
        picker.side_effect = lambda: order.append("picked")
        with patch.object(
            app,
            "drain_pending_uploads",
            MagicMock(side_effect=lambda: order.append("drained") or []),
        ):
            await app._collect_uploads(self._message(command=app.UPLOAD_COMMAND["id"]))

        assert order == ["picked", "drained"]
