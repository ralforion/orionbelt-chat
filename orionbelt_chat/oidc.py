"""OpenID Connect for signing users in: discovery, token checks and renewal.

Free of Chainlit, so it can be driven against a fake identity provider in
tests; `sign_in.py` wires it into Chainlit's OAuth flow.

The tokens a sign-in returns stay in this process (`TokenStore`). The browser
gets an opaque handle to them and nothing else: Chainlit's session cookie is a
signed, not encrypted, JWT, so anything put in the user's metadata is readable
by whoever holds it.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import secrets
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote_plus

import httpx
import jwt

logger = logging.getLogger(__name__)

DEFAULT_SCOPES = "openid profile email"
DEFAULT_PROVIDER_NAME = "SSO"
# Where the usual identity providers put roles and groups: Keycloak's realm
# roles in `realm_access.roles`, a group-membership mapper's `groups`, Entra
# app roles in `roles`.
DEFAULT_ROLE_CLAIMS = "roles,groups,realm_access.roles"
DEFAULT_USER_CLAIMS = "preferred_username,email,sub"

# How this client proves itself at the token and revocation endpoints. An IdP
# may register a client for exactly one of them: Okta defaults to Basic,
# Auth0 to post. Keycloak and Entra ID accept both.
CLIENT_SECRET_POST = "client_secret_post"
CLIENT_SECRET_BASIC = "client_secret_basic"
_CLIENT_AUTH_METHODS = (CLIENT_SECRET_POST, CLIENT_SECRET_BASIC)

# Signature algorithms an ID token may use. Asymmetric only: an HS* token is
# signed with the client secret, which proves nothing about who issued it, and
# `none` is no signature at all.
_SIGNING_ALGORITHMS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES384",
        "ES512",
        "EdDSA",
    }
)

# Leeway on a token's exp/iat/nbf, for clocks that disagree a little.
CLOCK_SKEW_SECONDS = 60
# An access token this close to expiring is renewed before it is used.
RENEW_BEFORE_SECONDS = 60
# Used when a token response says nothing about expiry: short, so a token of
# unknown lifetime is renewed early rather than used after it lapsed.
DEFAULT_EXPIRES_IN_SECONDS = 300
JWKS_CACHE_TTL_SECONDS = 86_400
# A token signed with a key not in the cached set fetches the set again, but
# no more often than this, so a stream of bad tokens cannot hammer the IdP.
JWKS_MIN_REFETCH_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 10.0

# Chainlit's own provider ids. The provider name is also the button label and
# a path segment of the redirect URI, so it must not shadow one of them.
_BUILTIN_PROVIDER_IDS = frozenset(
    {
        "github",
        "google",
        "azure-ad",
        "azure-ad-hybrid",
        "okta",
        "auth0",
        "descope",
        "aws-cognito",
        "gitlab",
        "keycloak",
        "generic",
    }
)
_PROVIDER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")


class OidcError(Exception):
    """Something about a sign-in went wrong."""


class OidcConfigError(OidcError):
    """The OIDC_* settings cannot work; raised at startup."""


class TokenRejectedError(OidcError):
    """A token failed its checks: signature, issuer, audience or lifetime."""


class SignInEndedError(OidcError):
    """The sign-in is over: refused by the IdP, expired, or not known here."""


class IdpUnavailableError(OidcError):
    """The identity provider could not be reached or answered with nonsense."""


class TokenEndpointError(OidcError):
    """The token endpoint refused a request with an OAuth error code."""

    def __init__(self, error: str, description: str = "") -> None:
        self.error = error
        self.description = description
        super().__init__(f"{error}: {description}" if description else error)


class _OidcSettings(Protocol):
    """The fields of `Settings` this module reads, so tests need not build one."""

    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: str
    oidc_client_secret_file: str
    oidc_scopes: str
    oidc_provider_name: str
    oidc_allowed_roles: str
    oidc_role_claims: str
    oidc_user_claims: str
    oidc_client_auth: str


def _split(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


@dataclass(frozen=True)
class OidcConfig:
    """Validated sign-in settings."""

    issuer: str
    client_id: str
    scopes: str
    provider_name: str = DEFAULT_PROVIDER_NAME
    role_claims: tuple[str, ...] = _split(DEFAULT_ROLE_CLAIMS)
    allowed_roles: frozenset[str] = frozenset()
    user_claims: tuple[str, ...] = _split(DEFAULT_USER_CLAIMS)
    client_auth: str = CLIENT_SECRET_POST
    # Exactly one of the two. Not shown in repr, so logging the config does
    # not log the secret.
    client_secret: str = field(default="", repr=False)
    client_secret_file: str = ""

    def secret(self) -> str:
        """The client secret, read from the file on each use so a rotated one is picked up."""
        if self.client_secret_file:
            try:
                return Path(self.client_secret_file).read_text(encoding="utf-8").strip()
            except OSError as e:
                raise OidcError(
                    f"OIDC_CLIENT_SECRET_FILE cannot be read: {type(e).__name__}"
                ) from e
        return self.client_secret

    @property
    def discovery_url(self) -> str:
        # OpenID Connect Discovery §4: a terminating "/" is removed before the
        # well-known path is appended (Auth0's issuers end in one).
        return self.issuer.rstrip("/") + "/.well-known/openid-configuration"


def load_config(settings: _OidcSettings) -> OidcConfig | None:
    """The sign-in configuration, or None when sign-in is off (no OIDC_ISSUER).

    Raises `OidcConfigError` for settings that cannot work, so the app refuses
    to start rather than serving a login page that can never succeed.
    """
    issuer = settings.oidc_issuer.strip()
    if not issuer:
        return None
    if not issuer.startswith(("https://", "http://")):
        raise OidcConfigError(f"OIDC_ISSUER must be an http(s) URL, got {issuer!r}")
    if issuer.startswith("http://"):
        # Allowed for a local Keycloak, but the client secret and every token
        # cross this connection.
        logger.warning(
            "OIDC_ISSUER is plain http: the client secret and users' tokens cross the "
            "network unencrypted. Use https outside local development."
        )

    client_id = settings.oidc_client_id.strip()
    if not client_id:
        raise OidcConfigError("OIDC_ISSUER is set but OIDC_CLIENT_ID is not")

    secret = settings.oidc_client_secret.strip()
    secret_file = settings.oidc_client_secret_file.strip()
    if secret and secret_file:
        raise OidcConfigError("Set OIDC_CLIENT_SECRET or OIDC_CLIENT_SECRET_FILE, not both")
    if not secret and not secret_file:
        # The chat signs users in with a confidential client: Chainlit's flow
        # sends no PKCE challenge, so a public client would be unprotected.
        raise OidcConfigError(
            "OIDC_ISSUER is set but neither OIDC_CLIENT_SECRET nor OIDC_CLIENT_SECRET_FILE is"
        )

    scopes = " ".join(settings.oidc_scopes.split())
    if "openid" not in scopes.split():
        raise OidcConfigError(
            f"OIDC_SCOPES must include 'openid', or no ID token is issued (got {scopes!r})"
        )

    name = settings.oidc_provider_name.strip()
    if not _PROVIDER_NAME.fullmatch(name):
        raise OidcConfigError(
            "OIDC_PROVIDER_NAME must be 1-32 letters, digits, '-' or '_', starting with a "
            f"letter or digit (got {name!r})"
        )
    if name in _BUILTIN_PROVIDER_IDS:
        raise OidcConfigError(
            f"OIDC_PROVIDER_NAME {name!r} is the id of one of Chainlit's own providers; "
            "pick another (it is also the button label, e.g. 'SSO' or 'Keycloak')"
        )

    client_auth = settings.oidc_client_auth.strip().lower()
    if client_auth not in _CLIENT_AUTH_METHODS:
        raise OidcConfigError(
            f"OIDC_CLIENT_AUTH must be one of {', '.join(_CLIENT_AUTH_METHODS)} "
            f"(got {settings.oidc_client_auth!r})"
        )

    role_claims = _split(settings.oidc_role_claims)
    user_claims = _split(settings.oidc_user_claims)
    if not user_claims:
        raise OidcConfigError("OIDC_USER_CLAIMS must name at least one claim")

    config = OidcConfig(
        issuer=issuer,
        client_id=client_id,
        scopes=scopes,
        provider_name=name,
        role_claims=role_claims,
        allowed_roles=frozenset(_split(settings.oidc_allowed_roles)),
        user_claims=user_claims,
        client_auth=client_auth,
        client_secret=secret,
        client_secret_file=secret_file,
    )
    if config.allowed_roles and not config.role_claims:
        raise OidcConfigError("OIDC_ALLOWED_ROLES is set but OIDC_ROLE_CLAIMS names no claim")
    try:
        secret_value = config.secret()
    except OidcError as e:
        raise OidcConfigError(str(e)) from e
    if not secret_value:
        raise OidcConfigError("The OIDC client secret is empty")
    return config


# ── Claims ────────────────────────────────────────────────────────────────


def claim_values(claims: Mapping[str, Any], path: str) -> frozenset[str]:
    """The string values at a dotted claim path: `realm_access.roles` reaches into an object.

    A single string is one value; a list contributes its strings. Anything
    else, or a path that leads nowhere, contributes nothing.
    """
    node: Any = claims
    for part in path.split("."):
        if not isinstance(node, Mapping):
            return frozenset()
        node = node.get(part)
    if isinstance(node, str):
        return frozenset({node}) if node else frozenset()
    if isinstance(node, list):
        return frozenset(item for item in node if isinstance(item, str) and item)
    return frozenset()


def roles_from(claim_sets: Iterable[Mapping[str, Any]], paths: Iterable[str]) -> frozenset[str]:
    """Every role or group any of `paths` holds in any of `claim_sets`."""
    paths = tuple(paths)
    found: set[str] = set()
    for claims in claim_sets:
        for path in paths:
            found |= claim_values(claims, path)
    return frozenset(found)


def is_admitted(roles: frozenset[str], allowed: frozenset[str]) -> bool:
    """Whether a user with `roles` may use the chat. No allow-list admits everyone."""
    return not allowed or bool(roles & allowed)


def user_identifier(claims: Mapping[str, Any], names: Iterable[str]) -> str:
    """The first of the `names` claims that holds a non-empty string.

    `email` is skipped when the token says it is unverified: anyone can type
    an address into a profile, and the identifier is what a data layer files
    a user's chat history under.
    """
    for name in names:
        if name == "email" and claims.get("email_verified") is False:
            continue
        value = claims.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    # `sub` is required on every ID token we accept.
    return str(claims["sub"])


# ── Tokens ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TokenSet:
    """What a token endpoint returned, with lifetimes turned into deadlines."""

    access_token: str = field(repr=False)
    expires_at: float
    refresh_token: str | None = field(default=None, repr=False)
    # None when the IdP did not say. Keycloak sends 0 for an offline token,
    # which never expires on its own; that is None here too.
    refresh_expires_at: float | None = None
    id_token: str | None = field(default=None, repr=False)

    @classmethod
    def from_response(cls, body: Mapping[str, Any], now: float) -> TokenSet:
        access = body.get("access_token")
        if not isinstance(access, str) or not access:
            raise IdpUnavailableError("the token response has no access_token")
        token_type = body.get("token_type", "Bearer")
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            raise IdpUnavailableError(f"the token response is of type {token_type!r}, not Bearer")
        refresh = body.get("refresh_token")
        id_token = body.get("id_token")
        refresh_in = _seconds(body.get("refresh_expires_in"))
        return cls(
            access_token=access,
            expires_at=now + (_seconds(body.get("expires_in")) or DEFAULT_EXPIRES_IN_SECONDS),
            refresh_token=refresh if isinstance(refresh, str) and refresh else None,
            refresh_expires_at=now + refresh_in if refresh_in else None,
            id_token=id_token if isinstance(id_token, str) and id_token else None,
        )


def _seconds(value: Any) -> float | None:
    """A positive lifetime in seconds, or None. Numeric strings are accepted."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, int | float) and value > 0:
        return float(value)
    return None


