"""OrionBelt Chat - Chainlit + Pydantic AI application entry point."""

import asyncio
import copy
import json
import logging
from importlib.metadata import PackageNotFoundError, version as _pkg_version

import chainlit as cl
from chainlit.context import local_steps
from chainlit.input_widget import Select, TextInput
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    BinaryContent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)

from mcp.types import ErrorData

from src.agent import make_agent
from src.chart_renderer import UI_URI_PATTERN, render_chart_if_present
from src.file_downloads import extract_downloads_from_response, extract_downloads_from_tool_results
from src.mcp_sampling import get_sampling_callback, set_sampling_callback
from src.mcp_servers import SERVERS_USING_SAMPLING, get_mcp_servers_named, get_sampling_model_label
from src.mermaid_renderer import extract_mermaid_from_tool_results
from src.providers import PROVIDER_LABELS, default_model_for, models_for
from src.settings import settings

logger = logging.getLogger(__name__)

try:
    _APP_VERSION = _pkg_version("orionbelt-chat")
except PackageNotFoundError:
    _APP_VERSION = "unknown"
logger.info("OrionBelt Chat v%s starting up", _APP_VERSION)

# Maximum characters to display in a Chainlit tool-call step output.
# Large MCP tool responses (e.g. SQL query results with many rows) can
# overwhelm the WebSocket/browser and stall the agent loop.  The model
# still receives the full content via pydantic-ai's internal history.
STEP_OUTPUT_LIMIT = 10_000
TOOL_CALL_TIMEOUT = settings.tool_call_timeout_seconds


def _split_tool_content(raw) -> tuple[str, list[BinaryContent]]:
    """Split tool return content into a text string and binary parts.

    Pydantic AI tool results may contain BinaryContent (e.g. BinaryImage)
    mixed with strings inside a list.  Returns the text portion (JSON-
    serialised for dicts/lists, str() otherwise) and any binary objects.
    """
    binaries: list[BinaryContent] = []
    if isinstance(raw, BinaryContent):
        return "", [raw]
    if isinstance(raw, list):
        text_parts = []
        for item in raw:
            if isinstance(item, BinaryContent):
                binaries.append(item)
            else:
                text_parts.append(item)
        if text_parts:
            if any(isinstance(p, (dict, list)) for p in text_parts):
                try:
                    text = json.dumps(text_parts)
                except TypeError:
                    text = "\n".join(str(p) for p in text_parts)
            else:
                text = "\n".join(str(p) for p in text_parts)
        else:
            text = ""
        return text, binaries
    if isinstance(raw, dict):
        try:
            return json.dumps(raw), binaries
        except TypeError:
            return str(raw), binaries
    return str(raw), binaries


# ── Chainlit Chat Settings (sidebar UI) ───────────────────────────────────


def build_chat_settings(provider: str | None = None) -> list[cl.input_widget.InputWidget]:
    """Define the settings panel shown in the Chainlit sidebar."""
    prov = provider or settings.default_provider
    model_list = models_for(prov)
    return [
        Select(
            id="provider",
            label="LLM Provider",
            values=list(PROVIDER_LABELS.keys()),
            initial_value=prov,
            tooltip="Select your AI provider",
        ),
        Select(
            id="model",
            label="Model",
            values=model_list,
            initial_value=default_model_for(prov),
        ),
        TextInput(
            id="custom_model",
            label="Custom model name (overrides above)",
            initial="",
            placeholder="e.g. openrouter:google/gemini-2.5-pro or mlx-community/MyModel-4bit",
            tooltip="Leave empty to use the model selected above",
        ),
    ]


# ── Session lifecycle ──────────────────────────────────────────────────────


@cl.on_chat_start
async def on_start():
    """
    Called once per user session.
    Sets up the agent and starts MCP server connections.
    """
    # Show settings panel
    await cl.ChatSettings(build_chat_settings()).send()

    # Read initial values
    provider = settings.default_provider
    model = default_model_for(provider)

    # Store in session
    cl.user_session.set("provider", provider)
    cl.user_session.set("model", model)
    cl.user_session.set("pydantic_history", None)

    # Create agent and start MCP servers
    init_success = await _init_agent(provider, model)

    if init_success:
        mcp_info = cl.user_session.get("mcp_info", "")
        status_msg = cl.Message(
            content=(
                f"**OrionBelt® Analytics Assistant** ready.\n\n"
                f"Provider: `{provider}` | Model: `{model}`\n\n"
                f"{mcp_info}\n\n"
                f"Ask me anything about your data."
            )
        )
        await status_msg.send()
        cl.user_session.set("status_msg", status_msg)


