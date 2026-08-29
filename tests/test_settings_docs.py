"""Every setting must be documented.

Five settings had drifted out of the README — they existed in `.env.example`
and in `settings.py` comments, but the README's Configuration section was
written as per-provider recipes, so anything that was not part of a recipe was
never mentioned. This pins the reference table to the actual `Settings` fields
so the next one added cannot quietly go undocumented.
"""

import re
from pathlib import Path

import pytest

from orionbelt_chat.settings import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

SETTING_NAMES = sorted(Settings.model_fields)


def _env_var(field: str) -> str:
    return field.upper()


@pytest.mark.skipif(not README.is_file(), reason="README not present (installed package)")
@pytest.mark.parametrize("field", SETTING_NAMES)
def test_setting_is_in_the_readme_table(field):
    text = README.read_text(encoding="utf-8")
    var = _env_var(field)
    assert f"| `{var}` |" in text, (
        f"{var} is missing from the README settings table — add a row with its default"
    )


@pytest.mark.skipif(not ENV_EXAMPLE.is_file(), reason=".env.example not present")
@pytest.mark.parametrize("field", SETTING_NAMES)
def test_setting_is_in_env_example(field):
    """Matched as a whole assignment, not a substring.

    A plain `in` check passes DEFAULT_MODEL off the back of
    OPENROUTER_DEFAULT_MODEL, which is exactly how DEFAULT_MODEL went missing.
    """
    import re

    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    var = _env_var(field)
    assert re.search(rf"^#?\s*{var}=", text, re.M), (
        f"{var} has no `{var}=` line of its own in .env.example"
    )


@pytest.mark.skipif(not README.is_file(), reason="README not present")
def test_readme_table_has_no_settings_that_do_not_exist():
    """The other direction: a renamed setting leaves a stale row behind."""
    text = README.read_text(encoding="utf-8")
    documented = set(re.findall(r"^\| `([A-Z][A-Z0-9_]+)` \|", text, re.M))
    real = {_env_var(f) for f in SETTING_NAMES}
    # The app root variables are read by Chainlit and the launcher, not by
    # Settings, but belong in the same table for the reader.
    external = {"CHAINLIT_APP_ROOT", "ORIONBELT_CHAT_HOME"}
    stale = documented - real - external
    assert not stale, f"README documents settings that no longer exist: {sorted(stale)}"
