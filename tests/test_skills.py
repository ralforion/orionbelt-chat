"""Tests for Agent Skills: discovery, the catalog, the two tools, and the app glue.

Every test runs in a scratch working directory with a scratch app root, so a
`skills/` folder on the machine running the suite can never leak in.
"""

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    BinaryContent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset

from orionbelt_chat import agent as agent_module
from orionbelt_chat import app, skills
from orionbelt_chat.skills import (
    LOAD_SKILL_TOOL,
    MAX_DESCRIPTION_CHARS,
    READ_SKILL_FILE_TOOL,
    SKILL_FILENAME,
    LocalSkill,
    ServerSkill,
    Skill,
    SkillError,
    catalog,
    disabled_skills_from_cookie,
    discover_server_skills,
    discover_skills,
    merge_skills,
    parse_disabled_skills,
    parse_skill,
    read_bundled_file,
    render_skill,
    skills_toolset,
)

DESCRIPTION = "Builds OBML models. Use when the user asks for a semantic model."


@pytest.fixture
def env(tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    root = tmp_path / "approot"
    cwd.mkdir()
    root.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("CHAINLIT_APP_ROOT", str(root))
    monkeypatch.setattr(skills.settings, "skills_dir", "")
    return SimpleNamespace(cwd=cwd, root=root, tmp=tmp_path)


def make_skill(
    parent: Path,
    dirname: str,
    *,
    frontmatter: str | None = None,
    body: str = "# Steps\n\nFollow these steps.",
) -> Path:
    """A skill folder whose SKILL.md carries *frontmatter* (a valid one by default)."""
    skill_dir = parent / dirname
    skill_dir.mkdir(parents=True)
    if frontmatter is None:
        frontmatter = f"name: {dirname}\ndescription: {DESCRIPTION}"
    (skill_dir / SKILL_FILENAME).write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")
    return skill_dir


def names(found: list[Skill]) -> list[str]:
    return [s.name for s in found]


class TestDiscovery:
    def test_nothing_configured_finds_nothing_and_says_nothing(self, env):
        assert discover_skills() == ([], [])

    def test_finds_skills_under_the_working_directory(self, env):
        make_skill(env.cwd / "skills", "obml-modeling")
        found, errors = discover_skills()
        assert names(found) == ["obml-modeling"]
        assert found[0].description == DESCRIPTION
        assert errors == []

    def test_working_directory_and_app_root_are_both_used(self, env):
        make_skill(env.cwd / "skills", "project-skill")
        make_skill(env.root / "skills", "personal-skill")
        found, _ = discover_skills()
        # The working directory comes first.
        assert names(found) == ["project-skill", "personal-skill"]

    def test_working_directory_that_is_the_app_root_finds_each_skill_once(self, env, monkeypatch):
        make_skill(env.root / "skills", "only-once")
        monkeypatch.chdir(env.root)
        found, errors = discover_skills()
        assert names(found) == ["only-once"]
        assert errors == []

    def test_skills_dir_replaces_the_defaults(self, env, monkeypatch):
        make_skill(env.cwd / "skills", "default-one")
        make_skill(env.tmp / "elsewhere", "configured-one")
        monkeypatch.setattr(skills.settings, "skills_dir", str(env.tmp / "elsewhere"))
        found, _ = discover_skills()
        assert names(found) == ["configured-one"]

    def test_skills_dir_takes_several_directories(self, env, monkeypatch):
        make_skill(env.tmp / "a", "first")
        make_skill(env.tmp / "b", "second")
        configured = os.pathsep.join([str(env.tmp / "a"), "", str(env.tmp / "b")])
        monkeypatch.setattr(skills.settings, "skills_dir", configured)
        found, errors = discover_skills()
        assert names(found) == ["first", "second"]
        assert errors == []

    def test_skills_dir_expands_the_home_directory(self, env, monkeypatch):
        make_skill(env.tmp / "home" / "my-skills", "from-home")
        monkeypatch.setenv("HOME", str(env.tmp / "home"))
        monkeypatch.setattr(skills.settings, "skills_dir", "~/my-skills")
        found, _ = discover_skills()
        assert names(found) == ["from-home"]

    def test_a_missing_skills_dir_is_reported_not_ignored(self, env, monkeypatch):
        monkeypatch.setattr(skills.settings, "skills_dir", str(env.tmp / "typo"))
        found, errors = discover_skills()
        assert found == []
        assert len(errors) == 1
        assert "typo" in errors[0] and "does not exist" in errors[0]

    def test_skills_dir_may_point_at_a_single_skill(self, env, monkeypatch):
        skill_dir = make_skill(env.tmp, "standalone")
        monkeypatch.setattr(skills.settings, "skills_dir", str(skill_dir))
        found, _ = discover_skills()
        assert names(found) == ["standalone"]

    def test_folders_without_skill_md_and_hidden_folders_are_not_skills(self, env):
        (env.cwd / "skills" / "notes").mkdir(parents=True)
        make_skill(env.cwd / "skills", ".hidden-skill")
        make_skill(env.cwd / "skills", "real-one")
        found, errors = discover_skills()
        assert names(found) == ["real-one"]
        assert errors == []

    def test_a_duplicate_name_keeps_the_first_and_says_so(self, env):
        make_skill(env.cwd / "skills", "same")
        make_skill(env.root / "skills", "same")
        found, errors = discover_skills()
        assert len(found) == 1
        assert found[0].path == (env.cwd / "skills" / "same").resolve()
        assert len(errors) == 1
        assert "already provided by" in errors[0] and "which comes first" in errors[0]

    def test_one_broken_skill_does_not_hide_the_others(self, env):
        make_skill(env.cwd / "skills", "broken", frontmatter="name: broken")
        make_skill(env.cwd / "skills", "fine")
        found, errors = discover_skills()
        assert names(found) == ["fine"]
        assert len(errors) == 1
        assert "broken" in errors[0] and "description" in errors[0]

    def test_found_in_a_stable_order(self, env):
        for dirname in ("charlie", "alpha", "bravo"):
            make_skill(env.cwd / "skills", dirname)
        found, _ = discover_skills()
        assert names(found) == ["alpha", "bravo", "charlie"]


class TestParse:
    def test_name_defaults_to_the_folder(self, tmp_path):
        skill_dir = make_skill(tmp_path, "from-folder", frontmatter=f"description: {DESCRIPTION}")
        assert parse_skill(skill_dir).name == "from-folder"

    def test_frontmatter_name_wins_over_the_folder(self, tmp_path):
        skill_dir = make_skill(
            tmp_path, "folder", frontmatter=f"name: declared\ndescription: {DESCRIPTION}"
        )
        assert parse_skill(skill_dir).name == "declared"

    def test_path_is_resolved(self, tmp_path):
        skill_dir = make_skill(tmp_path, "resolved")
        assert parse_skill(tmp_path / "." / "resolved").path == skill_dir.resolve()

    def test_no_frontmatter(self, tmp_path):
        skill_dir = tmp_path / "plain"
        skill_dir.mkdir()
        (skill_dir / SKILL_FILENAME).write_text("# Just markdown\n", encoding="utf-8")
        with pytest.raises(SkillError, match="no YAML frontmatter"):
            parse_skill(skill_dir)

    def test_unclosed_frontmatter(self, tmp_path):
        skill_dir = tmp_path / "unclosed"
        skill_dir.mkdir()
        (skill_dir / SKILL_FILENAME).write_text(
            f"---\nname: unclosed\ndescription: {DESCRIPTION}\n# body\n", encoding="utf-8"
        )
        with pytest.raises(SkillError, match="no YAML frontmatter"):
            parse_skill(skill_dir)

    def test_empty_frontmatter_asks_for_the_description(self, tmp_path):
        skill_dir = tmp_path / "empty"
        skill_dir.mkdir()
        (skill_dir / SKILL_FILENAME).write_text("---\n---\n# body\n", encoding="utf-8")
        with pytest.raises(SkillError, match="'description' is required"):
            parse_skill(skill_dir)

    def test_invalid_yaml(self, tmp_path):
        skill_dir = make_skill(tmp_path, "bad-yaml", frontmatter="name: [unclosed\ndescription: x")
        with pytest.raises(SkillError, match="not valid YAML"):
            parse_skill(skill_dir)

    def test_frontmatter_must_be_a_mapping(self, tmp_path):
        skill_dir = make_skill(tmp_path, "a-list", frontmatter="- name\n- description")
        with pytest.raises(SkillError, match="mapping"):
            parse_skill(skill_dir)

    @pytest.mark.parametrize("description", ["", "   ", "[a, b]", "42"])
    def test_description_must_be_non_empty_text(self, tmp_path, description):
        skill_dir = make_skill(
            tmp_path, "no-desc", frontmatter=f"name: no-desc\ndescription: {description}"
        )
        with pytest.raises(SkillError, match="'description' is required"):
            parse_skill(skill_dir)

    @pytest.mark.parametrize(
        "name",
        ["My Skill", "UPPER", "-leading", "trailing-", "dou--ble", "under_score", "x" * 65],
    )
    def test_invalid_names_are_refused(self, tmp_path, name):
        skill_dir = make_skill(
            tmp_path, "folder", frontmatter=f"name: '{name}'\ndescription: {DESCRIPTION}"
        )
        with pytest.raises(SkillError, match="lowercase letters"):
            parse_skill(skill_dir)

    @pytest.mark.parametrize("name", ["7", "2024-01-01", "true"])
    def test_an_unquoted_non_text_name_says_to_quote_it(self, tmp_path, name):
        skill_dir = make_skill(
            tmp_path, "folder", frontmatter=f"name: {name}\ndescription: {DESCRIPTION}"
        )
        with pytest.raises(SkillError, match="quote it"):
            parse_skill(skill_dir)

    def test_a_quoted_digit_name_is_valid(self, tmp_path):
        skill_dir = make_skill(
            tmp_path, "folder", frontmatter=f"name: '7'\ndescription: {DESCRIPTION}"
        )
        assert parse_skill(skill_dir).name == "7"

    def test_longest_valid_name(self, tmp_path):
        name = "a" * 64
        skill_dir = make_skill(tmp_path, name)
        assert parse_skill(skill_dir).name == name

    def test_folded_description_becomes_one_line(self, tmp_path):
        skill_dir = make_skill(
            tmp_path,
            "folded",
            frontmatter="name: folded\ndescription: >\n  Line one\n  and line two.\n",
        )
        assert parse_skill(skill_dir).description == "Line one and line two."

    def test_description_with_a_colon_survives(self, tmp_path):
        # A naive `key: value` splitter would cut this at the second colon.
        skill_dir = make_skill(
            tmp_path, "colon", frontmatter='name: colon\ndescription: "Use when: anything"'
        )
        assert parse_skill(skill_dir).description == "Use when: anything"

    def test_overlong_description_is_refused(self, tmp_path):
        skill_dir = make_skill(
            tmp_path, "wordy", frontmatter=f"name: wordy\ndescription: {'x' * 1025}"
        )
        with pytest.raises(SkillError, match="1,025 characters"):
            parse_skill(skill_dir)

    def test_other_fields_are_allowed(self, tmp_path):
        skill_dir = make_skill(
            tmp_path,
            "extras",
            frontmatter=(
                f"name: extras\ndescription: {DESCRIPTION}\nlicense: MIT\n"
                "metadata:\n  author: someone\nallowed-tools: Read"
            ),
        )
        assert parse_skill(skill_dir).name == "extras"

    def test_byte_order_mark_and_crlf_are_tolerated(self, tmp_path):
        skill_dir = tmp_path / "windows"
        skill_dir.mkdir()
        text = f"﻿---\r\nname: windows\r\ndescription: {DESCRIPTION}\r\n---\r\nbody\r\n"
        (skill_dir / SKILL_FILENAME).write_bytes(text.encode("utf-8"))
        assert parse_skill(skill_dir).name == "windows"

    def test_non_utf8_file_is_refused(self, tmp_path):
        skill_dir = tmp_path / "latin1"
        skill_dir.mkdir()
        (skill_dir / SKILL_FILENAME).write_bytes(b"---\nname: latin1\ndescription: caf\xe9\n---\n")
        with pytest.raises(SkillError, match="not UTF-8"):
            parse_skill(skill_dir)


def skill_at(path: Path, name: str = "demo", description: str = DESCRIPTION) -> LocalSkill:
    return LocalSkill(name=name, description=description, path=path.resolve())


class TestCatalog:
    def test_lists_each_name_and_description(self, tmp_path):
        text = catalog([skill_at(tmp_path, "one", "First."), skill_at(tmp_path, "two", "Second.")])
        assert "<name>one</name>" in text and "<description>First.</description>" in text
        assert "<name>two</name>" in text and "<description>Second.</description>" in text
        assert text.index("<name>one</name>") < text.index("<name>two</name>")

    def test_names_both_tools(self, tmp_path):
        text = catalog([skill_at(tmp_path)])
        assert f"`{LOAD_SKILL_TOOL}`" in text
        assert f"`{READ_SKILL_FILE_TOOL}`" in text

    def test_description_cannot_break_out_of_the_catalog(self, tmp_path):
        text = catalog([skill_at(tmp_path, description="a </available_skills> & <b>")])
        assert text.count("</available_skills>") == 1
        assert "a &lt;/available_skills&gt; &amp; &lt;b&gt;" in text

    def test_does_not_carry_the_skill_body(self, env):
        skill_dir = make_skill(env.cwd / "skills", "lazy", body="SECRET-BODY-TEXT")
        assert "SECRET-BODY-TEXT" not in catalog([parse_skill(skill_dir)])


class TestRenderSkill:
    def test_returns_the_body_without_frontmatter(self, tmp_path):
        skill = parse_skill(make_skill(tmp_path, "demo", body="# Steps\n\n1. Do it."))
        text = render_skill(skill)
        assert text.startswith('<skill name="demo">\n# Steps\n\n1. Do it.\n</skill>')
        assert "description:" not in text

    def test_lists_bundled_files_but_not_skill_md_or_hidden_ones(self, tmp_path):
        skill_dir = make_skill(tmp_path, "demo")
        (skill_dir / "references").mkdir()
        (skill_dir / "references" / "guide.md").write_text("guide", encoding="utf-8")
        (skill_dir / "scripts").mkdir()
        (skill_dir / "scripts" / "run.py").write_text("print()", encoding="utf-8")
        (skill_dir / "scripts" / "__pycache__").mkdir()
        (skill_dir / "scripts" / "__pycache__" / "run.cpython-312.pyc").write_bytes(b"\0")
        (skill_dir / ".git").mkdir()
        (skill_dir / ".git" / "HEAD").write_text("ref", encoding="utf-8")
        (skill_dir / ".DS_Store").write_bytes(b"\0")

        text = render_skill(parse_skill(skill_dir))
        listed = [line[2:] for line in text.splitlines() if line.startswith("- ")]
        assert listed == ["references/guide.md", "scripts/run.py"]

    def test_no_bundled_files_means_no_listing(self, tmp_path):
        text = render_skill(parse_skill(make_skill(tmp_path, "demo")))
        assert "Files in this skill" not in text

    def test_listing_is_capped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(skills, "MAX_LISTED_FILES", 2)
        skill_dir = make_skill(tmp_path, "demo")
        for i in range(5):
            (skill_dir / f"ref{i}.md").write_text("x", encoding="utf-8")
        text = render_skill(parse_skill(skill_dir))
        assert "- ref0.md\n- ref1.md\n- … (only the first 2 files are listed)" in text
        assert "ref2.md" not in text

    def test_an_edit_applies_the_next_time_it_is_loaded(self, tmp_path):
        skill_dir = make_skill(tmp_path, "demo", body="old instructions")
        skill = parse_skill(skill_dir)
        (skill_dir / SKILL_FILENAME).write_text(
            f"---\nname: demo\ndescription: {DESCRIPTION}\n---\nnew instructions\n",
            encoding="utf-8",
        )
        assert "new instructions" in render_skill(skill)

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
    def test_a_symlink_out_of_the_skill_is_not_listed(self, tmp_path):
        outside = tmp_path / "outside.md"
        outside.write_text("secret", encoding="utf-8")
        skill_dir = make_skill(tmp_path, "demo")
        (skill_dir / "inside.md").write_text("fine", encoding="utf-8")
        (skill_dir / "escape.md").symlink_to(outside)
        text = render_skill(parse_skill(skill_dir))
        assert "inside.md" in text
        assert "escape.md" not in text


class TestReadBundledFile:
    @pytest.fixture
    def skill(self, tmp_path) -> Skill:
        skill_dir = make_skill(tmp_path, "demo")
        (skill_dir / "references").mkdir()
        (skill_dir / "references" / "guide.md").write_text("# Guide\n", encoding="utf-8")
        make_skill(tmp_path, "neighbour", body="the neighbour's instructions")
        return parse_skill(skill_dir)

    def test_reads_a_reference(self, skill):
        assert read_bundled_file(skill, "references/guide.md") == "# Guide\n"

    def test_tolerates_surrounding_whitespace(self, skill):
        assert read_bundled_file(skill, "  references/guide.md\n") == "# Guide\n"

    def test_can_reread_skill_md(self, skill):
        assert "Follow these steps." in read_bundled_file(skill, SKILL_FILENAME)

    @pytest.mark.parametrize(
        "path", ["../neighbour/SKILL.md", "references/../../neighbour/SKILL.md", ".."]
    )
    def test_refuses_to_climb_out(self, skill, path):
        with pytest.raises(SkillError, match="outside the skill"):
            read_bundled_file(skill, path)

    def test_refuses_an_absolute_path(self, skill):
        with pytest.raises(SkillError, match="absolute"):
            read_bundled_file(skill, str(skill.path / "references" / "guide.md"))

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
    def test_refuses_a_symlink_that_points_out(self, skill, tmp_path):
        (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
        (skill.path / "link.txt").symlink_to(tmp_path / "secret.txt")
        with pytest.raises(SkillError, match="outside the skill"):
            read_bundled_file(skill, "link.txt")

    def test_missing_file(self, skill):
        with pytest.raises(SkillError, match="has no file 'references/nope.md'"):
            read_bundled_file(skill, "references/nope.md")

    def test_a_directory_is_not_a_file(self, skill):
        with pytest.raises(SkillError, match="has no file"):
            read_bundled_file(skill, "references")

    def test_empty_path(self, skill):
        with pytest.raises(SkillError, match="give the file's path"):
            read_bundled_file(skill, "  ")

    def test_too_large(self, skill, monkeypatch):
        monkeypatch.setattr(skills, "MAX_FILE_BYTES", 4)
        with pytest.raises(SkillError, match="over the 4-byte limit"):
            read_bundled_file(skill, "references/guide.md")

    def test_binary_file(self, skill):
        (skill.path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
        with pytest.raises(SkillError, match="not UTF-8"):
            read_bundled_file(skill, "logo.png")


def script(*calls: tuple[str, dict]):
    """A model that makes *calls* one per turn, then answers "done".

    Records each request's instructions, so a test can see what the model saw.
    """
    seen: list[str | None] = []

    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info.instructions)
        made = sum(
            isinstance(part, ToolCallPart)
            for message in messages
            if isinstance(message, ModelResponse)
            for part in message.parts
        )
        if made < len(calls):
            tool_name, args = calls[made]
            return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args)])
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(model_fn), seen