def _wrap_sampling_for_chainlit(server, server_name: str) -> None:
    """Wrap the MCP toolset's sampling callback to render a Chainlit Step.

    Must be called BEFORE ``server.__aenter__()``: the callback is read out
    of the session kwargs when the session opens.

    No-op when the server does no sampling (``MCP_ALLOW_SAMPLING=false``, or
    no default model resolved), so there is nothing to render.
    """
    original = get_sampling_callback(server)
    if original is None:
        return

    async def wrapped(context, params):
        try:
            lines = []
            for m in getattr(params, "messages", []) or []:
                role = getattr(m, "role", "?")
                content = getattr(m, "content", m)
                text = getattr(content, "text", None)
                lines.append(f"**[{role}]** {text or content}")
            question = "\n\n".join(lines)
        except Exception:
            question = str(params)
        if len(question) > STEP_OUTPUT_LIMIT:
            question = question[:STEP_OUTPUT_LIMIT] + f"\n\n… (truncated — {len(question):,} chars)"

        # Prefer the in-flight tool step (sampling is fired from inside the
        # tool's execution); fall back to the run step if no tool is active.
        parent_id = cl.user_session.get("active_tool_step_id") or cl.user_session.get("run_step_id")
        step = cl.Step(name=f"Sampling: {server_name}", type="tool", parent_id=parent_id)
        await step.send()
        try:
            result = await original(context, params)
            # FastMCP's callback swallows handler exceptions and returns
            # ErrorData instead of raising, so failures arrive here, not in
            # the except branch below.
            if isinstance(result, ErrorData):
                step.output = (
                    f"**Prompt sent to model:**\n\n{question}\n\n---\n\n**Error:** {result.message}"
                )
                await step.update()
                return result
            content = getattr(result, "content", None)
            text = getattr(content, "text", None) if content is not None else None
            answer = text or str(result)
            if len(answer) > STEP_OUTPUT_LIMIT:
                answer = answer[:STEP_OUTPUT_LIMIT] + f"\n\n… (truncated — {len(answer):,} chars)"
            # Render as markdown (wraps naturally) instead of step.input (code block)
            step.output = (
                f"**Prompt sent to model:**\n\n{question}\n\n---\n\n**Model response:**\n\n{answer}"
            )
            await step.update()
            return result
        except Exception as e:
            step.output = f"**Prompt sent to model:**\n\n{question}\n\n---\n\n**Error:** {e}"
            await step.update()
            raise

    set_sampling_callback(server, wrapped)


async def _init_agent(provider: str, model: str) -> bool:
    """
    Connect MCP servers individually, create agent with the successful ones.

    Stores ``mcp_info`` in the session describing connectivity status.

    Returns:
        True if agent was created (even with partial MCP connectivity)
    """
    # Close previously connected MCP servers
    for _name, ctx in cl.user_session.get("mcp_contexts") or []:
        try:
            await ctx.__aexit__(None, None, None)
        except Exception:
            pass

    named_servers = get_mcp_servers_named()
    connected = []
    connected_names = []
    failed_names = []
    active_contexts: list[tuple[str, object]] = []

    # Connect each MCP server individually
    for name, server in named_servers:
        _wrap_sampling_for_chainlit(server, name)
        try:
            await server.__aenter__()
            connected.append(server)
            connected_names.append(name)
            active_contexts.append((name, server))
            logger.info("MCP server connected: %s", name)
        except Exception as e:
            logger.warning("MCP server failed: %s — %s", name, e)
            failed_names.append((name, e))

    _update_mcp_info(connected_names, failed_names)

    # Create agent with whatever servers connected
    try:
        agent = make_agent(provider, model, toolsets=connected)
        cl.user_session.set("agent", agent)
        cl.user_session.set("mcp_contexts", active_contexts)
        return True
    except Exception as e:
        cl.user_session.set("agent", None)
        cl.user_session.set("mcp_contexts", [])
        await cl.Message(
            content=f"Failed to create agent: {e}",
            author="System",
        ).send()
        return False


