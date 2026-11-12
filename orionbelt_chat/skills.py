"""Agent Skills: folders of instructions the assistant loads when a task needs them.

A skill is a directory holding a ``SKILL.md`` — YAML frontmatter that names it
and says when to use it, followed by markdown instructions — plus any reference
files those instructions point to. That is the Agent Skills format
(https://agentskills.io), so a skill written for another agent works here::

    skills/
      obml-modeling/
        SKILL.md
        references/
          measures.md

where ``SKILL.md`` reads::

    ---
    name: obml-modeling
    description: Build an OBML semantic model from a discovered schema. Use when …
    ---
    # Building an OBML model
    1. …  For measure patterns, see references/measures.md.

Disclosure is progressive, as the format intends, so an unused skill costs a
line of context rather than its whole body:

1. Every request carries each skill's ``name`` and ``description`` only — the
   catalog, sent as the skills toolset's instructions.
2. When a task matches, the model calls ``load_skill`` and gets the body.
3. A file the body refers to is read with ``read_skill_file``, if it is needed.

Skills are instructions, not code: nothing in a skill directory is executed.
The model can read a bundled script, but this client has no sandbox to run one.

Where skills are found, in the order that settles a name used twice:

- ``SKILLS_DIR``: one or more directories, separated like ``PATH``. Each holds
  a folder per skill; a directory that is itself a skill counts as one.
  Otherwise ``./skills`` and ``<app root>/skills``, whichever exist.
- Connected MCP servers, in configured order. A server publishes a skill as the
  resource ``skill://<name>/SKILL.md`` — FastMCP's ``SkillsDirectoryProvider``
  convention — with ``skill://<name>/_manifest`` listing its files and
  ``skill://<name>/<path>`` serving each one. Local skills come first, so a
  copy on disk can stand in for a server's version of the same skill.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.parse
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from html import escape
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

import yaml
from pydantic_ai.toolsets import FunctionToolset

from .mcp_config import app_root
from .settings import settings

logger = logging.getLogger(__name__)

SKILL_FILENAME = "SKILL.md"

#: Looked for in the working directory and the app root when SKILLS_DIR is unset.
DEFAULT_DIRNAME = "skills"

#: How an MCP server addresses a skill's files: ``skill://<name>/<path>``.
SKILL_URI_PREFIX = "skill://"
#: The JSON file listing FastMCP serves beside each skill.
MANIFEST_PATH = "_manifest"

#: Holds, per browser, the names of the skills the user has switched off: a
#: URL-encoded JSON list, written by the skills checklist and read when a chat
#: starts. A cookie rather than server state, so each user of a shared
#: deployment keeps their own choice and nobody's applies to anybody else.
DISABLED_SKILLS_COOKIE = "orionbelt_disabled_skills"

#: Browsers cap a cookie near 4 KB, which a list of names can reach well before
#: this; the cap is for a value that did not come from the checklist.
MAX_DISABLED_SKILLS = 200

#: Tool names, shared with the history trimmer that has to recognise them.
LOAD_SKILL_TOOL = "load_skill"
READ_SKILL_FILE_TOOL = "read_skill_file"

#: Limits from the Agent Skills specification.
MAX_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 1024

#: A bundled file is read into the conversation whole, so it has to fit there.
#: Anything larger is better split into smaller references.
MAX_FILE_BYTES = 256 * 1024

#: The file listing that follows a loaded skill. A skill shipping a folder of
#: assets should not flood the context just by being loaded.
MAX_LISTED_FILES = 100

# Lowercase letters and digits in hyphen-separated runs: no leading, trailing or
# doubled hyphen. The name is what the model passes back to `load_skill`, so it
# must survive being retyped.
_NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(?:(.*?)\r?\n)?---[ \t]*(?:\r?\n|\Z)", re.S)
# What FastMCP 3's line-based frontmatter parser reports as the description of
# a skill written `description: >-` and folded over the following lines.
_BLOCK_SCALAR_RE = re.compile(r"[>|][1-9]?[+-]?[1-9]?")

# Never listed as part of a skill: version-control and editor droppings, and
# bytecode left behind by anyone who ran a bundled script.
_HIDDEN_PARTS = frozenset({"__pycache__"})


class SkillError(Exception):
    """A skill, or a file in one, that cannot be used — with the reason."""


@dataclass(frozen=True)
class Skill(ABC):
    """One discovered skill: what the catalog shows, and how to read the rest."""

    name: str
    description: str

    @property
    @abstractmethod
    def origin(self) -> str:
        """Where the skill came from, as messages about it should name it."""

    @abstractmethod
    async def instructions(self) -> str:
        """The skill's body, as ``load_skill`` returns it."""

    @abstractmethod
    async def read_file(self, relative: str) -> str:
        """One of the skill's own files, as ``read_skill_file`` returns it."""


