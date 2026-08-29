"""MCP server configuration and lifecycle management."""

import logging
from typing import Any

from pydantic_ai.mcp import MCPToolset, StdioTransport, StreamableHttpTransport

from .file_uploads import process_tool_call
from .mcp_config import ServerDef, load_server_defs
from .mcp_sampling import enable_sampling_tools
from .providers import default_model_for, resolve_model
from .settings import settings

logger = logging.getLogger(__name__)


def _is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _resolve_sampling_model():
    """Resolve the env-configured default model used to answer MCP sampling calls."""
    if not settings.mcp_allow_sampling:
        logger.info("MCP sampling disabled via MCP_ALLOW_SAMPLING=false")
        return None
    provider = settings.default_provider
    if not provider:
        return None
    model_name = default_model_for(provider)
    if not model_name:
        return None
    try:
        model = resolve_model(provider, model_name)
    except ValueError as e:
        logger.warning("MCP sampling disabled — could not resolve default model: %s", e)
        return None
    logger.info("MCP sampling model: %s/%s", provider, model_name)
    return model


def get_sampling_model_label() -> str | None:
    """Return a human-readable label for the sampling model, or None if disabled."""
    if _resolve_sampling_model() is None:
        return None
    provider = settings.default_provider
    return f"{provider}/{default_model_for(provider)}"


def _transport(defn: ServerDef) -> StreamableHttpTransport | StdioTransport:
    """Pick the transport a server definition describes."""
    if defn.command:
        return StdioTransport(
            command=defn.command,
            args=list(defn.args),
            env=defn.env or None,
        )
    if defn.is_url:
        return StreamableHttpTransport(url=defn.endpoint)
    # A directory endpoint: run the named module inside that project, which is
    # how the two OrionBelt servers have always been launched.
    return StdioTransport(
        command="uv",
        args=["run", "--directory", defn.endpoint, "python", "-m", defn.module],
        env=defn.env or None,
    )


def _make_server(defn: ServerDef, sampling_model) -> MCPToolset[Any]:
    # Sampling is enabled purely by passing a model: `_resolve_sampling_model`
    # already returns None when MCP_ALLOW_SAMPLING is false, which is what the
    # dropped `allow_sampling=` flag used to express.
    #
    # The per-server half of the same boundary lives here rather than in the
    # caller, so no future caller can hand a model to a server that did not ask
    # for one. A server only gets a sampling route back to the user's LLM if it
    # declares `sampling: true`.
    if not defn.sampling:
        sampling_model = None
    transport = _transport(defn)
    return enable_sampling_tools(
        MCPToolset(
            transport,
            read_timeout=settings.mcp_request_timeout_seconds,
            max_retries=3,
            sampling_model=sampling_model,
            # Expands `@upload:` handles into the uploaded file's content, so a
            # dropped OBSL model or ontology reaches the server in full without
            # ever passing through the model's context.
            process_tool_call=process_tool_call,
        )
    )


def servers_using_sampling() -> frozenset[str]:
    """Names of servers declared to issue MCP sampling/createMessage calls.

    The MCP protocol does not advertise this from the server side, so it stays
    a declaration: built-in for the OrionBelt servers, `sampling: true` for
    anything a user configures.
    """
    defs, _ = load_server_defs()
    return frozenset(defn.name for defn in defs if defn.sampling)


def get_mcp_server_errors() -> list[str]:
    """Configuration problems worth showing the user, empty when all is well."""
    _, errors = load_server_defs()
    return errors


def get_mcp_servers_named() -> list[tuple[str, MCPToolset[Any]]]:
    """Return (display_name, server) pairs for configured MCP servers.

    A server only receives a sampling model if it declares `sampling: true`.
    MCP_ALLOW_SAMPLING remains the global kill switch — this is the per-server
    half of the same boundary, so attaching a third-party server cannot hand it
    a route back to your LLM budget by default.
    """
    sampling_model = _resolve_sampling_model()
    defs, _ = load_server_defs()
    return [
        (defn.name, _make_server(defn, sampling_model if defn.sampling else None)) for defn in defs
    ]
