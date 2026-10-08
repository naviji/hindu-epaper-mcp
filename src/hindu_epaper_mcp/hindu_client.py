"""Client for The Hindu ePaper ccidist backend.

Public data (issue listings, issue structure, article HTML/text, preview
images) is fetched over plain HTTP. The full-resolution page PDFs are gated
behind the user's subscription, so those are fetched through the authenticated
browser context, which carries the user's own session.
"""
from __future__ import annotations

import html as _html
import re

import httpx

from . import config
from .auth import get_session

_REGISTRATION_RE = re.compile(r"/registration", re.I)


class NotAuthenticatedError(RuntimeError):
    """Raised when a subscription-gated resource is requested without a session."""


def _http() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"},
        timeout=30.0,
        follow_redirects=True,
    )


# ---------------------------------------------------------------------------
# Public endpoints
# ---------------------------------------------------------------------------
async def list_editions(date: str) -> list[dict]:
    """List all city editions published on ``date`` (YYYY-MM-DD)."""
    url = (
        f"{config.WS_BASE}/?json=true&fromDate={date}&toDate={date}"
        "&skipSections=true&os=web&excludePublications=*-*"
    )
    async with _http() as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    editions: list[dict] = []
    for pub in data.get("publications", []):
        web_issues = (pub.get("issues") or {}).get("web") or []
        if not web_issues:
            continue
        issue = web_issues[0]
        editions.append(
            {
                "edition_id": pub.get("id"),
                "title": pub.get("title"),
                "issue_id": issue.get("id"),
                "date": issue.get("title"),
                "page_count": issue.get("pageCount"),
                "cover_image": issue.get("coverImageUri"),
                "reader_url": issue.get("readerUrl"),
                "download_pages_url": issue.get("downloadPagesUrl"),
            }
        )
    return editions


async def resolve_edition(name: str, date: str) -> dict:
    """Find an edition for ``date`` by fuzzy name match (e.g. 'chennai')."""
    editions = await list_editions(date)
    if not editions:
        raise ValueError(f"No editions published for {date}.")
    needle = name.strip().lower()
    for e in editions:
        if needle in (e["edition_id"] or "").lower() or needle in (e["title"] or "").lower():
            return e
    available = ", ".join(e["edition_id"] for e in editions)
    raise ValueError(f"Edition '{name}' not found for {date}. Available: {available}")


def _issue_ops(edition_id: str, issue_id) -> str:
    return f"{config.WS_BASE}/{edition_id}/issues/{issue_id}/OPS"


async def get_structure(edition_id: str, issue_id) -> dict:
    """Fetch and return the raw cciobjects.json structure tree for an issue."""
    url = f"{_issue_ops(edition_id, issue_id)}/cciobjects.json"
    async with _http() as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


def _pdf_reference(page_node: dict) -> str | None:
    for c in page_node.get("content", []):
        if c.get("format") == "application/pdf":
            return c.get("reference")
    return None


def parse_pages(structure: dict) -> list[dict]:
    """Return a list of pages: number, name, section, pdf reference, article count."""
    pages: list[dict] = []
    for page in structure.get("children", []):
        if page.get("kind") != "Page":
            continue
        attrs = page.get("attributes", {})
        articles = [c for c in page.get("children", []) if c.get("kind") == "Article"]
        pages.append(
            {
                "page": attrs.get("Page"),
                "name": attrs.get("Name"),
                "section": attrs.get("SectionName"),
                "group": attrs.get("PageGroup"),
                "pdf_reference": _pdf_reference(page),
                "article_count": len(articles),
            }
        )
    return pages


def parse_articles(structure: dict, page: str | None = None) -> list[dict]:
    """Return article headlines/teasers across the issue, optionally one page."""
    out: list[dict] = []
    for pg in structure.get("children", []):
        if pg.get("kind") != "Page":
            continue
        pattrs = pg.get("attributes", {})
        pno = pattrs.get("Page")
        if page is not None and str(pno) != str(page):
            continue
        for node in pg.get("children", []):
            if node.get("kind") != "Article":
                continue
            attrs = node.get("attributes", {})
            html_ref = next(
                (c.get("reference") for c in node.get("content", []) if c.get("format") == "text/html"),
                None,
            )
            headline = (attrs.get("Headline") or "").strip()
            teaser = (attrs.get("TeaserText") or "").strip()
            if not headline and not teaser:
                continue
            out.append(
                {
                    "article_id": node.get("id"),
                    "page": pno,
                    "headline": headline or teaser,
                    "byline": (attrs.get("Byline") or "").strip(),
                    "teaser": teaser,
                    "html_reference": html_ref,
                }
            )
    return out


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = _html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def get_article_text(edition_id: str, issue_id, html_reference: str) -> str:
    """Fetch an article's HTML and return it as plain text."""
    url = f"{_issue_ops(edition_id, issue_id)}/{html_reference}"
    async with _http() as client:
        resp = await client.get(url, headers={"Accept": "text/html"})
        resp.raise_for_status()
        return _strip_html(resp.text)


# ---------------------------------------------------------------------------
# Subscription-gated endpoints (fetched via the authenticated browser context)
# ---------------------------------------------------------------------------
async def _fetch_authed_bytes(url: str) -> bytes:
    """GET ``url`` through the logged-in browser context; raise if not entitled."""
    ctx = await get_session().context()
    resp = await ctx.request.get(url)
    final_url = resp.url
    if _REGISTRATION_RE.search(final_url) or resp.status == 307:
        raise NotAuthenticatedError(
            "This resource requires your subscription. Run the `login` tool first."
        )
    if not resp.ok:
        raise RuntimeError(f"Fetch failed ({resp.status}) for {url}")
    return await resp.body()


async def download_page_pdf(edition_id: str, issue_id, pdf_reference: str) -> bytes:
    """Download a single page's full-resolution PDF (requires subscription)."""
    url = f"{_issue_ops(edition_id, issue_id)}/{pdf_reference}"
    return await _fetch_authed_bytes(url)
