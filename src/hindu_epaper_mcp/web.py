"""Login web pages for remote mode.

``/login`` is where the OAuth flow lands. It is protected by the owner
password (HINDU_EPAPER_OWNER_PASSWORD): the page exposes a browser that is
signed in to your Google account, so nobody else may reach it. Once unlocked
it shows the live browser for The Hindu sign-in and an "Allow" button that
completes the connector's OAuth handshake.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import os
import secrets
import time
from collections import deque
from urllib.parse import quote, urlparse

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from . import config
from .auth import get_session
from .live_view import live_view
from .oauth import HinduOAuthProvider

COOKIE = "hep_owner"
COOKIE_TTL = 12 * 60 * 60
MAX_FAILURES = 5
FAILURE_WINDOW = 15 * 60


def _secret_key() -> bytes:
    path = config.data_home() / "secret.key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(secrets.token_bytes(32))
    return path.read_bytes()


class LoginApp:
    def __init__(self, provider: HinduOAuthProvider, owner_password: str) -> None:
        self.provider = provider
        self.owner_password = owner_password
        self.key = _secret_key()
        self.origin = provider.public_url
        self.secure = urlparse(provider.public_url).scheme == "https"
        self.failures: deque[float] = deque()

    # -- owner session -------------------------------------------------------
    def _sign(self, value: str) -> str:
        return hmac.new(self.key, value.encode(), hashlib.sha256).hexdigest()

    def _make_cookie(self) -> str:
        expires = str(int(time.time()) + COOKIE_TTL)
        return f"{expires}.{self._sign(expires)}"

    def _is_owner(self, cookies) -> bool:
        value = cookies.get(COOKIE, "")
        expires, _, sig = value.partition(".")
        if not expires.isdigit() or int(expires) < time.time():
            return False
        return hmac.compare_digest(sig, self._sign(expires))

    def _csrf(self, request: Request) -> str:
        return self._sign("csrf:" + request.cookies.get(COOKIE, ""))

    def _locked_out(self) -> bool:
        now = time.time()
        while self.failures and self.failures[0] < now - FAILURE_WINDOW:
            self.failures.popleft()
        return len(self.failures) >= MAX_FAILURES

    # -- routes --------------------------------------------------------------
    def routes(self) -> list:
        return [
            Route("/", self.index, methods=["GET"]),
            Route("/login", self.login_get, methods=["GET"]),
            Route("/login", self.login_post, methods=["POST"]),
            Route("/login/status", self.status, methods=["GET"]),
            Route("/login/approve", self.approve, methods=["POST"]),
            Route("/login/deny", self.deny, methods=["POST"]),
            WebSocketRoute("/login/screen", self.screen),
        ]

    async def index(self, request: Request) -> Response:
        return RedirectResponse("/login", status_code=303)

    async def login_get(self, request: Request) -> Response:
        req = request.query_params.get("req", "")
        if req and self.provider.pending_request(req) is None:
            return HTMLResponse(_page("Link expired", "<p>This sign-in link has expired. Start connecting again from Claude.</p>"), 400)
        if not self._is_owner(request.cookies):
            return HTMLResponse(_password_page(req, error=""))
        pending = self.provider.pending_request(req) if req else None
        client = pending["client_name"] if pending else None
        return HTMLResponse(_app_page(req, client, self._csrf(request)))

    async def login_post(self, request: Request) -> Response:
        form = await request.form()
        req = str(form.get("req", ""))
        if self._locked_out():
            return HTMLResponse(_password_page(req, "Too many attempts. Try again in 15 minutes."), 429)
        password = str(form.get("password", ""))
        if not hmac.compare_digest(password.encode(), self.owner_password.encode()):
            self.failures.append(time.time())
            return HTMLResponse(_password_page(req, "Wrong password."), 401)
        target = f"/login?req={quote(req, safe='')}" if req else "/login"
        resp = RedirectResponse(target, status_code=303)
        resp.set_cookie(
            COOKIE, self._make_cookie(), max_age=COOKIE_TTL, httponly=True,
            secure=self.secure, samesite="lax", path="/",
        )
        return resp

    async def status(self, request: Request) -> Response:
        if not self._is_owner(request.cookies):
            return JSONResponse({"error": "unauthorized"}, 401)
        token = await get_session().token_from_open_pages()
        return JSONResponse({"hindu_logged_in": bool(token)})

    async def _checked_form(self, request: Request):
        if not self._is_owner(request.cookies):
            return None
        form = await request.form()
        if not hmac.compare_digest(str(form.get("csrf", "")), self._csrf(request)):
            return None
        return form

    async def approve(self, request: Request) -> Response:
        form = await self._checked_form(request)
        if form is None:
            return HTMLResponse(_page("Not allowed", "<p>Session expired. Reload the page.</p>"), 403)
        try:
            target = self.provider.complete_authorization(str(form.get("req", "")))
        except KeyError as exc:
            return HTMLResponse(_page("Link expired", f"<p>{html.escape(str(exc))}</p>"), 400)
        return RedirectResponse(target, status_code=303)

    async def deny(self, request: Request) -> Response:
        form = await self._checked_form(request)
        if form is None:
            return HTMLResponse(_page("Not allowed", "<p>Session expired. Reload the page.</p>"), 403)
        target = self.provider.deny_authorization(str(form.get("req", "")))
        if target is None:
            return HTMLResponse(_page("Cancelled", "<p>Nothing to cancel.</p>"))
        return RedirectResponse(target, status_code=303)

    async def screen(self, ws: WebSocket) -> None:
        origin = ws.headers.get("origin", "")
        if not self._is_owner(ws.cookies) or (origin and origin.rstrip("/") != self.origin):
            await ws.close(code=4401)
            return
        await live_view.serve(ws)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
_STYLE = """
:root{--bg:#f6f5f2;--fg:#1c1c1c;--muted:#666;--card:#fff;--line:#ddd;--accent:#1a5fb4;--ok:#26803c}
@media (prefers-color-scheme:dark){:root{--bg:#16171b;--fg:#ececec;--muted:#9a9a9a;--card:#202227;--line:#33353b;--accent:#6aa0ef;--ok:#57c06f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,-apple-system,sans-serif}
main{max-width:560px;margin:0 auto;padding:16px}h1{font-size:20px;margin:8px 0 12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin:12px 0}
input[type=password],input[type=text]{width:100%;padding:12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font-size:16px}
button{font-size:16px;padding:11px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff;width:100%}
button:disabled{opacity:.45}.muted{color:var(--muted);font-size:14px}.err{color:#c0392b}.ok{color:var(--ok);font-weight:600}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}.row button{flex:1}
#screen{width:100%;border:1px solid var(--line);border-radius:8px;background:#000;touch-action:none;display:block}
#url{font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin:6px 0}
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"
    )


def _password_page(req: str, error: str) -> str:
    err = f"<p class=err>{html.escape(error)}</p>" if error else ""
    return _page(
        "The Hindu ePaper",
        f"""<div class=card><p>Enter your server's owner password to continue.</p>{err}
<form method=post action=/login><input type=hidden name=req value="{html.escape(req)}">
<input type=password name=password autocomplete=current-password autofocus required>
<div class=row><button class=primary type=submit>Unlock</button></div></form></div>""",
    )


def _app_page(req: str, client: str | None, csrf: str) -> str:
    ask = (
        f"<p><b>{html.escape(client)}</b> wants to read your ePaper.</p>" if client else
        "<p>Sign in to The Hindu below. You can close this page when it says you're signed in.</p>"
    )
    actions = ""
    if client:
        actions = f"""<form method=post action=/login/approve>
<input type=hidden name=req value="{html.escape(req)}"><input type=hidden name=csrf value="{csrf}">
<button id=allow class=primary type=submit disabled>Allow access</button></form>
<p class=muted id=allowhint>Sign in to The Hindu first. <a href="#" id=anyway>Allow anyway</a></p>
<form method=post action=/login/deny><input type=hidden name=req value="{html.escape(req)}">
<input type=hidden name=csrf value="{csrf}"><div class=row><button type=submit>Cancel</button></div></form>"""
    body = f"""<div class=card>{ask}<p id=status class=muted>Checking your The Hindu session…</p>{actions}</div>
<div class=card id=viewer><p class=muted>This is a real browser on your server. Tap the 👤 icon at the top of the paper to sign in with Google. Tap to click, drag to scroll, and use the box below to type.</p>
<div id=url></div><p id=note class=muted>Connecting to the browser…</p><canvas id=screen></canvas>
<div class=row><input type=text id=typed placeholder="Type here, then tap Send" autocomplete=off autocapitalize=off autocorrect=off spellcheck=false></div>
<div class=row><button id=send>Send</button><button data-key=Enter>Enter ⏎</button><button data-key=Backspace>⌫</button><button data-key=Tab>Tab</button></div>
<div class=row><button data-nav=back>Back</button><button data-nav=reload>Reload</button><button data-nav=home>Start over</button></div></div>
<script>{_SCRIPT}</script>"""
    return _page("The Hindu ePaper", body)


_SCRIPT = r"""
const canvas=document.getElementById('screen'),ctx=canvas.getContext('2d');
let dev={w:430,h:900},ws,signedIn=false,failed=false;const note=document.getElementById('note');
function connect(){
  ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/login/screen');
  ws.binaryType='blob';
  ws.onmessage=async e=>{
    if(typeof e.data==='string'){const m=JSON.parse(e.data);
      if(m.t==='meta'&&m.w){dev={w:m.w,h:m.h}}
      if(m.t==='url'){document.getElementById('url').textContent=m.u}
      if(m.t==='status'){note.className='muted';note.textContent=m.m;note.hidden=false}
      if(m.t==='error'){failed=true;note.className='err';note.textContent='The browser on your server failed: '+m.m;note.hidden=false}
      return}
    note.hidden=true;
    const bmp=await createImageBitmap(e.data);
    if(canvas.width!==bmp.width||canvas.height!==bmp.height){canvas.width=bmp.width;canvas.height=bmp.height}
    ctx.drawImage(bmp,0,0)};
  ws.onclose=e=>{if(e.code===4000||signedIn)return;
    if(e.code===4401){note.className='err';note.textContent='Your session expired. Reload this page.';note.hidden=false;return}
    if(!failed){note.className='muted';note.textContent='Reconnecting…';note.hidden=false}
    setTimeout(connect,failed?8000:1500);failed=false};
}
function send(m){if(ws&&ws.readyState===1)ws.send(JSON.stringify(m))}
function pos(e){const r=canvas.getBoundingClientRect();return{x:(e.clientX-r.left)/r.width*dev.w,y:(e.clientY-r.top)/r.height*dev.h}}
let start=null,last=null;
canvas.addEventListener('pointerdown',e=>{start=last={x:e.clientX,y:e.clientY,p:pos(e)}});
canvas.addEventListener('pointermove',e=>{if(!start)return;const dy=last.y-e.clientY;
  if(Math.abs(e.clientY-start.y)>8){send({t:'scroll',x:start.p.x,y:start.p.y,dy:dy*dev.w/canvas.getBoundingClientRect().width});start.moved=true}
  last={x:e.clientX,y:e.clientY}});
canvas.addEventListener('pointerup',e=>{if(start&&!start.moved){const p=pos(e);send({t:'click',x:p.x,y:p.y})}start=null});
const typed=document.getElementById('typed');
document.getElementById('send').onclick=()=>{if(typed.value){send({t:'text',s:typed.value});typed.value=''}};
typed.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();if(typed.value){send({t:'text',s:typed.value});typed.value=''}send({t:'key',k:'Enter'})}});
document.querySelectorAll('[data-key]').forEach(b=>b.onclick=()=>send({t:'key',k:b.dataset.key}));
document.querySelectorAll('[data-nav]').forEach(b=>b.onclick=()=>send({t:'nav',a:b.dataset.nav}));
const allow=document.getElementById('allow'),anyway=document.getElementById('anyway');
if(anyway)anyway.onclick=e=>{e.preventDefault();allow.disabled=false};
async function poll(){
  try{const r=await fetch('/login/status',{credentials:'same-origin'});const j=await r.json();
    const s=document.getElementById('status');
    if(j.hindu_logged_in){signedIn=true;s.textContent='Signed in to The Hindu ✓';s.className='ok';
      if(allow){allow.disabled=false;document.getElementById('allowhint').hidden=true}
      document.getElementById('viewer').hidden=true;if(ws)ws.close();return}
    s.textContent='Not signed in to The Hindu yet.';s.className='muted'}catch(e){}
  setTimeout(poll,2500)}
connect();poll();
"""
