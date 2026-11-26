"""Signing users in to the chat with OpenID Connect: Chainlit's half.

`register()` adds one OIDC provider to Chainlit's login page when OIDC_ISSUER
is set, and does nothing otherwise. Chainlit's own providers are not used:
the generic one drops the refresh token, the Keycloak one keeps it on an
object every login shares, so two concurrent logins can swap tokens, and the
Azure one puts it in the session cookie. Here the tokens stay in this process
(`oidc.TokenStore`) and the cookie carries only a handle to them.

The session cookie outlives the sign-in by design: Chainlit issues it for
`user_session_timeout` (15 days), while the IdP may end a sign-in within
minutes. `check_signed_in()` closes that gap. Every new chat and every message
asks the store for a current access token, which renews it when needed, so a
user the IdP has disabled or signed out stops at the next message.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import chainlit as cl
import chainlit.oauth_providers as chainlit_oauth
import httpx
from chainlit.auth import get_token_from_cookies
from chainlit.auth.jwt import decode_jwt
from chainlit.config import config as chainlit_config
from chainlit.oauth_providers import OAuthProvider
from fastapi import HTTPException, Request, Response

from .oidc import (
    IdpUnavailableError,
    OidcClient,
    OidcConfig,
    OidcError,
    SignInEndedError,
    TokenStore,
    is_admitted,
    load_config,
    roles_from,
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
        access_claims = await self._client.access_token_claims(tokens.access_token)
        roles = roles_from((id_claims, access_claims), self._config.role_claims)
        handle = self._store.add(tokens, subject=id_claims["sub"], roles=roles)
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


async def _on_logout(request: Request, response: Response) -> dict[str, bool]:
    """End the sign-in behind the cookie being cleared, and revoke its tokens."""
    state = _state
    token = get_token_from_cookies(dict(request.cookies))
    if state is not None and token:
        try:
            user = decode_jwt(token)
        except Exception:  # an expired or forged cookie names no sign-in to end
            user = None
        if user is not None:
            await state.store.remove(user.metadata.get(SESSION_KEY))
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


def _session_handle() -> str | None:
    user = cl.user_session.get("user")
    metadata = getattr(user, "metadata", None)
    handle = metadata.get(SESSION_KEY) if isinstance(metadata, dict) else None
    return handle if isinstance(handle, str) else None


async def check_signed_in() -> str | None:
    """None when this chat session may go on; otherwise what to tell the user.

    Fails closed: when the IdP cannot be reached to renew an expired token,
    the session waits rather than going on unconfirmed.
    """
    state = _state
    if state is None:
        return None
    try:
        await state.store.access_token(_session_handle())
    except SignInEndedError as e:
        logger.info("Sign-in ended: %s", e)
        return SIGN_IN_ENDED
    except OidcError as e:
        logger.warning("Sign-in could not be confirmed: %s", e)
        return IDP_UNAVAILABLE
    return None
