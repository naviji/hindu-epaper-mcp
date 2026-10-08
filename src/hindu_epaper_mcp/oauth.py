"""OAuth 2.1 authorization server for the remote (HTTP) mode.

Claude (or any MCP client) registers itself with dynamic client registration,
then sends the user to ``/authorize``. Instead of a third-party identity
provider, the user lands on this server's own ``/login`` page, where they
first prove they are the owner (the owner password) and then, if needed, sign
in to The Hindu in the server's browser. Only then does the page call
``complete_authorization`` to mint a code and send them back to the client.

Clients, codes and tokens are persisted to ``<HINDU_EPAPER_HOME>/oauth.json``
so a server restart does not disconnect the connector. Tokens are stored only
as SHA-256 hashes.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from . import config

SCOPE = "epaper"
ACCESS_TOKEN_TTL = 60 * 60  # 1 hour
REFRESH_TOKEN_TTL = 60 * 60 * 24 * 30  # 30 days
AUTH_CODE_TTL = 5 * 60
PENDING_TTL = 30 * 60  # time the user has to finish the login page


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class HinduOAuthProvider:
    """Single-owner OAuth provider backed by a small JSON file."""

    def __init__(self, public_url: str, store_path: Path | None = None) -> None:
        self.public_url = public_url.rstrip("/")
        self.resource_url = f"{self.public_url}/mcp"
        self.store_path = store_path or (config.data_home() / "oauth.json")
        self._clients: dict[str, dict] = {}
        self._codes: dict[str, dict] = {}
        self._access: dict[str, dict] = {}
        self._refresh: dict[str, dict] = {}
        # Authorization requests waiting for the owner to finish /login.
        # Kept in memory only: a restart simply means starting the connect again.
        self._pending: dict[str, dict] = {}
        self._load()

    # -- persistence ---------------------------------------------------------
    def _load(self) -> None:
        if not self.store_path.exists():
            return
        data = json.loads(self.store_path.read_text())
        self._clients = data.get("clients", {})
        self._codes = data.get("codes", {})
        self._access = data.get("access", {})
        self._refresh = data.get("refresh", {})

    def _save(self) -> None:
        self._prune()
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store_path.with_suffix(".tmp")
        payload = {
            "clients": self._clients,
            "codes": self._codes,
            "access": self._access,
            "refresh": self._refresh,
        }
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, self.store_path)

    def _prune(self) -> None:
        now = time.time()
        for table in (self._codes, self._access, self._refresh):
            for key in [k for k, v in table.items() if v.get("expires_at") and v["expires_at"] < now]:
                del table[key]
        for key in [k for k, v in self._pending.items() if v["created"] + PENDING_TTL < now]:
            del self._pending[key]

    # -- client registration -------------------------------------------------
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self._clients.get(client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info.model_dump(mode="json", exclude_none=True)
        self._save()

    # -- authorization -------------------------------------------------------
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource.rstrip("/") != self.resource_url:
            raise AuthorizeError("invalid_target", "Unknown resource for this server.")
        req_id = secrets.token_urlsafe(24)
        self._prune()
        self._pending[req_id] = {
            "created": time.time(),
            "client_id": client.client_id,
            "client_name": client.client_name or client.client_id,
            "params": params.model_dump(mode="json"),
        }
        return f"{self.public_url}/login?{urlencode({'req': req_id})}"

    def pending_request(self, req_id: str) -> dict | None:
        self._prune()
        return self._pending.get(req_id)

    def complete_authorization(self, req_id: str) -> str:
        """Mint an authorization code for a pending request; return the redirect URL.

        Call only after the owner has authenticated on the login page.
        """
        pending = self._pending.pop(req_id, None)
        if pending is None:
            raise KeyError("Authorization request expired or unknown. Start the connection again.")
        params = AuthorizationParams.model_validate(pending["params"])
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [SCOPE],
            expires_at=time.time() + AUTH_CODE_TTL,
            client_id=pending["client_id"],
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=self.resource_url,
            subject="owner",
        ).model_dump(mode="json")
        self._save()
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    def deny_authorization(self, req_id: str) -> str | None:
        pending = self._pending.pop(req_id, None)
        if pending is None:
            return None
        params = AuthorizationParams.model_validate(pending["params"])
        return construct_redirect_uri(
            str(params.redirect_uri), error="access_denied", state=params.state
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        data = self._codes.get(authorization_code)
        if not data or data["client_id"] != client.client_id:
            return None
        return AuthorizationCode.model_validate(data)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self._codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "Authorization code already used.")
        return self._issue(client.client_id, authorization_code.scopes)

    # -- tokens --------------------------------------------------------------
    def _issue(self, client_id: str, scopes: list[str]) -> OAuthToken:
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        now = int(time.time())
        common = {"client_id": client_id, "scopes": scopes, "resource": self.resource_url, "subject": "owner"}
        self._access[_hash(access)] = {**common, "expires_at": now + ACCESS_TOKEN_TTL, "refresh": _hash(refresh)}
        self._refresh[_hash(refresh)] = {**common, "expires_at": now + REFRESH_TOKEN_TTL}
        self._save()
        return OAuthToken(
            access_token=access,
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        data = self._refresh.get(_hash(refresh_token))
        if not data or data["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, **data)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        # Rotate: the old refresh token and its access tokens stop working.
        self._revoke_refresh(_hash(refresh_token.token))
        return self._issue(client.client_id, scopes or refresh_token.scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self._access.get(_hash(token))
        if not data:
            return None
        if data["expires_at"] < time.time():
            return None
        fields = {k: v for k, v in data.items() if k != "refresh"}
        return AccessToken(token=token, **fields)

    def _revoke_refresh(self, refresh_hash: str) -> None:
        self._refresh.pop(refresh_hash, None)
        for key in [k for k, v in self._access.items() if v.get("refresh") == refresh_hash]:
            del self._access[key]
        self._save()

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        h = _hash(token.token)
        if isinstance(token, RefreshToken):
            self._revoke_refresh(h)
            return
        data = self._access.pop(h, None)
        if data and data.get("refresh"):
            self._revoke_refresh(data["refresh"])
        else:
            self._save()