async def run_collecting(agent: Agent) -> list[FunctionToolResultEvent]:
    """Drive one turn with the app's own node/event loop, keeping tool results."""
    results: list[FunctionToolResultEvent] = []
    async with agent.iter("go") as agent_run:
        async for node in agent_run:
            if Agent.is_call_tools_node(node):
                async with node.stream(agent_run.ctx) as stream:
                    async for event in stream:
                        if isinstance(event, FunctionToolResultEvent):
                            results.append(event)
    return results


class TestToolset:
    @pytest.fixture
    def skill(self, tmp_path) -> Skill:
        skill_dir = make_skill(tmp_path, "obml-modeling", body="Call get_obml_reference first.")
        (skill_dir / "references").mkdir()
        (skill_dir / "references" / "measures.md").write_text("SUM(amount)", encoding="utf-8")
        return parse_skill(skill_dir)

    def test_no_skills_no_toolset(self):
        assert skills_toolset([]) is None

    def test_tools_are_named_as_the_trimmer_expects(self, skill):
        toolset = skills_toolset([skill])
        assert isinstance(toolset, FunctionToolset)
        assert set(toolset.tools) == {LOAD_SKILL_TOOL, READ_SKILL_FILE_TOOL}

    async def test_a_real_run_sees_the_catalog_and_loads_the_skill(self, skill):
        model, seen = script(
            (LOAD_SKILL_TOOL, {"name": "obml-modeling"}),
            (READ_SKILL_FILE_TOOL, {"name": "obml-modeling", "path": "references/measures.md"}),
        )
        agent = Agent(model=model, toolsets=[skills_toolset([skill])])
        results = await run_collecting(agent)

        # The catalog went out with every request, the body did not.
        assert seen and all("<name>obml-modeling</name>" in (i or "") for i in seen)
        assert all("Call get_obml_reference first." not in (i or "") for i in seen)

        # And the results pass through the same helper `on_message` uses.
        loaded, reference = (app._tool_result_fields(event) for event in results)
        assert loaded.tool_name == LOAD_SKILL_TOOL
        assert "Call get_obml_reference first." in loaded.text
        assert "- references/measures.md" in loaded.text
        assert reference.tool_name == READ_SKILL_FILE_TOOL
        assert reference.text == "SUM(amount)"

    async def test_an_unknown_skill_is_an_answer_not_a_failed_run(self, skill):
        model, _ = script((LOAD_SKILL_TOOL, {"name": "nope"}))
        agent = Agent(model=model, toolsets=[skills_toolset([skill])])
        (event,) = await run_collecting(agent)
        text = app._tool_result_fields(event).text
        assert text.startswith("Error: there is no skill named 'nope'")
        assert "Available: obml-modeling" in text

    async def test_a_refused_path_is_an_answer_not_a_failed_run(self, skill):
        model, _ = script((READ_SKILL_FILE_TOOL, {"name": "obml-modeling", "path": "../../etc"}))
        agent = Agent(model=model, toolsets=[skills_toolset([skill])])
        (event,) = await run_collecting(agent)
        assert "outside the skill" in app._tool_result_fields(event).text

    async def test_an_unreadable_skill_does_not_end_the_turn(self, skill, monkeypatch):
        def unreadable(_skill):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(skills, "render_skill", unreadable)
        model, _ = script((LOAD_SKILL_TOOL, {"name": "obml-modeling"}))
        agent = Agent(model=model, toolsets=[skills_toolset([skill])])
        (event,) = await run_collecting(agent)
        assert app._tool_result_fields(event).text.startswith("Error:")


