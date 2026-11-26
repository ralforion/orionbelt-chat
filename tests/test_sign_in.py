"""Sign-in through Chainlit's own OAuth routes, against the fake identity provider.

Drives `chainlit.server.app` over ASGI: the login redirect, the callback with
its state cookie, the session cookie Chainlit issues, and logout. These are
the routes a browser hits, so a mismatch between what the provider returns
and what Chainlit expects of it shows up here rather than in production.
"""

import json
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from chainlit.auth import get_token_from_cookies
from chainlit.context import init_http_context

from orionbelt_chat import sign_in
from orionbelt_chat.oidc import DEFAULT_ROLE_CLAIMS, DEFAULT_USER_CLAIMS, OidcConfigError
from orionbelt_chat.settings import settings
from tests.fake_idp import CLIENT_ID, CLIENT_SECRET, ISSUER, FakeIdp, User

AUTH_SECRET = "test-chainlit-auth-secret-" + "x" * 40
CHAT = "http://chat.test"
ALICE = User(
    sub="u-alice", username="alice", roles=["analyst"], email="alice@example.com", name="Alice"
)
BOB = User(sub="u-bob", username="bob", roles=["viewer"])


@pytest.fixture()
def idp() -> FakeIdp:
    return FakeIdp()


@pytest.fixture()
def oidc_settings(monkeypatch):
    """Sign-in settings for the fake IdP, and Chainlit's globals put back afterwards."""
    import chainlit.oauth_providers as chainlit_oauth
    from chainlit.config import config as chainlit_config

    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", AUTH_SECRET)
    monkeypatch.delenv("CHAINLIT_URL", raising=False)
    values = {
        "oidc_issuer": ISSUER,
        "oidc_client_id": CLIENT_ID,
        "oidc_client_secret": CLIENT_SECRET,
        "oidc_client_secret_file": "",
        "oidc_scopes": "openid profile email",
        "oidc_provider_name": "SSO",
        "oidc_allowed_roles": "",
        "oidc_role_claims": DEFAULT_ROLE_CLAIMS,
        "oidc_user_claims": DEFAULT_USER_CLAIMS,
    }
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(chainlit_oauth, "providers", list(chainlit_oauth.providers))
    for hook in ("oauth_callback", "on_logout", "on_app_startup", "on_app_shutdown"):
        monkeypatch.setattr(chainlit_config.code, hook, None)
    monkeypatch.setattr(sign_in, "_state", None)
    return monkeypatch


@pytest.fixture()
async def chat(oidc_settings, idp):
    """An HTTP client for the Chainlit app with sign-in registered and discovery done."""
    from chainlit.server import app

    assert sign_in.register(transport=idp.transport())
    await sign_in._state.client.discovery()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=CHAT, follow_redirects=False
    ) as client:
        yield client


async def _log_in(chat: httpx.AsyncClient, idp: FakeIdp, user: User) -> httpx.Response:
    """What a browser does: start the login, sign in at the IdP, come back."""
    start = await chat.get("/auth/oauth/SSO")
    assert start.status_code in (302, 307), start.text
    authorize = urlsplit(start.headers["location"])
    params = {k: v[0] for k, v in parse_qs(authorize.query).items()}
    code = idp.authorize(user, params["redirect_uri"])
    return await chat.get(
        "/auth/oauth/SSO/callback", params={"code": code, "state": params["state"]}
    )


def _session_user(chat: httpx.AsyncClient) -> dict:
    token = get_token_from_cookies(dict(chat.cookies))
    assert token, "no session cookie was set"
    return jwt.decode(token, AUTH_SECRET, algorithms=["HS256"])


class TestRegister:
    def test_off_without_an_issuer(self, oidc_settings):
        import chainlit.oauth_providers as chainlit_oauth

        oidc_settings.setattr(settings, "oidc_issuer", "")
        before = list(chainlit_oauth.providers)
        assert sign_in.register() is False
        assert chainlit_oauth.providers == before

    def test_bad_settings_refuse_to_start_and_register_nothing(self, oidc_settings):
        from chainlit.config import config as chainlit_config

        oidc_settings.setattr(settings, "oidc_client_id", "")
        with pytest.raises(OidcConfigError):
            sign_in.register()
        assert chainlit_config.code.oauth_callback is None
        assert sign_in._state is None


