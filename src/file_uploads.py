"""Session-scoped registry for user-uploaded files, with MCP argument injection.

Users drop an OBSL/OBML semantic model (YAML/JSON) or an RDF ontology (Turtle)
into the chat and then ask the assistant to load it.  Pushing the whole file
through the LLM would be wasteful and unreliable: a 200 KB model costs tens of
thousands of tokens on the way in, and the model has to reproduce it verbatim
on the way out — where ``max_tokens`` truncates it into empty tool arguments.

So the file never goes through the model.  It is stored in the Chainlit user
session under a short handle (``@upload:model.yaml``); the model sees only a
notice with the file's kind, size and a short preview.  When it passes the
handle as a tool argument, :func:`substitute_handles` — reached through the
:func:`process_tool_call` hook that :mod:`src.mcp_servers` installs on every
MCP toolset — swaps in the real content just before the request leaves for the
MCP server.

Small files (under :data:`INLINE_THRESHOLD` chars) are additionally inlined in
the notice, so the model can reason about them directly without a tool call.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import chainlit as cl
import yaml
from pydantic_ai import ModelRetry

logger = logging.getLogger(__name__)

# Prefix the model writes to refer to an uploaded file in a tool argument.
HANDLE_PREFIX = "@upload:"

# Suffix that asks for the file as JSON instead of verbatim.  Needed because
# the Semantic Layer's `load_model(model=…)` runs `json.loads` on a string
# argument, so a native OBML *YAML* file has no route without conversion.
JSON_MODIFIER = "#json"

# Handle names are sanitised to this character class on registration, so the
# placeholder can be recognised inside a larger string without ambiguity.
_HANDLE_RE = re.compile(
    rf"{re.escape(HANDLE_PREFIX)}([A-Za-z0-9._-]+)({re.escape(JSON_MODIFIER)})?"
)
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

# Chainlit's own limit is 500 MB (see .chainlit/config.toml), which is meant
# for binary attachments.  Text we intend to hand to an MCP server as a single
# tool argument needs a far tighter bound.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_UPLOAD_MB = MAX_UPLOAD_BYTES // (1024 * 1024)

# File types offered by the explicit upload button.  Chainlit takes either MIME
# types or a ``{mime: [extensions]}`` mapping; the mapping form is used because
# browsers report .ttl and .obml inconsistently.
UPLOAD_ACCEPT: dict[str, list[str]] = {
    "text/plain": [
        ".yaml",
        ".yml",
        ".obml",
        ".obsl",
        ".json",
        ".jsonld",
        ".ttl",
        ".turtle",
        ".n3",
        ".nt",
        ".rdf",
        ".owl",
    ]
}

# Files below this size are quoted in full in the prompt notice — cheap enough
# that the model may as well see them.
INLINE_THRESHOLD = 4_000

# Preview shown for files too large to inline.
PREVIEW_LINES = 30
PREVIEW_CHARS = 1_500

_SESSION_KEY = "uploaded_files"
_PENDING_KEY = "pending_upload_notices"


@dataclass(frozen=True)
class UploadedFile:
    """One file the user dropped into the chat, held for the whole session."""

    handle_name: str
    """Sanitised name used in the handle, e.g. ``model.yaml``."""

    original_name: str
    """Name as uploaded, used when a tool wants the original filename."""

    kind: str
    """Human-readable description of the format, shown to the model."""

    content: str
    """Decoded file text."""

    @property
    def handle(self) -> str:
        """The placeholder the model writes to reference this file."""
        return f"{HANDLE_PREFIX}{self.handle_name}"

    @property
    def size_label(self) -> str:
        size = len(self.content.encode("utf-8"))
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size / (1024 * 1024):.1f} MB"

    @property
    def is_inlinable(self) -> bool:
        return len(self.content) <= INLINE_THRESHOLD

    def preview(self) -> str:
        """First few lines of the file, for the prompt notice."""
        lines = self.content.splitlines()[:PREVIEW_LINES]
        text = "\n".join(lines)
        if len(text) > PREVIEW_CHARS:
            text = text[:PREVIEW_CHARS]
        if len(text) < len(self.content):
            text += "\n…"
        return text


# ── Format detection ───────────────────────────────────────────────────────

_KIND_OBML = "OBSL/OBML semantic model (YAML)"
_KIND_OSI = "OSI semantic model (YAML)"
_KIND_YAML = "YAML document"
_KIND_OBML_JSON = "OBSL/OBML semantic model (JSON)"
_KIND_JSON = "JSON document"
_KIND_TURTLE = "RDF ontology (Turtle)"
_KIND_RDFXML = "RDF ontology (RDF/XML)"
_KIND_SPARQL = "SPARQL query"
_KIND_SQL = "SQL script"
_KIND_CSV = "CSV data"
_KIND_TEXT = "text file"

_EXTENSION_KINDS: dict[str, str] = {
    ".yaml": _KIND_YAML,
    ".yml": _KIND_YAML,
    ".obml": _KIND_OBML,
    ".obsl": _KIND_OBML,
    ".json": _KIND_JSON,
    ".jsonld": _KIND_JSON,
    ".ttl": _KIND_TURTLE,
    ".turtle": _KIND_TURTLE,
    ".n3": _KIND_TURTLE,
    ".nt": _KIND_TURTLE,
    ".rdf": _KIND_RDFXML,
    ".owl": _KIND_RDFXML,
    ".sparql": _KIND_SPARQL,
    ".rq": _KIND_SPARQL,
    ".sql": _KIND_SQL,
    ".csv": _KIND_CSV,
}

# Top-level OBML keys — enough of them present means this is a semantic model
# rather than an arbitrary YAML document.
_OBML_KEYS = ("dataObjects", "dimensions", "measures", "metrics")

# OSI (Open Semantic Interchange) markers.  The Semantic Layer converts OSI to
# OBML server-side via a different tool argument, so the two are worth telling
# apart in the notice.
_OSI_KEYS = ("semantic_model:", "osi_version:", "openSemanticInterchange")


def detect_kind(name: str, content: str) -> str:
    """Describe a file's format from its extension, refined by its content.

    The extension decides the family; the content distinguishes members of it
    (an OBML model from a plain YAML document, Turtle from RDF/XML) so a file
    named ``export.yaml`` is still recognised as a semantic model.
    """
    ext = Path(name).suffix.lower()
    kind = _EXTENSION_KINDS.get(ext, _KIND_TEXT)
    stripped = content.lstrip()

    if kind in (_KIND_YAML, _KIND_TEXT):
        if any(marker in content for marker in _OSI_KEYS):
            return _KIND_OSI
        if sum(f"{key}:" in content for key in _OBML_KEYS) >= 2:
            return _KIND_OBML
    if kind == _KIND_JSON and sum(f'"{key}"' in content for key in _OBML_KEYS) >= 2:
        return _KIND_OBML_JSON
    if kind == _KIND_TEXT:
        if stripped.startswith(("@prefix", "@base")):
            return _KIND_TURTLE
        if stripped.startswith("<?xml"):
            return _KIND_RDFXML
    return kind


# ── Registration ───────────────────────────────────────────────────────────


def _sanitize(name: str) -> str:
    """Reduce an uploaded filename to the handle character class."""
    cleaned = _UNSAFE_NAME_CHARS.sub("_", Path(name).name).strip("_")
    return cleaned or "upload"


def _allocate_handle_name(name: str, registry: dict[str, UploadedFile]) -> str:
    """Pick a free handle for *name*, keeping distinct files distinguishable.

    Sanitising is lossy — ``my model.yaml`` and ``my@model.yaml`` both reduce to
    ``my_model.yaml`` — so a raw sanitised name could silently rebind an
    existing handle to a different file.  Re-uploading the *same* filename
    still reuses its handle (that is the intended "newest version wins"), but a
    genuinely different file gets a suffixed handle instead.
    """
    base = _sanitize(name)
    existing = registry.get(base)
    if existing is None or existing.original_name == name:
        return base

    stem, dot, suffix = base.partition(".")
    for counter in range(2, len(registry) + 3):
        candidate = f"{stem}_{counter}{dot}{suffix}"
        taken = registry.get(candidate)
        if taken is None or taken.original_name == name:
            return candidate
    raise AssertionError("unreachable: the loop bound exceeds the registry size")


def _read_element(element: Any) -> str | None:
    """Decode an uploaded element's text, or None if it is not usable text.

    Chainlit gives file elements either a server-side ``path`` or, for some
    transports, in-memory ``content``.
    """
    name = getattr(element, "name", "?")
    raw = getattr(element, "content", None)
    if raw is None:
        path = getattr(element, "path", None)
        if not path:
            return None
        try:
            # Size-check before reading: Chainlit accepts attachments up to
            # 500 MB, and slurping one of those into memory to reject it would
            # be its own problem.
            if Path(path).stat().st_size > MAX_UPLOAD_BYTES:
                logger.warning(
                    "Uploaded file %s is over the %d byte limit; ignored.", name, MAX_UPLOAD_BYTES
                )
                return None
            raw = Path(path).read_bytes()
        except OSError as exc:
            logger.warning("Could not read uploaded file %s: %s", path, exc)
            return None

    if not isinstance(raw, (str, bytes)):
        return None

    # Measure in bytes either way: the limit bounds what we may hand an MCP
    # server as one tool argument, and an in-memory str is no cheaper than the
    # same file read off disk.
    size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
    if size > MAX_UPLOAD_BYTES:
        logger.warning(
            "Uploaded file %s is %d bytes — over the %d byte limit; ignored.",
            name,
            size,
            MAX_UPLOAD_BYTES,
        )
        return None

    if isinstance(raw, str):
        return raw
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        logger.info("Uploaded file %s is not UTF-8 text — ignored.", name)
        return None


def register_uploads(elements: list[Any] | None) -> list[UploadedFile]:
    """Store text files and queue them to be announced to the model.

    Accepts anything with ``name`` plus ``path`` or ``content`` — both Chainlit
    message elements and the ``AskFileResponse`` objects the upload button
    returns.  Binary attachments (images and the like) are skipped: they are
    handled by Chainlit's own element rendering, not by this registry.
    Re-uploading a name replaces the previous entry, so the handle always means
    the newest version of that file.

    The returned files are also queued for :func:`drain_pending_uploads`, so a
    file uploaded outside a chat turn — via the upload button — still gets
    announced on the next message the user sends.
    """
    if not elements:
        return []

    registry = _registry()
    added: list[UploadedFile] = []
    for element in elements:
        name = getattr(element, "name", None)
        if not name:
            continue
        content = _read_element(element)
        if content is None or not content.strip():
            continue

        upload = UploadedFile(
            handle_name=_allocate_handle_name(name, registry),
            original_name=name,
            kind=detect_kind(name, content),
            content=content,
        )
        registry[upload.handle_name] = upload
        added.append(upload)
        logger.info(
            "Registered upload %s (%s, %s) as %s",
            upload.original_name,
            upload.kind,
            upload.size_label,
            upload.handle,
        )

    if added:
        cl.user_session.set(_SESSION_KEY, registry)
        # Re-uploading a name supersedes its queued entry, so the notice
        # describes the newest version once rather than both versions.
        superseded = {u.handle_name for u in added}
        pending = [u for u in _pending() if u.handle_name not in superseded] + added
        cl.user_session.set(_PENDING_KEY, pending)
    return added


def drain_pending_uploads() -> list[UploadedFile]:
    """Return uploads not yet announced to the model, clearing the queue."""
    pending = _pending()
    if pending:
        cl.user_session.set(_PENDING_KEY, [])
    return pending


def _registry() -> dict[str, UploadedFile]:
    """The session's handle → file mapping, empty outside a Chainlit session."""
    try:
        return cl.user_session.get(_SESSION_KEY) or {}
    except Exception:  # pragma: no cover - no active session (e.g. in tests)
        return {}


