"""Print one version's section of CHANGELOG.md, as the GitHub Release notes.

Run by .github/workflows/release.yml for the tag being released, so a release
says what CHANGELOG.md says — written for readers, in the format the other
OrionBelt repositories use — instead of GitHub's generated list of pull
requests, which buries a feature among dozens of dependency bumps.

The section is everything under ``## [X.Y.Z] - date`` up to the next ``## ``
heading, without the heading itself (the release is already titled with the
tag) and without the link definitions Keep a Changelog puts at the end of the
file.

Usage:
    python scripts/release_notes.py X.Y.Z [--changelog CHANGELOG.md]

Exits non-zero, saying why, if the version has no section or an empty one: a
release must not go out without its notes.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"

# `[1.7.0]: https://…` — Keep a Changelog's link definitions at the end of the file.
_LINK_DEFINITION = re.compile(r"^\[[^\]]+\]:\s+\S+")


class NotesError(Exception):
    """The changelog has no usable section for the version."""


def section(changelog: str, version: str) -> str:
    """The body of *version*'s section in *changelog*, trimmed."""
    heading = re.compile(rf"^## \[{re.escape(version)}\](?:\s|$)")
    lines = changelog.splitlines()
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        raise NotesError(f"CHANGELOG.md has no '## [{version}]' section")

    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        body.append(line)
    # The last section runs into the link definitions; they belong to the file.
    while body and (not body[-1].strip() or _LINK_DEFINITION.match(body[-1])):
        body.pop()

    text = "\n".join(body).strip()
    if not text:
        raise NotesError(f"CHANGELOG.md's '## [{version}]' section is empty")
    return text + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("version", help="the version to release, without the leading 'v'")
    parser.add_argument("--changelog", type=Path, default=DEFAULT_CHANGELOG)
    args = parser.parse_args(argv)

    version = args.version.removeprefix("v")
    try:
        notes = section(args.changelog.read_text(encoding="utf-8"), version)
    except (NotesError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
