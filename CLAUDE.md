# CLAUDE.md

## Project

OrionBelt Chat — Chainlit + Pydantic AI chat client for OrionBelt Analytics & Semantic Layer MCP servers.

## Code Review

Code is reviewed with OpenAI Codex. Write clean, well-structured code that passes automated review.

## Git Workflow

- Never commit directly to main — always create feature/ or fix/ branches
- Version must be bumped in four places:
  - `pyproject.toml`, `orionbelt_chat/public/header.js` (`var VERSION =
    "vX.Y.Z"`) and the `README.md` badge — `release.yml` verifies these three
    against the tag and refuses to cut a release if they disagree
  - `uv.lock` — run `uv lock` after `pyproject.toml`; it pins the project's own
    version and CI's `uv sync --frozen` fails if it is stale
- See [RELEASING.md](./RELEASING.md) for the full release procedure

## Layout

- Everything importable and shippable lives in `orionbelt_chat/` — including
  `app.py` and the UI assets (`public/`, `chainlit.md`, `chainlit_config.toml`,
  `system_prompt.md`), so the published wheel is self-contained
- Run it with the `orionbelt-chat` console script, not `chainlit run app.py`:
  the script seeds a writable app root before handing off to Chainlit

## Testing

- `tests/fake_mcp_server.py` is a real MCP server (FastMCP, stdio) launched
  as a subprocess by `tests/test_mcp_integration.py`, which drives a genuine
  agent run with `FunctionModel` — no API key, no network, so it runs in CI
- Use it to cover anything in the tool-call/tool-result path: that code sits
  deep inside `on_message` and shipped a live `AttributeError` once because
  nothing reached it

## Stack

- Python 3.11+, Chainlit, Pydantic AI
- MCP servers: orionbelt-analytics (stdio/HTTP), orionbelt-semantic-layer (HTTP)
- LLM providers: OpenRouter, MLX, Ollama, Anthropic, OpenAI