def _pending() -> list[UploadedFile]:
    """Uploads registered but not yet described to the model."""
    try:
        return list(cl.user_session.get(_PENDING_KEY) or [])
    except Exception:  # pragma: no cover - no active session (e.g. in tests)
        return []


def session_uploads() -> dict[str, UploadedFile]:
    """Public read-only view of the session registry."""
    return dict(_registry())


# ── Prompt notice ──────────────────────────────────────────────────────────


def build_upload_notice(uploads: list[UploadedFile]) -> str:
    """Describe freshly uploaded files for the model.

    Appended to the user's message rather than the system prompt, so the notice
    stays attached to the turn the file arrived in and the model can see which
    request it belongs to.
    """
    if not uploads:
        return ""

    blocks = ["## Uploaded files"]
    for upload in uploads:
        lines = [
            f"- **{upload.original_name}** — {upload.kind}, {upload.size_label}",
            f"  - Handle: `{upload.handle}`",
        ]
        if upload.is_inlinable:
            lines.append(f"  - Full content:\n\n```\n{upload.content.rstrip()}\n```")
        else:
            lines.append(
                f"  - Preview (first {PREVIEW_LINES} lines):\n\n```\n{upload.preview()}\n```"
            )
        blocks.append("\n".join(lines))

    blocks.append(
        "Pass the handle **verbatim** as the tool argument that expects the file's "
        'content (for example `load_my_ontology(ontology_content="@upload:schema.ttl")`) — '
        "it is replaced with the full file before the tool runs. Never retype or "
        "summarise the content yourself, and never pass only the preview.\n\n"
        f"When the argument expects a JSON object rather than raw text (such as the "
        f"Semantic Layer's `load_model(model=…)`), append `{JSON_MODIFIER}` to the handle — "
        f'`load_model(model="@upload:model.yaml{JSON_MODIFIER}")` — and the file is '
        "converted from YAML to JSON on the way through."
    )
    return "\n\n".join(blocks)


