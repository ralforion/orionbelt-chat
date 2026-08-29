"""Console entry point for the ``orionbelt-chat`` command.

Chainlit resolves ``public/``, ``chainlit.md`` and ``.chainlit/config.toml``
relative to ``CHAINLIT_APP_ROOT`` (defaulting to the working directory) and
writes into that directory at runtime — uploaded files land in ``.files/``, and
missing config/markdown are regenerated from Chainlit's own defaults. An
installed package lives in ``site-packages``, which is neither a sane nor a
reliably writable place for any of that.

So this shim picks a writable app root, seeds it with the assets the wheel
ships, and then hands off to Chainlit's own ``run`` command — every flag it
accepts (``--port``, ``--host``, ``--headless``, ``-w`` …) passes straight
through.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent

#: Chainlit's target module — the file holding the @cl decorators.
APP_MODULE = _PACKAGE_ROOT / "app.py"

#: Where the app root lands when neither environment variable is set.
DEFAULT_APP_ROOT = Path.home() / ".orionbelt-chat"


def _app_root() -> Path:
    """Return the directory Chainlit should treat as the app root.

    ``CHAINLIT_APP_ROOT`` wins because Chainlit itself honours it, so anyone
    who already sets it keeps the behaviour they expect.
    """
    for var in ("CHAINLIT_APP_ROOT", "ORIONBELT_CHAT_HOME"):
        value = os.environ.get(var, "").strip()
        if value:
            return Path(value).expanduser().resolve()
    return DEFAULT_APP_ROOT


def _seed(root: Path) -> None:
    """Populate *root* with the assets this package ships.

    ``public/`` is refreshed on every launch: it is app-owned UI keyed to the
    installed version (``header.js`` carries the version banner), so a stale
    copy left behind by an upgrade would misreport what is running. The two
    user-facing files are copied only when absent, so local edits survive.
    """
    root.mkdir(parents=True, exist_ok=True)

    public_dst = root / "public"
    public_dst.mkdir(exist_ok=True)
    for asset in (_PACKAGE_ROOT / "public").iterdir():
        if asset.is_file():
            shutil.copyfile(asset, public_dst / asset.name)

    markdown = root / "chainlit.md"
    if not markdown.exists():
        shutil.copyfile(_PACKAGE_ROOT / "chainlit.md", markdown)

    config = root / ".chainlit" / "config.toml"
    if not config.exists():
        config.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_PACKAGE_ROOT / "chainlit_config.toml", config)


def main(argv: list[str] | None = None) -> int:
    """Seed the app root, then run Chainlit against the packaged app."""
    args = list(sys.argv[1:] if argv is None else argv)

    if args and args[0] in ("--version", "-V"):
        from . import __version__

        print(f"orionbelt-chat {__version__}")
        return 0

    root = _app_root()
    _seed(root)
    # Must be set before chainlit.config is imported: it snapshots the app root
    # into module-level constants (public_dir, config_dir) at import time.
    os.environ["CHAINLIT_APP_ROOT"] = str(root)

    from chainlit.cli import cli as chainlit_cli

    # standalone_mode=False returns instead of calling sys.exit, so the console
    # script owns the exit code.
    chainlit_cli(
        args=["run", str(APP_MODULE), *args],
        prog_name="orionbelt-chat",
        standalone_mode=False,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