class TestMakeAgent:
    @pytest.fixture(autouse=True)
    def offline_model(self, monkeypatch):
        monkeypatch.setattr(agent_module, "resolve_model", lambda provider, model: TestModel())

    @staticmethod
    def skill_toolsets(agent: Agent) -> list[FunctionToolset]:
        return [
            t
            for t in agent.toolsets
            if isinstance(t, FunctionToolset) and LOAD_SKILL_TOOL in t.tools
        ]

    def test_skills_are_offered_when_there_are_some(self, tmp_path):
        skill = parse_skill(make_skill(tmp_path, "demo"))
        agent = agent_module.make_agent("openrouter", "any", toolsets=[], skills=[skill])
        assert len(self.skill_toolsets(agent)) == 1

    def test_no_skills_means_no_skill_tools(self):
        agent = agent_module.make_agent("openrouter", "any")
        assert self.skill_toolsets(agent) == []

    def test_mcp_toolsets_are_kept_alongside(self, tmp_path):
        skill = parse_skill(make_skill(tmp_path, "demo"))
        other = FunctionToolset([])
        agent = agent_module.make_agent("openrouter", "any", toolsets=[other], skills=[skill])
        assert other in agent.toolsets


class TestHistoryTrim:
    def test_an_old_loaded_skill_is_kept_whole(self):
        body = "follow these instructions " * 100
        noise = "x" * 2_000
        messages = [
            ModelRequest(
                parts=[
                    ToolReturnPart(tool_name=LOAD_SKILL_TOOL, content=body, tool_call_id="1"),
                    ToolReturnPart(tool_name="describe_model", content=noise, tool_call_id="2"),
                ]
            ),
            *(
                ModelRequest(parts=[UserPromptPart(content=f"turn {i}")])
                for i in range(app.HISTORY_KEEP_RECENT)
            ),
        ]
        trimmed = app._trim_history(messages)
        skill_part, other_part = trimmed[0].parts
        assert skill_part.content == body
        assert len(other_part.content) < len(noise)