def augment_message(content: str, uploads: list[UploadedFile]) -> str:
    """Return the user message with the upload notice appended."""
    notice = build_upload_notice(uploads)
    if not notice:
        return content
    return f"{content}\n\n{notice}" if content.strip() else notice


# ── Handle substitution ────────────────────────────────────────────────────


def _resolve(handle_name: str, registry: dict[str, UploadedFile]) -> UploadedFile | None:
    """Look up a handle, tolerating the near-misses models actually produce."""
    if handle_name in registry:
        return registry[handle_name]

    lowered = handle_name.lower()
    for key, upload in registry.items():
        if key.lower() == lowered:
            return upload

    # The model dropped the extension (`@upload:model` for `model.yaml`).
    # Only accept it when exactly one file matches, so we never guess wrong.
    stem_matches = [u for k, u in registry.items() if Path(k).stem.lower() == lowered]
    if len(stem_matches) == 1:
        return stem_matches[0]
    return None


def _as_json(upload: UploadedFile) -> str:
    """Render an uploaded document as a JSON string.

    Tool arguments that hold a model object — notably the Semantic Layer's
    ``load_model(model=…)``, which runs ``json.loads`` on a string — cannot take
    YAML.  ``yaml.safe_load`` parses JSON too (JSON is a subset of YAML), so one
    path covers both source formats.

    Raises:
        ModelRetry: if the file does not parse, naming the file so the model
            reports the problem instead of sending unusable content.
    """
    try:
        parsed = yaml.safe_load(upload.content)
    except yaml.YAMLError as exc:
        raise ModelRetry(
            f"{upload.original_name} could not be parsed as YAML or JSON ({exc}). "
            f"Tell the user the file is malformed rather than retrying."
        ) from exc
    if not isinstance(parsed, (dict, list)):
        raise ModelRetry(
            f"{upload.original_name} does not contain a JSON object or array, so it "
            f"cannot be passed as a structured model argument. Use the plain "
            f"`{upload.handle}` handle if the tool expects raw text."
        )
    return json.dumps(parsed)


