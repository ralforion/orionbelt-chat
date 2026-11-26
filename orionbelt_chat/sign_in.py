"""Signing users in to the chat with OpenID Connect: Chainlit's half.

`register()` adds one OIDC provider to Chainlit's login page when OIDC_ISSUER
is set, and does nothing otherwise. Chainlit's own providers are not used:
the generic one drops the refresh token, the Keycloak one keeps it on an
object every login shares, so two concurrent logins can swap tokens, and the
Azure one puts it in the session cookie. Here the tokens stay in this process
(`oidc.TokenStore`) and the cookie carries only a handle to them.

The session cookie outlives the sign-in by design: Chainlit issues it for
`user_session_timeout` (15 days), while the IdP may end a sign-in within
minutes, and a cookie copied before a sign-out stays valid to Chainlit. Two
checks close that gap, both asking the store for a current access token, which
renews it when due:

- Chainlit's own authentication (`chainlit.auth.authenticate_user`) is
  wrapped, so every protected HTTP route and every websocket connection is
  refused once the sign-in behind the cookie has ended.
- `check_signed_in()` runs at every new chat and every message, for a
  websocket that stays open while the sign-in ends.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import chainlit as cl
import chainlit.auth as chainlit_auth
import chainlit.oauth_providers as chainlit_oauth
import httpx
from chainlit.auth import get_token_from_cookies
from chainlit.auth.jwt import decode_jwt
from chainlit.config import config as chainlit_config
from chainlit.context import context
from chainlit.oauth_providers import OAuthProvider
from fastapi import Depends, HTTPException, Request, Response

from .oidc import (
    IdpUnavailableError,
    OidcClient,
    OidcConfig,
    OidcError,
    SignInBearerAuth,
    SignInEndedError,
    TokenStore,
    is_admitted,
    load_config,
    user_identifier,
)
from .settings import settings

logger = logging.getLogger(__name__)

#: The user metadata key holding the handle to a sign-in's tokens.
SESSION_KEY = "oidc_session"

SIGN_IN_ENDED = (
    "Your sign-in has ended. Sign out from the menu at the top right, then sign in again."
)
IDP_UNAVAILABLE = (
    "The sign-in service cannot be reached, so this session cannot be confirmed. "
    "Try again in a moment."
)

# A code exchanged but not yet turned into a user is forgotten after this long:
# Chainlit asks for the user right after the exchange, in the same request.
_PENDING_TTL_SECONDS = 300
# Backoff for reaching the IdP at startup, which may come up after the chat.
_DISCOVERY_RETRY_FIRST = 2.0
_DISCOVERY_RETRY_MAX = 60.0


class OidcProvider(OAuthProvider):
    """A Chainlit OAuth provider for any OpenID Connect identity provider."""

    def __init__(self, config: OidcConfig, client: OidcClient, store: TokenStore) -> None:
        self.id = config.provider_name
        self.env = []
        self.client_id = config.client_id
        self.authorize_params = {"scope": config.scopes, "response_type": "code"}
        self._config = config
        self._client = client
        self._store = store
        # access token -> (when, handle, ID token claims), between Chainlit's
        # `get_token` and `get_user_info`. Keyed by the token, so two logins in
        # flight at once cannot pick up each other's user.
        self._pending: dict[str, tuple[float, str, dict[str, Any]]] = {}

    def is_configured(self) -> bool:
        # Only ever registered with a validated configuration.
        return True

    # Chainlit reads this to build the redirect to the IdP. It comes from the
    # discovery document, which is fetched at startup; until it has been, a
    # login says so rather than sending the user to a guessed URL.
    @property
    def authorize_url(self) -> str:  # type: ignore[override]
        discovery = self._client.discovered
        if discovery is None:
            raise HTTPException(
                status_code=503,
                detail="The sign-in service has not been reached yet. Try again in a moment.",
            )
        return discovery.authorization_endpoint

    async def get_token(self, code: str, url: str) -> str:
        tokens = await self._client.exchange_code(code, url)
        if tokens.id_token is None:
            raise OidcError("The identity provider returned no ID token")
        id_claims = await self._client.verify_id_token(tokens.id_token)
        roles = await self._client.roles(id_claims, tokens.access_token)
        handle = self._store.add(tokens, id_claims=id_claims, roles=roles)
        now = asyncio.get_running_loop().time()
        for token, (since, *_rest) in list(self._pending.items()):
            if now - since > _PENDING_TTL_SECONDS:
                del self._pending[token]
        self._pending[tokens.access_token] = (now, handle, id_claims)
        return tokens.access_token

    async def get_user_info(self, token: str) -> tuple[dict[str, Any], cl.User]:
        pending = self._pending.pop(token, None)
        if pending is None:
            raise OidcError("No sign-in is waiting for this token")
        _since, handle, claims = pending
        name = claims.get("name")
        user = cl.User(
            identifier=user_identifier(claims, self._config.user_claims),
            display_name=name if isinstance(name, str) and name.strip() else None,
            metadata={"provider": self.id, SESSION_KEY: handle},
        )
        return claims, user


class _State:
    """What `register()` set up, for the callbacks and `check_signed_in()`."""

    def __init__(self, config: OidcConfig, client: OidcClient, store: TokenStore) -> None:
        self.config = config
        self.client = client
        self.store = store
        self.provider = OidcProvider(config, client, store)
        self.discovery_task: asyncio.Task[None] | None = None


_state: _State | None = None


def register(*, transport: httpx.AsyncBaseTransport | None = None) -> bool:
    """Turn sign-in on when OIDC_ISSUER is set. Returns whether it is on.

    Raises `OidcConfigError` for settings that cannot work, so the app refuses
    to start instead of showing a login page nobody can get past. Chainlit
    then refuses to start without CHAINLIT_AUTH_SECRET.

    `transport` carries the requests to the identity provider; tests pass a
    fake one.
    """
    global _state  # noqa: PLW0603
    if _state is not None:
        return True
    config = load_config(settings)
    if config is None:
        return False
    client = OidcClient(config, transport=transport)
    store = TokenStore(client, max_age_seconds=chainlit_config.project.user_session_timeout)
    state = _State(config, client, store)
    # First, so that Chainlit's lookup by id finds this provider; and before
    # the decorator below, which refuses to register without a provider.
    chainlit_oauth.providers.insert(0, state.provider)
    _state = state
    _check_chainlit_authentication()
    cl.oauth_callback(_admit)
    cl.on_logout(_on_logout)
    cl.on_app_startup(_on_startup)
    cl.on_app_shutdown(_on_shutdown)
    logger.info(
        "Sign-in: OpenID Connect via %s as client %s (%s)",
        config.issuer,
        config.client_id,
        f"roles {', '.join(sorted(config.allowed_roles))}"
        if config.allowed_roles
        else "every user the IdP signs in",
    )
    return True


async def _admit(
    provider_id: str,
    token: str,
    raw_user_data: dict[str, Any],
    default_user: cl.User,
    id_token: str | None = None,
) -> cl.User | None:
    """Chainlit's OAuth callback: admit the user, or refuse the login."""
    state = _state
    if state is None or provider_id != state.provider.id:
        # Another provider configured through Chainlit's OAUTH_* variables.
        logger.warning("Sign-in refused: provider %r is not the configured one", provider_id)
        return None
    handle = default_user.metadata.get(SESSION_KEY)
    sign_in = state.store.get(handle)
    if sign_in is None:
        return None
    if not is_admitted(sign_in.roles, state.config.allowed_roles):
        logger.warning(
            "Sign-in refused for %s: none of the allowed roles (%s)",
            default_user.identifier,
            ", ".join(sorted(state.config.allowed_roles)),
        )
        await state.store.remove(handle)
        return None
    logger.info("Signed in: %s", default_user.identifier)
    return default_user