class TestGreeting:
    @pytest.fixture
    def session(self):
        store: dict = {}
        with (
            patch.object(app.cl, "user_session") as user_session,
            patch.object(
                app.cl, "CustomElement", side_effect=lambda **kwargs: SimpleNamespace(**kwargs)
            ),
        ):
            user_session.set.side_effect = store.__setitem__
            user_session.get.side_effect = lambda k, d=None: store.get(k, d)
            yield store

    def test_no_skills_adds_nothing(self, session):
        session["mcp_info"] = "Connected MCP servers:\n- `X`"
        assert app._skills_info() == ""
        assert app._skill_elements() == []
        assert app._status_info() == "Connected MCP servers:\n- `X`"

    def test_skills_get_a_checklist_under_the_greeting(self, session, tmp_path):
        session["skills"] = [
            skill_at(tmp_path, "one", "First."),
            server_skill("two", FakeServer(), "Second."),
        ]
        session["disabled_skills"] = frozenset({"two", "not-listed"})
        (element,) = app._skill_elements()
        assert element.name == "SkillsPanel" == app.SKILLS_PANEL
        assert element.display == "inline"
        assert element.props == {
            "skills": [
                {"name": "one", "description": "First.", "source": "local"},
                {"name": "two", "description": "Second.", "source": "Fake"},
            ],
            # Only the listed ones: the panel compares this with the cookie.
            "disabled": ["two"],
            "cookie": skills.DISABLED_SKILLS_COOKIE,
            "action": app.SKILLS_ACTION,
        }

    def test_the_checklist_ships_with_the_package(self):
        element = Path(app.__file__).parent / "public" / "elements" / f"{app.SKILLS_PANEL}.jsx"
        source = element.read_text(encoding="utf-8")
        assert "export default function SkillsPanel()" in source
        # The props the server sends are the ones the element reads.
        for prop in ("skills", "disabled", "cookie", "action"):
            assert f"props.{prop}" in source

    def test_the_element_and_the_composer_pane_speak_the_same_events(self):
        # The element owns the choice; header.js draws the Skills button and
        # its pane. Nothing but these names connects the two.
        public = Path(app.__file__).parent / "public"
        element = (public / "elements" / f"{app.SKILLS_PANEL}.jsx").read_text(encoding="utf-8")
        pane = (public / "header.js").read_text(encoding="utf-8")
        for event in ("orionbelt:skills-state", "orionbelt:skills-open", "orionbelt:skills-choose"):
            assert f'"{event}"' in element, event
            assert f'"{event}"' in pane, event
        assert "window.__orionbeltSkills" in element and "window.__orionbeltSkills" in pane

    def test_skipped_skills_are_named_with_the_reason(self, session):
        session["skill_errors"] = ["/x/SKILL.md: 'description' is required"]
        assert app._skills_info() == "⚠️ Skill ignored — /x/SKILL.md: 'description' is required"

    def test_status_info_puts_problems_after_the_servers(self, session):
        session["mcp_info"] = "Connected MCP servers:\n- `X`"
        session["skill_errors"] = ["broken"]
        info = app._status_info()
        assert info.index("Connected MCP servers") < info.index("⚠️ Skill ignored — broken")

    def test_discovery_is_kept_in_the_session(self, session, env):
        make_skill(env.cwd / "skills", "good")
        make_skill(env.cwd / "skills", "bad", frontmatter="name: bad")
        found = asyncio.run(app._gather_skills([]))
        assert names(found) == ["good"]
        assert session["skills"] == found
        assert len(session["skill_errors"]) == 1


