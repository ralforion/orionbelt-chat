"""OrionBelt Chat - Chainlit + Pydantic AI chat client for OrionBelt Analytics."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    # Single source of truth: pyproject's [project] version, read off the
    # installed distribution rather than duplicated as a literal here.
    __version__ = _pkg_version("orionbelt-chat")
except PackageNotFoundError:  # pragma: no cover - running from a bare checkout
    __version__ = "unknown"

__all__ = ["__version__"]
