#!/bin/bash
# ══════════════════════════════════════════════════════════════════
# My Villa — Setup del pannello su VPS (content.myvilla.la)
# Testato su: Ubuntu 22.04/24.04 (Hetzner CX22, DigitalOcean basic)
#
# USO (da root sul VPS appena creato):
#   export GH_PAT="github_pat_..."        # fine-grained, contents:rw sul repo
#   export PANEL_PASSWORD="una-password-seria"
#   bash setup_vps.sh
#
# Poi: DNS A record  content.myvilla.la → IP del VPS
# Caddy ottiene il certificato HTTPS da solo al primo accesso.
# ══════════════════════════════════════════════════════════════════
set -euo pipefail

: "${GH_PAT:?Serve GH_PAT (fine-grained PAT, Contents read/write sul repo ivogiuliani/LA)}"
: "${PANEL_PASSWORD:?Serve PANEL_PASSWORD}"
DOMAIN="${DOMAIN:-content.myvilla.la}"
PANEL_USER="${PANEL_USER:-ivo}"
REPO="https://x-access-token:${GH_PAT}@github.com/ivogiuliani/LA.git"
APP_DIR=/opt/myvilla

echo "── 1/6 Pacchetti ──"
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git curl debian-keyring debian-archive-keyring apt-transport-https
# Caddy (HTTPS automatico + basic auth)
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy.list >/dev/null
sed -i 's|deb |deb [signed-by=/usr/share/keyrings/caddy.gpg] |' /etc/apt/sources.list.d/caddy.list
apt-get update -qq && apt-get install -y -qq caddy

echo "── 2/6 Repo ──"
if [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO" "$APP_DIR"
else
  git -C "$APP_DIR" pull --rebase --autostash
fi
git -C "$APP_DIR" config user.name  "My Villa Panel"
git -C "$APP_DIR" config user.email "info@myvilla.la"
# credenziale per i push del pannello (approve → commit → push)
git -C "$APP_DIR" remote set-url origin "$REPO"

echo "── 3/6 Python deps ──"
python3 -m pip install -q --break-system-packages -r "$APP_DIR/_system/requirements.txt" \
  google-auth google-auth-oauthlib google-api-python-client 2>/dev/null || \
python3 -m pip install -q -r "$APP_DIR/_system/requirements.txt" \
  google-auth google-auth-oauthlib google-api-python-client

echo "── 4/6 Secrets ──"
if [ ! -f "$APP_DIR/.env" ]; then
  echo "⚠ Copia ora il tuo .env in $APP_DIR/.env (scp dal Mac):"
  echo "    scp ~/Code/myvilla-la/.env root@IP:$APP_DIR/.env"
  echo "  e le credenziali Gmail:"
  echo "    scp -r ~/Code/myvilla-la/_system/outreach/credentials root@IP:$APP_DIR/_system/outreach/"
fi
chmod 600 "$APP_DIR/.env" 2>/dev/null || true

echo "── 5/6 Servizio systemd ──"
cat > /etc/systemd/system/myvilla-panel.service << UNIT
[Unit]
Description=My Villa Review Panel
After=network.target
[Service]
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 $APP_DIR/_system/scripts/approve.py --no-browser --port 8787
Restart=always
RestartSec=5
User=root
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now myvilla-panel

echo "── 6/6 Caddy (HTTPS + password) ──"
HASH=$(caddy hash-password --plaintext "$PANEL_PASSWORD")
cat > /etc/caddy/Caddyfile << CADDY
$DOMAIN {
    basic_auth {
        $PANEL_USER $HASH
    }
    reverse_proxy 127.0.0.1:8787
}
CADDY
systemctl reload caddy

echo ""
echo "✅ Fatto. Quando il DNS punta qui:"
echo "   https://$DOMAIN  (utente: $PANEL_USER)"
