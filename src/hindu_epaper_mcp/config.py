"""Shared configuration, paths and constants."""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# The Hindu ePaper backend (public CCI "ccidist" distribution service)
# ---------------------------------------------------------------------------
SITE = "https://epaper.thehindu.com"
# Web-services base for the "th" (The Hindu) organization.
WS_BASE = f"{SITE}/ccidist-ws/th"
# The page the reader app serves; logging in here establishes the session.
LOGIN_URL = f"{SITE}/login"
READER_URL = f"{SITE}/reader"

# India Standard Time — the paper is published on an IST calendar.
IST = timezone(timedelta(hours=5, minutes=30))

# A desktop user-agent so the service treats us like the web reader.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def today_ist() -> str:
    """Return today's date in IST as YYYY-MM-DD."""
    return datetime.now(IST).strftime("%Y-%m-%d")


def data_home() -> Path:
    """Base directory for persisted session + downloads.

    Override with HINDU_EPAPER_HOME. Defaults to ~/.hindu-epaper-mcp.
    """
    base = os.environ.get("HINDU_EPAPER_HOME")
    if base:
        return Path(base).expanduser()
    return Path.home() / ".hindu-epaper-mcp"


def session_dir() -> Path:
    """Persistent Playwright user-data directory (holds the login session)."""
    d = data_home() / "user-data-dir"
    d.mkdir(parents=True, exist_ok=True)
    return d


def downloads_dir() -> Path:
    """Default directory for downloaded newspapers."""
    d = Path(os.environ.get("HINDU_EPAPER_DOWNLOADS", str(data_home() / "downloads")))
    d.mkdir(parents=True, exist_ok=True)
    return d


def chromium_executable() -> str | None:
    """Explicit Chromium path, if the environment pins one.

    In some managed environments Playwright's browsers live at a fixed path
    (PLAYWRIGHT_BROWSERS_PATH) and must not be re-downloaded. If the caller
    sets HINDU_EPAPER_CHROMIUM we honor it; otherwise we let Playwright pick.
    """
    return os.environ.get("HINDU_EPAPER_CHROMIUM") or None
