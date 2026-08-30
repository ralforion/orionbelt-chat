<!-- mcp-name: io.github.ralforion/orionbelt-chat -->
<p align="center">
  <img src="https://raw.githubusercontent.com/ralforion/orionbelt-chat/main/assets/ORIONBELT_Logo.png" alt="OrionBelt Logo" width="400">
</p>

<h1 align="center">OrionBelt® Chat</h1>

<p align="center"><strong>AI-powered chat interface for OrionBelt Analytics & Semantic Layer</strong></p>

[![Version](https://img.shields.io/badge/version-1.5.0-brightgreen.svg)](https://github.com/ralforion/orionbelt-chat)
[![PyPI](https://img.shields.io/pypi/v/orionbelt-chat.svg)](https://pypi.org/project/orionbelt-chat/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: BUSL-1.1](https://img.shields.io/badge/License-BUSL--1.1-orange.svg)](https://github.com/ralforion/orionbelt-chat/blob/main/LICENSE)
[![Chainlit](https://img.shields.io/badge/Chainlit-2.10+-blue)](https://chainlit.io)
[![Pydantic AI](https://img.shields.io/badge/Pydantic_AI-2.10+-blue)](https://ai.pydantic.dev)

[![Docker Hub](https://img.shields.io/docker/v/ralforion/orionbelt-chat?logo=docker&logoColor=white&label=Docker%20Hub&color=2496ED&sort=semver)](https://hub.docker.com/r/ralforion/orionbelt-chat)
[![Docker pulls](https://img.shields.io/docker/pulls/ralforion/orionbelt-chat?logo=docker&logoColor=white&color=2496ED)](https://hub.docker.com/r/ralforion/orionbelt-chat)
[![Image size](https://img.shields.io/docker/image-size/ralforion/orionbelt-chat/latest?logo=docker&logoColor=white&color=2496ED)](https://hub.docker.com/r/ralforion/orionbelt-chat)

[![OpenRouter](https://img.shields.io/badge/OpenRouter-300%2B_Models-blueviolet)](https://openrouter.ai)
[![MLX](https://img.shields.io/badge/MLX-Apple_Silicon-black)](https://github.com/ml-explore/mlx)
[![Ollama](https://img.shields.io/badge/Ollama-Local_LLMs-green)](https://ollama.com)

A production-ready chat application that connects to OrionBelt Analytics and OrionBelt Semantic Layer MCP servers, providing a conversational interface for database analysis, semantic modeling, and interactive data visualization. Built with Chainlit and Pydantic AI, supporting multiple LLM providers (cloud and local).

> **Better Together:** Works seamlessly with [**OrionBelt Analytics**](https://github.com/ralfbecher/orionbelt-analytics) and [**OrionBelt Semantic Layer**](https://github.com/ralfbecher/orionbelt-semantic-layer). Connect to both MCP servers simultaneously for schema-aware ontology generation, semantic modeling, guaranteed-correct SQL compilation, and interactive chart rendering.

<p align="center">
  <img src="https://raw.githubusercontent.com/ralforion/orionbelt-chat/main/assets/OrionBelt_Chat_1_Start.jpg" alt="OrionBelt Chat — Startup with connected MCP servers" width="800">
</p>
<p align="center"><em>Startup — connected MCP servers, provider & model selection</em></p>

<p align="center">
  <img src="https://raw.githubusercontent.com/ralforion/orionbelt-chat/main/assets/OrionBelt_Chat_2_Ontology.jpg" alt="OrionBelt Chat — Ontology generation pipeline with file download" width="800">
</p>
<p align="center"><em>Full pipeline — schema analysis, ontology generation, semantic enrichment & file download</em></p>

<p align="center">
  <img src="https://raw.githubusercontent.com/ralforion/orionbelt-chat/main/assets/OrionBelt_Chat_3_Charts.jpg" alt="OrionBelt Chat — Interactive Plotly charts" width="800">
</p>
<p align="center"><em>Interactive charts — heatmap, line & grouped bar rendered natively via Plotly.js</em></p>

## Key Features

### Multi-Provider LLM Support

- **OpenRouter** - Access 300+ models via single API (recommended for production)
- **MLX** - Local inference on Apple Silicon with mlx-openai-server
- **Ollama** - Cross-platform local inference with easy setup
- **Anthropic** - Direct API access (bypass OpenRouter)
- **OpenAI** - Direct API access (bypass OpenRouter)

### MCP Integration

- **Dual MCP server support** - Connect to Analytics and Semantic Layer simultaneously
- **Graceful degradation** - One unreachable server won't block the app; agent starts with available servers
- **Auto-reconnection** - Detects MCP session loss and reconnects automatically
- **Tool call resilience** - Retries failed tool calls up to 3 times; preserves conversation context on errors
- **Flexible transport** - Stdio (local subprocess) or Streamable HTTP (remote) per server
- **MCP sampling (with tools)** - Servers can delegate LLM calls back to the chat client via `sampling/createMessage`. The client advertises the `sampling.tools` sub-capability so servers can include tool definitions; sampling requests are handled by the env-configured default model (`DEFAULT_PROVIDER` + the matching `*_DEFAULT_MODEL`)
- **Tool visibility** - Collapsible steps show tool calls with arguments and results
- **Multi-turn context** - Full conversation history management with Pydantic AI

### Interactive Charts

- **Native Plotly rendering** - Charts render inline via Chainlit's bundled Plotly.js (no Python plotly package needed)
- **FastMCP Apps integration** - Fetches chart data from `ui://` resource URIs returned by MCP tools
- **Multiple chart types** - Bar, line, scatter, heatmap with auto-detection
- **Multiple extraction strategies** - Handles Plotly figure dicts, `Plotly.newPlot()` in HTML, and bare trace arrays

### Mermaid Diagrams

- **Client-side rendering** - Mermaid.js loaded from CDN renders `erDiagram`, `flowchart`, `sequenceDiagram`, and other diagram types inline
- **Auto-detection** - Mermaid syntax in MCP tool results is automatically surfaced as a rendered diagram
- **Theme-aware** - Diagrams re-render when switching between light and dark mode

### File Uploads

- **Upload button in the composer** - An **Upload** button sits next to the message input for the whole session, so a model or ontology can be attached at any point in the conversation; the paperclip works too
- **Handle-based injection** - The file is stored in the session under a handle (`@upload:model.yaml`); the model passes the handle as a tool argument and the client substitutes the real content just before the MCP call
- **No context cost** - A 200 KB model never enters the prompt and never has to be re-emitted by the LLM, so nothing is truncated by `max_tokens`
- **Format detection** - Extension plus content sniffing tells an OBML model from plain YAML and Turtle from RDF/XML, so the assistant routes it to the right tool (`load_model`, `load_my_ontology`)
- **YAML → JSON on demand** - Appending `#json` to a handle (`@upload:model.yaml#json`) converts the file on the way through, so a native OBML **YAML** upload works with arguments that take a JSON object, such as `load_model(model=…)`
- **Session-scoped** - Handles stay valid for the whole conversation; small files (<4 KB) are also inlined so the model can reason about them directly
- **Accepted formats** - `.yaml`, `.yml`, `.obml`, `.obsl`, `.json`, `.jsonld`, `.ttl`, `.turtle`, `.n3`, `.nt`, `.rdf`, `.owl` (UTF-8 text, up to 8 MB)

### File Downloads

- **Auto-detection** - Recognizes downloadable content in tool results and LLM response code blocks
- **Supported formats** - Turtle/RDF (.ttl), JSON, CSV, SQL, SPARQL, YAML, XML
- **Smart extraction** - Handles dict-shaped tool returns (e.g. `{'success': True, 'content': '@prefix ...'}`)
- **Inline attachments** - Download buttons appear directly in the response message

### Real-Time Streaming

- **Token-by-token streaming** - Smooth response rendering as the model generates
- **Thinking indicator** - Visual spinner while the model processes before responding
- **Tool call tracking** - Visual feedback for each MCP tool invocation with correct result matching
- **Stop generation** - Click the stop button or press **Escape** to cancel
- **Error handling** - Graceful failures with clear error messages

### Chainlit UI

- **Settings panel** - Switch providers and models on the fly; header updates live
- **Custom model input** - Override default models with specific versions
- **Customizable system prompt** - Edit `system_prompt.md` or set `SYSTEM_PROMPT_FILE` env var
- **Message recall** - Press **Arrow Up/Down** in the input to navigate message history
- **Responsive design** - Works on desktop and mobile browsers

## Quick Start

### Prerequisites

- **Python 3.11+** (3.13 recommended)
- **uv** package manager ([install](https://github.com/astral-sh/uv))
- **OrionBelt Analytics** and **Semantic Layer** repos cloned alongside this one

### Installation

```bash
# Clone the repository
cd orionbelt-chat

# Install dependencies
uv sync

# Copy environment template
cp .env.example .env

# Edit .env and add your API keys
```

### Configuration

Edit `.env` and configure your LLM provider:

**Option 1: OpenRouter (recommended for cloud)**

```bash
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_DEFAULT_MODEL=anthropic/claude-sonnet-4-5
DEFAULT_PROVIDER=openrouter
```

**Option 2: Anthropic direct**

```bash
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_DEFAULT_MODEL=claude-sonnet-4-6   # or claude-opus-4-6
DEFAULT_PROVIDER=anthropic
```

**Option 3: OpenAI direct**

```bash
OPENAI_API_KEY=sk-...
OPENAI_DEFAULT_MODEL=gpt-4o
DEFAULT_PROVIDER=openai
```

**Option 4: MLX local (Apple Silicon)**

```bash
# Start mlx-openai-server first:
mlx-openai-server launch \
  --model-path mlx-community/Qwen2.5-14B-Instruct-4bit \
  --model-type lm \
  --enable-auto-tool-choice \
  --port 8000

MLX_DEFAULT_MODEL=mlx-community/Qwen2.5-14B-Instruct-4bit
DEFAULT_PROVIDER=mlx
```

**Option 5: Ollama local (cross-platform)**

```bash
# Start Ollama first: ollama serve
OLLAMA_DEFAULT_MODEL=qwen2.5:14b
DEFAULT_PROVIDER=ollama
```

**MCP Server Paths:**

```bash
# Each can be a local directory (stdio) or HTTP(S) URL (Streamable HTTP):
ANALYTICS_SERVER_DIR=../orionbelt-analytics
SEMANTIC_LAYER_SERVER_DIR=../orionbelt-semantic-layer-mcp
# Remote example: ANALYTICS_SERVER_DIR=https://analytics.example.com/mcp
```

**Adding other MCP servers:**

The two variables above cover the OrionBelt servers. Any *other* MCP server —
someone else's, or your own — is declared in a YAML file. Copy
[`mcp_servers.example.yaml`](./mcp_servers.example.yaml) to `mcp_servers.yaml`
in the directory you launch from (or the app root, or anywhere with
`MCP_SERVERS_FILE=<path>`). The example file ships with working demos; the
two remote ones need no key and no install, so copying it as-is gives you
something to try on the next restart:

```yaml
servers:
  # ── Remote, over Streamable HTTP: no install, no API key ──
  - name: DeepWiki                       # ask questions about any public repo
    endpoint: https://mcp.deepwiki.com/mcp

  - name: Context7                       # current docs for most libraries
    endpoint: https://mcp.context7.com/mcp

  # ── Web search, remote: key lives in .env, not in this file ──
  - name: Tavily
    endpoint: https://mcp.tavily.com/mcp/?tavilyApiKey=${TAVILY_API_KEY}

  # ── Web search, local subprocess over stdio ──
  - name: Brave Search
    command: npx
    args: ["-y", "@brave/brave-search-mcp-server"]
    env:
      BRAVE_API_KEY: ${BRAVE_API_KEY}

  # ── Fetch any URL as markdown (needs uvx on PATH) ──
  - name: Fetch
    command: uvx
    args: ["mcp-server-fetch"]

  # ── Your own Python project, run as
  #    `uv run --directory <endpoint> python -m <module>` ──
  - name: My Analytics
    endpoint: ../my-analytics
    module: my_analytics
    sampling: false      # opt in before the server may call back for LLM sampling
```

The demos in the example file, and where their keys come from:

| Server | Transport | Key |
|---|---|---|
| [DeepWiki](https://mcp.deepwiki.com/mcp) — Q&A over any public GitHub repo | HTTP | none |
| [Context7](https://context7.com) — up-to-date library documentation | HTTP | none |
| [Tavily](https://app.tavily.com) — web search, extract, crawl | HTTP | `TAVILY_API_KEY` (free tier) |
| [Brave Search](https://brave.com/search/api/) — web, news, image, local | stdio (`npx`) | `BRAVE_API_KEY` (free tier) |
| [Fetch](https://github.com/modelcontextprotocol/servers/tree/main/src/fetch) — a URL as markdown | stdio (`uvx`) | none |
| [Filesystem](https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem) — files under a directory you name | stdio (`npx`) | none |

`${VAR}` in an `endpoint`, `command`, `args` or `env` value is replaced by that
environment variable — or by the matching line in your `.env`, the same file
the provider keys live in. So an API key is *named* in `mcp_servers.yaml` and
*stored* in `.env`, and the config file stays safe to commit. A variable that
is not set anywhere is reported in the servers panel rather than quietly sent
as an empty string.

| | |
|---|---|
| File is searched for | `./mcp_servers.yaml`, then `<app root>/mcp_servers.yaml` |
| Override the path | `MCP_SERVERS_FILE` |
| Relationship to the env vars | **Added** to them — one new server is one entry |
| Repoint a built-in | Reuse its name (`OrionBelt Analytics`, `OrionBelt Semantic Layer`) — the file entry replaces it |
| Disable a built-in | Unset its environment variable; there is no `enabled:` field, and an entry still needs a working `endpoint` or `command` |

Each entry needs exactly one of `endpoint` (a URL, or a directory plus
`module`) or `command` (plus optional `args`/`env`). A malformed entry does not
take the working servers down with it — the servers panel shows what was
rejected and why.

`env:` is additive: it is layered over the small set of variables an MCP
subprocess inherits by default (`PATH`, `HOME`, …), so naming one variable does
not take the rest away. That baseline is not the parent process's full
environment — a third-party server sees what you name plus the baseline, never
the rest of your keys.

The first launch of a `uvx` or `npx` server downloads it, which can outrun the
connection timeout; the server then connects normally on the next restart.

**System Prompt (optional):**

```bash
# Override the prompt file (defaults to the system_prompt.md inside the package)
# SYSTEM_PROMPT_FILE=~/my_custom_prompt.md
```

#### All settings

Every setting, with its default. All are read from the environment or a `.env`
file; see [`.env.example`](./.env.example) for a copyable starting point.

**LLM providers**

| Variable | Default | Purpose |
|---|---|---|
| `DEFAULT_PROVIDER` | `openrouter` | Provider selected on startup: `openrouter`, `mlx`, `ollama`, `anthropic`, `openai` |
| `DEFAULT_MODEL` | _(empty)_ | Model selected on startup; falls back to the provider default below |
| `OPENROUTER_API_KEY` | _(empty)_ | OpenRouter credential |
| `OPENROUTER_DEFAULT_MODEL` | `anthropic/claude-sonnet-4-5` | Model used when the provider is OpenRouter |
| `ANTHROPIC_API_KEY` | _(empty)_ | Anthropic credential, bypassing OpenRouter |
| `ANTHROPIC_DEFAULT_MODEL` | `claude-sonnet-4-6` | Model used when the provider is Anthropic |
| `OPENAI_API_KEY` | _(empty)_ | OpenAI credential, bypassing OpenRouter |
| `OPENAI_DEFAULT_MODEL` | `gpt-4o` | Model used when the provider is OpenAI |
| `MLX_BASE_URL` | `http://localhost:8000/v1` | Where `mlx-openai-server` is listening |
| `MLX_DEFAULT_MODEL` | `mlx-community/Qwen2.5-14B-Instruct-4bit` | Model used when the provider is MLX |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Where Ollama is listening |
| `OLLAMA_DEFAULT_MODEL` | `qwen2.5:14b` | Model used when the provider is Ollama |

**MCP servers**

| Variable | Default | Purpose |
|---|---|---|
| `ANALYTICS_SERVER_DIR` | _(empty)_ | OrionBelt Analytics: a local directory (stdio) or an HTTP(S) URL. Empty disables it |
| `SEMANTIC_LAYER_SERVER_DIR` | _(empty)_ | OrionBelt Semantic Layer, same forms |
| `MCP_SERVERS_FILE` | _(empty)_ | YAML file declaring any other servers. When empty, `mcp_servers.yaml` is searched for in the working directory, then the app root |
| `MCP_ALLOW_SAMPLING` | `true` | Whether servers may make LLM calls back through this client, answered with the default model. Set `false` as a cost/privacy kill switch |

**Behaviour**

| Variable | Default | Purpose |
|---|---|---|
| `SYSTEM_PROMPT_FILE` | _(empty)_ | Prompt file to load; defaults to the `system_prompt.md` shipped in the package |
| `TOOL_CALL_TIMEOUT_SECONDS` | `300` | Budget for the whole tool-call phase of one agent turn, including any sampling round-trips |
| `MCP_REQUEST_TIMEOUT_SECONDS` | `300` | Per-request MCP transport timeout. Must be at least as large as your slowest single tool call |

**App root** (not a `Settings` field — read by Chainlit and the launcher):

| Variable | Default | Purpose |
|---|---|---|
| `CHAINLIT_APP_ROOT` | `~/.orionbelt-chat` | Where `public/`, `chainlit.md`, `.chainlit/config.toml` are seeded and runtime state is written |
| `ORIONBELT_CHAT_HOME` | _(unset)_ | Alternative spelling of the same thing, used when `CHAINLIT_APP_ROOT` is unset |

### Run

```bash
uv run orionbelt-chat --watch
```

Open **http://localhost:8080** in your browser.

`orionbelt-chat` is a thin wrapper around `chainlit run` — every Chainlit flag
(`--port`, `--host`, `--headless`, `-w`) passes straight through. It also seeds
a writable *app root* with the UI assets the package ships, because Chainlit
resolves `public/`, `chainlit.md` and `.chainlit/config.toml` relative to that
directory and writes runtime state (`.files/`) into it:

| | |
|---|---|
| Default app root | `~/.orionbelt-chat` |
| Override | `CHAINLIT_APP_ROOT` or `ORIONBELT_CHAT_HOME` |
| Refreshed each launch | `public/` (versioned UI assets) |
| Created once, then yours to edit | `chainlit.md`, `.chainlit/config.toml` |

### Install from PyPI

To run the client without cloning the repo:

```bash
uv tool install orionbelt-chat     # or: pipx install orionbelt-chat
orionbelt-chat
```

Configuration is read from the environment, so put your keys in the shell or in
a `.env` file in the directory you launch from — see [Configuration](#configuration).

### Run with Docker

The app ships with a `Dockerfile` and `docker-compose.yml` so you can run it
without a local Python/uv toolchain.

**Using Docker Compose (recommended):**

```bash
# Configure your API keys first
cp .env.example .env   # then edit .env

docker compose up --build
```

**Using plain Docker:**

```bash
docker build -t orionbelt-chat .
docker run --rm -p 8080:8080 --env-file .env orionbelt-chat
```

**Using the published image from Docker Hub:**

```bash
docker run --rm -p 8080:8080 --env-file .env ralforion/orionbelt-chat:latest
```

Open **http://localhost:8080** in your browser. Configuration is read from the
environment (see `.env.example`); pass it via `--env-file .env` or individual
`-e KEY=value` flags.

## Usage Examples

**Connect to database:**

```
Connect to my PostgreSQL database at localhost
```

**Schema analysis:**

```
Analyze the schema and show me all tables with their relationships
```

**Query with charts:**

```
Show me revenue by product category as a bar chart
```

**Download an ontology:**

```
Generate an ontology for the schema and download it as Turtle
```

**Explore semantic models:**

```
What OBML models are available in the semantic layer?
```

**Generate OBML model:**

```
Create an OBML model for customer analytics with metrics for revenue, order count, and average order value
```

## Architecture

<p align="center">
  <img src="https://raw.githubusercontent.com/ralforion/orionbelt-chat/main/assets/architecture.png" alt="OrionBelt Chat Architecture" width="800">
</p>

```
┌──────────────────────────────────────────────────────────────┐
│          OrionBelt Chat (Chainlit + Pydantic AI)             │
│                                                              │
│  ┌──────────┐         ┌──────────────────────────────────┐   │
│  │  Chat UI │         │  Pydantic AI Agent + MCP Client  │   │
│  │          │────────>│  - Multi-turn context            │   │
│  │ Chainlit │         │  - Streaming events              │   │
│  │  2.10+   │         │  - Tool orchestration            │   │
│  └──────────┘         └──────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────┘
         │                      │
         │                      ├──> orionbelt-analytics (MCP stdio or HTTP)
         │                      │    - Schema analysis
         │                      │    - Ontology generation & download
         │                      │    - SQL execution
         │                      │    - Interactive Plotly charts
         │                      │
         │                      └──> orionbelt-semantic-layer (MCP stdio or HTTP)
         │                           - OBML model management
         │                           - Semantic query compilation
         │                           - Guaranteed-correct SQL
         │
         └──> LLM Provider (OpenRouter/MLX/Ollama/Anthropic/OpenAI)
```

**Key Components:**

- **Chainlit 2.10+** - Chat UI framework with streaming, steps, and settings
- **Pydantic AI 2.10+** - Agent framework with node-by-node iteration (`agent.iter()`)
- **MCP Transport** - Stdio (local subprocess) or Streamable HTTP (remote) per server
- **Chart Renderer** - Native Plotly rendering from FastMCP Apps `ui://` resources
- **Mermaid Renderer** - Client-side diagram rendering via Mermaid.js CDN
- **File Downloads** - Auto-detect downloadable content (TTL, JSON, CSV, SQL) in tool results
- **File Uploads** - Session registry for uploaded models/ontologies; `@upload:` handles are expanded into tool arguments via pydantic-ai's `process_tool_call` hook

## Development

```bash
# Install dev dependencies
uv sync --group dev

# Run tests (when available)
uv run pytest

# Format code
uv run ruff format

# Lint
uv run ruff check --fix
```

## Provider Details

### OpenRouter

- **Access**: 300+ models via single API
- **Reliability**: Best tool-calling support across vendors
- **Recommended models**:
  - `anthropic/claude-sonnet-4-5` - Best balance of speed and reasoning
  - `anthropic/claude-opus-4-5` - Maximum intelligence
  - `google/gemini-2.5-pro` - Fast and cost-effective
- **Setup**: Get API key at [openrouter.ai](https://openrouter.ai)

### Anthropic (direct)

- **Access**: Direct API, no intermediary
- **Available models**:
  - `claude-sonnet-4-6` - Fast, excellent tool use (default)
  - `claude-opus-4-6` - Maximum intelligence
  - `claude-haiku-4-5-20251001` - Fastest, lowest cost
- **Setup**: Get API key at [console.anthropic.com](https://console.anthropic.com)

### OpenAI (direct)

- **Access**: Direct API, no intermediary
- **Available models**:
  - `gpt-4o` - Best balance (default)
  - `gpt-4o-mini` - Fast and cost-effective
  - `o3-mini` - Reasoning model
- **Setup**: Get API key at [platform.openai.com](https://platform.openai.com)

### MLX (Apple Silicon)

- **Platform**: Mac with Apple Silicon (M1/M2/M3/M4)
- **Requirements**: `mlx-openai-server`
- **Recommended models**:
  - `mlx-community/Qwen2.5-14B-Instruct-4bit` - Excellent tool use
  - `mlx-community/Qwen2.5-32B-Instruct-4bit` - Better reasoning (requires 32GB+ RAM)
- **Setup**: Install with `pip install mlx-openai-server`
- **Notes**: Must use `--enable-auto-tool-choice` flag for tool calling

### Ollama

- **Platform**: Cross-platform (Mac/Linux/Windows)
- **Ease of use**: Simplest local setup
- **Recommended models**:
  - `qwen2.5:14b` - Good balance of speed and accuracy
  - `qwen2.5:32b` - Better reasoning (requires 32GB+ RAM)
- **Setup**: Download from [ollama.com](https://ollama.com)
- **Notes**: Built-in tool calling support with instruct models

## Troubleshooting

### MCP servers not connecting

**Symptom:** Status message shows "Failed to connect" for one or more servers

The app starts even when some servers are unreachable — it will show which connected and which failed. If a session drops mid-conversation, the app automatically reconnects.

**Solutions:**

- Ensure `ANALYTICS_SERVER_DIR` and `SEMANTIC_LAYER_SERVER_DIR` point to correct paths
- For local (stdio): check that repos have dependencies installed (`uv sync`)
- For remote (HTTP): verify the URL is reachable and the server is running
- Verify MCP servers can start independently (`uv run server.py`)

### Charts not rendering

**Symptom:** Charts don't appear after generate_chart tool call

**Solutions:**

- Verify `orionbelt-analytics` has MCP Apps support (v1.2.0+)
- Check server logs for `Chart URI detected` and `Plotly JSON extracted` messages
- Ensure the analytics server returns a `ui://` resource URI in the tool result
- Verify the resource content contains parseable Plotly figure data

### MLX model not calling tools

**Symptom:** Model ignores tools and tries to answer directly

**Solutions:**

- Ensure `--enable-auto-tool-choice` flag is set when starting mlx-openai-server
- Use an instruct-tuned model (with `-Instruct` suffix)
- Try a different model (Qwen2.5 series has best tool support)
- Check mlx-openai-server logs for errors

### Streaming stops or hangs

**Symptom:** Response stops mid-generation or "Thinking" indicator stays visible

**Solutions:**

- Press **Escape** or click the stop button to cancel, then retry
- Check MCP server logs for errors
- Verify tool calls are completing successfully (expand steps in the UI)
- Increase timeout settings if using slow local models
- Check the server console for detailed logs (each node transition is logged)

## AI Transparency

OrionBelt Chat is an AI system, and meets both transparency obligations of
**Article 50 of the EU AI Act** out of the box. Full reasoning, including who
carries responsibility when you self-host, is in [docs/AI_ACT.md](./docs/AI_ACT.md).

### Art. 50(1) — you are told it is an AI

- **UI chrome** — the assistant is named "OrionBelt Chat – AI Assistant"
  (`orionbelt_chat/chainlit_config.toml`, `orionbelt_chat/public/header.js`)
- **Welcome screen** — the notice in [`chainlit.md`](./orionbelt_chat/chainlit.md)
- **First interaction** — a message sent at the start of every session, before
  any agent work, so it appears even when the agent or its MCP servers fail to
  start (`AI_DISCLOSURE` in [`app.py`](./orionbelt_chat/app.py))

### Art. 50(2) — generated output is marked

Every channel by which content leaves the app carries a machine-readable
provenance record built in
[`orionbelt_chat/provenance.py`](./orionbelt_chat/provenance.py), using the
IPTC `digitalSourceType` term `trainedAlgorithmicMedia`:

- **Downloads** — comment header for TTL/SPARQL/SQL/YAML/XML, a `_provenance`
  key for JSON, and the absolute PROV IRI `…prov#wasGeneratedBy` for JSON-LD,
  which expands correctly whatever form the document's `@context` takes
- **CSV/TSV** — an adjacent `.prov.json` sidecar, since a comment line would
  break strict parsers
- **Images** — an XMP packet embedded in the PNG, no extra dependency
- **Charts** — a Plotly `meta` block plus a visible caption, which is the only
  marking that survives the modebar's client-side PNG export
- **Rendered page** — `<meta name="ai-generated">` and `data-ai-generated`
  attributes on non-user steps

Marking is never applied where it would corrupt the payload, and C2PA signing
is left to deployers with their own certificate — no signing key ships here.

`tests/test_ai_disclosure.py` and `tests/test_ai_provenance.py` pin every
channel so a refactor cannot silently drop the marking.

## License

Licensed under the **Business Source License 1.1** (SPDX: `BUSL-1.1`).

- **Production use allowed** for internal/personal use
- **Commercial embedding/SaaS restrictions** - contact licensing@ralforion.com
- **Change Date**: 2030-04-05
- **Change License**: Apache 2.0

See [LICENSE](./LICENSE) for full terms.

### Third-party dependencies

The published container image redistributes ~180 open-source packages, so their
attribution notices ship with it as `/app/THIRD_PARTY_LICENSES.md`. The same
file, plus a CycloneDX SBOM (`sbom.cdx.json`), is attached to every GitHub
Release; the image additionally carries an SBOM and build-provenance
attestation. The dependency tree is permissive throughout — Apache-2.0, MIT,
BSD, ISC and MPL-2.0, with no GPL/LGPL/AGPL — so nothing there constrains the
BSL terms above.

Attribution texts are read out of each installed distribution, including ones
vendored into a package tree rather than its `.dist-info`. The handful of
projects that ship no text at all in either their wheel or their sdist are
covered by curated copies in [`licenses/`](./licenses/README.md), taken verbatim
from upstream; `--fail-on-missing-notice` (which CI and the image build both
pass) stops a release if a new dependency needs one and hasn't got it.

Regenerate locally with:

```bash
uv sync --frozen --no-dev
.venv/bin/python scripts/gen_third_party_licenses.py \
  --venv .venv \
  --overrides licenses \
  --fail-on-missing-notice \
  --version "$(grep -m1 '^version = ' pyproject.toml | cut -d'"' -f2)"
```

## Links

### OrionBelt Platform

- [**OrionBelt Analytics**](https://github.com/ralfbecher/orionbelt-analytics) - MCP server for database analysis and ontology generation
- [**OrionBelt Semantic Layer**](https://github.com/ralfbecher/orionbelt-semantic-layer) - MCP server for OBML models and semantic SQL compilation
- [**OrionBelt Ontology Builder**](https://github.com/ralfbecher/orionbelt-ontology-builder) - Visual ontology editor (Streamlit app)

### Frameworks

- [**Chainlit**](https://docs.chainlit.io) - Chat UI framework
- [**Pydantic AI**](https://ai.pydantic.dev) - Agent framework with MCP support
- [**Model Context Protocol**](https://modelcontextprotocol.io) - Tool integration standard

### LLM Providers

- [**OpenRouter**](https://openrouter.ai) - Unified API for 300+ models
- [**MLX**](https://github.com/ml-explore/mlx) - Apple Silicon inference
- [**Ollama**](https://ollama.com) - Local LLM runtime

---

<p align="center">
  <a href="https://ralforion.com">
    <img src="https://raw.githubusercontent.com/ralforion/orionbelt-chat/main/assets/RALFORION_doo_Logo.png" alt="RALFORION d.o.o." width="200">
  </a>
</p>

<p align="center">
  Copyright © 2026 RALFORION d.o.o.<br>
  OrionBelt® is a registered trademark of RALFORION d.o.o.
</p>
