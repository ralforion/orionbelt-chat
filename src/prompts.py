"""System prompt loading for the OrionBelt Analytics Assistant.

The prompt is stored in an external file (``system_prompt.md`` at the project
root by default) so users can tweak the agent's behaviour without editing code.
The path can be overridden via the ``SYSTEM_PROMPT_FILE`` environment variable.
If the file is missing or unreadable, an embedded fallback is used.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .settings import settings

logger = logging.getLogger(__name__)

# Project root: repo directory containing app.py
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SYSTEM_PROMPT_FILE = _PROJECT_ROOT / "system_prompt.md"

# Minimal fallback used only when no prompt file can be read.
FALLBACK_SYSTEM_PROMPT = (
    "You are the OrionBelt Analytics Assistant — an expert data analyst that "
    "helps users understand their database and query it reliably through a "
    "semantic layer. Prefer OBML semantic models over raw SQL, and be concise."
)


def _resolve_prompt_path() -> Path:
    """Return the configured prompt file path, or the default location."""
    configured = settings.system_prompt_file.strip()
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_SYSTEM_PROMPT_FILE


def load_system_prompt() -> str:
    """Load the system prompt from disk, falling back to the embedded default.

    Reads the file fresh on each call so changes take effect on the next
    session start without requiring a full process restart.
    """
    path = _resolve_prompt_path()
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        logger.warning("System prompt file not found at %s — using embedded fallback.", path)
        return FALLBACK_SYSTEM_PROMPT
    except OSError as exc:
        logger.warning(
            "Failed to read system prompt file %s (%s) — using embedded fallback.",
            path,
            exc,
        )
        return FALLBACK_SYSTEM_PROMPT

    if not text:
        logger.warning("System prompt file %s is empty — using embedded fallback.", path)
        return FALLBACK_SYSTEM_PROMPT

    return text
