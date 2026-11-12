"""Pydantic AI agent factory for OrionBelt Chat."""

import logging
from collections.abc import Sequence

from pydantic_ai import Agent

from .prompts import load_system_prompt
from .providers import resolve_model
from .skills import Skill, skills_toolset

logger = logging.getLogger(__name__)

# High enough for the model to compose large tool arguments (e.g. full OBML
# YAML docs).  The default 4096 causes truncated JSON → empty tool args.
_DEFAULT_MAX_TOKENS = 16_384

#: Per server. Instructions go out with every request, so one server sending a
#: manual must not crowd out the conversation; past this they are cut, and say so.
MAX_SERVER_INSTRUCTIONS_CHARS = 16_000


def server_instructions_block(server_instructions: Sequence[tuple[str, str]]) -> str | None:
    """The instructions MCP servers sent, each under its server's name; None if none did.

    Pydantic AI's own `include_instructions` passes each server's text on
    unlabeled, which leaves the model to guess whose tools a paragraph is about
    once more than one server is connected.
    """
    if not server_instructions:
        return None
    sections = []
    for name, text in server_instructions:
        if len(text) > MAX_SERVER_INSTRUCTIONS_CHARS:
            logger.warning(
                "Instructions from %s cut to %d of %d characters",
                name,
                MAX_SERVER_INSTRUCTIONS_CHARS,
                len(text),
            )
            text = (
                text[:MAX_SERVER_INSTRUCTIONS_CHARS].rstrip()
                + f"\n\n… (cut at {MAX_SERVER_INSTRUCTIONS_CHARS:,} of {len(text):,} characters)"
            )
        sections.append(f"### {name}\n\n{text}")
    return (
        "## Instructions from MCP servers\n\n"
        "Each connected MCP server below sent these instructions about its own tools. "
        "Follow them when using that server's tools; where they conflict with your "
        "other instructions, those take precedence.\n\n" + "\n\n".join(sections)
    )


def make_agent(
    provider: str,
    model: str,
    toolsets=None,
    skills: Sequence[Skill] = (),
    server_instructions: Sequence[tuple[str, str]] = (),
) -> Agent:
    """
    Create a Pydantic AI Agent with the given toolsets (MCP servers).

    Args:
        provider: Provider name ("openrouter", "mlx", "ollama", "anthropic", "openai")
        model: Model identifier string
        toolsets: Pre-connected MCP server instances. If None, creates agent
                  with no toolsets.
        skills: Agent Skills to offer. Their catalog travels as the skills
                toolset's instructions, re-sent with every request rather than
                stored in history, so a rebuilt agent never shows a stale list.
        server_instructions: ``(server name, text)`` for each connected server
                whose initialization instructions should reach the model. Sent
                as instructions too, for the same reason.

    Returns:
        Configured Pydantic AI Agent
    """
    llm_model = resolve_model(provider, model)

    all_toolsets = list(toolsets or [])
    skill_tools = skills_toolset(skills)
    if skill_tools is not None:
        all_toolsets.append(skill_tools)

    return Agent(
        model=llm_model,
        toolsets=all_toolsets,
        system_prompt=load_system_prompt(),
        instructions=server_instructions_block(server_instructions),
        retries=3,
        model_settings={"max_tokens": _DEFAULT_MAX_TOKENS},
    )
