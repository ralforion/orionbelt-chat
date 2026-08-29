"""Tests for orionbelt_chat.settings."""

import os
from unittest.mock import patch

import pytest

from orionbelt_chat.settings import Settings

# All env var names that Pydantic Settings would read for our Settings class.
_SETTINGS_ENV_KEYS = [f.upper() for f in Settings.model_fields]


@pytest.fixture()
def clean_env(monkeypatch):
    """Remove all Settings-related env vars so only code defaults apply."""
    for key in _SETTINGS_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _clean_settings(**overrides) -> Settings:
    """Create Settings without loading .env, using only code defaults + overrides."""
    return Settings(_env_file=None, **overrides)


class TestSettingsDefaults:
    def test_default_provider(self, clean_env):
        s = _clean_settings()
        assert s.default_provider == "openrouter"

    def test_default_model_empty(self, clean_env):
        s = _clean_settings()
        assert s.default_model == ""

    def test_default_openrouter_model(self, clean_env):
        s = _clean_settings()
        assert "claude" in s.openrouter_default_model

    def test_default_mlx_base_url(self, clean_env):
        s = _clean_settings()
        assert s.mlx_base_url == "http://localhost:8000/v1"

    def test_default_ollama_base_url(self, clean_env):
        s = _clean_settings()
        assert s.ollama_base_url == "http://localhost:11434/v1"

    def test_api_keys_default_empty(self, clean_env):
        s = _clean_settings()
        assert s.openrouter_api_key == ""
        assert s.anthropic_api_key == ""
        assert s.openai_api_key == ""

    def test_mcp_server_dirs_default_empty(self, clean_env):
        s = _clean_settings()
        assert s.analytics_server_dir == ""
        assert s.semantic_layer_server_dir == ""


class TestSettingsEnvOverride:
    def test_override_default_provider(self):
        with patch.dict(os.environ, {"DEFAULT_PROVIDER": "ollama"}):
            s = _clean_settings()
            assert s.default_provider == "ollama"

    def test_override_api_key(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test-123"}):
            s = _clean_settings()
            assert s.openrouter_api_key == "sk-test-123"

    def test_override_mcp_server_dir(self):
        with patch.dict(os.environ, {"ANALYTICS_SERVER_DIR": "/tmp/analytics"}):
            s = _clean_settings()
            assert s.analytics_server_dir == "/tmp/analytics"
