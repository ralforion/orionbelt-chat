"""Tests for the EU AI Act Art. 50(1) transparency disclosure.

Art. 50(1) requires that a natural person interacting with an AI system is
informed of that fact at the time of the first interaction. These tests pin
the disclosure to the places a user actually sees it, so a UI refactor cannot
drop it silently.
"""

import tomllib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app

REPO_ROOT = Path(app.__file__).resolve().parent


def _sent_messages(message_cls) -> list[str]:
    """Message bodies passed to the patched ``cl.Message``, in send order."""
    return [call.kwargs.get("content", "") for call in message_cls.call_args_list]


class TestDisclosureText:
    def test_states_it_is_an_ai(self):
        assert "AI assistant" in app.AI_DISCLOSURE

    def test_warns_responses_may_be_wrong(self):
        assert "may contain errors" in app.AI_DISCLOSURE

    def test_is_a_single_plain_paragraph(self):
        # Sent as a Chainlit message body — no markdown scaffolding to break.
        assert "\n" not in app.AI_DISCLOSURE
        assert app.AI_DISCLOSURE == app.AI_DISCLOSURE.strip()


class TestDisclosureInUi:
    def test_welcome_screen_carries_it(self):
        text = (REPO_ROOT / "chainlit.md").read_text(encoding="utf-8")
        assert "You are interacting with an AI assistant" in text

    def test_chainlit_config_name_marks_it_as_ai(self):
        config = tomllib.loads(
            (REPO_ROOT / ".chainlit" / "config.toml").read_text(encoding="utf-8")
        )
        assert "AI Assistant" in config["UI"]["name"]

    def test_chainlit_config_description_marks_it_as_ai(self):
        config = tomllib.loads(
            (REPO_ROOT / ".chainlit" / "config.toml").read_text(encoding="utf-8")
        )
        assert "AI" in config["UI"]["description"]

    def test_header_badge_marks_it_as_ai(self):
        text = (REPO_ROOT / "public" / "header.js").read_text(encoding="utf-8")
        assert 'var APP_NAME = "Chat – AI Assistant";' in text


@pytest.fixture
def chainlit_session():
    """Patch the Chainlit surface ``on_start`` touches, yielding cl.Message."""
    with (
        patch.object(app.cl, "Message") as message_cls,
        patch.object(app.cl, "ChatSettings") as chat_settings,
        patch.object(app.cl, "user_session", MagicMock()),
        patch.object(app, "build_chat_settings", MagicMock(return_value=[])),
    ):
        message_cls.return_value.send = AsyncMock()
        chat_settings.return_value.send = AsyncMock()
        yield message_cls


class TestDisclosureOnFirstInteraction:
    async def test_sent_when_agent_starts_cleanly(self, chainlit_session):
        with patch.object(app, "_init_agent", AsyncMock(return_value=True)):
            await app.on_start()

        assert app.AI_DISCLOSURE in _sent_messages(chainlit_session)

    async def test_sent_even_when_agent_fails_to_start(self, chainlit_session):
        # No ready message follows a failed init, so the disclosure must not
        # be attached to it — a broken backend cannot suppress Art. 50(1).
        with patch.object(app, "_init_agent", AsyncMock(return_value=False)):
            await app.on_start()

        assert app.AI_DISCLOSURE in _sent_messages(chainlit_session)

    async def test_precedes_the_ready_message(self, chainlit_session):
        with patch.object(app, "_init_agent", AsyncMock(return_value=True)):
            await app.on_start()

        sent = _sent_messages(chainlit_session)
        ready = next(i for i, body in enumerate(sent) if "ready" in body)
        assert sent.index(app.AI_DISCLOSURE) < ready