def _handle_from_token(token: str | None) -> str | None:
    """The sign-in handle in a Chainlit session token, if it is valid and holds one.

    Read from the token rather than from the user Chainlit hands on: with a
    data layer that user comes from the database, and the token is the
    credential actually presented.
    """
    if not token:
        return None
    try:
        user = decode_jwt(token)
    except Exception:  # expired, forged or malformed: no sign-in behind it
        return None
    metadata = user.metadata if isinstance(user.metadata, dict) else {}
    handle = metadata.get(SESSION_KEY)
    return handle if isinstance(handle, str) else None


async def _confirm(token: str | None) -> None:
    """Return when the sign-in behind `token` is live, renewing it if due.

    Raises `SignInEndedError` when it has ended (signed out, refused by the
    IdP, a role taken away, or not known here) and another `OidcError` when
    the IdP is needed and cannot be reached.
    """
    state = _state
    if state is not None:
        await state.store.access_token(_handle_from_token(token))


def _check_chainlit_authentication() -> None:
    """Make Chainlit's own authentication refuse a cookie whose sign-in has ended.

    Every protected HTTP route depends on `get_current_user`, and the
    websocket's connect calls it; both reach `authenticate_user` through the
    `chainlit.auth` module, so wrapping it there covers all of them.
    """
    verify_cookie = chainlit_auth.authenticate_user

    # The same signature as Chainlit's, default included, so the wrapper is
    # also a valid FastAPI dependency should a route ever depend on it directly.
    async def authenticate_user(token: str = Depends(chainlit_auth.reuseable_oauth)) -> Any:
        user = await verify_cookie(token)
        try:
            await _confirm(token)
        except SignInEndedError as e:
            logger.info("Refused a session whose sign-in has ended: %s", e)
            raise HTTPException(status_code=401, detail="Your sign-in has ended") from e
        except OidcError as e:
            logger.warning("Could not confirm a sign-in: %s", e)
            raise HTTPException(
                status_code=503, detail="The sign-in service cannot be reached"
            ) from e
        return user

    chainlit_auth.authenticate_user = authenticate_user