def _update_mcp_info(
    connected_names: list[str], failed_names: list[tuple[str, Exception]] | None = None
):
    """Update the mcp_info session variable."""
    parts = []
    if connected_names:
        server_list = "\n".join(
            f"- `{n}`" + (" — uses sampling" if n in SERVERS_USING_SAMPLING else "")
            for n in connected_names
        )
        parts.append(f"Connected MCP servers:\n{server_list}")
    if failed_names:
        fail_list = "\n".join(f"- `{n}`: {e}" for n, e in failed_names)
        parts.append(f"Failed to connect:\n{fail_list}")
    if not connected_names and not failed_names:
        parts.append("No MCP servers configured.")

    sampling_label = get_sampling_model_label()
    if sampling_label:
        parts.append(f"Sampling Model: `{sampling_label}`")
    else:
        parts.append("Sampling Model: _disabled_")

    cl.user_session.set("mcp_info", "\n\n".join(parts))


@cl.on_stop
async def on_stop():
    """Called when the user clicks the stop button or presses Escape."""
    logger.info("User stopped the current task.")


@cl.on_chat_end
async def on_end():
    """Clean up MCP server subprocesses when session ends."""
    for _name, ctx in cl.user_session.get("mcp_contexts") or []:
        try:
            await ctx.__aexit__(None, None, None)
        except Exception:
            pass


# ── Settings change handler ────────────────────────────────────────────────


@cl.on_settings_edit
async def on_settings_edit(settings_values: dict):
    """
    Fires on-the-fly while the user edits any widget in the settings panel
    (before clicking Confirm).  When the provider dropdown changes, re-send
    the ChatSettings with the matching model list so the Model dropdown
    updates immediately.
    """
    new_provider = settings_values.get("provider")
    if not new_provider:
        return
    prev_provider = cl.user_session.get("provider", settings.default_provider)
    if new_provider != prev_provider:
        await cl.ChatSettings(build_chat_settings(new_provider)).send()


@cl.on_settings_update
async def on_settings_update(settings_values: dict):
    """
    Called when the user changes provider/model in the sidebar.
    Rebuilds the agent with the new model.
    """
    provider = settings_values.get("provider", settings.default_provider)
    custom_model = (settings_values.get("custom_model") or "").strip()
    selected_model = settings_values.get("model", default_model_for(provider))

    # If the selected model doesn't belong to the new provider, fall back
    # to that provider's env-configured default (safety net).
    provider_models = models_for(provider)
    if not custom_model and selected_model not in provider_models:
        selected_model = default_model_for(provider)

    model = custom_model if custom_model else selected_model

    cl.user_session.set("provider", provider)
    cl.user_session.set("model", model)
    cl.user_session.set("pydantic_history", None)  # clear history on model change

    await cl.Message(
        content=f"Switching to `{provider}` / `{model}`...",
        author="System",
    ).send()

    init_success = await _init_agent(provider, model)

    if init_success:
        # _init_agent already created/updated the status_msg with MCP info
        # Just send a confirmation
        await cl.Message(
            content=f"Now using `{model}` via `{provider}`.",
            author="System",
        ).send()

        # Also update the original header status message if it exists
        status_msg = cl.user_session.get("status_msg")
        mcp_info = cl.user_session.get("mcp_info", "")
        if status_msg:
            status_msg.content = (
                f"**OrionBelt® Analytics Assistant** ready.\n\n"
                f"Provider: `{provider}` | Model: `{model}`\n\n"
                f"{mcp_info}\n\n"
                f"Ask me anything about your data."
            )
            await status_msg.update()


# ── Message handler ────────────────────────────────────────────────────────


# ── History trimming ──────────────────────────────────────────────────────

# Maximum characters to keep for a single tool result in older history messages.
# Recent messages (last HISTORY_KEEP_RECENT) are never trimmed.
TOOL_RESULT_TRIM_LIMIT = 500
HISTORY_KEEP_RECENT = 6  # keep last N messages untrimmed

# Transient tools whose results are consumed once and not needed later in the
# analytical journey.  These are trimmed more aggressively (even in recent
# messages) to free context for structural results the model needs to retain.
_TRANSIENT_TOOLS: set[str] = {
    "sample_table_data",
    "get_table_details",
    "suggest_semantic_names",
    "get_obml_reference",
    "validate_model",
    "connect_database",
    "list_schemas",
}
_TRANSIENT_TRIM_LIMIT = 200


def _trim_limit_for_tool(tool_name: str | None, is_recent: bool) -> int | None:
    """Return the char limit for a tool result, or None to keep it intact."""
    if tool_name in _TRANSIENT_TOOLS:
        return _TRANSIENT_TRIM_LIMIT
    if not is_recent:
        return TOOL_RESULT_TRIM_LIMIT
    return None


