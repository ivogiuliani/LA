# Deploy del pannello su content.myvilla.la

Il pannello è un SERVER (approva → commit → API social): GitHub Pages
(statico) non può ospitarlo. Serve una piccola macchina sempre accesa.

## Architettura

    social manager → https://content.myvilla.la (HTTPS + password)
                       └─ VPS ~5€/mese: Caddy → approve.py :8787
                          repo clonato, auto-pull ogni 2 min,
                          push delle azioni → GitHub
    pipeline 8:00   → GitHub Actions (già attiva, indipendente)
    Mac di Ivo      → NON più necessario (resta rail opzionale)

## Passi (15 min totali)

1. **VPS** (lo crea Ivo — è un acquisto): Hetzner CX22 (~4.5€/m) o
   DigitalOcean Basic ($6/m), immagine Ubuntu 24.04. Salvare IP + chiave SSH.
2. **PAT GitHub**: github.com/settings/personal-access-tokens →
   fine-grained, repo `ivogiuliani/LA`, permesso Contents: Read+Write.
3. **Sul VPS**:
   ```bash
   export GH_PAT="github_pat_…"
   export PANEL_PASSWORD="password-seria"   # NON 'ivo': è esposto a internet
   curl -fsSL https://raw.githubusercontent.com/ivogiuliani/LA/main/_system/deploy/setup_vps.sh | bash
   ```
4. **Secrets** dal Mac:
   ```bash
   scp ~/Code/myvilla-la/.env root@IP:/opt/myvilla/.env
   scp -r ~/Code/myvilla-la/_system/outreach/credentials root@IP:/opt/myvilla/_system/outreach/
   systemctl restart myvilla-panel   # sul VPS
   ```
   Poi sul VPS togliere `ANTHROPIC_API_KEY=` da `/opt/myvilla/.env` e
   aggiungere `CLAUDE_CODE_OAUTH_TOKEN=` (vedi "Claude via abbonamento").