class TestDisabledSkillsCookie:
    @staticmethod
    def header(value: str, others: str = "theme=dark; bad cookie; =x") -> str:
        return f"{others}; {skills.DISABLED_SKILLS_COOKIE}={value}; session=abc"

    def test_reads_the_names(self):
        value = "%5B%22one%22%2C%22two%22%5D"  # ["one","two"], as the panel encodes it
        assert disabled_skills_from_cookie(self.header(value)) == {"one", "two"}

    @pytest.mark.parametrize("header", [None, "", "theme=dark"])
    def test_no_cookie_means_nothing_disabled(self, header):
        assert disabled_skills_from_cookie(header) == frozenset()

    @pytest.mark.parametrize("value", ["not-json", "%7B%7D", "%22one%22", "%5B"])
    def test_an_unreadable_value_means_nothing_disabled(self, value):
        assert disabled_skills_from_cookie(self.header(value)) == frozenset()

    def test_only_plausible_names_survive(self):
        assert parse_disabled_skills(["ok-one", 7, "Not Valid", "x" * 65, None]) == {"ok-one"}

    def test_the_list_is_capped(self, monkeypatch):
        monkeypatch.setattr(skills, "MAX_DISABLED_SKILLS", 2)
        assert len(parse_disabled_skills(["a", "b", "c"])) == 2


