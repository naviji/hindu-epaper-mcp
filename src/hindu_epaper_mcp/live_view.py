"""Stream the server's browser to the login page and forward the user's input.

Frames come from Chrome's DevTools screencast (JPEG). Taps, scrolls and
typing come back over the same WebSocket and are replayed with Playwright's
mouse/keyboard, so the user drives the real browser on the server from their
phone. Popups (Google's sign-in window) are followed automatically.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging

from playwright.async_api import BrowserContext, Page
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import config
from .auth import get_session

log = logging.getLogger("hindu_epaper_mcp")

_KEYS = {"Enter", "Backspace", "Tab", "Escape", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"}


def _visible_pages(ctx: BrowserContext) -> list[Page]:
    internal = get_session().internal_pages
    return [p for p in ctx.pages if not p.is_closed() and p not in internal]


async def login_page() -> Page:
    """Return the page the user should see, opening the ePaper login if needed."""
    ctx = await get_session().context()
    pages = _visible_pages(ctx)
    page = pages[-1] if pages else await ctx.new_page()
    if page.url in ("", "about:blank"):
        # A fresh profile opens with an empty tab; point it at the sign-in page.
        await page.goto(config.LOGIN_URL, wait_until="domcontentloaded")
    return page


class LiveView:
    """One viewer at a time; a new connection replaces the previous one."""

    def __init__(self) -> None:
        self._current: WebSocket | None = None

    async def serve(self, ws: WebSocket) -> None:
        if self._current is not None:
            with contextlib.suppress(Exception):
                await self._current.close(code=4000)
        self._current = ws
        session = get_session()
        session.pinned += 1
        await ws.accept()
        await self._send_json(ws, {"t": "status", "m": "Starting the browser on your server…"})
        try:
            await self._stream(ws)
        except Exception as exc:  # surface failures on the page, not a black box
            log.exception("Live view failed")
            await self._send_json(ws, {"t": "error", "m": f"{type(exc).__name__}: {exc}"[:600]})
            with contextlib.suppress(Exception):
                await ws.close(code=1011)
        finally:
            session.pinned -= 1
            session.touch()
            if self._current is ws:
                self._current = None
            asyncio.ensure_future(self._release_later())

    async def _release_later(self, grace: float = 120) -> None:
        """Free the sign-in tab's memory once nobody has watched it for a while.

        The grace period lets a dropped connection or a page reload resume
        mid-sign-in without losing the user's place.
        """
        await asyncio.sleep(grace)
        session = get_session()
        if self._current is not None or session.pinned or session._ctx is None:
            return
        pages = _visible_pages(session._ctx)
        for page in pages[:-1]:
            with contextlib.suppress(Exception):
                await page.close()
        if pages:
            with contextlib.suppress(Exception):
                await pages[-1].goto("about:blank")

    async def _stream(self, ws: WebSocket) -> None:
        ctx = await get_session().context()
        await login_page()
        switch = asyncio.Event()

        def on_new_page(_page) -> None:
            switch.set()

        ctx.on("page", on_new_page)
        state: dict = {"page": None}
        receiver = asyncio.create_task(self._receive(ws, state))
        try:
            while not receiver.done():
                pages = _visible_pages(ctx)
                page = pages[-1] if pages else await login_page()
                state["page"] = page
                switch.clear()

                def on_close(_page) -> None:
                    switch.set()

                def on_nav(frame) -> None:
                    if frame.parent_frame is None:
                        asyncio.ensure_future(self._send_json(ws, {"t": "url", "u": frame.url}))

                page.on("close", on_close)
                page.on("framenavigated", on_nav)
                cdp = await ctx.new_cdp_session(page)
                cdp.on("Page.screencastFrame", lambda ev: asyncio.ensure_future(self._frame(ws, cdp, ev)))
                await cdp.send(
                    "Page.startScreencast",
                    {"format": "jpeg", "quality": 60, "maxWidth": 900, "maxHeight": 1800},
                )
                await self._send_json(ws, {"t": "url", "u": page.url})
                waiter = asyncio.create_task(switch.wait())
                await asyncio.wait({waiter, receiver}, return_when=asyncio.FIRST_COMPLETED)
                waiter.cancel()
                page.remove_listener("close", on_close)
                page.remove_listener("framenavigated", on_nav)
                with contextlib.suppress(Exception):
                    await cdp.send("Page.stopScreencast")
                    await cdp.detach()
        finally:
            receiver.cancel()
            ctx.remove_listener("page", on_new_page)

    async def _frame(self, ws: WebSocket, cdp, ev: dict) -> None:
        with contextlib.suppress(Exception):
            await cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})
        meta = ev.get("metadata", {})
        try:
            await self._send_json(
                ws, {"t": "meta", "w": meta.get("deviceWidth"), "h": meta.get("deviceHeight")}
            )
            await ws.send_bytes(base64.b64decode(ev["data"]))
        except Exception:
            pass

    @staticmethod
    async def _send_json(ws: WebSocket, msg: dict) -> None:
        with contextlib.suppress(Exception):
            await ws.send_text(json.dumps(msg))

    async def _receive(self, ws: WebSocket, state: dict) -> None:
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                page: Page | None = state.get("page")
                if page is None or page.is_closed():
                    continue
                with contextlib.suppress(Exception):
                    await self._apply(page, msg)
        except (WebSocketDisconnect, RuntimeError):
            return

    @staticmethod
    async def _apply(page: Page, msg: dict) -> None:
        kind = msg.get("t")
        if kind == "click":
            await page.mouse.click(float(msg["x"]), float(msg["y"]))
        elif kind == "scroll":
            await page.mouse.move(float(msg["x"]), float(msg["y"]))
            await page.mouse.wheel(0, float(msg["dy"]))
        elif kind == "text":
            await page.keyboard.insert_text(str(msg["s"])[:500])
        elif kind == "key" and msg.get("k") in _KEYS:
            await page.keyboard.press(msg["k"])
        elif kind == "nav":
            action = msg.get("a")
            if action == "back":
                await page.go_back()
            elif action == "reload":
                await page.reload()
            elif action == "home":
                await page.goto(config.LOGIN_URL)


live_view = LiveView()
