"""A tiny MCP server over Streamable HTTP that requires a bearer token.

Not a test module (pytest ignores the name): the tests launch it as a
subprocess, `python fake_mcp_http_server.py <port>`, and connect to it over
real HTTP the way the app reaches a remote OrionBelt server in user mode.

It accepts any bearer token except `REFUSED_TOKEN` and answers 401 without
one, as a resource server does. Its `whoami` tool returns the token the
request carried, so a test can see exactly which token reached the server.
"""

from __future__ import annotations

import sys

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP

REFUSED_TOKEN = "refused-token"


class AnyBearerButOne:
    """Accepts every bearer token except the one the tests use to be refused."""

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or token == REFUSED_TOKEN:
            return None
        return AccessToken(token=token, client_id="orionbelt-chat", scopes=[])


def build(port: int) -> FastMCP:
    mcp = FastMCP(
        "fake-user-server",
        host="127.0.0.1",
        port=port,
        stateless_http=True,
        token_verifier=AnyBearerButOne(),
        auth=AuthSettings(
            issuer_url="https://idp.test",
            resource_server_url=f"http://127.0.0.1:{port}/mcp",
        ),
    )

    @mcp.tool()
    def whoami() -> str:
        """Return the bearer token this request carried."""
        access = get_access_token()
        return access.token if access is not None else ""

    return mcp


if __name__ == "__main__":
    build(int(sys.argv[1])).run(transport="streamable-http")
