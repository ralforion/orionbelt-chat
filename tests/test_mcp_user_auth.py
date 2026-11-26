"""An `auth: user` MCP server over real HTTP, reached with the signed-in user's token.

The server is `fake_mcp_http_server.py` in a subprocess. It requires a bearer
token and its `whoami` tool returns the one each request carried. The token
comes from the real token store, renewed against the fake identity provider.
No network beyond localhost and no API key, so this runs in CI.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import FunctionToolResultEvent

from orionbelt_chat import sign_in
from orionbelt_chat.mcp_config import ServerDef
from orionbelt_chat.mcp_servers import _make_server
from orionbelt_chat.oidc import OidcClient, SignInBearerAuth, SignInEndedError, TokenStore
from tests.fake_idp import FakeIdp, User
from tests.test_mcp_integration import _script_one_call
from tests.test_oidc import REDIRECT_URI, Clock, _config

SERVER = Path(__file__).parent / "fake_mcp_http_server.py"
ALICE = User(sub="u-alice", username="alice", roles=["analyst"])


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server_url():
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, str(SERVER), str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                if time.monotonic() > deadline or process.poll() is not None:
                    raise RuntimeError("the fake MCP HTTP server did not start") from None
                time.sleep(0.1)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture()
def idp() -> FakeIdp:
    return FakeIdp()


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
async def signed_in(idp, clock):
    """A token store holding alice's sign-in, and its handle."""
    client = OidcClient(_config(), transport=idp.transport(), clock=clock)
    store = TokenStore(client, max_age_seconds=86_400, clock=clock)
    tokens = await client.exchange_code(idp.authorize(ALICE, REDIRECT_URI), REDIRECT_URI)
    id_claims = await client.verify_id_token(tokens.id_token)
    handle = store.add(tokens, id_claims=id_claims, roles=frozenset(ALICE.roles))
    return store, handle


def _as_user(url: str, auth: httpx.Auth, **kwargs):
    return _make_server(
        ServerDef(name="As user", endpoint=url, auth="user", **kwargs), None, None, auth
    )


async def _whoami(toolset) -> str:
    return str(await toolset.direct_call_tool("whoami", {}))


class TestUserToken:
    async def test_the_server_sees_the_users_current_token(self, server_url, signed_in):
        store, handle = signed_in
        async with _as_user(server_url, SignInBearerAuth(store, handle)) as toolset:
            assert await _whoami(toolset) == store.get(handle).tokens.access_token

    async def test_a_renewed_token_reaches_the_server_within_one_connection(
        self, server_url, signed_in, idp, clock
    ):
        store, handle = signed_in
        async with _as_user(server_url, SignInBearerAuth(store, handle)) as toolset:
            first = await _whoami(toolset)
            clock.advance(290)  # due for renewal; the connection stays open
            second = await _whoami(toolset)
        assert second != first
        assert second == store.get(handle).tokens.access_token
        assert len(idp.token_requests("refresh_token")) == 1

    async def test_an_ended_sign_in_stops_the_request_and_says_why(
        self, server_url, signed_in, idp, clock
    ):
        store, handle = signed_in
        toolset = _as_user(server_url, SignInBearerAuth(store, handle))
        with pytest.raises(BaseException) as raised:  # noqa: PT011 - wrapped by the MCP client
            async with toolset:
                await _whoami(toolset)
                idp.refresh_error = "invalid_grant"  # signed out at the IdP
                clock.advance(290)
                await _whoami(toolset)
        assert sign_in.problem_from(raised.value) == sign_in.SIGN_IN_ENDED

    async def test_without_a_sign_in_nothing_connects(self, server_url, signed_in):
        store, _ = signed_in
        with pytest.raises(BaseException) as raised:  # noqa: PT011
            async with _as_user(server_url, SignInBearerAuth(store, None)):
                pass
        assert sign_in.problem_from(raised.value) == sign_in.SIGN_IN_ENDED

    async def test_other_servers_never_get_the_users_token(self, server_url, signed_in):
        store, handle = signed_in
        other = _make_server(
            ServerDef(
                name="Other", endpoint=server_url, headers={"Authorization": "Bearer its-own-key"}
            ),
            None,
            None,
            SignInBearerAuth(store, handle),
        )
        async with other as toolset:
            assert await _whoami(toolset) == "its-own-key"

    async def test_a_token_the_server_refuses_fails_the_connection(self, server_url):
        class Refused(httpx.Auth):
            def auth_flow(self, request):
                request.headers["Authorization"] = "Bearer refused-token"
                yield request

        with pytest.raises(httpx.HTTPStatusError, match="401"):
            async with _as_user(server_url, Refused()):
                pass


class TestAgentRun:
    async def test_an_agent_turn_calls_the_server_as_the_user(self, server_url, signed_in):
        """The path the chat takes: the model calls the tool, the result comes back."""
        store, handle = signed_in
        toolset = _as_user(server_url, SignInBearerAuth(store, handle))
        agent = Agent(model=_script_one_call("whoami", {}), toolsets=[toolset])
        results: list[FunctionToolResultEvent] = []
        async with agent:
            async with agent.iter("who am I?") as run:
                async for node in run:
                    if Agent.is_call_tools_node(node):
                        async with node.stream(run.ctx) as stream:
                            async for event in stream:
                                if isinstance(event, FunctionToolResultEvent):
                                    results.append(event)
        assert [r.part.content for r in results] == [store.get(handle).tokens.access_token]


class TestProblemFrom:
    def test_an_ended_sign_in_deep_in_a_group(self):
        try:
            try:
                raise SignInEndedError("signed out")
            except SignInEndedError as inner:
                raise RuntimeError("transport failed") from inner
        except RuntimeError as wrapped:
            group = BaseExceptionGroup("task group", [ValueError("x"), wrapped])
        assert sign_in.problem_from(group) == sign_in.SIGN_IN_ENDED

    def test_an_unreachable_idp(self):
        from orionbelt_chat.oidc import IdpUnavailableError

        assert sign_in.problem_from(IdpUnavailableError("down")) == sign_in.IDP_UNAVAILABLE

    def test_anything_else(self):
        assert sign_in.problem_from(RuntimeError("Session terminated")) is None
