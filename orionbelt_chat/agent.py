"""Pydantic AI agent factory for OrionBelt Chat."""

from pydantic_ai import Agent

from .prompts import load_system_prompt
from .providers import resolve_model

# High enough for the model to compose large tool arguments (e.g. full OBML
# YAML docs).  The default 4096 causes truncated JSON → empty tool args.
_DEFAULT_MAX_TOKENS = 16_384


def make_agent(provider: str, model: str, toolsets=None) -> Agent:
    """
    Create a Pydantic AI Agent with the given toolsets (MCP servers).

    Args:
        provider: Provider name ("openrouter", "mlx", "ollama", "anthropic", "openai")
        model: Model identifier string
        toolsets: Pre-connected MCP server instances. If None, creates agent
                  with no toolsets.

    Returns:
        Configured Pydantic AI Agent
    """
    llm_model = resolve_model(provider, model)

    return Agent(
        model=llm_model,
        toolsets=toolsets or [],
        system_prompt=load_system_prompt(),
        retries=3,
        model_settings={"max_tokens": _DEFAULT_MAX_TOKENS},
    )
