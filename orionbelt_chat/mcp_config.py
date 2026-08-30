"""Declarative MCP server configuration.

The app shipped with exactly two servers welded into the source, so pointing it
at a third — someone else's MCP server, or your own — meant editing Python. This
module reads a YAML file instead, and keeps the two environment variables
working so existing deployments do not have to change anything.

The file looks like::

    servers:
      # A remote server over Streamable HTTP.
      - name: Weather
        endpoint: https://weather.example.com/mcp
        headers:
          Authorization: Bearer ${WEATHER_API_KEY}

      # A local Python module, launched the way the OrionBelt servers are.
      - name: My Analytics
        endpoint: ../my-analytics
        module: my_analytics

      # Anything else, launched however you like over stdio.
      - name: Filesystem
        command: npx
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/data"]
        env:
          LOG_LEVEL: debug
        sampling: false

``${VAR}`` in an ``endpoint``, ``headers``, ``command``, ``args`` or ``env``
value is replaced by that environment variable (or the matching line in
``.env``), so an API key can be named here without being stored here.

Servers from the file are *added* to whatever the ``ANALYTICS_SERVER_DIR`` and
``SEMANTIC_LAYER_SERVER_DIR`` variables define, so the common case — "I want one
more server" — is a file with one entry. A file entry that reuses a built-in
name replaces it, which is how you repoint one; to switch a built-in off,
unset its environment variable.
"""

from __future__ import annotations

import functools
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

from .settings import settings

logger = logging.getLogger(__name__)

#: Looked for, in order, when MCP_SERVERS_FILE is not set.
DEFAULT_FILENAME = "mcp_servers.yaml"


class McpConfigError(Exception):
    """Raised for a malformed server configuration."""


# Callers ask for servers, sampling flags and errors separately, so the config
# is read several times per session. The file is small enough that re-reading it
# keeps edits live without a restart; only the logging needs to be quietened.
_logged: set[tuple[int, str]] = set()


def _log_once(level: int, message: str) -> None:
    key = (level, message)
    if key not in _logged:
        _logged.add(key)
        logger.log(level, message)


@dataclass(frozen=True)
class ServerDef:
    """One configured MCP server, transport-agnostic."""

    name: str
    endpoint: str = ""
    module: str = ""
    command: str = ""
    args: tuple[str, ...] = ()
    headers: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    sampling: bool = False

    @property
    def is_url(self) -> bool:
        return self.endpoint.startswith("http://") or self.endpoint.startswith("https://")


# The two servers this app was built for. Kept as data rather than as branches
# so they travel the same code path as anything a user adds.
BUILTIN_DEFS: tuple[tuple[str, str, str, bool], ...] = (
    ("OrionBelt Analytics", "analytics_server_dir", "orionbelt_analytics", True),
    ("OrionBelt Semantic Layer", "semantic_layer_server_dir", "orionbelt_semantic_layer", False),
)


def _builtin_defs() -> list[ServerDef]:
    """Server definitions from the two long-standing environment variables."""
    defs = []
    for name, attr, module, sampling in BUILTIN_DEFS:
        endpoint = getattr(settings, attr, "") or ""
        if endpoint:
            defs.append(ServerDef(name=name, endpoint=endpoint, module=module, sampling=sampling))
    return defs


def _app_root() -> Path:
    """Mirror the app root the CLI seeds, so a config can live beside it."""
    for var in ("CHAINLIT_APP_ROOT", "ORIONBELT_CHAT_HOME"):
        value = os.environ.get(var, "").strip()
        if value:
            return Path(value).expanduser()
    return Path.home() / ".orionbelt-chat"


def config_path() -> Path | None:
    """Return the servers file to read, or None if there isn't one.

    An explicitly configured path is returned even when it does not exist, so a
    typo surfaces as an error rather than as silence.
    """
    configured = settings.mcp_servers_file.strip()
    if configured:
        return Path(configured).expanduser()
    for candidate in (Path.cwd() / DEFAULT_FILENAME, _app_root() / DEFAULT_FILENAME):
        if candidate.is_file():
            return candidate
    return None


