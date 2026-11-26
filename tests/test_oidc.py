"""OpenID Connect sign-in: configuration, token checks and renewal, against a fake IdP."""

import asyncio
import logging
import time
from types import SimpleNamespace

import httpx
import pytest

from orionbelt_chat.oidc import (
    DEFAULT_ROLE_CLAIMS,
    DEFAULT_SCOPES,
    DEFAULT_USER_CLAIMS,
    IdpUnavailableError,
    OidcClient,
    OidcConfig,
    OidcConfigError,
    SignInEndedError,
    TokenRejectedError,
    TokenSet,
    TokenStore,
    claim_values,
    is_admitted,
    load_config,
    roles_from,
    user_identifier,
)
from tests.fake_idp import CLIENT_ID, CLIENT_SECRET, ISSUER, FakeIdp, User, new_key, public_jwk

REDIRECT_URI = "https://chat.test/auth/oauth/SSO/callback"
ALICE = User(sub="u-alice", username="alice", roles=["analyst"], email="alice@example.com")


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _settings(**overrides):
    values = {
        "oidc_issuer": ISSUER,
        "oidc_client_id": CLIENT_ID,
        "oidc_client_secret": CLIENT_SECRET,
        "oidc_client_secret_file": "",
        "oidc_scopes": DEFAULT_SCOPES,
        "oidc_provider_name": "SSO",
        "oidc_allowed_roles": "",
        "oidc_role_claims": DEFAULT_ROLE_CLAIMS,
        "oidc_user_claims": DEFAULT_USER_CLAIMS,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _config(**overrides) -> OidcConfig:
    config = load_config(_settings(**overrides))
    assert config is not None
    return config


@pytest.fixture()
def idp() -> FakeIdp:
    return FakeIdp()


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def client(idp, clock) -> OidcClient:
    return OidcClient(_config(), transport=idp.transport(), clock=clock)


async def _sign_in(client: OidcClient, idp: FakeIdp, user: User = ALICE) -> TokenSet:
    return await client.exchange_code(idp.authorize(user, REDIRECT_URI), REDIRECT_URI)


# ── Configuration ─────────────────────────────────────────────────────────


class TestLoadConfig:
    def test_off_without_an_issuer(self):
        assert load_config(_settings(oidc_issuer="")) is None

    def test_a_complete_configuration(self):
        config = _config(oidc_allowed_roles="analyst, admin", oidc_scopes="openid  email")
        assert config.issuer == ISSUER
        assert config.allowed_roles == frozenset({"analyst", "admin"})
        assert config.role_claims == ("roles", "groups", "realm_access.roles")
        assert config.scopes == "openid email"

    def test_the_secret_stays_out_of_repr(self):
        assert CLIENT_SECRET not in repr(_config())

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"oidc_issuer": "idp.test"}, "must be an http"),
            ({"oidc_client_id": ""}, "OIDC_CLIENT_ID"),
            ({"oidc_client_secret": ""}, "neither"),
            ({"oidc_client_secret_file": "/x"}, "not both"),
            ({"oidc_scopes": "profile email"}, "'openid'"),
            ({"oidc_provider_name": "S S O"}, "OIDC_PROVIDER_NAME"),
            ({"oidc_provider_name": "keycloak"}, "Chainlit's own providers"),
            ({"oidc_user_claims": " , "}, "OIDC_USER_CLAIMS"),
            ({"oidc_allowed_roles": "admin", "oidc_role_claims": ""}, "OIDC_ROLE_CLAIMS"),
        ],
    )
    def test_settings_that_cannot_work_are_refused(self, overrides, message):
        with pytest.raises(OidcConfigError, match=message):
            load_config(_settings(**overrides))

    def test_plain_http_is_allowed_with_a_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="orionbelt_chat.oidc"):
            assert _config(oidc_issuer="http://localhost:8080/realms/dev") is not None
        assert "unencrypted" in caplog.text

    def test_the_secret_file_is_read_on_each_use(self, tmp_path):
        secret = tmp_path / "secret"
        secret.write_text("first\n")
        config = _config(oidc_client_secret="", oidc_client_secret_file=str(secret))
        assert config.secret() == "first"
        secret.write_text("rotated")
        assert config.secret() == "rotated"

    def test_a_missing_secret_file_is_refused_at_startup(self, tmp_path):
        with pytest.raises(OidcConfigError, match="cannot be read"):
            load_config(
                _settings(oidc_client_secret="", oidc_client_secret_file=str(tmp_path / "nope"))
            )

    def test_an_empty_secret_file_is_refused(self, tmp_path):
        secret = tmp_path / "secret"
        secret.write_text("\n")
        with pytest.raises(OidcConfigError, match="empty"):
            load_config(_settings(oidc_client_secret="", oidc_client_secret_file=str(secret)))


