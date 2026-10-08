"""MCP (v2 SDK) server exposing The Hindu ePaper to MCP clients.

Tools
-----
login                  Establish/refresh the Google + Piano session (browser).
auth_status            Report whether a valid subscription session exists.
list_editions          List the city editions published on a date.
list_pages             List the pages of an edition's issue.
list_articles          List article headlines in an edition (optionally one page).
read_article           Fetch an article's text.
download_page          Download one page as a PDF (requires subscription).
download_edition       Download the whole edition as a single combined PDF.

Dates default to *today* in India Standard Time. Editions are matched loosely,
so "chennai" resolves to "th_chennai".
"""
from __future__ import annotations

import io
import json
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from pypdf import PdfReader, PdfWriter

from . import config
from .auth import get_session
from . import hindu_client as hc

# Tools are collected here and registered on a server built by build_server(),
# so the same set runs over stdio (local) or HTTP with OAuth (remote).
TOOLS: list = []

# Set in remote mode: the public base URL, and a callback that turns a file in
# the downloads directory into a signed, time-limited download link.
REMOTE: dict = {"public_url": None, "file_link": None}


def tool(fn):
    TOOLS.append(fn)
    return fn


def _login_hint() -> str:
    if REMOTE["public_url"]:
        return f"Open {REMOTE['public_url']}/login on your phone to sign in to The Hindu."
    return "Run the `login` tool."


def _out_dir(out_dir: str | None) -> Path:
    # A remote caller must not choose paths on the server.
    if out_dir and not REMOTE["public_url"]:
        return Path(out_dir)
    return config.downloads_dir()


def _file_result(result: dict, path: Path) -> str:
    if REMOTE["file_link"]:
        result["download_url"] = REMOTE["file_link"](path)
        result["detail"] = "Open download_url to get the PDF. The link expires in 24 hours."
    else:
        result["path"] = str(path)
    return json.dumps(result, indent=2)


def _date(date: str | None) -> str:
    return date or config.today_ist()


def _safe_name(edition_id: str, date: str, suffix: str) -> str:
    return f"{edition_id}_{date}{suffix}"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
@tool
async def login(
    email: str | None = None,
    password: str | None = None,
    timeout_seconds: int = 240,
) -> str:
    """Log in to The Hindu ePaper and persist the session for future runs.

    Opens a browser to the ePaper login page. If ``email`` and ``password`` are
    given, attempts the Google sign-in automatically; Google often blocks
    scripted entry, so in practice you finish the login in the opened window.
    The session is saved and reused automatically afterwards — you only need to
    do this again when it expires.

    Set HINDU_EPAPER_HEADLESS=0 (the default for login) so the window is
    visible and you can complete Google's flow.
    """
    if REMOTE["public_url"]:
        return json.dumps({"status": "open_link", "detail": _login_hint()}, indent=2)
    result = await get_session().login(
        email=email, password=password, timeout_seconds=timeout_seconds
    )
    return json.dumps(result, indent=2)


@tool
async def auth_status() -> str:
    """Report whether a valid subscription session currently exists."""
    ok = await get_session().is_logged_in()
    return json.dumps(
        {
            "logged_in": ok,
            "detail": "Subscription session active."
            if ok
            else f"No session. {_login_hint()}",
        },
        indent=2,
    )


# ---------------------------------------------------------------------------
# Browsing (public)
# ---------------------------------------------------------------------------
@tool
async def list_editions(date: str | None = None) -> str:
    """List the city editions of The Hindu published on a date (default: today IST)."""
    d = _date(date)
    editions = await hc.list_editions(d)
    summary = [
        {
            "edition_id": e["edition_id"],
            "title": e["title"],
            "page_count": e["page_count"],
        }
        for e in editions
    ]
    return json.dumps({"date": d, "editions": summary}, indent=2)


@tool
async def list_pages(edition: str, date: str | None = None) -> str:
    """List the pages of an edition's issue (page number, name, section)."""
    d = _date(date)
    e = await hc.resolve_edition(edition, d)
    structure = await hc.get_structure(e["edition_id"], e["issue_id"])
    pages = hc.parse_pages(structure)
    return json.dumps(
        {"edition": e["edition_id"], "date": d, "page_count": len(pages), "pages": pages},
        indent=2,
    )


@tool
async def list_articles(edition: str, date: str | None = None, page: str | None = None) -> str:
    """List article headlines in an edition, optionally restricted to one page."""
    d = _date(date)
    e = await hc.resolve_edition(edition, d)
    structure = await hc.get_structure(e["edition_id"], e["issue_id"])
    articles = hc.parse_articles(structure, page=page)
    return json.dumps(
        {"edition": e["edition_id"], "date": d, "count": len(articles), "articles": articles},
        indent=2,
    )