@dataclass(frozen=True)
class Discovery:
    """The parts of the IdP's discovery document a sign-in uses."""

    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    algorithms: frozenset[str]
    revocation_endpoint: str | None = None

    @classmethod
    def from_document(cls, doc: Mapping[str, Any], issuer: str) -> Discovery:
        # Discovery §4.3: the document must name exactly the issuer it was
        # fetched for, or tokens from it would be checked against the wrong one.
        if doc.get("issuer") != issuer:
            raise OidcConfigError(
                f"The discovery document names issuer {doc.get('issuer')!r}, not {issuer!r}. "
                "OIDC_ISSUER must match it exactly (a trailing slash counts)."
            )
        endpoints = {}
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            value = doc.get(key)
            if not isinstance(value, str) or not value.startswith(("https://", "http://")):
                raise IdpUnavailableError(f"the discovery document has no usable {key}")
            endpoints[key] = value
        advertised = doc.get("id_token_signing_alg_values_supported")
        if isinstance(advertised, list):
            algorithms = frozenset(a for a in advertised if a in _SIGNING_ALGORITHMS)
        else:
            # Required by the spec; RS256 is what every provider supports.
            algorithms = frozenset({"RS256"})
        if not algorithms:
            raise OidcConfigError(
                f"The identity provider signs ID tokens only with {advertised!r}; "
                f"an asymmetric algorithm is required ({', '.join(sorted(_SIGNING_ALGORITHMS))})"
            )
        revocation = doc.get("revocation_endpoint")
        return cls(
            authorization_endpoint=endpoints["authorization_endpoint"],
            token_endpoint=endpoints["token_endpoint"],
            jwks_uri=endpoints["jwks_uri"],
            algorithms=algorithms,
            revocation_endpoint=revocation if isinstance(revocation, str) and revocation else None,
        )


