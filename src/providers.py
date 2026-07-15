"""LLM provider resolution for Pydantic AI."""

from openai import AsyncOpenAI
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider

from .settings import settings

# OpenRouter app attribution headers (https://openrouter.ai/docs/app-attribution).
# Other OpenAI-compatible providers ignore unknown headers.
_OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://github.com/ralforion/orionbelt-chat",
    "X-Title": "OrionBelt Chat",
}


# Human-readable labels shown in the UI dropdown
PROVIDER_LABELS = {
    "openrouter": "OpenRouter (cloud, 300+ models)",
    "mlx": "MLX local (Apple Silicon)",
    "ollama": "Ollama local",
    "anthropic": "Anthropic direct",
    "openai": "OpenAI direct",
}

# Curated model lists per provider shown in the UI
PROVIDER_MODELS = {
    "openrouter": [
        "anthropic/claude-sonnet-4-5",
        "anthropic/claude-opus-4-5",
        "anthropic/claude-haiku-4-5",
        "openai/gpt-4o",
        "openai/gpt-4o-mini",
        "google/gemini-2.5-pro",
        "google/gemini-2.5-flash",
        "deepseek/deepseek-r1",
        "meta-llama/llama-3.3-70b-instruct",
        "qwen/qwen-2.5-72b-instruct",
        "mistralai/mistral-large",
    ],
    "mlx": [
        "mlx-community/Qwen2.5-14B-Instruct-4bit",
        "mlx-community/Llama-3.3-70B-Instruct-4bit",
        "mlx-community/mistral-7b-instruct-v0.3-4bit",
        "mlx-community/gemma-3-12b-it-4bit",
    ],
    "ollama": [
        "qwen2.5:14b",
        "llama3.3:70b",
        "mistral:7b",
        "phi4:14b",
    ],
    "anthropic": [
        "claude-sonnet-4-6",
        "claude-opus-4-6",
        "claude-haiku-4-5-20251001",
    ],
    "openai": [
        "gpt-4o",
        "gpt-4o-mini",
        "o3-mini",
    ],
}


def models_for(provider: str) -> list[str]:
    """Return model list for the UI dropdown, including the env-configured default."""
    curated = list(PROVIDER_MODELS.get(provider, []))
    env_default = default_model_for(provider)
    if env_default and env_default not in curated:
        curated.insert(0, env_default)
    return curated


def resolve_model(provider: str, model: str):
    """
    Return a Pydantic AI model object for the given provider + model name.

    Args:
        provider: Provider name ("openrouter", "mlx", "ollama", "anthropic", "openai")
        model: Model identifier string

    Returns:
        Pydantic AI model instance configured for the provider

    Raises:
        ValueError: For unknown providers or missing credentials
    """
    match provider:
        case "openrouter":
            if not settings.openrouter_api_key:
                raise ValueError("OPENROUTER_API_KEY not set in environment")
            return OpenAIChatModel(
                model,
                provider=OpenAIProvider(
                    openai_client=AsyncOpenAI(
                        base_url="https://openrouter.ai/api/v1",
                        api_key=settings.openrouter_api_key,
                        default_headers=_OPENROUTER_HEADERS,
                    ),
                ),
            )

        case "mlx":
            # mlx-openai-server exposes an OpenAI-compatible endpoint
            return OpenAIChatModel(
                model,
                provider=OpenAIProvider(
                    base_url=settings.mlx_base_url,
                    api_key="mlx-local",  # ignored by local server
                ),
            )

        case "ollama":
            return OpenAIChatModel(
                model,
                provider=OpenAIProvider(
                    base_url=settings.ollama_base_url,
                    api_key="ollama",  # ignored by local server
                ),
            )

        case "anthropic":
            if not settings.anthropic_api_key:
                raise ValueError("ANTHROPIC_API_KEY not set in environment")
            return AnthropicModel(
                model,
                provider=AnthropicProvider(
                    api_key=settings.anthropic_api_key,
                ),
            )

        case "openai":
            if not settings.openai_api_key:
                raise ValueError("OPENAI_API_KEY not set in environment")
            return OpenAIChatModel(
                model,
                provider=OpenAIProvider(
                    api_key=settings.openai_api_key,
                ),
            )

        case _:
            raise ValueError(f"Unknown provider: {provider!r}")


def default_model_for(provider: str) -> str:
    """Return the default model string for a provider."""
    # Use global default_model override if set
    if settings.default_model:
        return settings.default_model

    defaults = {
        "openrouter": settings.openrouter_default_model,
        "mlx": settings.mlx_default_model,
        "ollama": settings.ollama_default_model,
        "anthropic": settings.anthropic_default_model,
        "openai": settings.openai_default_model,
    }
    return defaults.get(provider, "")