async def _on_logout(request: Request, response: Response) -> dict[str, bool]:
    """End the sign-in behind the cookie being cleared, and revoke its tokens.

    The cookie is only cleared in this browser; a copy kept elsewhere is
    refused from now on, because the sign-in it names no longer exists.
    """
    state = _state
    if state is not None:
        token = get_token_from_cookies(dict(request.cookies))
        await state.store.remove(_handle_from_token(token))
    return {"success": True}


async def _on_startup() -> None:
    state = _state
    if state is not None:
        state.discovery_task = asyncio.create_task(_discover(state.client))


async def _on_shutdown() -> None:
    state = _state
    if state is not None and state.discovery_task is not None:
        state.discovery_task.cancel()


async def _discover(client: OidcClient) -> None:
    """Fetch the discovery document, retrying until the IdP answers."""
    delay = _DISCOVERY_RETRY_FIRST
    while True:
        try:
            await client.discovery()
            return
        except IdpUnavailableError as e:
            logger.warning(
                "Sign-in: the identity provider cannot be reached yet (%s); retrying in %.0fs",
                e,
                delay,
            )
        except OidcError as e:
            # The document contradicts the settings; retrying will not help.
            logger.error("Sign-in cannot work: %s", e)
            return
        await asyncio.sleep(delay)
        delay = min(delay * 2, _DISCOVERY_RETRY_MAX)


def _session_token() -> str | None:
    token = getattr(context.session, "token", None)
    return token if isinstance(token, str) else None


async def check_signed_in() -> str | None:
    """None when this chat session may go on; otherwise what to tell the user.

    The websocket was checked when it connected; this catches a sign-in that
    ends while it stays open. Fails closed: when the IdP cannot be reached to
    renew an expired token, the session waits rather than going on unconfirmed.
    """
    if _state is None:
        return None
    try:
        await _confirm(_session_token())
    except SignInEndedError as e:
        logger.info("Sign-in ended: %s", e)
        return SIGN_IN_ENDED
    except OidcError as e:
        logger.warning("Sign-in could not be confirmed: %s", e)
        return IDP_UNAVAILABLE
    return None


def session_auth() -> httpx.Auth | None:
    """Bearer auth with this chat session's sign-in, for MCP servers in `auth: user`.

    None while sign-in is off. Each request asks for a current access token,
    so the servers see the token renewed as the session goes on, and requests
    stop once the sign-in ends.
    """
    if _state is None:
        return None
    return SignInBearerAuth(_state.store, _handle_from_token(_session_token()))


def problem_from(exc: BaseException) -> str | None:
    """What to tell the user when `exc` comes from this session's sign-in, else None.

    A request to an `auth: user` server fails with the sign-in's own error,
    wrapped on its way out of the MCP client (exception groups, causes). Told
    apart here so the chat asks the user to sign in again rather than trying
    to reconnect a server that would only refuse again.
    """
    found: list[OidcError] = []
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, OidcError):
            found.append(current)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        pending.extend(e for e in (current.__cause__, current.__context__) if e is not None)
    if any(isinstance(e, SignInEndedError) for e in found):
        return SIGN_IN_ENDED
    return IDP_UNAVAILABLE if found else None
