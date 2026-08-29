"""Tests for orionbelt_chat.prompts system prompt loading."""

from pathlib import Path

from orionbelt_chat import prompts
from orionbelt_chat.prompts import (
    DEFAULT_SYSTEM_PROMPT_FILE,
    FALLBACK_SYSTEM_PROMPT,
    load_system_prompt,
)


class TestDefaultPath:
    def test_default_points_into_the_package(self):
        # system_prompt.md ships inside the package so an installed wheel
        # carries a working prompt.
        assert DEFAULT_SYSTEM_PROMPT_FILE.name == "system_prompt.md"
        assert DEFAULT_SYSTEM_PROMPT_FILE.parent == Path(prompts.__file__).resolve().parent

    def test_default_file_exists_in_repo(self):
        assert DEFAULT_SYSTEM_PROMPT_FILE.is_file(), (
            f"Expected {DEFAULT_SYSTEM_PROMPT_FILE} to ship with the package"
        )


class TestLoadSystemPrompt:
    def test_loads_shipped_default(self):
        # The shipped file mentions the OrionBelt Analytics Assistant.
        text = load_system_prompt()
        assert "OrionBelt Analytics Assistant" in text
        assert text == text.strip()  # trailing whitespace stripped

    def test_honors_settings_override(self, tmp_path, monkeypatch):
        custom = tmp_path / "custom_prompt.md"
        custom.write_text("You are a test agent.\n", encoding="utf-8")

        monkeypatch.setattr(prompts.settings, "system_prompt_file", str(custom))
        assert load_system_prompt() == "You are a test agent."

    def test_expands_user_home(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        prompt_file = fake_home / "my_prompt.md"
        prompt_file.write_text("Home prompt.", encoding="utf-8")

        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setattr(prompts.settings, "system_prompt_file", "~/my_prompt.md")
        assert load_system_prompt() == "Home prompt."

    def test_missing_file_returns_fallback(self, tmp_path, monkeypatch):
        missing = tmp_path / "does_not_exist.md"
        monkeypatch.setattr(prompts.settings, "system_prompt_file", str(missing))
        assert load_system_prompt() == FALLBACK_SYSTEM_PROMPT

    def test_empty_file_returns_fallback(self, tmp_path, monkeypatch):
        empty = tmp_path / "empty.md"
        empty.write_text("   \n\n", encoding="utf-8")
        monkeypatch.setattr(prompts.settings, "system_prompt_file", str(empty))
        assert load_system_prompt() == FALLBACK_SYSTEM_PROMPT

    def test_os_error_returns_fallback(self, tmp_path, monkeypatch):
        # Point at a directory — read_text will raise OSError (IsADirectoryError).
        monkeypatch.setattr(prompts.settings, "system_prompt_file", str(tmp_path))
        assert load_system_prompt() == FALLBACK_SYSTEM_PROMPT