def substitute_handles(value: Any, registry: dict[str, UploadedFile]) -> Any:
    """Recursively replace ``@upload:`` handles in a tool-argument structure.

    Raises:
        ModelRetry: if a handle names a file that was never uploaded, so the
            model is told about it and can correct the call instead of the MCP
            server receiving the literal placeholder as its payload.
    """
    if isinstance(value, str):
        if HANDLE_PREFIX not in value:
            return value

        unknown: list[str] = []

        def replace(match: re.Match[str]) -> str:
            upload = _resolve(match.group(1), registry)
            if upload is None:
                unknown.append(match.group(1))
                return match.group(0)
            return _as_json(upload) if match.group(2) else upload.content

        substituted = _HANDLE_RE.sub(replace, value)
        if unknown:
            available = ", ".join(f"{HANDLE_PREFIX}{name}" for name in registry) or "none"
            raise ModelRetry(
                f"No uploaded file matches {', '.join(HANDLE_PREFIX + n for n in unknown)}. "
                f"Available uploads: {available}. Ask the user to upload the file again if "
                f"it is missing."
            )
        return substituted
    if isinstance(value, dict):
        return {key: substitute_handles(item, registry) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute_handles(item, registry) for item in value]
    return value


async def process_tool_call(ctx: Any, call_tool: Any, name: str, tool_args: dict[str, Any]) -> Any:
    """pydantic-ai ``process_tool_call`` hook: inject uploads, then call the tool.

    Wired into every MCP toolset in :mod:`src.mcp_servers`, so any tool
    argument on any server can carry a handle.
    """
    registry = _registry()
    if registry:
        substituted = substitute_handles(tool_args, registry)
        if substituted != tool_args:
            logger.info("Substituted uploaded file content into %s arguments", name)
            tool_args = substituted
    return await call_tool(name, tool_args)
