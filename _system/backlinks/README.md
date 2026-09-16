# _system/backlinks — motore backlink My Villa (Fase 2)

Cartella dati del motore backlink. Nessun file qui è servito dal sito (prefisso `_`).

| File | Cosa contiene | Chi lo scrive |
|---|---|---|
| `prospects.yml` | 40 prospect (id, engine, target_url, contact generico, status…) | mano + `backlink_check.py` (status live/lost, last_checked) + `mention_monitor.py` (nuove menzioni unlinked → `needs_contact`) |
| `ledger.jsonl` | una riga per evento: `check`, `live`, `lost`, `mention`, `submission_draft`, `journo_draft`… MAI PII | tutti gli script (via `backlinklib.ledger_append`) |
| `status.json` | fotografia dell'ultimo `backlink_check` (per prospect + KPI) | `backlink_check.py` |
| `mentions.json` | menzioni trovate da Brave/CSE, dedup per URL, linked/unlinked | `mention_monitor.py` |
| `resource_pages.yml` | 27 pagine risorse .gov/.org/.edu da auditare | mano |
| `resource_audit.json` | esito audit link rotti per pagina | `resource_page_audit.py` |
| `backlinklib.py` | helper condiviso (fetch, parsing link, ledger, Claude, Journal) | — |

Formato riga ledger:
```json
{"ts":"2026-09-16T08:00:00+00:00","event":"check","prospect_id":"buromilan-journal","status_before":"todo","status_after":"todo","http":200,"links_found":0}
```

Regole:
- `contact` solo caselle generiche/form; niente nominativi. Se non verificato: `contact: ""` e `status: needs_contact`.
- Nessuno script qui invia email. `press_submit.py --send` è l'unica via di invio e passa dal policy layer `send_email.py` (kind `submission`, tetto `budgets_per_day.submission`, kill-switch `dry_run_kinds`).
- Il pannello "Link e citazioni" del digest viene da `backlink_check.digest_block()`.
