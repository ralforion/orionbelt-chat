"""Tests for orionbelt_chat.providers."""

import os
from unittest.mock import patch

import pytest

from orionbelt_chat.providers import (
    PROVIDER_LABELS,
    PROVIDER_MODELS,
    default_model_for,
    resolve_model,
)


class TestProviderConstants:
    def test_all_providers_have_labels(self):
        expected = {"openrouter", "mlx", "ollama", "anthropic", "openai"}
        assert set(PROVIDER_LABELS.keys()) == expected

    def test_all_providers_have_models(self):
        assert set(PROVIDER_MODELS.keys()) == set(PROVIDER_LABELS.keys())

    def test_each_provider_has_at_least_one_model(self):
        for provider, models in PROVIDER_MODELS.items():
            assert len(models) > 0, f"{provider} has no models"


class TestDefaultModelFor:
    def test_openrouter_default(self):
        result = default_model_for("openrouter")
        assert result != ""

    def test_mlx_default(self):
        result = default_model_for("mlx")
        assert "mlx-community" in result

    def test_ollama_default(self):
        result = default_model_for("ollama")
        assert result != ""

    def test_anthropic_default(self):
        result = default_model_for("anthropic")
        assert "claude" in result

    def test_openai_default(self):
        result = default_model_for("openai")
        assert "gpt" in result

    def test_unknown_provider_returns_empty(self):
        assert default_model_for("nonexistent") == ""

    def test_global_override(self):
        with patch.dict(os.environ, {"DEFAULT_MODEL": "my-custom-model"}):
            from orionbelt_chat.settings import Settings

            with patch("orionbelt_chat.providers.settings", Settings()):
                assert default_model_for("openrouter") == "my-custom-model"


class TestResolveModel:
    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            resolve_model("nonexistent", "some-model")

    def test_openrouter_missing_key_raises(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}, clear=False):
            from orionbelt_chat.settings import Settings

            with patch("orionbelt_chat.providers.settings", Settings(openrouter_api_key="")):
                with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
                    resolve_model("openrouter", "anthropic/claude-sonnet-4-5")

    def test_anthropic_missing_key_raises(self):
        with patch("orionbelt_chat.providers.settings") as mock_settings:
            mock_settings.anthropic_api_key = ""
            with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
                resolve_model("anthropic", "claude-sonnet-4-6")

    def test_openai_missing_key_raises(self):
        with patch("orionbelt_chat.providers.settings") as mock_settings:
            mock_settings.openai_api_key = ""
            with pytest.raises(ValueError, match="OPENAI_API_KEY"):
                resolve_model("openai", "gpt-4o")

    def test_mlx_returns_model(self):
        model = resolve_model("mlx", "mlx-community/Qwen2.5-14B-Instruct-4bit")
        assert model is not None

    def test_ollama_returns_model(self):
        model = resolve_model("ollama", "qwen2.5:14b")
        assert model is not None
