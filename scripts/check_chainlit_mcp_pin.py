"""Report whether the latest Chainlit release on PyPI allows mcp 2.x.

MCP protocol 2026-07-28 needs the mcp 2.x SDK (with fastmcp 4), but Chainlit
pins `mcp<2`, and one environment can hold only one `mcp`. This app never runs
Chainlit's own MCP client (`features.mcp.enabled` is false), yet the pin still
decides what resolves. Tracked upstream in Chainlit#3002 / PR #3030.

Run weekly by .github/workflows/watch-chainlit-mcp.yml, which opens an issue
the first time this reports `unblocked=true`. Checking the published metadata
rather than the PR catches the fix however it lands, and only once it ships.

Usage:
    python scripts/check_chainlit_mcp_pin.py [--metadata FILE]

`--metadata` reads a saved PyPI JSON document instead of fetching, for tests.
Writes `unblocked`, `version` and `requirement` to $GITHUB_OUTPUT when set.
"""

import argparse
import json
import os
import sys
import urllib.request

from packaging.requirements import Requirement

PYPI_URL = "https://pypi.org/pypi/chainlit/json"

# The SDK line the elicitation work was verified against (fastmcp 4 needs >=2.0).
TARGET_MCP = "2.2.0"


def mcp_requirement(requires_dist: list[str]) -> Requirement | None:
    """Chainlit's unconditional `mcp` dependency, or None if it has none.

    Requirements behind an extra (`; extra == ...`) are skipped: they only
    apply when someone asks for that extra, which this app does not.
    """
    for line in requires_dist:
        req = Requirement(line)
        if req.name.lower() == "mcp" and (req.marker is None or "extra" not in str(req.marker)):
            return req
    return None


def check(metadata: dict) -> dict[str, str]:
    version = metadata["info"]["version"]
    req = mcp_requirement(metadata["info"].get("requires_dist") or [])
    # No mcp dependency at all also unblocks us.
    unblocked = req is None or req.specifier.contains(TARGET_MCP, prereleases=True)
    return {
        "unblocked": "true" if unblocked else "false",
        "version": version,
        "requirement": str(req) if req else "(no mcp dependency)",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--metadata", help="read PyPI JSON from this file instead of fetching")
    args = parser.parse_args()

    if args.metadata:
        with open(args.metadata, encoding="utf-8") as f:
            metadata = json.load(f)
    else:
        with urllib.request.urlopen(PYPI_URL, timeout=30) as resp:
            metadata = json.load(resp)

    result = check(metadata)
    verdict = "allows" if result["unblocked"] == "true" else "still blocks"
    print(f"chainlit {result['version']}: {result['requirement']} — {verdict} mcp {TARGET_MCP}")

    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a", encoding="utf-8") as f:
            for key, value in result.items():
                f.write(f"{key}={value}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
