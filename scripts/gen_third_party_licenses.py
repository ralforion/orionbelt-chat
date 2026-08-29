#!/usr/bin/env python3
"""Generate THIRD_PARTY_LICENSES.md from an installed virtual environment.

We redistribute binary copies of every dependency inside the Docker image, which
triggers the attribution clauses of the licenses they ship under (Apache-2.0 §4,
the MIT/BSD/ISC notice requirements, MPL-2.0 §3.2). This walks the `.dist-info`
metadata of a built venv and emits a single Markdown file carrying the verbatim
license and NOTICE texts, so the obligation travels with the artifact.

Stdlib only, on purpose: it runs inside the Docker build where the only thing
installed is the application's own runtime environment.

    python scripts/gen_third_party_licenses.py --venv .venv -o THIRD_PARTY_LICENSES.md
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Basenames that carry attribution text. AUTHORS is included because several
# BSD-licensed projects keep the copyright holders there rather than in LICENSE.
LICENSE_FILE_RE = re.compile(r"^(licen[cs]e|copying|notice|authors)", re.IGNORECASE)

# Licenses that would impose source-disclosure obligations on the Licensed Work.
# Nothing in the current tree matches; --fail-on-copyleft keeps it that way.
COPYLEFT_RE = re.compile(r"\b(a?gpl|lgpl|sspl|cecill|osl-)", re.IGNORECASE)

# A full Apache-2.0 text runs ~11 KB and ends with the appendix boilerplate.
APACHE_MARKERS = ("Apache License", "Version 2.0", "APPENDIX")


@dataclass
class Package:
    name: str
    version: str
    license: str
    urls: list[str] = field(default_factory=list)
    texts: list[tuple[str, str]] = field(default_factory=list)

    @property
    def sort_key(self) -> str:
        return self.name.lower()

    @property
    def has_text(self) -> bool:
        return bool(self.texts)


def read_metadata(dist_info: Path) -> Package | None:
    """Parse the RFC-822 header block of a dist-info METADATA file."""
    metadata = dist_info / "METADATA"
    if not metadata.is_file():
        return None

    name = version = expression = legacy = ""
    classifiers: list[str] = []
    urls: list[str] = []

    with metadata.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                break  # end of headers; the long description follows
            key, _, value = line.partition(": ")
            value = value.strip()
            if key == "Name":
                name = value
            elif key == "Version":
                version = value
            elif key == "License-Expression":
                expression = value
            elif key == "License":
                # Some projects paste the entire license body into this field.
                legacy = value if len(value) <= 80 else ""
            elif key == "Classifier" and value.startswith("License ::"):
                classifiers.append(value.split("::")[-1].strip())
            elif key == "Home-page":
                urls.append(value)
            elif key == "Project-URL":
                label, _, url = value.partition(", ")
                if label.strip().lower() in {"homepage", "source", "repository"}:
                    urls.append(url.strip())

    if not name:
        return None

    declared = expression or legacy or " / ".join(classifiers)
    return Package(
        name=name,
        version=version,
        license=declared or "Not declared in package metadata",
        urls=list(dict.fromkeys(urls)),
    )


def collect_texts(dist_info: Path) -> list[tuple[str, str]]:
    """Return (label, body) for every attribution file the wheel shipped."""
    candidates = [path for path in dist_info.rglob("*") if path.is_file()]
    texts: list[tuple[str, str]] = []
    for path in sorted(candidates, key=lambda p: str(p).lower()):
        if not LICENSE_FILE_RE.match(path.name):
            continue
        body = path.read_text(encoding="utf-8", errors="replace").strip()
        if body:
            texts.append((path.relative_to(dist_info).as_posix(), body))
    return texts


def discover(venv: Path, exclude: str) -> list[Package]:
    site_packages = sorted(venv.glob("lib/*/site-packages")) + sorted(
        venv.glob("Lib/site-packages")  # Windows layout
    )
    if not site_packages:
        raise SystemExit(f"error: no site-packages directory under {venv}")

    packages: dict[str, Package] = {}
    for root in site_packages:
        for dist_info in sorted(root.glob("*.dist-info")):
            package = read_metadata(dist_info)
            if package is None:
                continue
            if package.name.lower().replace("_", "-") == exclude.lower().replace("_", "-"):
                continue  # the Licensed Work itself, covered by ./LICENSE
            package.texts = collect_texts(dist_info)
            packages[package.name.lower()] = package

    return sorted(packages.values(), key=lambda p: p.sort_key)


def find_apache_text(packages: list[Package]) -> str | None:
    """Reuse a verbatim Apache-2.0 copy already present in the tree.

    36 dependencies ship the full text; another 36 (the traceloop and
    opentelemetry-instrumentation families) declare Apache-2.0 but ship no file
    at all. Quoting one real copy in an appendix satisfies §4(a) for those
    without embedding a license blob in this script.
    """
    for package in packages:
        for _, body in package.texts:
            if all(marker in body for marker in APACHE_MARKERS):
                return body
    return None


def render(packages: list[Package], project: str, version: str) -> str:
    counts: dict[str, int] = {}
    for package in packages:
        counts[package.license] = counts.get(package.license, 0) + 1

    missing = [p for p in packages if not p.has_text]
    apache_text = find_apache_text(packages)

    out: list[str] = []
    add = out.append

    add(f"# Third-Party Licenses — {project} {version}".rstrip())
    add("")
    add(
        f"{project} is distributed under the Business Source License 1.1 "
        "(see `LICENSE`). This file covers the third-party open-source "
        "packages redistributed alongside it — every dependency installed into "
        "the runtime environment of the published container image."
    )
    add("")
    add(
        "It is generated from installed package metadata by "
        "`scripts/gen_third_party_licenses.py`; do not edit it by hand."
    )
    add("")
    add(f"Packages: **{len(packages)}**")
    add("")

    add("## Summary")
    add("")
    add("| License | Packages |")
    add("| --- | ---: |")
    for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower())):
        add(f"| {name} | {count} |")
    add("")

    if missing:
        add("## Packages without an upstream license file")
        add("")
        add(
            "These wheels declare a license in their metadata but ship no "
            "license file. The declared license governs; for Apache-2.0 the "
            "required copy of the license is reproduced in "
            "[Appendix A](#appendix-a--apache-license-20)."
        )
        add("")
        add("| Package | Version | Declared license | Project |")
        add("| --- | --- | --- | --- |")
        for package in missing:
            url = package.urls[0] if package.urls else "—"
            add(f"| {package.name} | {package.version} | {package.license} | {url} |")
        add("")

    add("## Packages")
    add("")
    for package in packages:
        add(f"### {package.name} {package.version}")
        add("")
        add(f"License: {package.license}")
        if package.urls:
            add("")
            add(f"Project: {package.urls[0]}")
        add("")
        if package.texts:
            for label, body in package.texts:
                add(f"<details><summary>{label}</summary>")
                add("")
                add("```text")
                add(body)
                add("```")
                add("")
                add("</details>")
                add("")
        else:
            add("_No license file distributed by upstream._")
            add("")

    if apache_text:
        add("## Appendix A — Apache License 2.0")
        add("")
        add(
            "Reproduced for the packages above that declare Apache-2.0 without "
            "shipping the text, per section 4(a) of the license."
        )
        add("")
        add("```text")
        add(apache_text)
        add("```")
        add("")

    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--venv", type=Path, default=Path(".venv"), help="virtualenv to scan")
    parser.add_argument(
        "-o", "--output", type=Path, default=Path("THIRD_PARTY_LICENSES.md"), help="file to write"
    )
    parser.add_argument("--project", default="OrionBelt Chat", help="name of the Licensed Work")
    parser.add_argument("--version", default="", help="version of the Licensed Work")
    parser.add_argument(
        "--exclude", default="orionbelt-chat", help="distribution to omit as first-party"
    )
    parser.add_argument(
        "--fail-on-copyleft",
        action="store_true",
        help="exit non-zero if a dependency declares GPL/LGPL/AGPL/SSPL",
    )
    args = parser.parse_args()

    packages = discover(args.venv, args.exclude)
    if not packages:
        raise SystemExit(f"error: no distributions found in {args.venv}")

    if args.fail_on_copyleft:
        flagged = [p for p in packages if COPYLEFT_RE.search(p.license)]
        if flagged:
            for package in flagged:
                print(f"copyleft dependency: {package.name} {package.version} — {package.license}")
            return 2

    args.output.write_text(render(packages, args.project, args.version), encoding="utf-8")
    print(f"wrote {args.output} — {len(packages)} packages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
