"""Browser-backed authentication for The Hindu ePaper.

The ePaper authenticates with Piano ID (tinypass) on top of a Google OAuth
sign-in. There is no public API token we can mint ourselves, so we drive a
real browser, let the user's Google/Piano login happen there, and persist the
resulting session in a Playwright *persistent context* (a user-data
directory). Every later run reuses that directory, so the login survives
across restarts and the user is not asked again until it expires.

A best-effort scripted-credentials path is included, but note that Google
deliberately blocks fully-automated password entry (CAPTCHA / 2FA / "this
browser may not be secure"), so the dependable mode is: launch the browser
once, finish the login by hand, and let the session persist.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time

from playwright.async_api import (
    async_playwright,
    BrowserContext,
    Playwright,
    TimeoutError as PWTimeout,
)

from . import config

log = logging.getLogger("hindu_epaper_mcp")

# JS that returns the Piano auth token when the user is signed in, else null.
_TOKEN_JS = "() => (window.tp && window.tp.pianoId && window.tp.pianoId.getToken) ? window.tp.pianoId.getToken() : null"


# Close the browser after this long without use; it relaunches on demand.
IDLE_SECONDS = int(os.environ.get("HINDU_EPAPER_BROWSER_IDLE", "600"))

# Requests pages never need for signing in: ad/analytics hosts and the newspaper's
# page images. Google and Piano (the sign-in providers) are deliberately absent.
# Context routes don't apply to context.request, so PDF downloads are unaffected.
_BLOCK = re.compile(
    r"^https?://([^/]*\.)?("
    r"googletagmanager\.com|google-analytics\.com|doubleclick\.net|googlesyndication\.com"
    r"|googleadservices\.com|adservice\.google\.com|facebook\.net|facebook\.com"
    r"|scorecardresearch\.com|taboola\.com|outbrain\.com|chartbeat\.(com|net)"
    r"|izooto\.com|moengage\.com|clevertap-prod\.com|hotjar\.com|clarity\.ms"
    r"|mtdg\.thehindu\.com|truste\.com|trustarc\.com"
    r")/"
    r"|^https://epaper\.thehindu\.com/ccidist-ws/.*\.(jpe?g|png)(\?|$)",
    re.I,
)


def _headless_default() -> bool:
    return os.environ.get("HINDU_EPAPER_HEADLESS", "").lower() in ("1", "true", "yes")


class BrowserSession:
    """Owns a single persistent browser context, lazily launched and reused."""

    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._ctx: BrowserContext | None = None
        self._lock = asyncio.Lock()
        # Launch settings; remote (HTTP) mode switches these via configure().
        self.headless: bool | None = None
        self.viewport = {"width": 1280, "height": 1600}
        self.device_scale_factor = 1
        # Pages opened for internal checks; kept out of the live login view.
        self.internal_pages: set = set()
        # Number of live-view viewers; the browser is never reaped while > 0.
        self.pinned = 0
        self._last_used = time.monotonic()
        self._reaper: asyncio.Task | None = None

    def configure(self, *, headless: bool, viewport: dict, device_scale_factor: float) -> None:
        """Set launch options. Must be called before the browser first starts."""
        self.headless = headless
        self.viewport = viewport
        self.device_scale_factor = device_scale_factor

    async def _ensure_context(self, headless: bool) -> BrowserContext:
        """Launch (or return the already-running) persistent context."""
        if self._ctx is not None:
            return self._ctx
        self._pw = await async_playwright().start()
        launch_kwargs: dict = {
            "user_data_dir": str(config.session_dir()),
            "headless": headless,
            "user_agent": config.USER_AGENT,
            "viewport": self.viewport,
            "device_scale_factor": self.device_scale_factor,
            "args": [
                "--disable-blink-features=AutomationControlled",
                # Keep Chrome lean so it fits on small (≈1 GB) servers.
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-features=Translate,MediaRouter,OptimizationHints,AutofillServerCommunication",
                "--renderer-process-limit=1",
                "--in-process-gpu",
                "--js-flags=--max-old-space-size=256",
            ],
            # Fail with a clear error instead of hanging for Playwright's 3-minute default.
            "timeout": 90_000,
        }
        exe = config.chromium_executable()
        if exe:
            launch_kwargs["executable_path"] = exe
        proxy = os.environ.get("HINDU_EPAPER_BROWSER_PROXY")
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
        log.warning("Launching browser (%s)", "headless" if headless else "headful")
        started = time.monotonic()
        try:
            self._ctx = await self._pw.chromium.launch_persistent_context(**launch_kwargs)
        except Exception:
            # Don't leak a Playwright driver per failed attempt; the next call retries cleanly.
            await self._pw.stop()
            self._pw = None
            raise
        log.warning("Browser ready in %.1fs", time.monotonic() - started)
        await self._ctx.route(_BLOCK, lambda route: route.abort())
        return self._ctx

    async def context(self, headless: bool | None = None) -> BrowserContext:
        """Return a running context suitable for authenticated fetches."""
        async with self._lock:
            if headless is None:
                headless = _headless_default() if self.headless is None else self.headless
            ctx = await self._ensure_context(headless)
            self._last_used = time.monotonic()
            if IDLE_SECONDS > 0 and (self._reaper is None or self._reaper.done()):
                self._reaper = asyncio.create_task(self._reap_when_idle())
            return ctx

    def touch(self) -> None:
        self._last_used = time.monotonic()

    async def _reap_when_idle(self) -> None:
        """Close the browser after IDLE_SECONDS unused, to free memory on small servers."""
        while self._ctx is not None:
            await asyncio.sleep(30)
            if self.pinned == 0 and time.monotonic() - self._last_used > IDLE_SECONDS:
                log.warning("Closing idle browser")
                await self.close()
                return

    async def current_token(self) -> str | None:
        """Return the Piano auth token if a valid session exists, else None."""
        token = await self.token_from_open_pages()
        if token:
            return token
        ctx = await self.context()
        page = await ctx.new_page()
        self.internal_pages.add(page)
        try:
            await page.goto(config.READER_URL, wait_until="domcontentloaded")
            # tinypass loads asynchronously; poll briefly for the token.
            for _ in range(20):
                token = await page.evaluate(_TOKEN_JS)
                if token:
                    return token
                await asyncio.sleep(0.5)
            return None
        finally:
            self.internal_pages.discard(page)
            await page.close()

    async def token_from_open_pages(self) -> str | None:
        """Cheaply read the Piano token from pages already open on the ePaper site."""
        if self._ctx is None:
            return None
        for page in list(self._ctx.pages):
            if page.is_closed() or not page.url.startswith(config.SITE):
                continue
            try:
                token = await page.evaluate(_TOKEN_JS)
            except Exception:
                continue
            if token:
                return token
        return None

    async def is_logged_in(self) -> bool:
        return bool(await self.current_token())

    async def login(
        self,
        email: str | None = None,
        password: str | None = None,
        timeout_seconds: int = 240,
    ) -> dict:
        """Establish a session.

        If already logged in, returns immediately. Otherwise opens the login
        page. When email/password are supplied, attempts the Google flow; in
        all cases it then waits (up to ``timeout_seconds``) for the login to
        complete — giving the user time to finish it interactively in the
        launched browser.
        """
        # Login should be visible so the user can complete Google's flow,
        # unless the caller explicitly forces headless.
        ctx = await self.context()

        if await self.is_logged_in():
            return {"status": "already_logged_in"}

        page = await ctx.new_page()
        try:
            await page.goto(config.LOGIN_URL, wait_until="domcontentloaded")

            if email and password:
                await self._try_google_credentials(page, email, password)

            # Wait for the session to appear, whether finished by script or by
            # the user interacting with the visible browser window.
            deadline = asyncio.get_event_loop().time() + timeout_seconds
            while asyncio.get_event_loop().time() < deadline:
                token = await page.evaluate(_TOKEN_JS)
                if token:
                    return {
                        "status": "logged_in",
                        "detail": "Session saved; it will be reused on future runs.",
                    }
                await asyncio.sleep(1.0)

            return {
                "status": "timeout",
                "detail": (
                    "No session detected before timeout. Complete the Google/Piano "
                    "login in the opened browser window, then call login again. "
                    "If running headless, set HINDU_EPAPER_HEADLESS=0 so the window "
                    "is visible."
                ),
            }
        finally:
            await page.close()

    async def _try_google_credentials(self, page, email: str, password: str) -> None:
        """Best-effort scripted Google sign-in. Failures are non-fatal.

        Google frequently blocks automated entry; when it does, we simply leave
        the browser on the login page for the user to finish by hand.
        """
        try:
            # The Piano modal exposes a "Sign in with Google" button; its exact
            # markup changes, so match loosely and open the Google popup/page.
            selectors = [
                "text=Sign in with Google",
                "text=Continue with Google",
                "[aria-label*='Google']",
                "iframe[title*='Sign in with Google']",
            ]
            async with page.context.expect_page(timeout=8000) as popup_info:
                for sel in selectors:
                    try:
                        await page.click(sel, timeout=2000)
                        break
                    except PWTimeout:
                        continue
            google = await popup_info.value
            await google.fill("input[type=email]", email, timeout=8000)
            await google.click("#identifierNext, button:has-text('Next')", timeout=5000)
            await google.fill("input[type=password]", password, timeout=10000)
            await google.click("#passwordNext, button:has-text('Next')", timeout=5000)
        except Exception:
            # Fall through to interactive completion.
            return

    async def close(self) -> None:
        async with self._lock:
            if self._ctx is not None:
                await self._ctx.close()
                self._ctx = None
            if self._pw is not None:
                await self._pw.stop()
                self._pw = None


# Module-level singleton so login and fetch share one user-data directory
# (a persistent context cannot be opened twice concurrently).
_session: BrowserSession | None = None


def get_session() -> BrowserSession:
    global _session
    if _session is None:
        _session = BrowserSession()
    return _session
