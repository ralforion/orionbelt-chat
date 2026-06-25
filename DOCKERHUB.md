# OrionBelt Chat

Chainlit + Pydantic AI chat client for the **OrionBelt Analytics** and
**OrionBelt Semantic Layer** MCP servers — a conversational interface for
database analysis, semantic modeling, and interactive data visualization.

Supports multiple LLM providers (OpenRouter, Anthropic, OpenAI) and local
models (MLX on Apple Silicon, Ollama).

## Quick start

```bash
docker run --rm -p 8080:8080 --env-file .env ralforion/orionbelt-chat:latest
```

Then open <http://localhost:8080>.

Configuration is read from the environment — pass it with `--env-file .env`
or individual `-e KEY=value` flags. See the project README for the full list
of variables (LLM provider keys, MCP server URLs, timeouts).

### Docker Compose

```yaml
services:
  orionbelt-chat:
    image: ralforion/orionbelt-chat:latest
    ports:
      - "8080:8080"
    env_file:
      - .env
    restart: unless-stopped
```

## Tags

- `latest` — the most recent release
- `X.Y.Z`, `X.Y`, `X` — specific semantic versions

Images are published for `linux/amd64` and `linux/arm64`.

## Links

- **Source & docs:** https://github.com/ralforion/orionbelt-chat
- **Issues:** https://github.com/ralforion/orionbelt-chat/issues
- **License:** BSL 1.1
