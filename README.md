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

## Two ways to run it

| | **Remote connector** (recommended) | **Local (stdio)** |
| --- | --- | --- |
| Runs on | An always-on Linux box on your Tailscale tailnet | The same machine as your MCP client |
| Use from | Any Claude app, including your phone | That machine only |
| Sign-in | A web page on your phone that streams the server's browser | A browser window that opens on that machine |

## Remote connector (use it from your phone)

Like other Claude connectors: you paste a URL into Claude, Claude sends you to
a sign-in page, and after that the tools just work.

The Hindu doesn't offer OAuth to other apps, so this server runs its own OAuth
server. Its sign-in page shows you a **live view of a real browser running on
your server**, open at The Hindu's site. You sign in with Google there, on the
real site. The server keeps that browser profile, so your Hindu session
survives restarts.

### Install on the server (for example `hermes`)

Requirements: Ubuntu, Python 3.10+, Tailscale signed in to your tailnet.

```bash
git clone -b claude/mcp-v2-hindu-epaper-o1gzm4 https://github.com/naviji/hindu-epaper-mcp.git
cd hindu-epaper-mcp
./deploy/install.sh
```

The script installs Chromium and a virtual display (Xvfb), creates a systemd
service, generates an **owner password**, and publishes the server over HTTPS
with **Tailscale Funnel**. At the end it prints:

```
Connector URL : https://hermes.tailXXXX.ts.net/mcp
Owner password: ...
```

If Funnel isn't enabled for your tailnet yet, the script prints a link to
allow it. Open it, then run `sudo tailscale funnel --bg <port>` with the port the script printed.

> Claude's connectors connect from Anthropic's servers, not from your phone, so
> the server must be reachable on the public internet. Funnel does that
> without opening ports. Every MCP request still needs an OAuth token, and the
> sign-in page needs your owner password.

### Connect Claude

1. In Claude: **Settings → Connectors → Add custom connector**, and paste the
   connector URL.
2. Claude opens the server's sign-in page. Enter your **owner password**.
3. The page shows the live browser. Tap the 👤 icon at the top of the paper
   and sign in with Google. Use the text box under the view to type.
4. When the page says **Signed in to The Hindu ✓**, tap **Allow access**.

When the Hindu session expires, the tools reply with a link to
`https://<server>/login`. Open it on your phone and sign in again; you don't
need to reconnect the connector.

Downloads (`download_page`, `download_edition`) return a **download link**
that works for 24 hours, so you can open the PDF on your phone.

### Security

- The owner password protects the sign-in page, because that page controls a
  browser signed in to your Google account. Five wrong attempts lock it for 15
  minutes. Use a long password and keep it in a password manager.
- OAuth tokens are stored only as SHA-256 hashes in
  `~/.hindu-epaper-mcp/oauth.json`. Access tokens last 1 hour; refresh tokens
  rotate on every use.
- Treat `~/.hindu-epaper-mcp/` as a secret. It holds the browser profile with
  your sessions.

## Local mode (stdio)

The ePaper uses **Piano ID** on top of **Google OAuth**. In local mode the
server drives a browser on your machine:

1. You call the `login` tool. A browser window opens on the ePaper login page.
2. You complete the Google sign-in in that window (first time only).
3. The session is saved in a persistent browser profile and **reused
   automatically** on every later run.

A best-effort *scripted* Google login (passing `email`/`password` to the
`login` tool) is included, but Google deliberately blocks automated password
entry (CAPTCHA, 2FA, "this browser may not be secure"), so the dependable path
is to finish the login by hand in the opened window the first time. Keep
`HINDU_EPAPER_HEADLESS` unset (or `0`) so the window is visible during login.

### Install (local)

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

### Configure your MCP client

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
| `HINDU_EPAPER_BROWSER_PROXY` | unset | Proxy server for the browser, e.g. `http://proxy:3128`. |
| `HINDU_EPAPER_TRANSPORT` | `stdio` | `http` for the remote connector. |
| `HINDU_EPAPER_PUBLIC_URL` | unset | Public HTTPS base URL (remote mode). |
| `HINDU_EPAPER_OWNER_PASSWORD` | unset | Sign-in page password, 12+ characters (remote mode). |
| `HINDU_EPAPER_HOST` / `HINDU_EPAPER_PORT` | `127.0.0.1` / `8000` (installer: `8787`, or the next free port) | Where the HTTP server listens. |

## Notes & limitations

- Issue listings, page structure, article text and preview images are public;
  full-resolution page PDFs require your subscription session.
- Your login session is stored locally under `HINDU_EPAPER_HOME` and is never
  committed (see `.gitignore`). Treat that directory as a secret.
- This is an unofficial client and not affiliated with The Hindu.
