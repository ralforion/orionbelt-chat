"""MCP elicitation: let a server ask the user for input mid tool call.

Two wire shapes reach the same handler:

- protocol 2025-11-25 and earlier: the server sends `elicitation/create` over
  the open session while its tool call is still running;
- protocol 2026-07-28 (SEP-2322, SEP-2575): the tool call returns an
  `InputRequiredResult` whose `inputRequests` carry the `elicitation/create`,
  and the client retries the call with `inputResponses` plus the echoed
  `requestState`. FastMCP drives that retry loop and hands each embedded
  request to the very same `elicitation_handler`.

So nothing here depends on the negotiated protocol version. What stays ours is
what the spec leaves to the client: rendering the restricted form schema,
validating the answer before it is sent, and handling URL mode safely (show
the full URL, flag look-alike hosts, never open it without consent).

This module holds no UI. `make_elicitation_handler` takes a `prompt` coroutine
that shows a view to the user and returns their answer, so the whole flow runs
under test against a real MCP server without a browser.
"""

import logging
import math
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from fastmcp.client.elicitation import ElicitResult

logger = logging.getLogger(__name__)

Action = Literal["accept", "decline", "cancel"]

# A prompt shows one view and returns `{"action": ..., "content": {...}}`, or
# None when the user never answered (timeout, disconnect).
Prompt = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]

# Invalid submissions are sent back to the user with the errors marked. The
# cap keeps a form the user cannot satisfy from holding the tool call forever.
MAX_FORM_ATTEMPTS = 3

_ACTIONS: frozenset[str] = frozenset({"accept", "decline", "cancel"})
_STRING_FORMATS = frozenset({"email", "uri", "date", "date-time"})
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class UnsupportedSchemaError(ValueError):
    """The requested schema uses JSON Schema beyond the spec's flat subset."""


# ── Form mode: schema → fields ─────────────────────────────