class TestLogin:
    async def test_the_login_page_offers_the_provider(self, chat):
        config = (await chat.get("/auth/config")).json()
        assert config["requireLogin"] is True
        assert config["oauthProviders"][0] == "SSO"

    async def test_the_redirect_asks_the_idp_for_a_code(self, chat, idp):
        start = await chat.get("/auth/oauth/SSO")
        location = urlsplit(start.headers["location"])
        assert f"{location.scheme}://{location.netloc}{location.path}" == (
            ISSUER + "/protocol/openid-connect/auth"
        )
        params = parse_qs(location.query)
        assert params["client_id"] == [CLIENT_ID]
        assert params["response_type"] == ["code"]
        assert params["scope"] == ["openid profile email"]
        assert params["redirect_uri"] == [CHAT + "/auth/oauth/SSO/callback"]
        assert "client_secret" not in params

    async def test_a_login_before_discovery_says_to_wait(self, oidc_settings, idp):
        from chainlit.server import app

        sign_in.register(transport=idp.transport())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=CHAT
        ) as client:
            response = await client.get("/auth/oauth/SSO")
        assert response.status_code == 503

    async def test_a_signed_in_user_gets_a_session_with_only_a_handle(self, chat, idp):
        response = await _log_in(chat, idp, ALICE)
        assert response.status_code == 302
        assert response.headers["location"].endswith("/login/callback?success=True")

        user = _session_user(chat)
        assert user["identifier"] == "alice"
        assert user["display_name"] == "Alice"
        handle = user["metadata"][sign_in.SESSION_KEY]
        sign_in_record = sign_in._state.store.get(handle)
        assert sign_in_record is not None
        assert sign_in_record.roles == {"analyst"}

        # The cookie is signed, not encrypted: no token may be in it.
        tokens = sign_in_record.tokens
        cookie_payload = json.dumps(user)
        for secret in (tokens.access_token, tokens.refresh_token, tokens.id_token):
            assert secret and secret not in cookie_payload

    async def test_a_forged_state_is_refused(self, chat, idp):
        await chat.get("/auth/oauth/SSO")
        code = idp.authorize(ALICE, CHAT + "/auth/oauth/SSO/callback")
        response = await chat.get(
            "/auth/oauth/SSO/callback", params={"code": code, "state": "forged"}
        )
        assert "error=oauthSignin" in response.headers["location"]
        assert len(sign_in._state.store) == 0

    async def test_a_user_without_an_allowed_role_is_turned_away(self, chat, idp, oidc_settings):
        config = replace(sign_in._state.config, allowed_roles=frozenset({"analyst"}))
        oidc_settings.setattr(sign_in._state, "config", config)
        refused = await _log_in(chat, idp, BOB)
        assert "error=oauthSignin" in refused.headers["location"]
        assert len(sign_in._state.store) == 0
        assert len(idp.revoked) == 1  # the refused sign-in's tokens are revoked

        admitted = await _log_in(chat, idp, ALICE)
        assert admitted.headers["location"].endswith("success=True")

    async def test_logout_ends_the_sign_in_and_revokes_it(self, chat, idp):
        await _log_in(chat, idp, ALICE)
        handle = _session_user(chat)["metadata"][sign_in.SESSION_KEY]
        refresh = sign_in._state.store.get(handle).tokens.refresh_token

        response = await chat.post("/logout")
        assert response.status_code == 200
        assert sign_in._state.store.get(handle) is None
        assert idp.revoked == [refresh]


class TestCheckSignedIn:
    async def _user_from_login(self, chat, idp):
        import chainlit as cl

        await _log_in(chat, idp, ALICE)
        user = _session_user(chat)
        return cl.User(identifier=user["identifier"], metadata=user["metadata"])

    async def test_nothing_to_check_when_sign_in_is_off(self, oidc_settings):
        init_http_context(user=None)
        assert await sign_in.check_signed_in() is None

    async def test_a_live_sign_in_goes_on(self, chat, idp):
        init_http_context(user=await self._user_from_login(chat, idp))
        assert await sign_in.check_signed_in() is None

    async def test_a_sign_in_the_idp_ended_stops_the_session(self, chat, idp):
        user = await self._user_from_login(chat, idp)
        init_http_context(user=user)
        handle = user.metadata[sign_in.SESSION_KEY]
        record = sign_in._state.store.get(handle)
        record.tokens = replace(record.tokens, expires_at=0)
        idp.refresh_error = "invalid_grant"  # the user was disabled or signed out at the IdP
        assert await sign_in.check_signed_in() == sign_in.SIGN_IN_ENDED

    async def test_a_session_from_before_a_restart_must_sign_in_again(self, chat):
        import chainlit as cl

        init_http_context(user=cl.User(identifier="alice", metadata={"oidc_session": "gone"}))
        assert await sign_in.check_signed_in() == sign_in.SIGN_IN_ENDED

    async def test_an_unreachable_idp_holds_an_expired_session(self, chat, idp):
        user = await self._user_from_login(chat, idp)
        init_http_context(user=user)
        record = sign_in._state.store.get(user.metadata[sign_in.SESSION_KEY])
        record.tokens = replace(record.tokens, expires_at=0)
        idp.token_status = 503
        assert await sign_in.check_signed_in() == sign_in.IDP_UNAVAILABLE


class TestDiscoveryAtStartup:
    async def test_retries_until_the_idp_answers(self, oidc_settings, idp):
        attempts = []

        def flaky(request):
            attempts.append(request)
            if len(attempts) < 3:
                raise httpx.ConnectError("not up yet", request=request)
            return idp.handle(request)

        oidc_settings.setattr(sign_in, "_DISCOVERY_RETRY_FIRST", 0.0)
        sign_in.register(transport=httpx.MockTransport(flaky))
        await sign_in._on_startup()
        await sign_in._state.discovery_task
        assert sign_in._state.client.discovered is not None
        assert len(attempts) == 3

    async def test_gives_up_on_a_document_that_contradicts_the_settings(
        self, oidc_settings, idp, caplog
    ):
        idp.discovery_overrides["issuer"] = "https://elsewhere.test"
        sign_in.register(transport=idp.transport())
        await sign_in._on_startup()
        await sign_in._state.discovery_task
        assert sign_in._state.client.discovered is None
        assert "cannot work" in caplog.text
