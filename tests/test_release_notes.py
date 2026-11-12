"""Release notes come from CHANGELOG.md, and a release cannot go out without them.

`scripts/release_notes.py` is what `.github/workflows/release.yml` runs for the
tag being released. The guard that matters most is the last class here: the
release workflow can only fail *after* a tag is pushed, by which point the
same tag has already started the PyPI and Docker publishes, so the missing
section has to be caught where the version is bumped — in CI, on the PR.
"""

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "release_notes.py"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"

SAMPLE = """# Changelog

Intro text that belongs to no release.

## [Unreleased]

### Added
- **Something not released yet.**

## [1.2.0] - 2026-11-12

### Added
- **A feature.** Explained over
  two lines. (#12)

### Fixed
- **A fix.** (#13)

## [1.1.0] - 2026-10-01

### Internal
- **The oldest entry.** (#9)

[Unreleased]: https://example.com/compare/v1.2.0...HEAD
[1.2.0]: https://example.com/releases/tag/v1.2.0
[1.1.0]: https://example.com/releases/tag/v1.1.0
"""


def run(version: str, changelog: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), version, "--changelog", str(changelog)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def changelog(tmp_path):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


class TestSection:
    def test_prints_the_body_without_its_heading(self, changelog):
        result = run("1.2.0", changelog)
        assert result.returncode == 0
        assert result.stdout == (
            "### Added\n- **A feature.** Explained over\n  two lines. (#12)\n\n"
            "### Fixed\n- **A fix.** (#13)\n"
        )

    def test_the_last_section_stops_before_the_link_definitions(self, changelog):
        assert run("1.1.0", changelog).stdout == "### Internal\n- **The oldest entry.** (#9)\n"

    def test_a_leading_v_is_accepted(self, changelog):
        # The workflow passes the tag with the `v` stripped, but a person may not.
        assert run("v1.2.0", changelog).stdout == run("1.2.0", changelog).stdout

    def test_a_version_is_matched_whole(self, changelog):
        # 1.2 must not pick up the 1.2.0 section.
        result = run("1.2", changelog)
        assert result.returncode == 1
        assert "no '## [1.2]' section" in result.stderr

    def test_a_missing_version_fails(self, changelog):
        result = run("9.9.9", changelog)
        assert result.returncode == 1
        assert result.stdout == ""
        assert "no '## [9.9.9]' section" in result.stderr

    def test_an_empty_section_fails(self, tmp_path):
        path = tmp_path / "CHANGELOG.md"
        path.write_text("## [2.0.0] - 2026-12-01\n\n## [1.0.0] - 2026-01-01\n- x\n")
        result = run("2.0.0", path)
        assert result.returncode == 1
        assert "section is empty" in result.stderr

    def test_a_missing_changelog_fails(self, tmp_path):
        result = run("1.0.0", tmp_path / "nope.md")
        assert result.returncode == 1
        assert result.stderr.startswith("error:")


# The sdist carries tests/ and scripts/ but not CHANGELOG.md or the workflows,
# so these only run against a checkout — as test_settings_docs does for README.
@pytest.mark.skipif(not CHANGELOG.is_file(), reason="CHANGELOG.md not present (sdist)")
class TestThisRepository:
    def test_the_current_version_has_release_notes(self):
        """Bumping the version without a CHANGELOG.md section fails here, before any tag."""
        version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]
        result = run(version, CHANGELOG)
        assert result.returncode == 0, (
            f"CHANGELOG.md needs a '## [{version}] - YYYY-MM-DD' section with the release "
            f"notes — the release workflow publishes it as the GitHub Release body"
        )

    def test_every_release_heading_has_a_date(self):
        text = CHANGELOG.read_text(encoding="utf-8")
        headings = re.findall(r"^## \[(.+?)\](.*)$", text, re.M)
        assert headings, "CHANGELOG.md has no release sections"
        for version, rest in headings:
            if version == "Unreleased":
                continue
            assert re.fullmatch(r" - \d{4}-\d{2}-\d{2}", rest), f"## [{version}]{rest}"

    @pytest.mark.skipif(not RELEASE_WORKFLOW.is_file(), reason="workflows not present (sdist)")
    def test_the_release_workflow_publishes_the_changelog_section(self):
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        assert "scripts/release_notes.py" in workflow
        assert "--notes-file release-notes.md" in workflow
        assert "--generate-notes" not in workflow