def _trim_history(messages: list) -> list:
    """Return a copy of *messages* with old, large tool results truncated.

    This frees up context window for the model so it can compose complex
    tool arguments (like full OBML YAML) without drowning in earlier results.

    Transient tool results (exploratory data, one-shot references) are trimmed
    aggressively regardless of age.  Structural tool results (schema analysis,
    model descriptions, query results) are only trimmed when they fall outside
    the recent window.
    """
    if not messages:
        return messages

    cutoff = max(0, len(messages) - HISTORY_KEEP_RECENT)
    trimmed = []
    trimmed_count = 0

    for idx, msg in enumerate(messages):
        is_recent = idx >= cutoff

        parts = getattr(msg, "parts", None)
        if parts is None:
            trimmed.append(msg)
            continue

        needs_trim = False
        for part in parts:
            if type(part).__name__ == "ToolReturnPart":
                tool_name = getattr(part, "tool_name", None)
                limit = _trim_limit_for_tool(tool_name, is_recent)
                if limit is not None:
                    content = getattr(part, "content", "")
                    if isinstance(content, str) and len(content) > limit:
                        needs_trim = True
                        break

        if not needs_trim:
            trimmed.append(msg)
            continue

        msg_copy = copy.deepcopy(msg)
        new_parts = []
        for part in msg_copy.parts:
            if type(part).__name__ == "ToolReturnPart":
                tool_name = getattr(part, "tool_name", None)
                limit = _trim_limit_for_tool(tool_name, is_recent)
                if limit is not None:
                    content = getattr(part, "content", "")
                    if isinstance(content, str) and len(content) > limit:
                        part.content = (
                            content[:limit] + f"\n\n… (trimmed — {len(content):,} chars total)"
                        )
                        trimmed_count += 1
            new_parts.append(part)
        msg_copy.parts = new_parts
        trimmed.append(msg_copy)

    if trimmed_count:
        logger.info("Trimmed %d tool results to save context space.", trimmed_count)

    return trimmed


# ── MCP reconnection helpers ──────────────────────────────────────────────

_MCP_ERROR_PHRASES = (
    "Session terminated",
    "session expired",
    "McpError",
    "Connection refused",
    "Connection reset",
    "Server disconnected",
    "EOF",
    "Broken pipe",
    "stream has been closed",
)

_MCP_ERROR_TYPES = (
    ConnectionError,
    ConnectionResetError,
    BrokenPipeError,
    EOFError,
    OSError,
)


_MODEL_HTTP_HINTS = {
    401: "Authentication failed — check the API key for this provider.",
    402: "Out of credits or quota exceeded for this provider.",
    403: "Access denied — the API key may not be allowed to use this model.",
    404: "Model not found — verify the selected model is still available.",
    408: "Provider timed out — try again or pick a different model.",
    413: "Request too large — shorten the message or trim history.",
    429: "Rate limit hit — wait a moment and try again.",
}


def _extract_body_message(body) -> str:
    """Pull the human-readable message out of an OpenAI / provider error body."""
    if isinstance(body, dict):
        inner = body.get("error") if isinstance(body.get("error"), dict) else body
        if isinstance(inner, dict):
            msg = inner.get("message")
            if msg:
                return str(msg).strip()
    if isinstance(body, str):
        return body.strip()
    return ""


def _hint_for_status(status: int | None) -> str:
    if status is None:
        return "Provider returned an error."
    if status in _MODEL_HTTP_HINTS:
        return _MODEL_HTTP_HINTS[status]
    return "Provider error." if status >= 500 else f"Provider returned HTTP {status}."


def _format_model_http_error(err: ModelHTTPError) -> str:
    detail = _extract_body_message(err.body)
    parts = [f"**{_hint_for_status(err.status_code)}**", f"Model: `{err.model_name}`"]
    if detail:
        parts.append(detail)
    return "\n\n".join(parts)


def _format_provider_error(exc: BaseException) -> str | None:
    """Walk the cause chain for a model/provider error; format if found."""
    cur: BaseException | None = exc
    while cur is not None:
        if isinstance(cur, ModelHTTPError):
            return _format_model_http_error(cur)
        body = getattr(cur, "body", None)
        if body is not None:
            detail = _extract_body_message(body) or str(cur).strip()
            status = getattr(cur, "status_code", None)
            parts = [f"**{_hint_for_status(status)}**"]
            if detail:
                parts.append(detail)
            return "\n\n".join(parts)
        cur = cur.__cause__ if cur.__cause__ is not cur else None
    return None