# ── Claims ────────────────────────────────────────────────────────────────


class TestClaims:
    def test_a_dotted_path_reaches_into_objects(self):
        claims = {"realm_access": {"roles": ["analyst", 7, ""]}}
        assert claim_values(claims, "realm_access.roles") == {"analyst"}

    def test_a_string_is_one_value_and_a_dead_end_is_none(self):
        assert claim_values({"roles": "Data Analysts"}, "roles") == {"Data Analysts"}
        assert claim_values({"roles": {"a": 1}}, "roles") == frozenset()
        assert claim_values({"realm_access": "x"}, "realm_access.roles") == frozenset()

    def test_roles_are_gathered_from_every_token(self):
        roles = roles_from(
            [{"groups": ["/team"]}, {"realm_access": {"roles": ["analyst"]}}],
            ["groups", "realm_access.roles"],
        )
        assert roles == {"/team", "analyst"}

    def test_admission(self):
        assert is_admitted(frozenset(), frozenset())
        assert is_admitted(frozenset({"a", "b"}), frozenset({"b"}))
        assert not is_admitted(frozenset({"a"}), frozenset({"b"}))

    def test_the_first_claim_present_names_the_user(self):
        names = ("preferred_username", "email", "sub")
        assert user_identifier({"sub": "s", "preferred_username": "alice"}, names) == "alice"
        assert user_identifier({"sub": "s", "email": "a@x"}, names) == "a@x"
        assert user_identifier({"sub": "s", "preferred_username": " "}, names) == "s"

    def test_an_unverified_email_does_not_name_the_user(self):
        claims = {"sub": "s", "email": "ceo@example.com", "email_verified": False}
        assert user_identifier(claims, ("email", "sub")) == "s"


class TestTokenSet:
    def test_keycloak_offline_tokens_never_expire_on_their_own(self):
        tokens = TokenSet.from_response(
            {"access_token": "a", "expires_in": 60, "refresh_token": "r", "refresh_expires_in": 0},
            now=1000.0,
        )
        assert tokens.expires_at == 1060.0
        assert tokens.refresh_expires_at is None

    def test_an_unknown_lifetime_is_assumed_short(self):
        tokens = TokenSet.from_response({"access_token": "a"}, now=0.0)
        assert tokens.expires_at == 300.0

    def test_only_bearer_tokens_are_accepted(self):
        with pytest.raises(IdpUnavailableError, match="DPoP"):
            TokenSet.from_response({"access_token": "a", "token_type": "DPoP"}, now=0.0)

    def test_tokens_stay_out_of_repr(self):
        tokens = TokenSet(access_token="AT", expires_at=0, refresh_token="RT", id_token="IT")
        assert "AT" not in repr(tokens) and "RT" not in repr(tokens)


# ── Discovery and ID tokens ───────────────────────────────────────────────


class TestDiscovery:
    async def test_only_asymmetric_algorithms_are_taken(self, client):
        discovery = await client.discovery()
        assert discovery.algorithms == {"PS384", "RS256"}
        assert discovery.revocation_endpoint is not None

    async def test_an_issuer_that_does_not_match_is_refused(self, idp, client):
        idp.discovery_overrides["issuer"] = ISSUER + "/"
        with pytest.raises(OidcConfigError, match="trailing slash"):
            await client.discovery()

    async def test_a_provider_without_asymmetric_signatures_is_refused(self, idp, client):
        idp.discovery_overrides["id_token_signing_alg_values_supported"] = ["HS256"]
        with pytest.raises(OidcConfigError, match="asymmetric"):
            await client.discovery()

    async def test_a_document_without_endpoints_is_unusable(self, idp, client):
        idp.discovery_overrides["token_endpoint"] = None
        with pytest.raises(IdpUnavailableError, match="token_endpoint"):
            await client.discovery()

    async def test_an_unreachable_provider(self, clock):
        def refuse(request):
            raise httpx.ConnectError("refused", request=request)

        client = OidcClient(_config(), transport=httpx.MockTransport(refuse), clock=clock)
        with pytest.raises(IdpUnavailableError, match="ConnectError"):
            await client.discovery()
        assert client.discovered is None