class TestSkillSelection:
    """The checklist's action: switch skills off for the running chat."""

    @pytest.fixture
    def session(self, tmp_path):
        store: dict = {
            "provider": "openrouter",
            "model": "m",
            "agent": object(),
            "mcp_contexts": [],
            "pydantic_history": ["kept"],
            "skills": [skill_at(tmp_path, "one"), skill_at(tmp_path, "two")],
            "disabled_skills": frozenset(),
        }
        built: list[dict] = []

        def fake_make_agent(provider, model, **kwargs):
            built.append(kwargs)
            return SimpleNamespace(kwargs=kwargs)

        with (
            patch.object(app.cl, "user_session") as user_session,
            patch.object(app, "make_agent", side_effect=fake_make_agent),
        ):
            user_session.set.side_effect = store.__setitem__
            user_session.get.side_effect = lambda k, d=None: store.get(k, d)
            yield store, built

    @staticmethod
    def select(disabled) -> dict:
        action = SimpleNamespace(payload={"disabled": disabled})
        return asyncio.run(app.on_skill_selection(action))

    def test_rebuilds_the_agent_without_the_unticked_skill(self, session):
        store, built = session
        assert self.select(["two", "absent-one"]) == {"applied": True}
        assert store["disabled_skills"] == {"two", "absent-one"}
        assert names(built[0]["skills"]) == ["one"]
        assert store["agent"].kwargs is built[0]
        assert store["pydantic_history"] == ["kept"]

    def test_the_same_choice_again_rebuilds_nothing(self, session):
        _store, built = session
        self.select([])
        assert built == []

    def test_a_malformed_payload_switches_nothing_off(self, session):
        store, built = session
        store["disabled_skills"] = frozenset({"one"})
        self.select("one,two")
        assert store["disabled_skills"] == frozenset()
        assert names(built[0]["skills"]) == ["one", "two"]

    def test_without_an_agent_the_choice_waits_for_the_next_build(self, session):
        store, built = session
        store["agent"] = None
        assert self.select(["one"]) == {"applied": False}
        assert store["disabled_skills"] == {"one"}
        assert built == []

    def test_every_agent_build_leaves_disabled_skills_out(self, session):
        store, _built = session
        store["disabled_skills"] = frozenset({"one"})
        assert names(app._enabled_skills(store["skills"])) == ["two"]


class TestCookieAtChatStart:
    def test_the_connection_cookie_is_read(self):
        value = "%5B%22one%22%5D"
        session = SimpleNamespace(
            environ={"HTTP_COOKIE": f"{skills.DISABLED_SKILLS_COOKIE}={value}"}
        )
        with patch.object(app.cl, "context", SimpleNamespace(session=session)):
            assert disabled_skills_from_cookie(app._cookie_header()) == {"one"}

    @pytest.mark.parametrize("environ", [None, {}, {"HTTP_COOKIE": 7}])
    def test_no_usable_header_is_none(self, environ):
        session = SimpleNamespace(environ=environ)
        with patch.object(app.cl, "context", SimpleNamespace(session=session)):
            assert app._cookie_header() is None


# ── Skills from MCP servers ───────────────────────────────────────────────


class FakeServer:
    """Stands in for a connected MCPToolset: listed resources, readable URIs, and a log."""

    def __init__(self, resources=None, listed=None, listing_error=None, listing_delay=0.0):
        self.resources = resources or {}
        self.listed = listed or []
        self.listing_error = listing_error
        self.listing_delay = listing_delay
        self.reads: list[str] = []

    async def list_resources(self):
        if self.listing_delay:
            await asyncio.sleep(self.listing_delay)
        if self.listing_error:
            raise self.listing_error
        return self.listed

    async def read_resource(self, uri: str):
        self.reads.append(uri)
        if uri not in self.resources:
            raise RuntimeError(f"Unknown resource: {uri}")
        return self.resources[uri]


def listed(uri: str, description: str | None = DESCRIPTION) -> SimpleNamespace:
    return SimpleNamespace(uri=uri, description=description)


def server_skill(name: str, server: FakeServer, description: str = DESCRIPTION) -> ServerSkill:
    return ServerSkill(name=name, description=description, server_name="Fake", toolset=server)


SKILL_TEXT = f"---\nname: remote\ndescription: {DESCRIPTION}\n---\n# Remote\n\nDo the thing.\n"


