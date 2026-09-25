"""A tiny MCP server used by the integration tests.

Not a test module (pytest ignores the name) — it is launched as a subprocess
over stdio, exactly the way the app launches a real OrionBelt server. Its tools
exist to produce the *shapes* the streaming handler has to cope with: plain
text, structured data, binary content, and an upstream session error — plus two
that ask the user for input through MCP elicitation.
"""

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.utilities.types import Image
from pydantic import BaseModel, Field

mcp = FastMCP("fake-orionbelt")

# Smallest valid PNG: 1x1, fully transparent.
ONE_PIXEL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


@mcp.tool()
def echo(text: str) -> str:
    """Return the text unchanged."""
    return text


@mcp.tool()
def row_count(table: str) -> dict:
    """Return a structured result, the way an analytics tool would."""
    return {"table": table, "rows": 42}


@mcp.tool()
def render_chart() -> Image:
    """Return binary image content."""
    return Image(data=ONE_PIXEL_PNG, format="png")


@mcp.tool()
def flaky() -> str:
    """Return text carrying one of the app's MCP-failure phrases."""
    return "upstream failure: Session terminated by peer"


class QueryOptions(BaseModel):
    table: str = Field(description="Table to query")
    limit: int = Field(default=10, ge=1, le=100, description="Rows to return")


@mcp.tool()
async def ask_query_options(ctx: Context) -> str:
    """Ask the user for input through a form-mode elicitation."""
    result = await ctx.elicit("Which table, and how many rows?", QueryOptions)
    if result.action == "accept":
        return f"accept {result.data.model_dump()}"
    return result.action


@mcp.tool()
async def connect_warehouse(ctx: Context) -> str:
    """Send the user to a URL through a URL-mode elicitation."""
    result = await ctx.elicit_url(
        "Sign in to the warehouse to continue.",
        url="https://auth.example.com/connect?session=abc",
        elicitation_id="wh-1",
    )
    return result.action


if __name__ == "__main__":
    mcp.run()
