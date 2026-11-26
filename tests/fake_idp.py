"""A fake OpenID Connect provider, served through `httpx.MockTransport`.

Shaped like Keycloak: realm roles in the access token's `realm_access.roles`,
`refresh_expires_in` in token responses, an encryption key published next to
the signing key, refresh tokens rotated on each use. It signs real RS256
tokens with a key generated per instance, so every check the client makes
(signature, issuer, audience, lifetime) runs for real.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://idp.test/realms/analytics"
CLIENT_ID = "orionbelt-chat"
CLIENT_SECRET = "chat-client-secret"


def new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def public_jwk(key: rsa.RSAPrivateKey, kid: str, **extra: Any) -> dict[str, Any]:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**jwk, "kid": kid, "use": "sig", **extra}


@dataclass
class User:
    sub: str
    username: str
    roles: list[str] = field(default_factory=list)
    email: str | None = None
    name: str | None = None


@dataclass
class FakeIdp:
    issuer: str = ISSUER
    client_id: str = CLIENT_ID
    client_secret: str = CLIENT_SECRET
    access_lifetime: int = 300
    refresh_lifetime: int = 1800
    rotate_refresh: bool = True
    # When set, the refresh grant answers 400 with this OAuth error code.
    refresh_error: str | None = None
    # When set, the token endpoint answers with this HTTP status and no body.
    token_status: int | None = None
    # Override or remove (value None) keys of the discovery document.
    discovery_overrides: dict[str, Any] = field(default_factory=dict)
    # When set, the next refresh returns an ID token for this subject instead.
    refresh_id_token_sub: str | None = None
    # When false, a refresh response carries no new refresh token.
    refresh_returns_refresh_token: bool = True

    def __post_init__(self) -> None:
        self.key = new_key()
        self.kid = "sig-1"
        self.extra_jwks: list[dict[str, Any]] = [
            # Keycloak publishes its RSA-OAEP key too; it must never verify a token.
            {**public_jwk(new_key(), "enc-1"), "use": "enc", "alg": "RSA-OAEP"}
        ]
        self.codes: dict[str, tuple[User, str]] = {}
        self.refresh_tokens: dict[str, User] = {}
        self.revoked: list[str] = []
        self.requests: list[httpx.Request] = []

    # ── Test helpers ──

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def authorize(self, user: User, redirect_uri: str) -> str:
        """What the IdP's login page does: issue a code for `user`."""
        code = secrets.token_urlsafe(16)
        self.codes[code] = (user, redirect_uri)
        return code

    def sign(
        self,
        claims: dict[str, Any],
        *,
        key: rsa.RSAPrivateKey | None = None,
        kid: str | None = None,
        alg: str = "RS256",
    ) -> str:
        headers = {"kid": kid or self.kid}
        return jwt.encode(claims, key or self.key, algorithm=alg, headers=headers)

    def id_token_claims(self, user: User, **overrides: Any) -> dict[str, Any]:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "aud": self.client_id,
            "azp": self.client_id,
            "sub": user.sub,
            "iat": now,
            "exp": now + self.access_lifetime,
            "preferred_username": user.username,
            "typ": "ID",
        }
        if user.email:
            claims["email"] = user.email
            claims["email_verified"] = True
        if user.name:
            claims["name"] = user.name
        claims.update(overrides)
        return {k: v for k, v in claims.items() if v is not None}

    def access_token_claims(self, user: User) -> dict[str, Any]:
        now = int(time.time())
        return {
            "iss": self.issuer,
            "aud": "account",
            "azp": self.client_id,
            "sub": user.sub,
            "iat": now,
            "exp": now + self.access_lifetime,
            "typ": "Bearer",
            "realm_access": {"roles": list(user.roles)},
        }

    def token_requests(self, grant_type: str) -> list[dict[str, str]]:
        forms = [_form(r) for r in self.requests if r.url.path.endswith("/token")]
        return [f for f in forms if f.get("grant_type") == grant_type]

    def jwks_requests(self) -> int:
        return sum(1 for r in self.requests if r.url.path.endswith("/certs"))

    # ── The provider ──

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        base = httpx.URL(self.issuer).path
        if path == base + "/.well-known/openid-configuration":
            return httpx.Response(200, json=self.discovery_document())
        if path == base + "/protocol/openid-connect/certs":
            keys = [public_jwk(self.key, self.kid), *self.extra_jwks]
            return httpx.Response(200, json={"keys": keys})
        if path == base + "/protocol/openid-connect/token":
            return self._token(_form(request))
        if path == base + "/protocol/openid-connect/revoke":
            self.revoked.append(_form(request)["token"])
            return httpx.Response(200)
        return httpx.Response(404)

    def discovery_document(self) -> dict[str, Any]:
        endpoint = self.issuer + "/protocol/openid-connect"
        doc: dict[str, Any] = {
            "issuer": self.issuer,
            "authorization_endpoint": endpoint + "/auth",
            "token_endpoint": endpoint + "/token",
            "jwks_uri": endpoint + "/certs",
            "revocation_endpoint": endpoint + "/revoke",
            "id_token_signing_alg_values_supported": ["PS384", "RS256", "HS256", "none"],
        }
        for key, value in self.discovery_overrides.items():
            if value is None:
                doc.pop(key, None)
            else:
                doc[key] = value
        return doc

    def _tokens_for(self, user: User, *, id_token_sub: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "access_token": self.sign(self.access_token_claims(user)),
            "token_type": "Bearer",
            "expires_in": self.access_lifetime,
            "refresh_expires_in": self.refresh_lifetime,
            "id_token": self.sign(self.id_token_claims(user, sub=id_token_sub or user.sub)),
        }
        refresh = secrets.token_urlsafe(24)
        self.refresh_tokens[refresh] = user
        body["refresh_token"] = refresh
        return body

    def _token(self, form: dict[str, str]) -> httpx.Response:
        if self.token_status is not None:
            return httpx.Response(self.token_status)
        if form.get("client_id") != self.client_id or form.get("client_secret") != (
            self.client_secret
        ):
            return httpx.Response(401, json={"error": "invalid_client"})
        grant = form.get("grant_type")
        if grant == "authorization_code":
            issued = self.codes.pop(form.get("code", ""), None)
            if issued is None or issued[1] != form.get("redirect_uri"):
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json=self._tokens_for(issued[0]))
        if grant == "refresh_token":
            if self.refresh_error:
                return httpx.Response(
                    400, json={"error": self.refresh_error, "error_description": "refused"}
                )
            token = form.get("refresh_token", "")
            user = self.refresh_tokens.get(token)
            if user is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            if self.rotate_refresh:
                del self.refresh_tokens[token]
            body = self._tokens_for(user, id_token_sub=self.refresh_id_token_sub)
            if not self.refresh_returns_refresh_token:
                self.refresh_tokens.pop(body.pop("refresh_token"))
                body.pop("refresh_expires_in")
            return httpx.Response(200, json=body)
        return httpx.Response(400, json={"error": "unsupported_grant_type"})


def _form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
