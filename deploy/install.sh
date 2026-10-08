#!/usr/bin/env bash
# Install The Hindu ePaper MCP server as a remote connector on an Ubuntu host
# that is on your Tailscale tailnet.
#
# Run from inside a clone of this repository, as the user the service should
# run as (not root):
#
#   ./deploy/install.sh
#
# It installs system packages, a virtualenv, Chromium, a systemd service, and
# exposes the server publicly over HTTPS with Tailscale Funnel. Re-running it
# upgrades the install and keeps your existing password and login session.
set -euo pipefail

PORT="${HINDU_EPAPER_PORT:-8000}"
SERVICE=hindu-epaper-mcp
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$HOME/.config/hindu-epaper-mcp.env"
RUN_USER="$(id -un)"

say() { printf '\n==> %s\n' "$*"; }

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as your normal user (it uses sudo where needed), not as root." >&2
  exit 1
fi
command -v tailscale >/dev/null || { echo "tailscale is not installed on this host." >&2; exit 1; }

# Public HTTPS name of this machine on the tailnet, e.g. hermes.tail33fd5.ts.net
DNS_NAME="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
PUBLIC_URL="https://${DNS_NAME}"

say "Installing system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv xvfb >/dev/null

say "Creating virtualenv and installing the server"
cd "$REPO_DIR"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e .

CHROME_PATH=""
if [ "$(dpkg --print-architecture)" = amd64 ]; then
  # Google Chrome from Google's own package. Google sign-in trusts real Chrome
  # more than Playwright's test build, and dl.google.com is reachable from
  # hosts that can't reach Playwright's CDN.
  say "Installing Google Chrome"
  if ! command -v google-chrome-stable >/dev/null; then
    TMP_DEB="$(mktemp -d)/google-chrome.deb"
    curl -4 -fL --retry 3 --connect-timeout 20 -o "$TMP_DEB" \
      https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    sudo apt-get install -y -qq "$TMP_DEB" >/dev/null
    rm -f "$TMP_DEB"
  fi
  CHROME_PATH="$(command -v google-chrome-stable)"
  echo "Using $CHROME_PATH ($("$CHROME_PATH" --version))"
else
  # Google Chrome has no Linux build for this architecture; use Playwright's.
  say "Installing Chromium and its libraries"
  sudo "$REPO_DIR/.venv/bin/python" -m playwright install-deps chromium >/dev/null
  for attempt in 1 2 3; do
    if NODE_OPTIONS=--dns-result-order=ipv4first PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=300000 \
       .venv/bin/python -m playwright install chromium; then
      break
    fi
    if [ "$attempt" = 3 ]; then
      echo "Chromium download failed 3 times. Check that this host can reach cdn.playwright.dev, then re-run." >&2
      exit 1
    fi
    echo "Download failed; retrying (${attempt}/3)..."
    sleep 5
  done
fi

if [ ! -f "$ENV_FILE" ]; then
  say "Generating your owner password"
  mkdir -p "$(dirname "$ENV_FILE")"
  PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
  umask 077
  cat > "$ENV_FILE" <<EOF
HINDU_EPAPER_OWNER_PASSWORD=${PASSWORD}
HINDU_EPAPER_PUBLIC_URL=${PUBLIC_URL}
HINDU_EPAPER_PORT=${PORT}
EOF
  NEW_PASSWORD=1
else
  NEW_PASSWORD=0
fi

# Point the server at the installed browser (kept in sync on every run).
sed -i '/^HINDU_EPAPER_CHROMIUM=/d' "$ENV_FILE"
if [ -n "$CHROME_PATH" ]; then
  echo "HINDU_EPAPER_CHROMIUM=${CHROME_PATH}" >> "$ENV_FILE"
fi

say "Installing systemd service"
sudo tee /etc/systemd/system/${SERVICE}.service >/dev/null <<EOF
[Unit]
Description=The Hindu ePaper MCP server
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
User=${RUN_USER}
WorkingDirectory=${REPO_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${REPO_DIR}/.venv/bin/hindu-epaper-mcp --transport http --host 127.0.0.1 --port ${PORT}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now ${SERVICE} >/dev/null
sudo systemctl restart ${SERVICE}

say "Exposing it over HTTPS with Tailscale Funnel"
if ! sudo tailscale funnel --bg "$PORT"; then
  echo "Funnel could not be enabled. If it printed a link, open it to allow Funnel for this"
  echo "machine in your tailnet settings, then run: sudo tailscale funnel --bg $PORT"
fi

for _ in $(seq 1 30); do
  curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/login" && break
  sleep 1
done

say "Done"
echo "Connector URL : ${PUBLIC_URL}/mcp"
echo "Sign-in page  : ${PUBLIC_URL}/login"
if [ "$NEW_PASSWORD" = 1 ]; then
  echo "Owner password: ${PASSWORD}"
  echo "(Saved in ${ENV_FILE}. Store it in your password manager.)"
else
  echo "Owner password: unchanged (see ${ENV_FILE})"
fi
echo
echo "In Claude: Settings > Connectors > Add custom connector, paste the connector URL."
echo "Logs: journalctl -u ${SERVICE} -f"