@tool
async def read_article(edition: str, article_id: str, date: str | None = None) -> str:
    """Fetch the plain text of an article by its id (see list_articles)."""
    d = _date(date)
    e = await hc.resolve_edition(edition, d)
    structure = await hc.get_structure(e["edition_id"], e["issue_id"])
    articles = hc.parse_articles(structure)
    match = next((a for a in articles if a["article_id"] == article_id), None)
    if match is None:
        return json.dumps({"error": f"Article '{article_id}' not found in {e['edition_id']} {d}."})
    if not match["html_reference"]:
        return json.dumps({"headline": match["headline"], "text": match["teaser"]})
    text = await hc.get_article_text(e["edition_id"], e["issue_id"], match["html_reference"])
    return json.dumps(
        {
            "article_id": article_id,
            "page": match["page"],
            "headline": match["headline"],
            "byline": match["byline"],
            "text": text,
        },
        indent=2,
    )


# ---------------------------------------------------------------------------
# Downloads (subscription-gated)
# ---------------------------------------------------------------------------
@tool
async def download_page(
    edition: str, page: str, date: str | None = None, out_dir: str | None = None
) -> str:
    """Download a single newspaper page as a PDF. Requires an active session."""
    d = _date(date)
    e = await hc.resolve_edition(edition, d)
    structure = await hc.get_structure(e["edition_id"], e["issue_id"])
    pages = hc.parse_pages(structure)
    target = next((p for p in pages if str(p["page"]) == str(page)), None)
    if target is None or not target["pdf_reference"]:
        return json.dumps({"error": f"Page {page} not found in {e['edition_id']} {d}."})

    try:
        data = await hc.download_page_pdf(e["edition_id"], e["issue_id"], target["pdf_reference"])
    except hc.NotAuthenticatedError:
        return json.dumps({"error": f"Not signed in to The Hindu. {_login_hint()}"})
    out = _out_dir(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / _safe_name(e["edition_id"], d, f"_p{page}.pdf")
    path.write_bytes(data)
    return _file_result({"edition": e["edition_id"], "date": d, "page": page, "bytes": len(data)}, path)


@tool
async def download_edition(
    edition: str, date: str | None = None, out_dir: str | None = None
) -> str:
    """Download the full edition for a date as one combined PDF.

    Requires an active subscription session (run `login` first). Fetches every
    page's PDF and merges them in order.
    """
    d = _date(date)
    e = await hc.resolve_edition(edition, d)
    structure = await hc.get_structure(e["edition_id"], e["issue_id"])
    pages = [p for p in hc.parse_pages(structure) if p["pdf_reference"]]
    if not pages:
        return json.dumps({"error": f"No page PDFs found for {e['edition_id']} {d}."})

    writer = PdfWriter()
    fetched = 0
    for p in pages:
        try:
            data = await hc.download_page_pdf(e["edition_id"], e["issue_id"], p["pdf_reference"])
        except hc.NotAuthenticatedError:
            return json.dumps({"error": f"Not signed in to The Hindu. {_login_hint()}"})
        reader = PdfReader(io.BytesIO(data))
        for pg in reader.pages:
            writer.add_page(pg)
        fetched += 1

    out = _out_dir(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / _safe_name(e["edition_id"], d, ".pdf")
    with open(path, "wb") as fh:
        writer.write(fh)
    return _file_result({"edition": e["edition_id"], "date": d, "pages": fetched}, path)


def build_server(**kwargs) -> MCPServer:
    """Create an MCPServer with every tool registered."""
    server = MCPServer(
        "hindu-epaper",
        version="0.2.0",
        instructions=(
            "Reads the user's The Hindu ePaper subscription. Dates default to today (IST). "
            "Use list_editions, then list_articles/read_article for text or download_edition for the PDF."
        ),
        **kwargs,
    )
    for fn in TOOLS:
        server.tool()(fn)
    return server


def main() -> None:
    """Entry point. ``--transport stdio`` (default) or ``--transport http``."""
    import argparse
    import os

    parser = argparse.ArgumentParser(prog="hindu-epaper-mcp")
    parser.add_argument("--transport", choices=["stdio", "http"],
                        default=os.environ.get("HINDU_EPAPER_TRANSPORT", "stdio"))
    parser.add_argument("--host", default=os.environ.get("HINDU_EPAPER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("HINDU_EPAPER_PORT", "8000")))
    parser.add_argument("--public-url", default=os.environ.get("HINDU_EPAPER_PUBLIC_URL"),
                        help="Public HTTPS base URL, e.g. https://hermes.tailXXXX.ts.net")
    args = parser.parse_args()

    if args.transport == "stdio":
        build_server().run()
        return

    from .remote import run_http

    run_http(host=args.host, port=args.port, public_url=args.public_url)


if __name__ == "__main__":
    main()
