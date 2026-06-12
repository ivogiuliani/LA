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