def _require_str(entry: dict[str, Any], key: str, where: str) -> str:
    value = entry.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise McpConfigError(f"{where}: '{key}' must be a string, got {type(value).__name__}")
    return value.strip()


#: `${VAR}` anywhere in a value is replaced by that environment variable.
_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _env_lookup() -> dict[str, str]:
    """Where `${VAR}` looks: the process environment, then the `.env` file.

    Settings reads `.env` through pydantic-settings, which parses the file
    itself rather than exporting it to `os.environ` — so a key that already
    works for the LLM providers would otherwise not resolve here. Reading both
    keeps one place for secrets.
    """
    values = {k: v for k, v in dotenv_values(".env").items() if v is not None}
    values.update(os.environ)
    return values


def _expand(
    value: str,
    lookup: Callable[[], dict[str, str]],
    where: str,
    name: str,
    key: str,
) -> str:
    """Substitute `${VAR}` so an API key stays out of the config file.

    An unset variable is an error rather than an empty string: a server that
    connects without its credentials fails later, somewhere far less obvious
    than the file that forgot to name the variable.

    `lookup` is deferred because the overwhelming majority of values hold no
    `${...}` at all, and the config is re-read several times per session.
    """
    if "${" not in value:
        return value

    missing: list[str] = []
    env = lookup()

    def replace(match: re.Match[str]) -> str:
        var = match.group(1)
        if var not in env:
            missing.append(var)
            return ""
        return env[var]

    expanded = _VAR_PATTERN.sub(replace, value)
    if missing:
        raise McpConfigError(
            f"{where} ({name}): '{key}' uses unset environment "
            f"variable(s): {', '.join(sorted(set(missing)))}"
        )
    return expanded


def parse_server(entry: Any, where: str) -> ServerDef:
    """Validate one entry from the ``servers:`` list.

    Rejects the ambiguous combinations rather than guessing, because a server
    that silently starts with the wrong transport is far harder to diagnose
    than one that refuses to load.
    """
    if not isinstance(entry, dict):
        raise McpConfigError(f"{where}: each server must be a mapping, got {type(entry).__name__}")

    name = _require_str(entry, "name", where)
    if not name:
        raise McpConfigError(f"{where}: 'name' is required")

    lookup = functools.cache(_env_lookup)
    endpoint = _expand(_require_str(entry, "endpoint", where), lookup, where, name, "endpoint")
    module = _require_str(entry, "module", where)
    command = _expand(_require_str(entry, "command", where), lookup, where, name, "command")

    if not endpoint and not command:
        raise McpConfigError(f"{where} ({name}): needs either 'endpoint' or 'command'")
    if endpoint and command:
        raise McpConfigError(f"{where} ({name}): set 'endpoint' or 'command', not both")

    defn = ServerDef(
        name=name,
        endpoint=endpoint,
        module=module,
        command=command,
        args=tuple(
            _expand(arg, lookup, where, name, "args")
            for arg in _parse_args(entry.get("args"), where, name)
        ),
        headers={
            key: _expand(value, lookup, where, name, f"headers.{key}")
            for key, value in _parse_string_mapping(
                entry.get("headers"), "headers", where, name
            ).items()
        },
        env={
            key: _expand(value, lookup, where, name, f"env.{key}")
            for key, value in _parse_string_mapping(entry.get("env"), "env", where, name).items()
        },
        sampling=_parse_bool(entry.get("sampling"), where, name),
    )

    if command and module:
        raise McpConfigError(f"{where} ({name}): 'module' does not apply to a 'command' server")
    if defn.headers and not defn.is_url:
        raise McpConfigError(f"{where} ({name}): 'headers' only applies to an HTTP endpoint")
    if endpoint and defn.args:
        raise McpConfigError(f"{where} ({name}): 'args' only applies to a 'command' server")
    if endpoint and not defn.is_url and not module:
        raise McpConfigError(
            f"{where} ({name}): a directory endpoint needs 'module' "
            f"(the Python module to run), or use 'command' instead"
        )
    if defn.is_url and module:
        raise McpConfigError(f"{where} ({name}): 'module' does not apply to an HTTP endpoint")
    return defn


