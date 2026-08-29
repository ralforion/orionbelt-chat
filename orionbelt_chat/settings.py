"""Configuration management via Pydantic Settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """OrionBelt Chat configuration loaded from environment variables or .env file."""

    # ── LLM providers ──────────────────────────────────────────
    # Primary: OpenRouter
    openrouter_api_key: str = ""
    openrouter_default_model: str = "anthropic/claude-sonnet-4-5"

    # Local: mlx-openai-server (primary local option for Apple Silicon)
    mlx_base_url: str = "http://localhost:8000/v1"
    mlx_default_model: str = "mlx-community/Qwen2.5-14B-Instruct-4bit"

    # Local: Ollama (cross-platform fallback)
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_default_model: str = "qwen2.5:14b"

    # Direct provider keys (optional, bypass OpenRouter)
    anthropic_api_key: str = ""
    anthropic_default_model: str = "claude-sonnet-4-6"
    openai_api_key: str = ""
    openai_default_model: str = "gpt-4o"

    # ── Default provider on startup ────────────────────────────
    # Values: "openrouter" | "mlx" | "ollama" | "anthropic" | "openai"
    default_provider: str = "openrouter"
    default_model: str = ""  # if empty, uses provider default above

    # ── MCP servers ─────────────────────────────────────────
    # Each server can be configured as either:
    #   - a local directory path  → stdio transport (spawns subprocess)
    #   - an HTTP(S) URL          → Streamable HTTP transport (remote)
    # Set the *_dir variable to a path or URL accordingly.
    analytics_server_dir: str = ""
    semantic_layer_server_dir: str = ""

    # ── MCP sampling ────────────────────────────────────────
    # When true (default), advertises the sampling.tools capability and
    # answers `sampling/createMessage` requests using the env-configured
    # default model. Set to false to disable server-initiated LLM calls
    # (cost / privacy kill switch); servers will then fall back to whatever
    # manual review path they implement.
    mcp_allow_sampling: bool = True

    # ── System prompt ───────────────────────────────────────
    # Path to the markdown/text file holding the agent's system prompt.
    # If empty, defaults to `system_prompt.md` at the project root.
    # If the file is missing, an embedded fallback prompt is used.
    system_prompt_file: str = ""

    # ── Timeouts ────────────────────────────────────────────
    # Budget for the entire tool-call phase of a single agent turn (covers
    # all tool calls the model issues in that turn, plus any MCP sampling
    # round-trips back to the client). Raised from the previous 120s to
    # accommodate slow tools like ontology generation on large schemas
    # combined with server-initiated MCP sampling. Override per-deployment.
    tool_call_timeout_seconds: int = 300

    # Per-request timeout passed to pydantic-ai's MCP transport (HTTP request
    # timeout for streamable-HTTP, equivalent for stdio). Must be at least as
    # large as the slowest individual tool call you expect.
    mcp_request_timeout_seconds: int = 300

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


# Global settings instance
settings = Settings()
