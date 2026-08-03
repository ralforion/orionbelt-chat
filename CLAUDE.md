# CLAUDE.md

## Project

OrionBelt Chat — Chainlit + Pydantic AI chat client for OrionBelt Analytics & Semantic Layer MCP servers.

## Code Review

Code is reviewed with OpenAI Codex. Write clean, well-structured code that passes automated review.

## Git Workflow

- Never commit directly to main — always create feature/ or fix/ branches
- Version must be bumped in four places:
  - `pyproject.toml`, `public/header.js` (`var VERSION = "vX.Y.Z"`) and the
    `README.md` badge — `release.yml` verifies these three against the tag and
    refuses to cut a release if they disagree
  - `uv.lock` — run `uv lock` after `pyproject.toml`; it pins the project's own
    version and CI's `uv sync --frozen` fails if it is stale
- See [RELEASING.md](./RELEASING.md) for the full release procedure

## Stack

- Python 3.11+, Chainlit, Pydantic AI
- MCP servers: orionbelt-analytics (stdio/HTTP), orionbelt-semantic-layer (HTTP)
- LLM providers: OpenRouter, MLX, Ollama, Anthropic, OpenAI
