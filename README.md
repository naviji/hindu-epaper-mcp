# The Hindu ePaper — MCP server

An [MCP](https://modelcontextprotocol.io) server that fetches your daily
newspaper from **The Hindu ePaper** (<https://epaper.thehindu.com/reader>)
using **your own** logged-in subscription.

It lets an MCP client (Claude, etc.) list the city editions published on a
day, browse their pages and article headlines, read article text, and
download pages or a whole edition as PDF.

> This server only accesses content **you are entitled to** through your own
> subscription. It signs in as you via a real browser and reuses that session;
> it does not bypass the paywall.

## How login works

The ePaper authenticates with **Piano ID** on top of **Google OAuth**. There
is no API key to mint, so this server drives a real browser:

1. You call the `login` tool. A browser window opens on the ePaper login page.
2. You complete the Google sign-in in that window (first time only).
3. The session is saved in a persistent browser profile and **reused
   automatically** on every later run — you are not asked again until it
   expires.

A best-effort *scripted* Google login (passing `email`/`password` to the
`login` tool) is included, but Google deliberately blocks automated password
entry (CAPTCHA, 2FA, "this browser may not be secure"), so the dependable path
is to finish the login by hand in the opened window the first time. Keep
`HINDU_EPAPER_HEADLESS` unset (or `0`) so the window is visible during login.

## Install

Requires Python 3.10+.

```bash
cd hindu-epaper-mcp
uv venv && source .venv/bin/activate
uv pip install -e .
# One-time: install the Chromium browser Playwright drives
playwright install chromium
```

If your environment pins a prebuilt Chromium (so `playwright install` should
be skipped), point the server at it with `HINDU_EPAPER_CHROMIUM=/path/to/chromium`.

## Configure your MCP client

Run over stdio. Example client config entry:

```json
{
  "mcpServers": {
    "hindu-epaper": {
      "command": "hindu-epaper-mcp"
    }
  }
}
```

Or run directly: `python -m hindu_epaper_mcp.server`.

## Tools

| Tool | What it does |
| --- | --- |
| `login` | Open a browser and establish/refresh your subscription session. |
| `auth_status` | Report whether a valid session exists. |
| `list_editions` | List city editions published on a date (default: today, IST). |
| `list_pages` | List an edition's pages (number, name, section). |
| `list_articles` | List article headlines in an edition, optionally one page. |
| `read_article` | Fetch an article's text by id. |
| `download_page` | Download one page as a PDF (needs session). |
| `download_edition` | Download the whole edition as one combined PDF (needs session). |

Dates are `YYYY-MM-DD` and default to today in India Standard Time. Edition
names match loosely, so `chennai` resolves to `th_chennai`.

### Typical flow

```
login                                  # once; finish Google sign-in in the window
list_editions                          # see what's available today
list_pages      edition=chennai        # browse the pages
download_edition edition=chennai       # save today's Chennai edition as one PDF
```

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `HINDU_EPAPER_HOME` | `~/.hindu-epaper-mcp` | Base dir for the saved session and downloads. |
| `HINDU_EPAPER_DOWNLOADS` | `<home>/downloads` | Where PDFs are written. |
| `HINDU_EPAPER_HEADLESS` | unset (visible for login) | Set `1` to force headless. |
| `HINDU_EPAPER_CHROMIUM` | unset | Explicit Chromium executable path. |

## Notes & limitations

- Issue listings, page structure, article text and preview images are public;
  full-resolution page PDFs require your subscription session.
- Your login session is stored locally under `HINDU_EPAPER_HOME` and is never
  committed (see `.gitignore`). Treat that directory as a secret.
- This is an unofficial client and not affiliated with The Hindu.
