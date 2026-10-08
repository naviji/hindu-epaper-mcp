"""Remote mode: serve the MCP server over streamable HTTP with OAuth.

Add the public URL + ``/mcp`` as a custom connector in Claude. Connecting sends
you to this server's ``/login`` page, where you unlock with the owner password,
sign in to The Hindu in the streamed browser, and allow access.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import uvicorn
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response
from starlette.routing import Route

from . import config, server
from .auth import get_session
from .display import ensure_display
from .oauth import SCOPE, HinduOAuthProvider
from .web import LoginApp, _secret_key

log = logging.getLogger("hindu_epaper_mcp")
LINK_TTL = 24 * 60 * 60


def _fail(msg: str) -> None:
    print(f"hindu-epaper-mcp: {msg}", file=sys.stderr)
    raise SystemExit(2)


class FileLinks:
    """Signed, expiring links to files in the downloads directory."""

    def __init__(self, public_url: str) -> None:
        self.public_url = public_url
        self.key = _secret_key()
        self.root = config.downloads_dir().resolve()

    def _sig(self, name: str, exp: str) -> str:
        return hmac.new(self.key, f"file:{name}:{exp}".encode(), hashlib.sha256).hexdigest()

    def link(self, path: Path) -> str:
        name = path.name
        exp = str(int(time.time()) + LINK_TTL)
        return f"{self.public_url}/files/{quote(name)}?exp={exp}&sig={self._sig(name, exp)}"

    async def serve(self, request: Request) -> Response:
        name = request.path_params["name"]
        exp = request.query_params.get("exp", "")
        sig = request.query_params.get("sig", "")
        if not exp.isdigit() or int(exp) < time.time() or not hmac.compare_digest(sig, self._sig(name, exp)):
            return PlainTextResponse("Link expired or invalid.", 403)
        path = (self.root / name).resolve()
        if path.parent != self.root or not path.is_file():
            return PlainTextResponse("Not found.", 404)
        return FileResponse(path, media_type="application/pdf", filename=name)


def run_http(host: str, port: int, public_url: str | None) -> None:
    if not public_url:
        _fail("--public-url (or HINDU_EPAPER_PUBLIC_URL) is required for --transport http.")
    public_url = public_url.rstrip("/")
    parsed = urlparse(public_url)
    if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1"):
        _fail("--public-url must be https:// (except localhost for testing).")
    owner_password = os.environ.get("HINDU_EPAPER_OWNER_PASSWORD", "")
    if len(owner_password) < 12:
        _fail("Set HINDU_EPAPER_OWNER_PASSWORD to a password of at least 12 characters.")

    # Prefer a real (headful) browser on a virtual display: Google trusts it more.
    headful = ensure_display() and os.environ.get("HINDU_EPAPER_HEADLESS", "") not in ("1", "true")
    get_session().configure(
        headless=not headful,
        viewport={"width": 430, "height": 900},
        device_scale_factor=1.5,
    )

    provider = HinduOAuthProvider(public_url)
    links = FileLinks(public_url)
    server.REMOTE["public_url"] = public_url
    server.REMOTE["file_link"] = links.link

    mcp = server.build_server(
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=public_url,
            resource_server_url=f"{public_url}/mcp",
            validate_token_resource=True,
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
    )
    host_header = parsed.netloc
    app = mcp.streamable_http_app(
        host=host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[host_header, "127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=[public_url, "http://127.0.0.1:*", "http://localhost:*"],
        ),
    )
    app.router.routes.extend(LoginApp(provider, owner_password).routes())
    app.router.routes.append(Route("/files/{name}", links.serve, methods=["GET"]))

    log.warning("Serving The Hindu ePaper MCP at %s/mcp (browser: %s)", public_url,
                "headful on virtual display" if headful else "headless")
    uvicorn.run(app, host=host, port=port, proxy_headers=True, forwarded_allow_ips="127.0.0.1")