5. **DNS** (dove è gestito myvilla.la): record `A` → `content` → IP del VPS.
   HTTPS automatico (Caddy/Let's Encrypt) al primo accesso.

## Note operative

- Le azioni del pannello committano+pushano; la pipeline cloud pusha a
  sua volta: i pattern anti-conflitto (autostash, retry, marker) sono
  già nel codice.
- Aggiornare il pannello sul VPS: `git -C /opt/myvilla pull && systemctl restart myvilla-panel`
  (oppure si automatizza con un webhook in futuro).
- La password è nell'hash del Caddyfile; cambiarla = rilanciare il
  blocco 6 dello script.
- Difesa in profondità: oltre a Caddy, il pannello stesso supporta
  `PANEL_PASSWORD=...` in `.env` (HTTP Basic integrato in approve.py).

## Alternativa a COSTO ZERO (il Mac resta in gioco)

Se 5€/mese non li vogliamo spendere, il pannello può essere esposto
dal Mac di Ivo con un tunnel — nessun VPS, nessun DNS:

```bash
# 1. password del pannello (obbligatoria se esposto!)
echo 'PANEL_PASSWORD=una-password-seria' >> ~/Code/myvilla-la/.env

# 2. tunnel (scegline uno)
brew install cloudflared
cloudflared tunnel --url http://127.0.0.1:8787   # → https://xxx.trycloudflare.com (URL casuale, cambia a ogni avvio)

# oppure Tailscale Funnel: URL STABILE https://<mac>.<tailnet>.ts.net
brew install --cask tailscale   # login una tantum
tailscale funnel 8787
```

Trade-off rispetto al VPS:
| | VPS | Tunnel dal Mac |
|---|---|---|
| Costo | ~5€/mese | 0€ |
| Mac acceso | NO, mai | SÌ, quando la SMM lavora |
| URL | content.myvilla.la | trycloudflare casuale / ts.net stabile |
| Setup | 15 min una tantum | 5 min |

Nota: la pipeline delle 8:00 resta su GitHub Actions in entrambi i
casi — il tunnel serve SOLO per far vedere il pannello alla SMM.
Lo stato viaggia via git (auto-pull nel pannello), quindi le due
soluzioni sono intercambiabili in qualsiasi momento.

## Fase 2 — Lead API, Chat API e intake lead sul VPS (2026-09-16)

Tre servizi in più accanto al pannello. Tutti leggono `/opt/myvilla/.env`
e scrivono i dati personali SOLO in `/opt/myvilla-private` (fuori dal
repo). File pronti in questa cartella: `lead-api.service`,
`chat-api.service`, `lead-intake.service` + `lead-intake.timer`,
`Caddyfile.snippet`.

Comandi da lanciare **da root sul VPS** (Ivo; il coordinatore non ha SSH):

```bash
# 0. codice aggiornato
git -C /opt/myvilla pull

# 1. private dir (registro lead, log chat, suppression list)
mkdir -p /opt/myvilla-private && chmod 700 /opt/myvilla-private
grep -q MYVILLA_PRIVATE_DIR /opt/myvilla/.env || echo 'MYVILLA_PRIVATE_DIR=/opt/myvilla-private' >> /opt/myvilla/.env

# 2. servizi systemd
cp /opt/myvilla/_system/deploy/lead-api.service     /etc/systemd/system/
cp /opt/myvilla/_system/deploy/chat-api.service     /etc/systemd/system/
cp /opt/myvilla/_system/deploy/lead-intake.service  /etc/systemd/system/
cp /opt/myvilla/_system/deploy/lead-intake.timer    /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now lead-api chat-api lead-intake.timer
systemctl status lead-api chat-api lead-intake.timer --no-pager

# 3. Caddy: route API pubbliche senza password (sostituire IVO_HASH con
#    l'hash già presente in /etc/caddy/Caddyfile)
HASH=$(grep -oE '\$2[aby]\$[^ ]+' /etc/caddy/Caddyfile | head -1)
sed "s|IVO_HASH|$HASH|" /opt/myvilla/_system/deploy/Caddyfile.snippet > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy

# 4. verifica
curl -s https://content.myvilla.la/api/healthz          # lead_api
curl -s https://content.myvilla.la/api/chat/healthz     # chat_api
curl -s -X POST https://content.myvilla.la/api/lead -H 'Content-Type: application/json' \
  -d '{"first_name":"Test","email":"info@myvilla.la","message":"self-send test","_gotcha":"x"}'
#   ↑ con _gotcha pieno risponde ok ma NON registra nulla: prova innocua del routing
journalctl -u lead-intake -n 30 --no-pager                # log dell'ultimo run intake
```

Il pannello sul VPS mostra la sezione **Leads** perché `PANEL_PASSWORD` è
impostata (owner-only). Per il Mac: `MYVILLA_OWNER=1` oppure la stessa
password in `.env`.

Aggiornamenti futuri: `git -C /opt/myvilla pull && systemctl restart lead-api chat-api`.
La chat sul sito resta spenta finché `chat.enabled: true` in
`_system/config/lead_settings.yml` (widget a cura del conversion-layer).

## Claude via abbonamento sul VPS (policy 2026-09-16)

**Nessuna Claude API a consumo.** `chat_api.py`, `lead_api.py` (→
`lead_score.py`) e ogni altro script che usa un modello passano da
`_system/scripts/llm_client.py`, che lancia **Claude Code CLI in modalità
headless** (`claude -p`) con l'abbonamento claude.ai (piano Max).
**`chat_api` e `lead_api` NON usano più `ANTHROPIC_API_KEY`**: la riga va
tolta dal `.env` del VPS (se restasse, l'adapter la rimuove comunque
dall'ambiente della CLI, ma è una chiave viva in giro per niente).
Quadro completo e checklist: `_system/docs/fase2/llm-subscription.md`.

Setup una tantum, **da root sul VPS** (Ubuntu 24.04):

```bash
# 1. Node.js 20 LTS (NodeSource) + Claude Code CLI
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs
npm install -g @anthropic-ai/claude-code
which claude && claude --version          # atteso: /usr/bin/claude (o /usr/local/bin/claude)

# 2. token dell'abbonamento: generato SUL MAC da Ivo con `claude setup-token`
#    (richiede il piano; è un token OAuth di lunga durata, ~1 anno). Va
#    custodito SOLO in /opt/myvilla/.env (chmod 600, fuori dal repo).
sed -i '/^ANTHROPIC_API_KEY=/d' /opt/myvilla/.env
echo 'CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...' >> /opt/myvilla/.env
chmod 600 /opt/myvilla/.env

# 3. i service file aggiornati (IS_SANDBOX=1: i servizi girano come root e la
#    CLI rifiuta bypassPermissions da root senza quel flag; serve alle sole
#    chiamate con web search)
git -C /opt/myvilla pull
cp /opt/myvilla/_system/deploy/{lead-api,chat-api,lead-intake}.service /etc/systemd/system/
systemctl daemon-reload
systemctl restart lead-api chat-api

# 4. verifica (cwd neutra + token dal .env, come fanno i servizi)
cd /opt/myvilla && set -a && . ./.env && set +a
python3 _system/scripts/llm_client.py --status      # auth.loggedIn: true, subscriptionType: max
python3 _system/scripts/llm_client.py --prompt "Reply OK" --tier cheap
tail -1 _system/logs/llm_calls.jsonl                # "backend": "claude_code", "billed": 0
```

Note:

- La CLI non deve trovarsi in PATH per forza: l'adapter cerca anche
  `/usr/local/bin/claude`; per un percorso diverso impostare
  `MYVILLA_CLAUDE_BIN=/percorso/claude` nel `.env`.
- La CLI usa `$HOME/.claude` per la propria configurazione: con `User=root`
  è `/root/.claude`. La cwd delle chiamate è `MYVILLA_PRIVATE_DIR/llm-cwd`
  (`/opt/myvilla-private/llm-cwd`, creata da sola), così nessun `CLAUDE.md`
  del repo viene letto.
- Se il token scade o viene revocato, i log dei servizi mostrano
  `LLMUnavailable: Claude Code non autenticata`: la chat risponde con il
  fallback, il lead resta con il tier da regole. Rigenerare con
  `claude setup-token` e aggiornare il `.env` + il secret GitHub.
- Limiti d'uso del piano (finestre di 5 ore): l'adapter ritenta con backoff
  e poi degrada; il conteggio è in `_system/logs/llm_calls.jsonl`
  (gitignored).
- Aggiornare la CLI ogni tanto: `npm update -g @anthropic-ai/claude-code`.

## Pannello come servizio sul Mac (launchd)

Installato il 2026-06-12: `com.myvilla.panel.plist` (template in questa
cartella) → il pannello gira SEMPRE su 127.0.0.1:8787 (parte al login,
si riavvia se crasha). Logs: `_system/logs/panel.log`.
Comandi utili:
```bash
launchctl kickstart -k gui/$UID/com.myvilla.panel   # riavvia
launchctl bootout   gui/$UID/com.myvilla.panel      # ferma
```
Esposizione alla SMM: Tailscale Funnel → 8787 (PANEL_PASSWORD in .env
fa da serratura). Il Mac non deve dormire nelle ore di lavoro della
SMM: Impostazioni di Sistema → Blocco schermo / Batteria → "Impedisci
lo stop automatico quando il display è spento" (su alimentazione).