class TestIdToken:
    async def test_a_valid_id_token(self, idp, client):
        claims = await client.verify_id_token(idp.sign(idp.id_token_claims(ALICE)))
        assert claims["sub"] == ALICE.sub

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"aud": "someone-else", "azp": None}, "InvalidAudience"),
            ({"iss": "https://evil.test"}, "InvalidIssuer"),
            ({"exp": int(time.time()) - 3600}, "ExpiredSignature"),
            ({"sub": None}, "MissingRequiredClaim"),
            ({"aud": [CLIENT_ID, "other"], "azp": None}, "issued to None"),
            ({"azp": "other"}, "issued to 'other'"),
        ],
    )
    async def test_claims_that_do_not_hold_are_refused(self, idp, client, overrides, message):
        token = idp.sign(idp.id_token_claims(ALICE, **overrides))
        with pytest.raises(TokenRejectedError, match=message):
            await client.verify_id_token(token)

    async def test_a_token_signed_by_another_key_is_refused(self, idp, client):
        token = idp.sign(idp.id_token_claims(ALICE), key=new_key())
        with pytest.raises(TokenRejectedError, match="InvalidSignature"):
            await client.verify_id_token(token)

    @pytest.mark.parametrize("alg", ["HS256", "none"])
    async def test_symmetric_and_unsigned_tokens_are_refused(self, idp, client, alg):
        import jwt

        key = "" if alg == "none" else CLIENT_SECRET.ljust(32, "x")
        token = jwt.encode(idp.id_token_claims(ALICE), key, algorithm=alg, headers={"kid": "sig-1"})
        with pytest.raises(TokenRejectedError, match="does not use"):
            await client.verify_id_token(token)

    async def test_the_encryption_key_never_verifies_a_token(self, idp, client):
        token = idp.sign(idp.id_token_claims(ALICE), kid="enc-1")
        with pytest.raises(TokenRejectedError, match="no signing key"):
            await client.verify_id_token(token)

    async def test_a_key_is_used_only_for_the_algorithm_it_names(self, idp, client):
        idp.extra_jwks = [public_jwk(idp.key, "rs-only", alg="RS256")]
        token = idp.sign(idp.id_token_claims(ALICE), kid="rs-only", alg="PS384")
        with pytest.raises(TokenRejectedError, match="is for 'RS256'"):
            await client.verify_id_token(token)

    async def test_a_rotated_key_is_fetched_once_and_then_rate_limited(self, idp, client, clock):
        await client.verify_id_token(idp.sign(idp.id_token_claims(ALICE)))
        assert idp.jwks_requests() == 1
        clock.advance(120)
        rotated = new_key()
        idp.key, idp.kid = rotated, "sig-2"
        await client.verify_id_token(idp.sign(idp.id_token_claims(ALICE)))
        assert idp.jwks_requests() == 2
        # An unknown kid right after a fetch does not fetch again.
        with pytest.raises(TokenRejectedError, match="no signing key"):
            await client.verify_id_token(idp.sign(idp.id_token_claims(ALICE), kid="sig-3"))
        assert idp.jwks_requests() == 2

    async def test_access_token_claims_come_only_from_a_verified_jwt(self, idp, client):
        tokens = await _sign_in(client, idp)
        claims = await client.access_token_claims(tokens.access_token)
        assert claims["realm_access"]["roles"] == ["analyst"]
        assert await client.access_token_claims("opaque-token") == {}
        forged = idp.sign(idp.access_token_claims(ALICE), key=new_key())
        assert await client.access_token_claims(forged) == {}


class TestTokenEndpoint:
    async def test_the_code_is_exchanged_with_the_client_credentials(self, idp, client):
        tokens = await _sign_in(client, idp)
        assert tokens.refresh_token and tokens.id_token
        (form,) = idp.token_requests("authorization_code")
        assert form["client_id"] == CLIENT_ID
        assert form["client_secret"] == CLIENT_SECRET
        assert form["redirect_uri"] == REDIRECT_URI

    async def test_a_refused_code(self, idp, client):
        with pytest.raises(Exception, match="invalid_grant"):
            await client.exchange_code("not-a-code", REDIRECT_URI)

    async def test_a_server_error_is_unavailability(self, idp, client):
        await client.discovery()
        idp.token_status = 502
        with pytest.raises(IdpUnavailableError, match="502"):
            await client.refresh("r")


# ── The token store ───────────────────────────────────────────────────────


@pytest.fixture()
def store(client, clock) -> TokenStore:
    return TokenStore(client, max_age_seconds=86_400, clock=clock)


async def _stored(store: TokenStore, client: OidcClient, idp: FakeIdp) -> str:
    tokens = await _sign_in(client, idp)
    return store.add(tokens, subject=ALICE.sub, roles=frozenset({"analyst"}))