def _is_mcp_session_error(exc: BaseException) -> bool:
    """Walk the exception chain looking for MCP / connection-loss signals."""
    cur: BaseException | None = exc
    while cur is not None:
        text = str(cur)
        if any(phrase in text for phrase in _MCP_ERROR_PHRASES):
            return True
        if isinstance(cur, _MCP_ERROR_TYPES):
            return True
        # Empty exception message from MCP transport failures
        if not text.strip() and type(cur).__module__ and "mcp" in type(cur).__module__:
            return True
        cur = cur.__cause__ if cur.__cause__ is not cur else None
    # Empty error with no cause — likely a transport-level failure
    if not str(exc).strip():
        return True
    return False


async def _reconnect_mcp() -> bool:
    """Test each MCP server, reconnect only the failed ones.

    Returns True if all servers are healthy after reconnection.
    """
    current_contexts: list[tuple[str, object]] = cl.user_session.get("mcp_contexts") or []
    if not current_contexts:
        # No servers to reconnect — fall back to full init
        return await _full_reconnect_mcp()

    healthy: list[tuple[str, object]] = []
    failed_names: list[str] = []

    for name, server in current_contexts:
        try:
            await asyncio.wait_for(server.list_tools(), timeout=5)
            healthy.append((name, server))
            logger.info("MCP server healthy: %s", name)
        except Exception:
            logger.warning("MCP server down: %s", name)
            failed_names.append(name)
            try:
                await server.__aexit__(None, None, None)
            except Exception:
                pass

    if not failed_names:
        logger.info("All MCP servers healthy — no reconnection needed")
        return True

    await cl.Message(
        content=f"MCP server connection lost: {', '.join(failed_names)}. Reconnecting …",
        author="System",
    ).send()

    # Reconnect only failed servers
    named_servers = get_mcp_servers_named()
    reconnected: list[str] = []
    still_failed: list[tuple[str, Exception]] = []
    for name, server in named_servers:
        if name not in failed_names:
            continue
        try:
            await server.__aenter__()
            healthy.append((name, server))
            reconnected.append(name)
            logger.info("MCP server reconnected: %s", name)
        except Exception as e:
            logger.warning("MCP server reconnect failed: %s — %s", name, e)
            still_failed.append((name, e))

    connected_names = [n for n, _ in healthy]
    _update_mcp_info(connected_names, still_failed)

    # Rebuild agent with the mix of healthy + reconnected servers
    provider = cl.user_session.get("provider")
    model = cl.user_session.get("model")
    try:
        toolsets = [s for _, s in healthy]
        agent = make_agent(provider, model, toolsets=toolsets)
        cl.user_session.set("agent", agent)
        cl.user_session.set("mcp_contexts", healthy)
    except Exception as e:
        await cl.Message(content=f"Failed to create agent: {e}", author="System").send()
        return False

    mcp_info = cl.user_session.get("mcp_info", "")
    status = f"Reconnected: {', '.join(reconnected)}." if reconnected else ""
    if still_failed:
        status += f" Still down: {', '.join(n for n, _ in still_failed)}."
    await cl.Message(content=f"{status} {mcp_info}".strip(), author="System").send()
    return not still_failed


async def _full_reconnect_mcp() -> bool:
    """Full reconnection — close everything and reinitialise."""
    await cl.Message(content="MCP server connection lost. Reconnecting …", author="System").send()
    provider = cl.user_session.get("provider")
    model = cl.user_session.get("model")
    if await _init_agent(provider, model):
        mcp_info = cl.user_session.get("mcp_info", "")
        await cl.Message(content=f"Reconnected. {mcp_info}", author="System").send()
        return True
    await cl.Message(
        content="Reconnection failed. Check that MCP servers are running.", author="System"
    ).send()
    return False


# ── Message handler ────────────────────────────────────────────────────────


@cl.action_callback("retry_message")
async def on_retry(action: cl.Action):
    """Resubmit the last user message after a model/provider error."""
    await action.remove()
    content = cl.user_session.get("retry_content")
    if not content:
        await cl.Message(content="Nothing to retry.", author="System").send()
        return
    await on_message(cl.Message(content=content, author="User"))


