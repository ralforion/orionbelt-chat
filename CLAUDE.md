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

## Stack

- Python 3.11+, Chainlit, Pydantic AI
- MCP servers: orionbelt-analytics (stdio/HTTP), orionbelt-semantic-layer (HTTP)
- LLM providers: OpenRouter, MLX, Ollama, Anthropic, OpenAI