class TestTokenStore:
    async def test_a_fresh_token_is_used_as_it_is(self, idp, client, store):
        handle = await _stored(store, client, idp)
        first = store.get(handle).tokens.access_token
        assert await store.access_token(handle) == first
        assert idp.token_requests("refresh_token") == []

    async def test_an_ageing_token_is_renewed_with_the_rotated_refresh_token(
        self, idp, client, store, clock
    ):
        handle = await _stored(store, client, idp)
        first_refresh = store.get(handle).tokens.refresh_token
        clock.advance(250)  # within a minute of the 300 s lifetime
        renewed = await store.access_token(handle)
        assert renewed != ""
        assert [f["refresh_token"] for f in idp.token_requests("refresh_token")] == [first_refresh]
        second_refresh = store.get(handle).tokens.refresh_token
        assert second_refresh != first_refresh
        clock.advance(250)
        await store.access_token(handle)
        assert idp.token_requests("refresh_token")[-1]["refresh_token"] == second_refresh

    async def test_concurrent_requests_renew_once(self, idp, client, store, clock):
        handle = await _stored(store, client, idp)
        clock.advance(290)
        tokens = await asyncio.gather(*(store.access_token(handle) for _ in range(5)))
        assert len(set(tokens)) == 1
        assert len(idp.token_requests("refresh_token")) == 1

    async def test_a_refused_renewal_ends_the_sign_in(self, idp, client, store, clock):
        handle = await _stored(store, client, idp)
        idp.refresh_error = "invalid_grant"
        clock.advance(290)
        with pytest.raises(SignInEndedError, match="invalid_grant"):
            await store.access_token(handle)
        assert store.get(handle) is None

    async def test_a_client_failure_ends_the_sign_in_and_is_logged(
        self, idp, client, store, clock, caplog
    ):
        handle = await _stored(store, client, idp)
        idp.refresh_error = "unauthorized_client"
        clock.advance(290)
        with caplog.at_level(logging.ERROR, logger="orionbelt_chat.oidc"):
            with pytest.raises(SignInEndedError):
                await store.access_token(handle)
        assert "unauthorized_client" in caplog.text

    async def test_a_valid_token_outlasts_an_unreachable_idp(self, idp, client, store, clock):
        handle = await _stored(store, client, idp)
        first = store.get(handle).tokens.access_token
        idp.token_status = 503
        clock.advance(250)
        assert await store.access_token(handle) == first
        clock.advance(100)  # now expired
        with pytest.raises(IdpUnavailableError):
            await store.access_token(handle)
        assert store.get(handle) is not None  # not ended: the IdP may come back

    async def test_without_a_refresh_token_the_sign_in_ends_with_the_token(
        self, client, store, clock
    ):
        tokens = TokenSet(access_token="a", expires_at=clock.now + 300)
        handle = store.add(tokens, subject="s", roles=frozenset())
        clock.advance(270)
        assert await store.access_token(handle) == "a"
        clock.advance(60)
        with pytest.raises(SignInEndedError, match="no refresh token"):
            await store.access_token(handle)

    async def test_a_renewal_for_another_user_ends_the_sign_in(self, idp, client, store, clock):
        handle = await _stored(store, client, idp)
        idp.refresh_id_token_sub = "u-mallory"
        clock.advance(290)
        with pytest.raises(SignInEndedError, match="different user"):
            await store.access_token(handle)
        assert store.get(handle) is None

    async def test_a_kept_refresh_token_stays_in_use(self, idp, client, store, clock):
        idp.rotate_refresh = False
        idp.refresh_returns_refresh_token = False
        handle = await _stored(store, client, idp)
        first_refresh = store.get(handle).tokens.refresh_token
        clock.advance(290)
        await store.access_token(handle)
        assert store.get(handle).tokens.refresh_token == first_refresh

    async def test_an_unknown_handle_is_an_ended_sign_in(self, store):
        with pytest.raises(SignInEndedError):
            await store.access_token("no-such-handle")
        with pytest.raises(SignInEndedError):
            await store.access_token(None)

    async def test_removing_revokes_the_refresh_token(self, idp, client, store):
        handle = await _stored(store, client, idp)
        refresh = store.get(handle).tokens.refresh_token
        await store.remove(handle)
        assert store.get(handle) is None
        assert idp.revoked == [refresh]

    async def test_old_and_dead_sign_ins_are_pruned_on_the_next_sign_in(
        self, idp, client, store, clock
    ):
        idp.refresh_lifetime = 0  # an offline token: only the age limit ends it
        old = await _stored(store, client, idp)
        dead = store.add(
            TokenSet(access_token="a", expires_at=clock.now + 10), subject="s", roles=frozenset()
        )
        clock.advance(3600)
        assert store.get(dead) is not None
        await _stored(store, client, idp)
        assert store.get(dead) is None
        assert store.get(old) is not None  # its refresh token never expires
        clock.advance(86_401)
        await _stored(store, client, idp)
        assert store.get(old) is None
        assert len(store) == 1
