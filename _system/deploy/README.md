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