class TestServerSkillDiscovery:
    def test_only_skill_md_resources_are_skills(self):
        server = FakeServer(
            listed=[
                listed("skill://remote/SKILL.md"),
                listed("skill://remote/_manifest", "File listing"),
                listed("skill://remote"),  # an older, bare URI some servers keep
                listed("notes://readme", "Not a skill"),
            ]
        )
        found, errors = asyncio.run(discover_server_skills("Fake", server))
        assert [s.name for s in found] == ["remote"]
        assert found[0].description == DESCRIPTION
        assert errors == []

    def test_a_listed_description_costs_no_read(self):
        server = FakeServer(listed=[listed("skill://remote/SKILL.md")])
        asyncio.run(discover_server_skills("Fake", server))
        assert server.reads == []

    @pytest.mark.parametrize("uri", ["skill://Bad_Name/SKILL.md", "skill://a/b/SKILL.md"])
    def test_an_unusable_name_is_reported(self, uri):
        found, errors = asyncio.run(
            discover_server_skills("Fake", FakeServer(listed=[listed(uri)]))
        )
        assert found == []
        assert len(errors) == 1
        assert errors[0].startswith("MCP server 'Fake', skill")
        assert "lowercase letters" in errors[0]

    @pytest.mark.parametrize("description", [None, "", ">-", ">", "|", "|-", ">2+"])
    def test_a_missing_or_mangled_description_is_read_from_the_file(self, description):
        server = FakeServer(
            listed=[listed("skill://remote/SKILL.md", description)],
            resources={"skill://remote/SKILL.md": SKILL_TEXT},
        )
        (skill,), errors = asyncio.run(discover_server_skills("Fake", server))
        assert skill.description == DESCRIPTION
        assert server.reads == ["skill://remote/SKILL.md"]

    def test_no_description_anywhere_is_reported(self):
        server = FakeServer(
            listed=[listed("skill://remote/SKILL.md", ">-")],
            resources={"skill://remote/SKILL.md": "# no frontmatter"},
        )
        found, errors = asyncio.run(discover_server_skills("Fake", server))
        assert found == []
        assert "no usable description" in errors[0]

    def test_an_overlong_description_is_shortened_not_refused(self):
        server = FakeServer(listed=[listed("skill://remote/SKILL.md", "word " * 400)])
        (skill,), errors = asyncio.run(discover_server_skills("Fake", server))
        assert len(skill.description) == MAX_DESCRIPTION_CHARS
        assert skill.description.endswith("…")
        assert errors == []

    def test_a_failed_listing_is_the_callers_to_handle(self):
        server = FakeServer(listing_error=RuntimeError("boom"))
        with pytest.raises(RuntimeError, match="boom"):
            asyncio.run(discover_server_skills("Fake", server))


class TestServerSkillReading:
    def test_instructions_drop_the_frontmatter_and_list_the_manifest(self):
        manifest = (
            '{"skill": "remote", "files": [{"path": "SKILL.md"}, '
            '{"path": "references/a.md"}, {"path": ".hidden"}, '
            '{"path": "scripts/__pycache__/x.pyc"}, {"path": "scripts/run.py"}]}'
        )
        server = FakeServer(
            resources={
                "skill://remote/SKILL.md": SKILL_TEXT,
                "skill://remote/_manifest": manifest,
            }
        )
        text = asyncio.run(server_skill("remote", server).instructions())
        assert text.startswith('<skill name="remote">\n# Remote\n\nDo the thing.\n</skill>')
        listed_files = [line[2:] for line in text.splitlines() if line.startswith("- ")]
        assert listed_files == ["references/a.md", "scripts/run.py"]

    @pytest.mark.parametrize("manifest", [None, "not json", '{"files": "nope"}', "[]"])
    def test_no_usable_manifest_means_no_listing(self, manifest):
        resources = {"skill://remote/SKILL.md": SKILL_TEXT}
        if manifest is not None:
            resources["skill://remote/_manifest"] = manifest
        text = asyncio.run(server_skill("remote", FakeServer(resources=resources)).instructions())
        assert "Do the thing." in text
        assert "Files in this skill" not in text

    def test_a_body_served_without_frontmatter_is_used_whole(self):
        server = FakeServer(resources={"skill://remote/SKILL.md": "Just do it."})
        text = asyncio.run(server_skill("remote", server).instructions())
        assert '<skill name="remote">\nJust do it.\n</skill>' in text

    def test_a_server_error_becomes_a_skill_error(self):
        with pytest.raises(SkillError, match="could not read skill://remote/SKILL.md"):
            asyncio.run(server_skill("remote", FakeServer()).instructions())

    def test_reads_a_text_file(self):
        server = FakeServer(resources={"skill://remote/references/a.md": "A"})
        assert asyncio.run(server_skill("remote", server).read_file(" references/a.md ")) == "A"

    def test_several_text_parts_are_joined(self):
        server = FakeServer(resources={"skill://remote/a.md": ["one ", "two"]})
        assert asyncio.run(server_skill("remote", server).read_file("a.md")) == "one two"

    def test_binary_content_is_refused(self):
        png = BinaryContent(data=b"\x89PNG", media_type="image/png")
        server = FakeServer(resources={"skill://remote/logo.png": png})
        with pytest.raises(SkillError, match="not a text file"):
            asyncio.run(server_skill("remote", server).read_file("logo.png"))

    def test_too_large(self, monkeypatch):
        monkeypatch.setattr(skills, "MAX_FILE_BYTES", 3)
        server = FakeServer(resources={"skill://remote/a.md": "four"})
        with pytest.raises(SkillError, match="over the 3-byte limit"):
            asyncio.run(server_skill("remote", server).read_file("a.md"))

    @pytest.mark.parametrize(
        ("path", "message"),
        [
            ("", "give the file's path"),
            ("/etc/passwd", "absolute"),
            ("\\\\share\\x", "absolute"),
            ("../other/SKILL.md", "outside the skill"),
            ("references/../../x", "outside the skill"),
            ("..\\x", "outside the skill"),
        ],
    )
    def test_paths_outside_the_skill_never_reach_the_server(self, path, message):
        server = FakeServer()
        with pytest.raises(SkillError, match=message):
            asyncio.run(server_skill("remote", server).read_file(path))
        assert server.reads == []


class TestMergeSkills:
    def test_the_first_source_keeps_a_shared_name(self, tmp_path):
        local = skill_at(tmp_path, "shared")
        remote = server_skill("shared", FakeServer())
        other = server_skill("other", FakeServer())
        kept, errors = merge_skills([local], [remote, other])
        assert kept == [local, other]
        assert errors == [
            f"MCP server 'Fake': skill 'shared' is already provided by "
            f"{local.origin}, which comes first"
        ]