class OidcClient:
    """Talks to one identity provider on behalf of this app's client registration."""

    def __init__(
        self,
        config: OidcConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._transport = transport
        self._clock = clock
        self._discovery: Discovery | None = None
        self._discovery_lock = asyncio.Lock()
        # kid -> (key, the algorithm the key declares, if it declares one)
        self._keys: dict[str, tuple[jwt.PyJWK, str | None]] = {}
        self._keys_fetched_at: float | None = None
        self._keys_lock = asyncio.Lock()

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport, timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False
        )

    async def _get_json(self, url: str, what: str) -> dict[str, Any]:
        try:
            async with self._http() as http:
                response = await http.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError as e:
            raise IdpUnavailableError(f"{what} unreachable ({type(e).__name__})") from e
        if response.status_code != 200:
            raise IdpUnavailableError(f"{what} answered HTTP {response.status_code}")
        body = _json_object(response)
        if body is None:
            raise IdpUnavailableError(f"{what} did not answer with a JSON object")
        return body

    # ── Discovery and keys ──

    @property
    def discovered(self) -> Discovery | None:
        """The discovery document, once it has been fetched."""
        return self._discovery

    async def discovery(self) -> Discovery:
        if self._discovery is not None:
            return self._discovery
        async with self._discovery_lock:
            if self._discovery is None:
                doc = await self._get_json(self.config.discovery_url, "the discovery document")
                self._discovery = Discovery.from_document(doc, self.config.issuer)
                logger.info("OIDC: discovered %s", self.config.issuer)
        return self._discovery

    async def _signing_key(self, kid: str | None) -> tuple[jwt.PyJWK, str | None]:
        key = self._pick_key(kid)
        if key is not None and not self._keys_stale():
            return key
        async with self._keys_lock:
            key = self._pick_key(kid)
            if key is not None and not self._keys_stale():
                return key
            fetched_at = self._keys_fetched_at
            if fetched_at is None or self._clock() - fetched_at >= JWKS_MIN_REFETCH_SECONDS:
                await self._fetch_keys()
                key = self._pick_key(kid)
        if key is None:
            raise TokenRejectedError(f"no signing key with kid {kid!r} at the identity provider")
        return key

    def _keys_stale(self) -> bool:
        fetched_at = self._keys_fetched_at
        return fetched_at is None or self._clock() - fetched_at >= JWKS_CACHE_TTL_SECONDS

    def _pick_key(self, kid: str | None) -> tuple[jwt.PyJWK, str | None] | None:
        if kid is not None:
            return self._keys.get(kid)
        # A token without a kid can only mean the provider has one key.
        if len(self._keys) == 1:
            return next(iter(self._keys.values()))
        return None

    async def _fetch_keys(self) -> None:
        discovery = await self.discovery()
        doc = await self._get_json(discovery.jwks_uri, "the JWKS endpoint")
        keys: dict[str, tuple[jwt.PyJWK, str | None]] = {}
        raw_keys = doc.get("keys")
        for raw in raw_keys if isinstance(raw_keys, list) else []:
            # Keycloak publishes its encryption key alongside the signing keys.
            if not isinstance(raw, dict) or raw.get("use", "sig") != "sig":
                continue
            try:
                key = jwt.PyJWK(raw)
            except jwt.PyJWTError:
                continue  # a key type or curve PyJWT cannot use
            declared = raw.get("alg")
            keys[str(raw.get("kid", ""))] = (key, declared if isinstance(declared, str) else None)
        self._keys = keys
        self._keys_fetched_at = self._clock()
        if not keys:
            logger.warning("OIDC: the JWKS endpoint publishes no usable signing key")

    # ── Token checks ──

    async def _decode(
        self, token: str, *, audience: str | None, required: list[str]
    ) -> dict[str, Any]:
        discovery = await self.discovery()
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as e:
            raise TokenRejectedError(f"not a JWT ({type(e).__name__})") from e
        alg = header.get("alg")
        if alg not in discovery.algorithms:
            raise TokenRejectedError(f"signed with {alg!r}, which this provider does not use")
        kid = header.get("kid")
        key, declared_alg = await self._signing_key(kid if isinstance(kid, str) else None)
        # A key that names its algorithm is used for that one only (RFC 8725 §3.1).
        if declared_alg is not None and declared_alg != alg:
            raise TokenRejectedError(f"signed with {alg!r} but key {kid!r} is for {declared_alg!r}")
        try:
            return jwt.decode(
                token,
                key=key.key,
                algorithms=[alg],
                audience=audience,
                issuer=self.config.issuer,
                leeway=CLOCK_SKEW_SECONDS,
                options={"require": required, "verify_aud": audience is not None},
            )
        except jwt.PyJWTError as e:
            raise TokenRejectedError(f"{type(e).__name__}: {e}") from e

    async def verify_id_token(self, id_token: str) -> dict[str, Any]:
        """The claims of an ID token issued to this client, or `TokenRejectedError`."""
        claims = await self._decode(
            id_token,
            audience=self.config.client_id,
            required=["iss", "sub", "aud", "exp", "iat"],
        )
        # OIDC Core §3.1.3.7: with several audiences, azp must name this
        # client; when present it must name this client in any case.
        audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
        azp = claims.get("azp")
        if (len(audiences) > 1 or azp is not None) and azp != self.config.client_id:
            raise TokenRejectedError(f"issued to {azp!r}, not to this client")
        if not isinstance(claims["sub"], str) or not claims["sub"]:
            raise TokenRejectedError("the ID token has no subject")
        return claims

    async def access_token_claims(self, access_token: str) -> dict[str, Any]:
        """The access token's claims when it is a JWT this issuer signed, else `{}`.

        Read for roles only: Keycloak puts realm roles in the access token and
        not in the ID token. The audience is not checked, since the token is
        meant for an API rather than for this client; it came straight from
        the token endpoint, and its signature and issuer must still hold. An
        opaque or foreign token simply contributes no roles.
        """
        if access_token.count(".") != 2:
            return {}
        try:
            return await self._decode(access_token, audience=None, required=["iss", "exp"])
        except TokenRejectedError as e:
            logger.debug("OIDC: access token not used for roles: %s", e)
            return {}

    async def roles(self, id_claims: Mapping[str, Any], access_token: str) -> frozenset[str]:
        """The roles and groups a sign-in holds, from its ID token and access token."""
        access_claims = await self.access_token_claims(access_token)
        return roles_from((id_claims, access_claims), self.config.role_claims)

    # ── Token endpoint ──

    def _authenticated(self, form: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
        """`form` and headers carrying the client's credentials, the configured way."""
        secret = self.config.secret()
        if self.config.client_auth == CLIENT_SECRET_BASIC:
            # RFC 6749 §2.3.1: each half is form-urlencoded before the two are
            # joined, so a ':' in the secret cannot split it.
            pair = f"{quote_plus(self.config.client_id)}:{quote_plus(secret)}"
            basic = base64.b64encode(pair.encode("utf-8")).decode("ascii")
            return form, {"Authorization": f"Basic {basic}"}
        return {**form, "client_id": self.config.client_id, "client_secret": secret}, {}

    async def _token_request(self, form: dict[str, str]) -> TokenSet:
        discovery = await self.discovery()
        form, headers = self._authenticated(form)
        try:
            async with self._http() as http:
                response = await http.post(
                    discovery.token_endpoint,
                    data=form,
                    headers={"Accept": "application/json", **headers},
                )
        except httpx.HTTPError as e:
            raise IdpUnavailableError(f"the token endpoint unreachable ({type(e).__name__})") from e
        body = _json_object(response) or {}
        if response.status_code == 200:
            return TokenSet.from_response(body, self._clock())
        error = body.get("error")
        if response.status_code in (400, 401) and isinstance(error, str) and error:
            description = body.get("error_description")
            raise TokenEndpointError(error, description if isinstance(description, str) else "")
        raise IdpUnavailableError(f"the token endpoint answered HTTP {response.status_code}")

    async def exchange_code(self, code: str, redirect_uri: str) -> TokenSet:
        """Redeem an authorization code for tokens."""
        return await self._token_request(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
        )

    async def refresh(self, refresh_token: str) -> TokenSet:
        """Renew the tokens with a refresh token."""
        return await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    async def revoke(self, refresh_token: str) -> None:
        """Revoke a refresh token (RFC 7009), if the IdP offers that. Best effort."""
        discovery = await self.discovery()
        if discovery.revocation_endpoint is None:
            return
        form, headers = self._authenticated(
            {"token": refresh_token, "token_type_hint": "refresh_token"}
        )
        try:
            async with self._http() as http:
                response = await http.post(
                    discovery.revocation_endpoint, data=form, headers=headers
                )
        except httpx.HTTPError as e:
            logger.info("OIDC: revoking a refresh token failed (%s)", type(e).__name__)
            return
        if response.status_code != 200:
            logger.info("OIDC: revoking a refresh token answered HTTP %s", response.status_code)


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


# ── Signed-in sessions ────────────────────────────────────────────────────


@dataclass
class SignIn:
    """One user's sign-in, as this process holds it."""

    tokens: TokenSet
    # The latest verified ID token's claims, for who the user is. Never for
    # their roles after a renewal: those come from what the renewal brought.
    id_claims: dict[str, Any] = field(repr=False)
    roles: frozenset[str]
    created_at: float
    # Tokens a renewal got but could not check yet, because the IdP's signing
    # keys were out of reach. The refresh token that got them is spent, so
    # these hold the only live one. Never handed out until they pass their
    # checks, but revoked on sign-out.
    pending: TokenSet | None = field(default=None, repr=False)
    # Renewal and removal both hold this, so a sign-out cannot slip in
    # between a refresh token being spent and its replacement being stored.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    # Set once the sign-in is over, for a caller that waited on the lock.
    ended: bool = False

    @property
    def subject(self) -> str:
        return str(self.id_claims["sub"])


class TokenStore:
    """Sign-ins by opaque handle, renewed with their refresh tokens as they age.

    In memory: a restart ends every sign-in, and several replicas need sticky
    sessions (which Chainlit's websocket needs anyway).
    """

    def __init__(
        self,
        client: OidcClient,
        *,
        max_age_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._max_age = max_age_seconds
        self._clock = clock
        self._sign_ins: dict[str, SignIn] = {}

    def __len__(self) -> int:
        return len(self._sign_ins)

    def add(self, tokens: TokenSet, *, id_claims: Mapping[str, Any], roles: frozenset[str]) -> str:
        """Keep a new sign-in and return its handle."""
        self._prune()
        handle = secrets.token_urlsafe(32)
        self._sign_ins[handle] = SignIn(
            tokens=tokens, id_claims=dict(id_claims), roles=roles, created_at=self._clock()
        )
        return handle

    def get(self, handle: str | None) -> SignIn | None:
        return self._sign_ins.get(handle) if handle else None

    async def remove(self, handle: str | None, *, revoke: bool = True) -> None:
        """End a sign-in and revoke its refresh token at the IdP.

        Gone from the store at once, so no new request can use it. The
        revocation waits for a renewal in flight, so the refresh token revoked
        is the one that renewal leaves behind, not the one it already spent.
        """
        sign_in = self._sign_ins.pop(handle, None) if handle else None
        if sign_in is None:
            return
        async with sign_in.lock:
            sign_in.ended = True
            if revoke:
                await self._revoke(sign_in.pending, sign_in.tokens)
            sign_in.pending = None

    async def _revoke(self, *token_sets: TokenSet | None) -> None:
        revoked: set[str] = set()
        for tokens in token_sets:
            refresh = tokens.refresh_token if tokens is not None else None
            if refresh is None or refresh in revoked:
                continue
            revoked.add(refresh)
            try:
                await self._client.revoke(refresh)
            except OidcError as e:
                logger.info("OIDC: could not revoke a sign-in's refresh token: %s", e)

    def _end(self, handle: str, sign_in: SignIn) -> None:
        """Forget a sign-in from inside its lock."""
        sign_in.ended = True
        if self._sign_ins.get(handle) is sign_in:
            del self._sign_ins[handle]

    async def access_token(self, handle: str | None) -> str:
        """A current access token for the sign-in, renewed first if it is about to expire.

        Raises `SignInEndedError` when there is no such sign-in or the IdP will not
        renew it, and `IdpUnavailableError` when the token has expired and the IdP
        cannot be reached to renew it. A token that is still valid is returned
        even while the IdP is down.
        """
        sign_in = self.get(handle)
        if sign_in is None or handle is None:
            raise SignInEndedError("this sign-in is not known here")
        if self._fresh(sign_in.tokens):
            return sign_in.tokens.access_token
        async with sign_in.lock:
            # Another task may have renewed it, or ended it, while this one waited.
            if not sign_in.ended and not self._fresh(sign_in.tokens):
                await self._renew(handle, sign_in)
            if sign_in.ended or self._sign_ins.get(handle) is not sign_in:
                raise SignInEndedError("the user signed out")
            return sign_in.tokens.access_token

    def _fresh(self, tokens: TokenSet) -> bool:
        return tokens.expires_at - self._clock() > RENEW_BEFORE_SECONDS

    async def _renew(self, handle: str, sign_in: SignIn) -> None:
        """Replace the sign-in's tokens with checked new ones, or end it.

        Returns with the old tokens in place, still valid, when the IdP cannot
        be reached; raises `IdpUnavailableError` when they have expired too.
        """
        now = self._clock()
        if sign_in.pending is None or not self._fresh(sign_in.pending):
            # Renew from the newest refresh token: the waiting tokens' when a
            # renewal is already waiting, since it spent the old one.
            source = sign_in.pending or sign_in.tokens
            if source.refresh_token is None or (
                source.refresh_expires_at is not None and source.refresh_expires_at <= now
            ):
                if sign_in.tokens.expires_at > now:
                    return  # nothing to renew with yet, but still valid for a little while
                self._end(handle, sign_in)
                raise SignInEndedError("the access token expired and there is no refresh token")
            try:
                renewed = await self._client.refresh(source.refresh_token)
            except TokenEndpointError as e:
                self._end(handle, sign_in)
                if e.error != "invalid_grant":
                    # Not the user's sign-in running out but this client failing:
                    # a wrong secret, a disabled client. Every renewal will fail.
                    logger.error("OIDC: the identity provider refused to renew a sign-in: %s", e)
                raise SignInEndedError(str(e)) from e
            except IdpUnavailableError:
                self._hold(sign_in, now)
                return
            # RFC 6749 §6: the IdP may keep the refresh token; then the old one stands.
            if renewed.refresh_token is None:
                renewed = replace(
                    renewed,
                    refresh_token=source.refresh_token,
                    refresh_expires_at=source.refresh_expires_at,
                )
            sign_in.pending = renewed
        await self._adopt(handle, sign_in, sign_in.pending, now)

    async def _adopt(self, handle: str, sign_in: SignIn, renewed: TokenSet, now: float) -> None:
        """Check `renewed`, the pending tokens, and make them the sign-in's, or end the sign-in."""
        try:
            id_claims: dict[str, Any] | None = None
            if renewed.id_token:
                # OIDC Core §12.2: a renewed ID token must be for the same user.
                id_claims = await self._client.verify_id_token(renewed.id_token)
            # Roles come only from what this renewal brought. The sign-in's own
            # ID token is not asked again: its roles are as old as the sign-in,
            # and an IdP that sends no new ID token would keep them forever.
            roles = await self._client.roles(id_claims or {}, renewed.access_token)
        except TokenRejectedError as e:
            await self._refuse(handle, sign_in)
            raise SignInEndedError(f"the renewed ID token was rejected: {e}") from e
        except IdpUnavailableError:
            self._hold(sign_in, now)
            return
        if id_claims is not None and id_claims["sub"] != sign_in.subject:
            await self._refuse(handle, sign_in)
            raise SignInEndedError("the renewed ID token is for a different user")
        if not is_admitted(roles, self._client.config.allowed_roles):
            await self._refuse(handle, sign_in)
            logger.info(
                "OIDC: %s no longer holds an allowed role%s",
                sign_in.subject,
                ""
                if id_claims
                else " (the renewal brought no ID token, so only the access token's roles count)",
            )
            raise SignInEndedError("the user no longer holds an allowed role")
        sign_in.tokens = renewed
        sign_in.pending = None
        if id_claims is not None:
            sign_in.id_claims = id_claims
        sign_in.roles = roles

    def _hold(self, sign_in: SignIn, now: float) -> None:
        """The IdP is out of reach: carry on with the current token while it lasts."""
        if sign_in.tokens.expires_at > now:
            logger.warning("OIDC: could not renew a sign-in; using its token until it expires")
            return
        raise IdpUnavailableError("the sign-in's token expired and the IdP cannot be reached")

    async def _refuse(self, handle: str, sign_in: SignIn) -> None:
        """End a sign-in whose renewal came back unacceptable, revoking what it brought."""
        self._end(handle, sign_in)
        await self._revoke(sign_in.pending)
        sign_in.pending = None

    def _prune(self) -> None:
        now = self._clock()
        for handle, sign_in in list(self._sign_ins.items()):
            tokens = sign_in.pending or sign_in.tokens
            renewable = tokens.refresh_token is not None and (
                tokens.refresh_expires_at is None or tokens.refresh_expires_at > now
            )
            if now - sign_in.created_at > self._max_age or (
                not renewable and sign_in.tokens.expires_at <= now
            ):
                self._end(handle, sign_in)