@cl.on_message
async def on_message(message: cl.Message, *, _retried: bool = False):
    """
    Main handler. Iterates the Pydantic AI agent graph node-by-node,
    streaming text deltas and showing tool call steps in the Chainlit UI.

    Uses agent.iter() instead of run_stream_events() to avoid the anyio
    rendezvous-channel backpressure that can stall the agent after many
    tool calls.
    """
    agent = cl.user_session.get("agent")
    if agent is None:
        await cl.Message(
            content="No agent initialised. Check your provider settings.",
            author="System",
        ).send()
        return

    # Get message history for multi-turn context.
    # Trim old tool results to free context for the model.
    msg_history = _trim_history(cl.user_session.get("pydantic_history") or [])

    # The @cl.on_message decorator wraps this handler in an "on_message" Step
    # via local_steps. All Steps must be children of that wrapper so they render
    # in chronological order (Steps first, response text last).
    _parent_steps = local_steps.get() or []
    _run_step_id = _parent_steps[-1].id if _parent_steps else None
    # Stash for the sampling-callback wrapper so its Step lives under the
    # same run timeline as the tool steps (rather than as a root-level Step
    # at the bottom of the chat).
    cl.user_session.set("run_step_id", _run_step_id)

    chart_elements: list = []
    fallback_images: list = []
    needs_reconnect = False
    response_msg = cl.Message(content="")
    response_sent = False
    tool_steps: dict[str, cl.Step] = {}  # tool_call_id → Step
    result_messages = None

    history_chars = sum(len(str(m)) for m in msg_history)
    logger.info(
        "Message received (%d history messages, ~%dk chars): %.100s",
        len(msg_history),
        history_chars // 1000,
        message.content,
    )
    # Log history structure for debugging context issues
    if msg_history:
        for i, m in enumerate(msg_history):
            parts_info = []
            for p in getattr(m, "parts", []):
                kind = type(p).__name__
                content = getattr(p, "content", "")
                content_len = len(str(content)) if content else 0
                tool = getattr(p, "tool_name", "")
                if tool:
                    parts_info.append(f"{kind}({tool},{content_len}c)")
                else:
                    parts_info.append(f"{kind}({content_len}c)")
            logger.info("  history[%d] %s: %s", i, type(m).__name__, " | ".join(parts_info))

    try:
        text_parts: list[str] = []
        thinking_step: cl.Step | None = None

        async with agent.iter(
            message.content,
            message_history=msg_history or None,
        ) as agent_run:
            async for node in agent_run:
                node_name = type(node).__name__
                logger.debug("Agent node: %s", node_name)

                # ── Model request: collect text ────────────────────
                if Agent.is_model_request_node(node):
                    logger.info("Streaming model request …")
                    # Show a thinking indicator while the model generates
                    thinking_step = cl.Step(name="Thinking", type="run", parent_id=_run_step_id)
                    await thinking_step.send()
                    async with node.stream(agent_run.ctx) as stream:
                        async for event in stream:
                            if isinstance(event, PartStartEvent) and isinstance(
                                event.part, TextPart
                            ):
                                if thinking_step:
                                    thinking_step.output = ""
                                    await thinking_step.update()
                                    thinking_step = None
                                if event.part.content:
                                    text_parts.append(event.part.content)
                            elif isinstance(event, PartDeltaEvent) and isinstance(
                                event.delta, TextPartDelta
                            ):
                                if thinking_step:
                                    thinking_step.output = ""
                                    await thinking_step.update()
                                    thinking_step = None
                                chunk = event.delta.content_delta
                                # Filter leaked model thinking tokens (e.g. Gemma)
                                if "<|channel>" in chunk or "<channel|>" in chunk:
                                    continue
                                text_parts.append(chunk)
                    # Close thinking step if model produced no text (only tool calls)
                    if thinking_step:
                        thinking_step.output = ""
                        await thinking_step.update()
                        thinking_step = None
                    logger.info("Model request complete.")

                # ── Tool calls: show as Chainlit steps ──────────────
                elif Agent.is_call_tools_node(node):
                    logger.info("Processing tool calls …")
                    try:
                        async with (
                            asyncio.timeout(TOOL_CALL_TIMEOUT),
                            node.stream(agent_run.ctx) as stream,
                        ):
                            async for event in stream:
                                if isinstance(event, FunctionToolCallEvent):
                                    tool_name = event.part.tool_name
                                    tool_args = event.part.args
                                    call_id = event.part.tool_call_id
                                    if isinstance(tool_args, str):
                                        try:
                                            tool_args = json.loads(tool_args)
                                        except (json.JSONDecodeError, TypeError):
                                            pass

                                    logger.info(
                                        "Tool call [%s]: %s(%s)", call_id, tool_name, tool_args
                                    )
                                    if isinstance(tool_args, dict):
                                        for k, v in tool_args.items():
                                            vlen = len(str(v)) if v else 0
                                            logger.info(
                                                "  arg %s: %s (%d chars)", k, type(v).__name__, vlen
                                            )
                                    if tool_name == "load_model" and not tool_args:
                                        logger.warning(
                                            "load_model called with EMPTY args — model failed to compose YAML"
                                        )

                                    step = cl.Step(
                                        name=tool_name, type="tool", parent_id=_run_step_id
                                    )
                                    step.input = (
                                        json.dumps(tool_args, indent=2)
                                        if isinstance(tool_args, dict)
                                        else str(tool_args)
                                    )
                                    await step.send()
                                    tool_steps[call_id] = step
                                    # Sampling that fires while this tool is in flight should
                                    # nest under it (the server triggers sampling from inside
                                    # tool execution).
                                    cl.user_session.set("active_tool_step_id", step.id)

                                elif isinstance(event, FunctionToolResultEvent):
                                    result_text, result_binaries = _split_tool_content(
                                        event.result.content
                                    )
                                    result_content = result_text or str(event.result.content)
                                    call_id = event.result.tool_call_id
                                    logger.info(
                                        "Tool result [%s] (%d chars): %s → %s",
                                        call_id,
                                        len(result_content),
                                        event.result.tool_name,
                                        result_content[:200],
                                    )

                                    if any(
                                        phrase in result_content for phrase in _MCP_ERROR_PHRASES
                                    ):
                                        logger.warning(
                                            "MCP session error detected in tool result — will reconnect: %s",
                                            result_content[:200],
                                        )
                                        step = tool_steps.pop(call_id, None)
                                        if step:
                                            step.output = result_content
                                            await step.update()
                                        for cid, s in list(tool_steps.items()):
                                            s.output = "Cancelled (session lost)"
                                            await s.update()
                                        tool_steps.clear()
                                        needs_reconnect = True
                                        break

                                    step = tool_steps.pop(call_id, None)
                                    cl.user_session.set("active_tool_step_id", None)
                                    if step:
                                        display_text = result_text or (
                                            "(image)" if result_binaries else ""
                                        )
                                        if len(display_text) > STEP_OUTPUT_LIMIT:
                                            step.output = (
                                                display_text[:STEP_OUTPUT_LIMIT]
                                                + f"\n\n… (truncated — {len(display_text):,} chars total)"
                                            )
                                        else:
                                            step.output = display_text
                                        await step.update()

                                    for binary in result_binaries:
                                        if binary.is_image:
                                            fallback_images.append(
                                                cl.Image(
                                                    name="chart",
                                                    content=binary.data,
                                                    display="inline",
                                                    mime=binary.media_type,
                                                )
                                            )
                    except TimeoutError:
                        logger.warning("Tool call timed out after %ds", TOOL_CALL_TIMEOUT)
                        for call_id, step in list(tool_steps.items()):
                            step.output = "Timed out"
                            await step.update()
                        tool_steps.clear()
                        needs_reconnect = True
                        break
                    except Exception as tool_err:
                        logger.warning("Tool execution error: %s", tool_err)
                        for call_id, step in list(tool_steps.items()):
                            step.output = f"Error: {tool_err}"
                            await step.update()
                        tool_steps.clear()

                        if _is_mcp_session_error(tool_err):
                            needs_reconnect = True
                        else:
                            text_parts.append(f"\n\nTool error: {tool_err}")
                        break
                    logger.info("Tool calls complete.")

                if needs_reconnect:
                    break

            # Capture full message history while the run context is still open.
            # Even when the run didn't complete (tool error → break), preserve
            # the partial history so the model retains context on the next turn.
            try:
                result_messages = agent_run.all_messages()
                if agent_run.result is not None:
                    logger.info(
                        "Agent run finished — %d messages in history.", len(result_messages)
                    )
                else:
                    logger.info(
                        "Agent run incomplete — preserving %d messages in history.",
                        len(result_messages),
                    )
            except Exception:
                logger.warning("Agent run ended without recoverable history.")

        logger.info(
            "Agent context closed. needs_reconnect=%s, _retried=%s", needs_reconnect, _retried
        )

        if needs_reconnect:
            logger.info("Triggering full MCP reconnection …")
            reconnected = await _full_reconnect_mcp()
            logger.info("Reconnection result: %s", reconnected)
            if reconnected and not _retried:
                logger.info("Retrying user message after reconnection …")
                await on_message(message, _retried=True)
                return

        # ── Chart rendering (before response) ──────────────────
        if result_messages and not needs_reconnect:
            current_agent = cl.user_session.get("agent") or agent
            mcp_servers = [s for s in current_agent.toolsets if hasattr(s, "read_resource")]
            logger.info(
                "Chart scan: %d messages, %d MCP servers with read_resource",
                len(result_messages),
                len(mcp_servers),
            )
            for msg in result_messages:
                for part in getattr(msg, "parts", []):
                    if type(part).__name__ == "ToolReturnPart":
                        raw = getattr(part, "content", "")
                        content, _ = _split_tool_content(raw)
                        logger.info(
                            "ToolReturnPart [%s] (%d chars): %.200s",
                            getattr(part, "tool_name", "?"),
                            len(content),
                            content[:200],
                        )
                        if UI_URI_PATTERN.search(content):
                            for server in mcp_servers:
                                chart_el = await render_chart_if_present(content, server)
                                if chart_el:
                                    chart_elements.append(chart_el)
                                    break
                            else:
                                # All servers failed — likely stale session
                                if not _retried:
                                    logger.warning(
                                        "Chart rendering failed on all servers — reconnecting and retrying"
                                    )
                                    if await _reconnect_mcp():
                                        await on_message(message, _retried=True)
                                        return

        if not chart_elements and fallback_images:
            chart_elements = fallback_images

        # Send the response message AFTER all steps so it appears below them
        response_msg.content = "".join(text_parts)

        # Attach downloadable files from code blocks in the response
        download_elements = extract_downloads_from_response(response_msg.content)
        if result_messages:
            download_elements.extend(extract_downloads_from_tool_results(result_messages))
        if download_elements:
            # Deduplicate by content (code block and tool result may overlap)
            seen_content: set[bytes] = set()
            unique = []
            for el in download_elements:
                key = el.content if isinstance(el.content, bytes) else (el.content or "").encode()
                if key not in seen_content:
                    seen_content.add(key)
                    unique.append(el)
            response_msg.elements = unique

        await response_msg.send()
        response_sent = True
        logger.debug("Response message sent.")

        # Save message history for next turn — skip when the run was
        # interrupted by a session error to avoid persisting incomplete
        # tool calls that would block the next turn.
        if result_messages is not None and not needs_reconnect:
            cl.user_session.set("pydantic_history", result_messages)

        if chart_elements:
            logger.info("Sending %d chart elements", len(chart_elements))
            await cl.Message(
                content="",
                elements=chart_elements,
            ).send()

        # ── Mermaid diagram rendering ──────────────────────────────
        # If tool results contain Mermaid syntax and the LLM response
        # doesn't already include a mermaid code block, send it so
        # the client-side Mermaid.js renderer picks it up.
        if result_messages and "```mermaid" not in response_msg.content:
            for diagram in extract_mermaid_from_tool_results(result_messages):
                logger.info("Sending Mermaid diagram (%d chars)", len(diagram))
                await cl.Message(content=f"```mermaid\n{diagram}\n```").send()

    except BaseException as e:
        # BaseException catches asyncio.CancelledError (Python 3.9+)
        # which Chainlit may raise on WebSocket disconnect / timeout.
        if isinstance(e, KeyboardInterrupt | SystemExit):
            raise

        provider_msg = _format_provider_error(e)

        if provider_msg is not None:
            # Expected provider failure (402, 429, stream-level error) — short log, no traceback.
            logger.warning("Provider error: %s", str(e).strip() or type(e).__name__)
        else:
            logger.exception("Error in message handler")

        try:
            if provider_msg is None and _is_mcp_session_error(e):
                if await _reconnect_mcp() and not _retried:
                    await on_message(message, _retried=True)
                    return
            else:
                content = provider_msg if provider_msg else f"Error: {e}"
                cl.user_session.set("retry_content", message.content)
                await cl.Message(
                    content=content,
                    author="System",
                    actions=[cl.Action(name="retry_message", payload={}, label="Retry")],
                ).send()
        except Exception:
            pass  # UI may already be gone
    finally:
        # Ensure the response message is sent even on error so the UI
        # never shows a permanent "loading" state.
        try:
            if not response_sent:
                response_msg.content = "".join(text_parts) if text_parts else ""
                await response_msg.send()
        except Exception:
            pass