@dataclass(frozen=True)
class LocalSkill(Skill):
    """A skill folder on this machine."""

    #: The skill's directory, resolved, so file reads can be confined to it.
    path: Path

    @property
    def skill_file(self) -> Path:
        return self.path / SKILL_FILENAME

    @property
    def origin(self) -> str:
        return str(self.skill_file)

    # File reads run in a worker thread, as the tools did when they were sync:
    # a slow disk must not stall every other chat this process is serving.
    async def instructions(self) -> str:
        return await asyncio.to_thread(render_skill, self)

    async def read_file(self, relative: str) -> str:
        return await asyncio.to_thread(read_bundled_file, self, relative)


@dataclass(frozen=True)
class ServerSkill(Skill):
    """A skill an MCP server publishes as ``skill://`` resources."""

    server_name: str
    #: The connected toolset to read from. Not part of equality: two listings of
    #: the same skill from the same server are the same skill.
    toolset: Any = field(compare=False, repr=False)

    @property
    def origin(self) -> str:
        return f"MCP server '{self.server_name}'"

    async def instructions(self) -> str:
        text = await _read_resource_text(self.toolset, skill_uri(self.name))
        try:
            _meta, body = split_frontmatter(text, skill_uri(self.name))
        except SkillError:
            # The frontmatter only feeds the catalog, which has it already; a
            # server that serves the body alone is still serving the skill.
            body = text
        files, truncated = await self._listed_files()
        return _render(self.name, body, files, truncated)

    async def read_file(self, relative: str) -> str:
        relative = relative.strip()
        if not relative:
            raise SkillError("give the file's path inside the skill, e.g. references/guide.md")
        # The server confines reads to the skill too, but a path that is not
        # plainly inside it never needs to reach the server at all.
        if relative.startswith(("/", "\\")):
            raise SkillError(f"{relative!r} is an absolute path — give one inside the skill")
        if ".." in PurePosixPath(relative.replace("\\", "/")).parts:
            raise SkillError(f"{relative!r} is outside the skill '{self.name}'")

        text = await _read_resource_text(self.toolset, skill_uri(self.name, relative))
        size = len(text.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise SkillError(
                f"{relative!r} is {size:,} bytes, over the {MAX_FILE_BYTES:,}-byte limit "
                f"for reading a file into the conversation"
            )
        return text

    async def _listed_files(self) -> tuple[list[str], bool]:
        """The skill's files according to its manifest; none if there is no usable one.

        The manifest is a convenience — it tells the model what it may read —
        so a server that does not serve one still has a working skill.
        """
        try:
            manifest = json.loads(
                await _read_resource_text(self.toolset, skill_uri(self.name, MANIFEST_PATH))
            )
            paths = [entry["path"] for entry in manifest["files"]]
        except (SkillError, ValueError, KeyError, TypeError) as exc:
            logger.debug(
                "No usable manifest for skill %s on %s: %s", self.name, self.server_name, exc
            )
            return [], False
        return _listable(path for path in paths if isinstance(path, str))


#: Either kind of skill, kept as the kind it was given as.
AnySkill = TypeVar("AnySkill", bound=Skill)


def skill_uri(name: str, path: str = SKILL_FILENAME) -> str:
    """The resource URI of one of a server skill's files."""
    return f"{SKILL_URI_PREFIX}{name}/{path}"


def parse_disabled_skills(raw: Any) -> frozenset[str]:
    """The skill names in *raw*, a list from the browser; anything else is dropped.

    The value arrives from the client — a cookie or an action payload — so it
    is filtered to what could be a skill name rather than trusted.
    """
    if not isinstance(raw, list):
        return frozenset()
    names = [item for item in raw if isinstance(item, str) and _valid_name(item)]
    return frozenset(names[:MAX_DISABLED_SKILLS])


def disabled_skills_from_cookie(cookie_header: str | None) -> frozenset[str]:
    """The disabled skills recorded in a request's ``Cookie`` header; none if unreadable.

    Parsed by hand: `http.cookies` gives up on the first cookie it dislikes,
    and other apps on the same host set whatever cookies they please.
    """
    for part in (cookie_header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key != DISABLED_SKILLS_COOKIE:
            continue
        try:
            return parse_disabled_skills(json.loads(urllib.parse.unquote(value)))
        except ValueError:
            return frozenset()
    return frozenset()


def skill_name(reference: str) -> str:
    """A skill's name from however the model referred to it.

    A server's instructions name its skills by URI, and a model will sometimes
    pass one on as it read it: ``skill://<name>/SKILL.md`` → ``<name>``.
    """
    return reference.strip().removeprefix(SKILL_URI_PREFIX).removesuffix("/" + SKILL_FILENAME)


async def _read_resource_text(toolset: Any, uri: str) -> str:
    """Read a text resource from an MCP server, as SkillError on any failure.

    Every exception is caught, not just MCP's own: a transport error, a
    timeout or a refused read would otherwise escape the tool and end the turn
    in `on_message`. The message keeps the server's wording, so a lost session
    still reads as one to the reconnect check that scans tool results.
    """
    try:
        content = await toolset.read_resource(uri)
    except Exception as exc:
        raise SkillError(f"could not read {uri}: {exc}") from exc
    if isinstance(content, list) and all(isinstance(part, str) for part in content):
        content = "".join(content)
    if not isinstance(content, str):
        raise SkillError(f"{uri} is not a text file")
    return content


# ── Discovery ─────────────────────────────────────────────────────────────


def skill_roots() -> tuple[list[Path], list[str]]:
    """Directories to look for skills in, plus problems with configured ones.

    A configured directory that does not exist is an error rather than an empty
    result, so a typo in SKILLS_DIR surfaces instead of looking like no skills.
    """
    configured = settings.skills_dir.strip()
    if not configured:
        defaults = (Path.cwd() / DEFAULT_DIRNAME, app_root() / DEFAULT_DIRNAME)
        return [path for path in defaults if path.is_dir()], []

    roots: list[Path] = []
    errors: list[str] = []
    for raw in configured.split(os.pathsep):
        if not raw.strip():
            continue
        path = Path(raw.strip()).expanduser()
        if path.is_dir():
            roots.append(path)
        else:
            errors.append(f"{path}: SKILLS_DIR names a directory that does not exist")
    return roots, errors


def _skill_dirs(root: Path) -> list[Path]:
    """The skill directories under *root*, in a stable order."""
    if (root / SKILL_FILENAME).is_file():
        return [root]
    return [
        child
        for child in sorted(root.iterdir())
        if child.is_dir() and not child.name.startswith(".") and (child / SKILL_FILENAME).is_file()
    ]


def _one_line(text: str) -> str:
    return " ".join(text.split())


def split_frontmatter(text: str, where: Path | str) -> tuple[dict[str, Any], str]:
    """Return the parsed frontmatter mapping and the markdown body after it."""
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise SkillError(
            f"{where}: no YAML frontmatter — the file must open with a '---' block "
            f"giving the skill's name and description"
        )
    try:
        meta = yaml.safe_load(match.group(1) or "")
    except yaml.YAMLError as exc:
        raise SkillError(f"{where}: frontmatter is not valid YAML ({_one_line(str(exc))})") from exc
    if meta is None:
        # An empty block: report the missing fields, not a type error.
        meta = {}
    if not isinstance(meta, dict):
        raise SkillError(f"{where}: frontmatter must be a mapping of fields")
    return meta, text[match.end() :]


def _read_text(path: Path) -> str:
    try:
        # utf-8-sig: an editor-added byte-order mark must not hide the frontmatter.
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SkillError(f"{path}: not UTF-8 text") from exc
    except OSError as exc:
        raise SkillError(f"{path}: {exc.strerror or exc}") from exc


def _valid_name(name: str) -> bool:
    return len(name) <= MAX_NAME_CHARS and _NAME_RE.fullmatch(name) is not None


def _description(meta: dict[str, Any], where: Path | str) -> str:
    """The frontmatter's description on one line. Raises SkillError if there is none."""
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        raise SkillError(
            f"{where}: 'description' is required — it is how the assistant knows "
            f"when to use the skill"
        )
    # Folded YAML (`description: >`) keeps its line breaks; the catalog wants one line.
    return _one_line(description)


def parse_skill(skill_dir: Path) -> LocalSkill:
    """Read a skill's frontmatter. Raises SkillError if it cannot be used."""
    path = skill_dir / SKILL_FILENAME
    meta, _body = split_frontmatter(_read_text(path), path)

    # The specification requires `name`, but the directory carries the same
    # value by convention, so a skill that omits it is still unambiguous.
    name = meta.get("name", skill_dir.name)
    if not isinstance(name, str):
        # `name: 7` or `name: 2024-01-01` arrive as a number or a date.
        raise SkillError(f"{path}: name {name!r} is not text — quote it in the frontmatter")
    if not _valid_name(name):
        raise SkillError(
            f"{path}: name {name!r} must be at most {MAX_NAME_CHARS} lowercase letters, "
            f"digits and single hyphens"
        )

    description = _description(meta, path)
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise SkillError(
            f"{path}: description is {len(description):,} characters — at most "
            f"{MAX_DESCRIPTION_CHARS:,}, since it is sent with every request"
        )

    return LocalSkill(name=name, description=description, path=skill_dir.resolve())


def discover_skills() -> tuple[list[LocalSkill], list[str]]:
    """Every usable skill on disk, plus a line for each one skipped and why.

    Problems are returned rather than raised: one malformed skill should not
    take the others down with it, but it must not pass unnoticed either — the
    caller shows them to the user. Read afresh on each call, so an edited or
    added skill is picked up by the next session without a restart.
    """
    roots, errors = skill_roots()
    parsed: list[LocalSkill] = []
    seen_dirs: set[Path] = set()

    for root in roots:
        try:
            skill_dirs = _skill_dirs(root)
        except OSError as exc:
            errors.append(f"{root}: {exc.strerror or exc}")
            continue
        for skill_dir in skill_dirs:
            # The working directory may be the app root, which would otherwise
            # find every skill twice and report each as its own duplicate.
            resolved = skill_dir.resolve()
            if resolved in seen_dirs:
                continue
            seen_dirs.add(resolved)
            try:
                parsed.append(parse_skill(skill_dir))
            except SkillError as exc:
                errors.append(str(exc))

    skills, duplicates = merge_skills(parsed)
    errors.extend(duplicates)
    for problem in errors:
        logger.warning("Skill ignored — %s", problem)
    if skills:
        logger.info("Local skills: %s", ", ".join(s.name for s in skills))
    return skills, errors


def _skill_name_from_uri(uri: str) -> str | None:
    """The name in ``skill://<name>/SKILL.md``; None for any other resource."""
    suffix = "/" + SKILL_FILENAME
    if uri.startswith(SKILL_URI_PREFIX) and uri.endswith(suffix):
        return uri[len(SKILL_URI_PREFIX) : -len(suffix)]
    return None


async def discover_server_skills(
    server_name: str, toolset: Any
) -> tuple[list[ServerSkill], list[str]]:
    """The skills one connected MCP server publishes, plus a line per one skipped.

    Only the resource listing is read, so finding a skill costs nothing beyond
    it — unless the listed description is unusable, which costs one read of
    that skill's SKILL.md. Raises whatever the listing raises: a server that
    cannot be asked is the caller's to report, not a problem with a skill.
    """
    skills: list[ServerSkill] = []
    errors: list[str] = []
    for resource in await toolset.list_resources():
        name = _skill_name_from_uri(resource.uri)
        if name is None:
            continue
        where = f"MCP server '{server_name}', skill {name!r}"
        if not _valid_name(name):
            errors.append(
                f"{where}: the name must be at most {MAX_NAME_CHARS} lowercase letters, "
                f"digits and single hyphens"
            )
            continue

        description = _one_line(resource.description or "")
        if not description or _BLOCK_SCALAR_RE.fullmatch(description):
            # FastMCP before 4 parses frontmatter line by line, so a folded
            # `description: >-` is listed as just ">-". The file has the real one.
            try:
                text = await _read_resource_text(toolset, resource.uri)
                description = _description(split_frontmatter(text, resource.uri)[0], where)
            except SkillError as exc:
                errors.append(f"{where}: no usable description ({exc})")
                continue
        if len(description) > MAX_DESCRIPTION_CHARS:
            # Unlike a local skill, this is not the user's to shorten, and the
            # rest of the server's skills should not be lost over it.
            description = description[: MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"

        skills.append(
            ServerSkill(
                name=name, description=description, server_name=server_name, toolset=toolset
            )
        )

    for problem in errors:
        logger.warning("Skill ignored — %s", problem)
    if skills:
        logger.info("Skills from %s: %s", server_name, ", ".join(s.name for s in skills))
    return skills, errors


def merge_skills(*sources: Sequence[AnySkill]) -> tuple[list[AnySkill], list[str]]:
    """Combine skills from several sources; the first to use a name keeps it.

    The model asks for a skill by name alone, so a name can mean only one
    skill. The later one is reported rather than dropped silently.
    """
    kept: list[AnySkill] = []
    errors: list[str] = []
    by_name: dict[str, AnySkill] = {}
    for source in sources:
        for skill in source:
            first = by_name.get(skill.name)
            if first is not None:
                errors.append(
                    f"{skill.origin}: skill '{skill.name}' is already provided by "
                    f"{first.origin}, which comes first"
                )
                continue
            by_name[skill.name] = skill
            kept.append(skill)
    return kept, errors


# ── What the model sees ──────────────────────────────────────────────────


def catalog(skills: Sequence[Skill]) -> str:
    """The instructions that tell the model which skills exist and how to use them."""
    entries = "\n".join(
        "  <skill>\n"
        f"    <name>{escape(skill.name, quote=False)}</name>\n"
        f"    <description>{escape(skill.description, quote=False)}</description>\n"
        "  </skill>"
        for skill in skills
    )
    return (
        "## Skills\n\n"
        "Skills are packaged instructions for specific tasks. Only each skill's name "
        "and description are listed below. When a task matches a skill's description, "
        f"call `{LOAD_SKILL_TOOL}` with its name before starting, then follow the "
        "instructions it returns. If a skill refers to one of its files, read it with "
        f"`{READ_SKILL_FILE_TOOL}` — only when the instructions call for it. Do not "
        "load a skill the task does not need. Scripts bundled with a skill can be read "
        "but not run here. Instructions from an MCP server may refer to a skill by its "
        f"URI, `{SKILL_URI_PREFIX}<name>/{SKILL_FILENAME}` — that is the skill of that "
        f"name; load it with `{LOAD_SKILL_TOOL}`.\n\n"
        f"<available_skills>\n{entries}\n</available_skills>"
    )


def _listable(paths: Any) -> tuple[list[str], bool]:
    """The paths worth showing the model, capped; and whether the list was cut."""
    files: list[str] = []
    for path in paths:
        parts = PurePosixPath(path).parts
        if path == SKILL_FILENAME or any(
            part.startswith(".") or part in _HIDDEN_PARTS for part in parts
        ):
            continue
        if len(files) == MAX_LISTED_FILES:
            return files, True
        files.append(path)
    return files, False


def _bundled_files(skill: LocalSkill) -> tuple[list[str], bool]:
    """Files beside SKILL.md, relative to the skill, and whether the list was cut."""
    return _listable(
        path.relative_to(skill.path).as_posix()
        for path in sorted(skill.path.rglob("*"))
        # A symlink out of the skill would only be refused by read_skill_file.
        if path.is_file() and path.resolve().is_relative_to(skill.path)
    )


def _render(name: str, body: str, files: list[str], truncated: bool) -> str:
    """A skill's body, then the files the model may read from it."""
    parts = [f'<skill name="{name}">\n{body.strip()}\n</skill>']
    if files:
        listing = "\n".join(f"- {path}" for path in files)
        if truncated:
            listing += f"\n- … (only the first {MAX_LISTED_FILES} files are listed)"
        parts.append(
            f"Files in this skill — read one with `{READ_SKILL_FILE_TOOL}` only when the "
            f"instructions above call for it:\n{listing}"
        )
    return "\n\n".join(parts)


def render_skill(skill: LocalSkill) -> str:
    """A local skill's instructions, as ``load_skill`` returns them.

    Read from disk on every call, so an edit applies the next time it is loaded.
    The frontmatter is dropped — the model already has it from the catalog.
    """
    _meta, body = split_frontmatter(_read_text(skill.skill_file), skill.skill_file)
    files, truncated = _bundled_files(skill)
    return _render(skill.name, body, files, truncated)


def read_bundled_file(skill: LocalSkill, relative: str) -> str:
    """Read one file from inside the skill's directory, and from nowhere else.

    The path comes from the model, so it is confined to the skill: an absolute
    path, a ``..`` that climbs out, or a symlink pointing elsewhere is refused.
    """
    relative = relative.strip()
    if not relative:
        raise SkillError("give the file's path inside the skill, e.g. references/guide.md")
    if Path(relative).is_absolute():
        raise SkillError(f"{relative!r} is an absolute path — give one inside the skill")

    target = (skill.path / relative).resolve()
    if not target.is_relative_to(skill.path):
        raise SkillError(f"{relative!r} is outside the skill '{skill.name}'")
    if not target.is_file():
        raise SkillError(f"the skill '{skill.name}' has no file {relative!r}")

    try:
        size = target.stat().st_size
    except OSError as exc:
        raise SkillError(f"{relative!r}: {exc.strerror or exc}") from exc
    if size > MAX_FILE_BYTES:
        raise SkillError(
            f"{relative!r} is {size:,} bytes, over the {MAX_FILE_BYTES:,}-byte limit "
            f"for reading a file into the conversation"
        )
    return _read_text(target)


def skills_toolset(skills: Sequence[Skill]) -> FunctionToolset[Any] | None:
    """The tools and catalog that expose *skills* to the agent; None if there are none.

    Errors come back as the tool's result rather than as a retry, so a model
    asking for a skill that is not there is told what is, without spending the
    run's retry budget. OSError is caught too: an exception escaping a tool
    ends the whole turn in `on_message`, and a skill folder someone made
    unreadable is not worth that.
    """
    if not skills:
        return None
    by_name = {skill.name: skill for skill in skills}

    def lookup(name: str) -> Skill:
        skill = by_name.get(skill_name(name))
        if skill is None:
            available = ", ".join(sorted(by_name))
            raise SkillError(f"there is no skill named {name!r}. Available: {available}")
        return skill

    async def load_skill(name: str) -> str:
        """Load a skill's full instructions. Call this before starting a task one of the available skills covers.

        Args:
            name: The skill's name, exactly as listed in available_skills.
        """
        try:
            return await lookup(name).instructions()
        except (SkillError, OSError) as exc:
            return f"Error: {exc}"

    async def read_skill_file(name: str, path: str) -> str:
        """Read a file bundled with a skill, such as a reference document its instructions point to.

        Args:
            name: The skill's name, exactly as listed in available_skills.
            path: The file's path inside the skill's directory, e.g. references/guide.md.
        """
        try:
            skill = lookup(name)
            # `skill://<name>/references/a.md` → `references/a.md`
            return await skill.read_file(path.strip().removeprefix(skill_uri(skill.name, "")))
        except (SkillError, OSError) as exc:
            return f"Error: {exc}"

    return FunctionToolset([load_skill, read_skill_file], instructions=catalog(skills))