def _parse_args(raw: Any, where: str, name: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(a, str) for a in raw):
        raise McpConfigError(f"{where} ({name}): 'args' must be a list of strings")
    return list(raw)


#: Accepted spellings for a YAML scalar that a template tool may have quoted.
_TRUE = frozenset({"true", "yes", "on", "1"})
_FALSE = frozenset({"false", "no", "off", "0"})


def _parse_bool(raw: Any, where: str, name: str) -> bool:
    """Parse `sampling:` without letting a quoted "false" mean True.

    `bool("false")` is True, so a template tool that quotes YAML scalars could
    silently grant a server access to the LLM. Anything unrecognised is refused
    rather than coerced.
    """
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    raise McpConfigError(f"{where} ({name}): 'sampling' must be true or false, got {raw!r}")


def _parse_string_mapping(raw: Any, key: str, where: str, name: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise McpConfigError(f"{where} ({name}): '{key}' must be a mapping")
    return {str(k): str(v) for k, v in raw.items()}


def _server_entries(path: Path) -> list[Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise McpConfigError(f"{path}: no such file") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise McpConfigError(f"{path}: {exc}") from exc

    if raw is None:
        return []
    if not isinstance(raw, dict) or "servers" not in raw:
        raise McpConfigError(f"{path}: expected a top-level 'servers:' list")
    entries = raw["servers"]
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise McpConfigError(f"{path}: 'servers' must be a list")
    return entries


def _parse_server_entries(
    entries: list[Any], path: Path, *, strict: bool
) -> tuple[list[ServerDef], list[str]]:
    defs: list[ServerDef] = []
    errors: list[str] = []
    seen: set[str] = set()

    for i, entry in enumerate(entries):
        try:
            defn = parse_server(entry, f"{path}[{i}]")
            if defn.name in seen:
                raise McpConfigError(f"{path}[{i}] ({defn.name}): duplicate server name")
            seen.add(defn.name)
            defs.append(defn)
        except McpConfigError as exc:
            if strict:
                raise
            errors.append(str(exc))

    return defs, errors


def load_from_file(path: Path) -> list[ServerDef]:
    """Read and validate a servers file. Raises McpConfigError on any problem."""
    defs, _errors = _parse_server_entries(_server_entries(path), path, strict=True)
    return defs


def _load_from_file_tolerant(path: Path) -> tuple[list[ServerDef], list[str]]:
    """Read a servers file, keeping valid entries and returning per-entry errors."""
    return _parse_server_entries(_server_entries(path), path, strict=False)


def load_server_defs() -> tuple[list[ServerDef], list[str]]:
    """Return every configured server, plus any errors worth showing the user.

    Errors are returned rather than raised: a broken config file should not stop
    the built-in servers from connecting, but it must not pass unnoticed either
    — the caller surfaces these in the UI.
    """
    defs = _builtin_defs()
    errors: list[str] = []

    path = config_path()
    if path is None:
        return defs, errors

    try:
        extra, entry_errors = _load_from_file_tolerant(path)
    except McpConfigError as exc:
        _log_once(logging.ERROR, f"MCP server config ignored — {exc}")
        return defs, [str(exc)]

    for problem in entry_errors:
        _log_once(logging.ERROR, f"MCP server config entry ignored — {problem}")
    errors.extend(entry_errors)

    # A file entry with a built-in's name replaces it, so a user can override
    # or disable one without editing their environment.
    by_name = {defn.name: defn for defn in defs}
    for defn in extra:
        if defn.name in by_name:
            _log_once(logging.INFO, f"MCP server '{defn.name}' overridden by {path}")
        by_name[defn.name] = defn

    _log_once(logging.INFO, f"Loaded {len(extra)} MCP server definition(s) from {path}")
    return list(by_name.values()), errors