class TestGatherSkills:
    @pytest.fixture
    def session(self, env):
        store: dict = {}
        with patch.object(app.cl, "user_session") as user_session:
            user_session.set.side_effect = store.__setitem__
            user_session.get.side_effect = lambda k, d=None: store.get(k, d)
            yield store

    @staticmethod
    def allow(*names):
        return patch.object(app, "servers_with_skills", return_value=frozenset(names))

    def test_local_skills_come_first(self, session, env):
        make_skill(env.cwd / "skills", "shared")
        server = FakeServer(
            listed=[listed("skill://shared/SKILL.md"), listed("skill://remote/SKILL.md")]
        )
        with self.allow("Fake"):
            found = asyncio.run(app._gather_skills([("Fake", server)]))
        assert [(s.name, type(s).__name__) for s in found] == [
            ("shared", "LocalSkill"),
            ("remote", "ServerSkill"),
        ]
        assert len(session["skill_errors"]) == 1
        assert "already provided by" in session["skill_errors"][0]

    def test_a_server_switched_off_is_not_asked(self, session):
        server = FakeServer(listing_error=AssertionError("should not be listed"))
        with self.allow():
            assert asyncio.run(app._gather_skills([("Fake", server)])) == []

    def test_a_failed_or_slow_server_does_not_cost_the_others(self, session, monkeypatch):
        monkeypatch.setattr(app, "_TOOL_LIST_TIMEOUT", 0.05)
        servers = [
            ("Broken", FakeServer(listing_error=RuntimeError("boom"))),
            ("Slow", FakeServer(listed=[listed("skill://slow/SKILL.md")], listing_delay=5)),
            ("Fine", FakeServer(listed=[listed("skill://fine/SKILL.md")])),
        ]
        with self.allow("Broken", "Slow", "Fine"):
            found = asyncio.run(app._gather_skills(servers))
        assert names(found) == ["fine"]


class TestSkillUsageInTheUI:
    @pytest.mark.parametrize(
        ("tool", "args", "title"),
        [
            (LOAD_SKILL_TOOL, {"name": "obml-modeling"}, "Skill: obml-modeling"),
            (LOAD_SKILL_TOOL, {"name": "skill://obml-modeling/SKILL.md"}, "Skill: obml-modeling"),
            (
                READ_SKILL_FILE_TOOL,
                {"name": "obml-modeling", "path": "references/a.md"},
                "Skill file: obml-modeling › references/a.md",
            ),
            (
                READ_SKILL_FILE_TOOL,
                {"name": "obml-modeling", "path": "skill://obml-modeling/references/a.md"},
                "Skill file: obml-modeling › references/a.md",
            ),
            (READ_SKILL_FILE_TOOL, {"name": "obml-modeling"}, READ_SKILL_FILE_TOOL),
            (LOAD_SKILL_TOOL, {"name": "  "}, LOAD_SKILL_TOOL),
            (LOAD_SKILL_TOOL, '{"name": unparsed', LOAD_SKILL_TOOL),
            (LOAD_SKILL_TOOL, {"name": 7}, LOAD_SKILL_TOOL),
            ("describe_model", {"name": "sales"}, "describe_model"),
        ],
    )
    def test_step_titles(self, tool, args, title):
        assert app._tool_step_name(tool, args) == title

    def test_a_loaded_skill_is_recognised_by_its_result(self):
        assert app._loaded_skill(LOAD_SKILL_TOOL, '<skill name="x">\nbody\n</skill>') == "x"

    @pytest.mark.parametrize(
        ("tool", "text"),
        [
            (LOAD_SKILL_TOOL, "Error: there is no skill named 'x'"),
            (READ_SKILL_FILE_TOOL, '<skill name="x">'),
            ("echo", '<skill name="x">'),
        ],
    )
    def test_anything_else_is_not_a_loaded_skill(self, tool, text):
        assert app._loaded_skill(tool, text) is None

    def test_used_line_names_each_skill_once_with_its_server(self, tmp_path):
        store = {"skills": [skill_at(tmp_path, "local-one"), server_skill("remote", FakeServer())]}
        with patch.object(app.cl, "user_session") as user_session:
            user_session.get.side_effect = lambda k, d=None: store.get(k, d)
            line = app._skills_used_line(["remote", "local-one", "remote"])
            single = app._skills_used_line(["local-one"])
        assert line == "_Skills used:_ `remote` _(from Fake)_, `local-one`"
        assert single == "_Skill used:_ `local-one`"


class TestServerInstructionsBlock:
    def test_none_without_instructions(self):
        assert agent_module.server_instructions_block([]) is None

    def test_each_server_under_its_own_name(self):
        block = agent_module.server_instructions_block([("A", "Use a."), ("B", "Use b.")])
        assert block is not None
        assert block.startswith("## Instructions from MCP servers")
        assert block.index("### A\n\nUse a.") < block.index("### B\n\nUse b.")

    def test_a_long_text_is_cut_and_says_so(self, monkeypatch):
        monkeypatch.setattr(agent_module, "MAX_SERVER_INSTRUCTIONS_CHARS", 10)
        block = agent_module.server_instructions_block([("A", "x" * 50)])
        assert "### A\n\nxxxxxxxxxx\n\n… (cut at 10 of 50 characters)" in (block or "")

    def test_make_agent_without_any_sends_none(self, monkeypatch):
        monkeypatch.setattr(agent_module, "resolve_model", lambda provider, model: TestModel())
        model, seen = script()
        monkeypatch.setattr(agent_module, "resolve_model", lambda provider, name: model)
        asyncio.run(agent_module.make_agent("openrouter", "any").run("go"))
        assert seen == [None]