def parse_form_schema(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn a `requestedSchema` into field descriptors a form can render.

    The spec restricts form schemas to a flat object of primitives: string
    (optionally with a format), number/integer, boolean, and single- or
    multi-select enums, each with or without titles. Anything else raises
    `UnsupportedSchemaError` rather than being rendered as something the
    server did not ask for.
    """
    if schema.get("type", "object") != "object":
        raise UnsupportedSchemaError("requestedSchema must be an object")
    required = set(schema.get("required") or [])
    return [
        _parse_field(name, prop, name in required)
        for name, prop in (schema.get("properties") or {}).items()
    ]


def _parse_field(name: str, prop: dict[str, Any], required: bool) -> dict[str, Any]:
    field: dict[str, Any] = {
        "name": name,
        "title": prop.get("title") or name,
        "description": prop.get("description") or "",
        "required": required,
    }
    if "default" in prop:
        field["default"] = prop["default"]
    kind = prop.get("type")

    if kind == "string":
        options = _options(prop)
        if options is not None:
            return {**field, "kind": "enum", "options": options}
        fmt = prop.get("format")
        if fmt is not None and fmt not in _STRING_FORMATS:
            raise UnsupportedSchemaError(f"{name}: unsupported string format {fmt!r}")
        return {
            **field,
            "kind": "string",
            "format": fmt,
            "minLength": prop.get("minLength"),
            "maxLength": prop.get("maxLength"),
        }
    if kind in ("number", "integer"):
        return {
            **field,
            "kind": kind,
            "minimum": prop.get("minimum"),
            "maximum": prop.get("maximum"),
        }
    if kind == "boolean":
        return {**field, "kind": "boolean"}
    if kind == "array":
        options = _options(prop.get("items") or {})
        if options is None:
            raise UnsupportedSchemaError(f"{name}: arrays are only supported as multi-select enums")
        return {
            **field,
            "kind": "multi_enum",
            "options": options,
            "minItems": prop.get("minItems"),
            "maxItems": prop.get("maxItems"),
        }
    raise UnsupportedSchemaError(f"{name}: unsupported type {kind!r}")


def _options(prop: dict[str, Any]) -> list[dict[str, str]] | None:
    """Enum choices as `{value, label}`, or None when `prop` is not an enum.

    Covers all three spellings: `enum`, titled `oneOf` (single select) and
    titled `anyOf` (multi-select items).
    """
    if "enum" in prop:
        return [{"value": str(v), "label": str(v)} for v in prop["enum"]]
    titled = prop.get("oneOf") or prop.get("anyOf")
    if titled:
        return [
            {"value": str(o["const"]), "label": str(o.get("title", o["const"]))} for o in titled
        ]
    return None


# ── Form mode: validation ──────────────────────────────────


def validate_form(
    fields: list[dict[str, Any]], values: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Coerce a submission to the schema's types and check its constraints.

    Returns `(content, errors)`. `errors` maps field name to a message and is
    empty when the submission is valid. Empty optional fields are left out of
    `content` rather than sent as empty strings.
    """
    content: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for field in fields:
        name = field["name"]
        raw = values.get(name)
        if _is_unset(field["kind"], raw):
            if not field["required"]:
                continue
            # An untouched checkbox is a definite "no", which is a complete
            # answer for a required boolean — the other kinds have none.
            if field["kind"] == "boolean":
                content[name] = False
            else:
                errors[name] = "Required"
            continue
        try:
            content[name] = _coerce(field, raw)
        except ValueError as e:
            errors[name] = str(e)
    return content, errors


def _is_unset(kind: str, value: Any) -> bool:
    """Whether the user left this field alone.

    For a boolean only a missing value counts: `False` is an answer, and
    sending it for an optional box nobody touched would erase the difference
    between "unticked" and "not asked about".
    """
    if kind == "boolean":
        return value is None
    return value is None or value == "" or value == []


def _coerce(field: dict[str, Any], raw: Any) -> Any:
    kind = field["kind"]
    if kind == "boolean":
        return bool(raw)
    if kind in ("number", "integer"):
        return _check_range(field, _to_number(raw, integer=kind == "integer"))
    if kind == "enum":
        allowed = {o["value"] for o in field["options"]}
        if str(raw) not in allowed:
            raise ValueError("Pick one of the listed options")
        return str(raw)
    if kind == "multi_enum":
        allowed = {o["value"] for o in field["options"]}
        picked = [str(v) for v in (raw if isinstance(raw, list) else [raw])]
        if any(v not in allowed for v in picked):
            raise ValueError("Pick only listed options")
        if field.get("minItems") is not None and len(picked) < field["minItems"]:
            raise ValueError(f"Pick at least {field['minItems']}")
        if field.get("maxItems") is not None and len(picked) > field["maxItems"]:
            raise ValueError(f"Pick at most {field['maxItems']}")
        return picked
    return _check_string(field, str(raw).strip())


def _to_number(raw: Any, *, integer: bool) -> int | float:
    if isinstance(raw, bool):
        raise ValueError("Enter a number")
    try:
        number = float(raw)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: an integer too large to convert, e.g. a schema default
        # of 10**400 — a crash rather than a rejected value without this.
        raise ValueError("Enter a number") from None
    # NaN and infinity pass every range check (all comparisons against NaN are
    # false, and infinity has no bound to exceed) and are not JSON numbers:
    # `json.dumps` writes a bare NaN that a strict parser refuses.
    if not math.isfinite(number):
        raise ValueError("Enter a finite number")
    if integer:
        if not number.is_integer():
            raise ValueError("Enter a whole number")
        return int(number)
    return number


def _check_range(field: dict[str, Any], number: int | float) -> int | float:
    if field.get("minimum") is not None and number < field["minimum"]:
        raise ValueError(f"Must be at least {field['minimum']}")
    if field.get("maximum") is not None and number > field["maximum"]:
        raise ValueError(f"Must be at most {field['maximum']}")
    return number


def _check_string(field: dict[str, Any], text: str) -> str:
    if field.get("minLength") is not None and len(text) < field["minLength"]:
        raise ValueError(f"Use at least {field['minLength']} characters")
    if field.get("maxLength") is not None and len(text) > field["maxLength"]:
        raise ValueError(f"Use at most {field['maxLength']} characters")
    fmt = field.get("format")
    if fmt == "email" and not _EMAIL.match(text):
        raise ValueError("Enter an email address")
    if fmt == "uri" and not (urlsplit(text).scheme and urlsplit(text).netloc):
        raise ValueError("Enter a full URL, including https://")
    if fmt == "date":
        try:
            date.fromisoformat(text)
        except ValueError:
            raise ValueError("Enter a date as YYYY-MM-DD") from None
    if fmt == "date-time":
        try:
            datetime.fromisoformat(text)
        except ValueError:
            raise ValueError("Enter a date and time as YYYY-MM-DDTHH:MM") from None
    return text


# ── URL mode ───────────────────────────────────────────────


def inspect_url(url: str) -> dict[str, Any]:
    """Describe a URL-mode target so the user can judge it before opening it.

    The spec requires showing the full URL before consent and recommends
    highlighting the domain and warning about ambiguous hosts. Raises
    ValueError for anything that is not an absolute http(s) URL, so a
    `javascript:` or `file:` target can never be offered as a link.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError(f"not an absolute http(s) URL: {url!r}")
    warnings: list[str] = []
    if parts.scheme != "https" and host not in _LOCAL_HOSTS:
        warnings.append("This link is not encrypted (http, not https).")
    if any(label.startswith("xn--") for label in host.split(".")) or not host.isascii():
        warnings.append(
            "The domain uses internationalized characters, which can imitate a different, "
            "familiar domain. Check it letter by letter."
        )
    if parts.username is not None:
        warnings.append(
            f"The link carries text before '@' — the site it opens is {host}, "
            "not what appears before the '@'."
        )
    return {
        "url": url,
        "host": host,
        "url_parts": _split_on_host(url, parts.netloc),
        "warnings": warnings,
    }


def _split_on_host(url: str, netloc: str) -> list[str]:
    """Split `url` into `[before, host, after]` for highlighting the host.

    Located through the netloc rather than by searching for the hostname:
    `urlsplit` lowercases the hostname, and in `https://bank.com@evil.com`
    the first match would highlight the misleading user part, not the host.
    """
    start = url.index(netloc)
    host_start = start + netloc.rfind("@") + 1
    host_text = netloc[netloc.rfind("@") + 1 :]
    if host_text.startswith("["):
        host_text = host_text[: host_text.index("]") + 1]
    else:
        host_text = host_text.split(":", 1)[0]
    host_end = host_start + len(host_text)
    return [url[:host_start], url[host_start:host_end], url[host_end:]]


# ── The handler ────────────────────────────────────────────


def make_elicitation_handler(server_name: str, prompt: Prompt):
    """Build a FastMCP `elicitation_handler` that asks through `prompt`.

    Every outcome maps onto the spec's three actions. An unanswered prompt is
    `cancel` (dismissed without a choice), never an error, so the server can
    offer to ask again later.
    """

    async def handler(message: str, response_type: Any, params: Any, context: Any) -> ElicitResult:
        # Read by wire name, not attribute: the SDK's Python attribute names
        # differ between the mcp 1.x and 2.x lines, the JSON names do not.
        wire = params.model_dump(by_alias=True, exclude_none=True)
        if wire.get("mode") == "url":
            return await _elicit_url(server_name, message, wire["url"], prompt)
        return await _elicit_form(server_name, message, wire.get("requestedSchema") or {}, prompt)

    return handler


async def _elicit_url(server_name: str, message: str, url: str, prompt: Prompt) -> ElicitResult:
    # Raising here reaches the server as an error response, which is right
    # for a request the spec says MUST carry a valid URL.
    target = inspect_url(url)
    answer = await prompt({"mode": "url", "server": server_name, "message": message, **target})
    action = _action_of(answer)
    logger.info("Elicitation (url) from %s → %s", server_name, action)
    # URL mode never carries content: data entered on the page goes to the
    # server directly, and accept only records the user's consent to go there.
    return ElicitResult(action=action)


async def _elicit_form(
    server_name: str, message: str, schema: dict[str, Any], prompt: Prompt
) -> ElicitResult:
    fields = parse_form_schema(schema)
    values = {f["name"]: f["default"] for f in fields if "default" in f}
    errors: dict[str, str] = {}
    for _ in range(MAX_FORM_ATTEMPTS):
        answer = await prompt(
            {
                "mode": "form",
                "server": server_name,
                "message": message,
                "fields": fields,
                "values": values,
                "errors": errors,
            }
        )
        action = _action_of(answer)
        if action != "accept":
            logger.info("Elicitation (form) from %s → %s", server_name, action)
            return ElicitResult(action=action)
        submitted = (answer or {}).get("content") or {}
        content, errors = validate_form(fields, submitted)
        if not errors:
            logger.info("Elicitation (form) from %s → accept", server_name)
            return ElicitResult(action="accept", content=content)
        values = submitted
    logger.info(
        "Elicitation (form) from %s → cancel after %d invalid attempts",
        server_name,
        MAX_FORM_ATTEMPTS,
    )
    return ElicitResult(action="cancel")


def _action_of(answer: dict[str, Any] | None) -> Action:
    action = (answer or {}).get("action")
    return action if action in _ACTIONS else "cancel"
