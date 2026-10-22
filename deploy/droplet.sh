#!/usr/bin/env bash
# SnapLocate -> Ubuntu DigitalOcean Droplet, under systemd, port 8000.
#
# Because the backend is stdlib-only there is NOTHING to pip install. This
# script installs git and python3 and nothing else. Keep it that way: if you
# ever find yourself adding a pip line here, the stdlib-only constraint has
# already been broken somewhere upstream.
#
# RUN THIS AS ROOT ON THE DROPLET:
#   ssh root@<droplet-ip>
#   curl -fsSL <raw-url>/deploy/droplet.sh -o droplet.sh
#   GEMMA_API_KEY=xxx REPO_URL=https://github.com/you/snaplocate bash droplet.sh
#
# Or, if you already scp'd the repo to /opt/snaplocate, just:
#   GEMMA_API_KEY=xxx bash deploy/droplet.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/snaplocate}"
SERVICE="${SERVICE:-snaplocate}"
PORT="${PORT:-8000}"
RUN_USER="${RUN_USER:-snaplocate}"
REPO_URL="${REPO_URL:-}"
# The repo SnapLocate searches. Clone the demo target here before the demo.
TARGET_REPO="${TARGET_REPO:-$APP_DIR/target-repo}"
GRAPH_PATH="${GRAPH_PATH:-$TARGET_REPO/graphify-out/graph.json}"

log() { printf '\n== %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root"
[ -n "${GEMMA_API_KEY:-}" ] || die \
  "GEMMA_API_KEY is not set. The vision stage cannot run without it.
   GEMMA_API_KEY=xxx bash $0"

log "packages (git + python3 only -- no pip, by design)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 ufw >/dev/null

PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' \
  || die "python3 is $PYV; SnapLocate needs 3.10+"
log "python3 $PYV ok"

log "code -> $APP_DIR"
if [ -n "$REPO_URL" ]; then
  if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" pull --ff-only
  else
    rm -rf "$APP_DIR"
    git clone --depth 1 "$REPO_URL" "$APP_DIR"
  fi
elif [ -f "$APP_DIR/snaplocate/cli.py" ]; then
  echo "using the code already at $APP_DIR"
elif [ -f "./snaplocate/cli.py" ]; then
  mkdir -p "$APP_DIR"
  cp -r ./. "$APP_DIR"/
else
  die "no code: set REPO_URL=... or run this from inside the repo"
fi
[ -f "$APP_DIR/snaplocate/cli.py" ] || die "$APP_DIR has no snaplocate/cli.py"
[ -f "$APP_DIR/web/static/index.html" ] || die "$APP_DIR/web/static/index.html missing"

log "self-test (must pass before we start anything)"
cd "$APP_DIR"
python3 scripts/selftest.py || die "self-test FAILED -- not deploying a broken build"

log "service user"
id -u "$RUN_USER" >/dev/null 2>&1 || useradd --system --no-create-home \
  --shell /usr/sbin/nologin "$RUN_USER"

log "target repo"
if [ -d "$TARGET_REPO" ]; then
  echo "target repo present: $TARGET_REPO"
else
  echo "NOTE: $TARGET_REPO does not exist yet."
  echo "      Clone the repo you will demo against, at the right commit:"
  echo "        git clone <repo> $TARGET_REPO"
  echo "        git -C $TARGET_REPO checkout <base-commit>"
fi
if [ ! -f "$GRAPH_PATH" ]; then
  echo "NOTE: no graph at $GRAPH_PATH -- SnapLocate will run SEARCH-ONLY."
  echo "      Build it with Graphify, or (insurance) with the fallback:"
  echo "        python3 -m snaplocate.cli mkgraph --repo $TARGET_REPO \\"
  echo "          --out $GRAPH_PATH"
fi

log "secrets -> /etc/snaplocate.env (0600, root only)"
umask 077
cat > /etc/snaplocate.env <<ENVEOF
GEMMA_API_KEY=${GEMMA_API_KEY}
GITHUB_TOKEN=${GITHUB_TOKEN:-}
SNAPLOCATE_MODEL=${SNAPLOCATE_MODEL:-}
ENVEOF
chmod 600 /etc/snaplocate.env
umask 022

log "systemd unit (survives your SSH session closing)"
cat > /etc/systemd/system/${SERVICE}.service <<UNITEOF
[Unit]
Description=SnapLocate
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=/etc/snaplocate.env
ExecStart=/usr/bin/python3 -m snaplocate.cli serve \\
  --host 0.0.0.0 --port ${PORT} \\
  --repo ${TARGET_REPO} --graph ${GRAPH_PATH}
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

# Light hardening. Nothing here needs to write outside its own directory.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
UNITEOF

chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"
systemctl daemon-reload
systemctl enable --now "$SERVICE"
systemctl restart "$SERVICE"

log "firewall: 22 and ${PORT}"
ufw allow 22/tcp  >/dev/null 2>&1 || true
ufw allow "${PORT}"/tcp >/dev/null 2>&1 || true
yes | ufw enable >/dev/null 2>&1 || true
ufw status | sed 's/^/   /'

log "waiting for the service"
IP=$(curl -fsS --max-time 5 http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address 2>/dev/null \
     || hostname -I | awk '{print $1}')
ok=0
for _ in $(seq 1 20); do
  if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    ok=1; break
  fi
  sleep 1
done

if [ "$ok" -ne 1 ]; then
  echo
  echo "SERVICE DID NOT COME UP. Logs:"
  journalctl -u "$SERVICE" -n 40 --no-pager | sed 's/^/   /'
  die "deploy failed"
fi

echo
curl -fsS "http://127.0.0.1:${PORT}/api/health" | sed 's/^/   /'
echo
log "UP:  http://${IP}:${PORT}"
cat <<DONEEOF

   systemctl status ${SERVICE}
   journalctl -u ${SERVICE} -f
   systemctl restart ${SERVICE}

   After changing code:
     git -C ${APP_DIR} pull && systemctl restart ${SERVICE}

   This is plain HTTP on port ${PORT}, no TLS and no auth. Fine for a demo;
   do not leave an API key on a box that is open to the internet longer than
   the event. Destroy the Droplet afterwards.
DONEEOF
