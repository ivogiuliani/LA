#!/usr/bin/env python3
"""
lead_desk.py — vista "Leads" per il pannello (approve.py) e blocco digest.

    from lead_desk import render_section, digest_block, handle_state_change, is_owner
    render_section()            # HTML della sezione (owner-only) con tabella + azioni
    digest_block()              # HTML "In attesa di te" SENZA PII (per publish_all_drafts)
    handle_state_change(data)   # → (json_dict, http_status) per POST /api/lead-state

Owner-only: la sezione è visibile solo se `MYVILLA_OWNER=1` oppure il
pannello ha già un'autenticazione (PANEL_PASSWORD impostata) e NON è in
modalità condivisa (PANEL_MODE=shared).

CLI:
    python3 lead_desk.py --digest      # stampa il blocco digest (HTML)
    python3 lead_desk.py --text        # riassunto testuale senza PII
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from lead_settings import get as cfg_get, lead_states

OPEN_STATES = ("new", "acked", "founder_replied", "call_booked", "briefing_done")


def is_owner() -> bool:
    if os.environ.get("PANEL_MODE", "full").strip().lower() == "shared":
        return False
    if os.environ.get("MYVILLA_OWNER", "").strip() in ("1", "true", "yes"):
        return True
    return bool(os.environ.get("PANEL_PASSWORD", "").strip())


def _esc(s: Any) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _days_since(lead: dict) -> Optional[float]:
    import lead_ledger
    last = lead_ledger.last_contact_at(lead) or _parse_ts(lead.get("received_at"))
    if not last:
        return None
    return round((datetime.now(timezone.utc) - last).total_seconds() / 86400, 1)


def _hours_since_received(lead: dict) -> Optional[float]:
    t = _parse_ts(lead.get("received_at"))
    return None if not t else (datetime.now(timezone.utc) - t).total_seconds() / 3600


def _load_leads() -> list:
    try:
        import lead_ledger
        return lead_ledger.list_leads()
    except Exception as exc:  # noqa: BLE001
        print(f"  [lead_desk] ledger unavailable: {exc}", file=sys.stderr)
        return []


def stats() -> dict:
    """Conteggi SENZA PII: per stato, per tier, e lead A/B in attesa del founder >SLA (test esclusi)."""
    leads = [l for l in _load_leads() if not l.get("is_test")]
    sla = float(cfg_get("lead.sla_hours_founder", 24) or 24)
    by_state: dict = {s: 0 for s in lead_states()}
    by_tier: dict = {"A": 0, "B": 0, "C": 0, "?": 0}
    waiting: list = []
    for l in leads:
        by_state[l.get("state", "new")] = by_state.get(l.get("state", "new"), 0) + 1
        by_tier[l.get("tier") or "?"] = by_tier.get(l.get("tier") or "?", 0) + 1
        if l.get("state") in ("new", "acked") and (l.get("tier") in ("A", "B")):
            h = _hours_since_received(l)
            if h is not None and h > sla:
                waiting.append({"lead_id": l["lead_id"], "tier": l.get("tier"),
                                "first_name": l.get("first_name", ""),
                                "project_type": l.get("project_type", ""),
                                "site_location": l.get("site_location", ""),
                                "hours": int(h), "state": l.get("state")})
    waiting.sort(key=lambda w: (-{"A": 2, "B": 1}.get(w["tier"], 0), -w["hours"]))
    open_count = sum(by_state.get(s, 0) for s in OPEN_STATES)
    return {"total": len(leads), "open": open_count, "by_state": by_state,
            "by_tier": by_tier, "waiting_founder": waiting, "sla_hours": sla}


# --------------------------------------------------------------------------- #
# Panel section
# --------------------------------------------------------------------------- #

_CSS = """
<style>
.lead-table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
.lead-table th,.lead-table td{padding:6px 8px;border-bottom:1px solid #e6e1d8;text-align:left;vertical-align:top}
.lead-table th{font-weight:600;color:#6b6257;font-size:11px;letter-spacing:.06em;text-transform:uppercase}
.lead-tier{display:inline-block;min-width:20px;text-align:center;border-radius:3px;padding:1px 6px;font-weight:700;color:#fff}
.lead-tier-A{background:#B5563A}.lead-tier-B{background:#C4A265}.lead-tier-C{background:#9a948b}.lead-tier-\\?{background:#ccc;color:#333}
.lead-late{color:#B5563A;font-weight:600}
.lead-msg{color:#555;max-width:380px;white-space:pre-wrap;font-size:12px}
.lead-actions select{font-size:12px;padding:2px 4px}
.lead-actions button{font-size:12px;padding:3px 8px;margin-left:4px;cursor:pointer}
.lead-muted{color:#8a8378;font-size:12px}
.lead-summary{display:flex;gap:14px;flex-wrap:wrap;margin:6px 0 4px;font-size:12px;color:#6b6257}
</style>
"""

_JS = """
<script>
function leadSetState(btn){
  const row = btn.closest('tr'); if(!row) return;
  const id = row.getAttribute('data-lead-id');
  const sel = row.querySelector('select.lead-state');
  const note = row.querySelector('input.lead-note');
  const state = sel ? sel.value : '';
  if(!id || !state) return;
  btn.disabled = true; const orig = btn.textContent; btn.textContent = '…';
  fetch('/api/lead-state', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({lead_id:id, state:state, note: note ? note.value : ''})})
  .then(r => r.json().then(d => ({status:r.status, data:d})))
  .then(({status, data}) => {
    btn.disabled = false; btn.textContent = orig;
    if(status === 200 && data.ok){
      const cell = row.querySelector('td.lead-state-cell'); if(cell) cell.textContent = data.state;
      if (typeof showToast === 'function') showToast('Lead → ' + data.state);
    } else {
      alert('Errore: ' + (data.error || status));
    }
  }).catch(e => { btn.disabled = false; btn.textContent = orig; alert('Errore rete: ' + e); });
}
</script>
"""


def render_section(*, force: bool = False) -> str:
    """HTML della sezione Leads. Stringa vuota se non owner (o nessun lead)."""
    if not force and not is_owner():
        return ""
    leads = _load_leads()
    st = stats()
    states = lead_states()
    rows = ""
    for l in leads:
        if l.get("state") in ("do_not_contact",):
            continue
        tier = l.get("tier") or "?"
        days = _days_since(l)
        late = (l.get("state") in ("new", "acked") and tier in ("A", "B")
                and (_hours_since_received(l) or 0) > st["sla_hours"])
        opts = "".join(
            f'<option value="{_esc(s)}"{" selected" if s == l.get("state") else ""}>{_esc(s)}</option>'
            for s in states)
        contact = f'{_esc(l.get("email",""))}' + (f' · {_esc(l.get("phone"))}' if l.get("phone") else "")
        _at = l.get("attribution") or {}
        _camp = "/".join(str(x) for x in (_at.get("utm_source"), _at.get("utm_medium"), _at.get("utm_campaign")) if x)
        origin = (" · from " + _esc(_camp)) if _camp else ((" · ref " + _esc(str(_at.get("referrer"))[:60])) if _at.get("referrer") else "")
        if l.get("is_test"):
            origin += " · <strong>TEST</strong>"
        rows += f"""
<tr data-lead-id="{_esc(l.get('lead_id'))}">
  <td><span class="lead-tier lead-tier-{_esc(tier)}">{_esc(tier)}</span><div class="lead-muted">{_esc(l.get('score') if l.get('score') is not None else '')}</div></td>
  <td class="lead-state-cell{' lead-late' if late else ''}">{_esc(l.get('state'))}</td>
  <td>{'—' if days is None else _esc(days)}{' ⚠︎' if late else ''}</td>
  <td><strong>{_esc(l.get('first_name'))} {_esc(l.get('last_name'))}</strong><div class="lead-muted">{contact}</div>
      <div class="lead-muted">{_esc(l.get('project_type'))} · {_esc(l.get('timeline'))} · {_esc(l.get('site_location') or '-')}</div>
      <div class="lead-muted">via {_esc(l.get('source'))}{(' · found: ' + _esc(l.get('how_found'))) if l.get('how_found') else ''}{origin} · {_esc((l.get('received_at') or '')[:16])}</div>
      <div class="lead-msg">{_esc((l.get('message') or '')[:600])}</div></td>
  <td>{_esc(l.get('next_action') or '')}{'<div class="lead-muted">needs human</div>' if l.get('needs_human') else ''}</td>
  <td class="lead-actions"><select class="lead-state">{opts}</select>
      <input class="lead-note" placeholder="nota" style="font-size:12px;width:110px;margin-left:4px">
      <button onclick="leadSetState(this)">Save</button>
      <div class="lead-muted" style="margin-top:4px">id {_esc(l.get('lead_id'))}</div></td>
</tr>"""
    if not rows:
        rows = '<tr><td colspan="6" class="lead-muted">Nessun lead nel registro privato.</td></tr>'
    by_state = " · ".join(f"{k} <strong>{v}</strong>" for k, v in st["by_state"].items() if v)
    waiting = st["waiting_founder"]
    warn = ""
    if waiting:
        warn = (f'<div class="lead-late">⚠︎ {len(waiting)} lead A/B senza risposta del founder da più di '
                f'{int(st["sla_hours"])}h</div>')
    return f"""
{_CSS}
<div class="section section-leads" id="leads">
  <h2 class="section-heading">🤝 Leads<span class="section-count">{st['open']}</span></h2>
  <p class="section-subtitle">Registro privato (fuori git): {_esc(st['total'])} lead totali. Il founder risponde personalmente entro {int(st['sla_hours'])}h.</p>
  <div class="lead-summary"><span>{by_state or 'nessuno'}</span><span>tier A <strong>{st['by_tier'].get('A',0)}</strong> · B <strong>{st['by_tier'].get('B',0)}</strong> · C <strong>{st['by_tier'].get('C',0)}</strong></span></div>
  {warn}
  <table class="lead-table">
    <thead><tr><th>Tier</th><th>Stato</th><th>Giorni dall'ultimo contatto</th><th>Lead</th><th>Next action</th><th>Azione</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
{_JS}
"""


def count_pill() -> str:
    if not is_owner():
        return ""
    try:
        st = stats()
    except Exception:  # noqa: BLE001
        return ""
    if not st["open"]:
        return ""
    late = len(st["waiting_founder"])
    cls = "count-pill count-reply" if late else "count-pill"
    return (f'<a class="{cls}" href="#leads" style="text-decoration:none"><strong>{st["open"]}</strong>'
            f'&ensp;🤝 Leads{(" · " + str(late) + " late") if late else ""}</a>')


# --------------------------------------------------------------------------- #
# Digest block (no PII)
# --------------------------------------------------------------------------- #

def digest_block() -> str:
    """HTML per il digest quotidiano: 'In attesa di te' (conteggi + ritardi). Nessuna PII."""
    try:
        st = stats()
    except Exception:  # noqa: BLE001
        return ""
    if not st["total"]:
        return ""
    counts = " · ".join(f"{_esc(k)} <strong>{v}</strong>" for k, v in st["by_state"].items() if v)
    rows = ""
    for w in st["waiting_founder"]:
        rows += (f'<li>Tier <strong>{_esc(w["tier"])}</strong> — {_esc(w["project_type"] or "project")}'
                 f' · {_esc(w["site_location"] or "area n/d")} · in attesa da <strong>{w["hours"]}h</strong>'
                 f' <span style="color:#888">(stato {_esc(w["state"])}, id {_esc(w["lead_id"])})</span></li>')
    waiting_html = (f'<p style="margin:8px 0 4px;color:#B5563A;font-weight:600;">'
                    f'{len(st["waiting_founder"])} lead A/B senza risposta del founder da oltre '
                    f'{int(st["sla_hours"])}h:</p><ul style="margin:0 0 0 18px;padding:0;">{rows}</ul>'
                    if rows else '<p style="margin:8px 0 0;color:#4a7d4a;">Nessun lead A/B in ritardo.</p>')
    return (
        '<tr><td style="padding:12px 32px;">'
        '<div style="background:#F7F3EC;border-left:3px solid #B5563A;padding:12px 16px;'
        'font-family:-apple-system,sans-serif;font-size:14px;line-height:1.5;color:#2b2622;">'
        '<div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#6b6257;margin-bottom:6px;">In attesa di te — Leads</div>'
        f'<div>Aperti: <strong>{st["open"]}</strong> su {st["total"]} · {counts}</div>'
        f'{waiting_html}'
        '<p style="margin:8px 0 0;font-size:12px;color:#888;">Dettagli (con contatti) solo nel pannello: sezione Leads.</p>'
        '</div></td></tr>'
    )


def digest_text() -> str:
    st = stats()
    if not st["total"]:
        return ""
    lines = [f"LEADS — aperti {st['open']} su {st['total']}: " +
             ", ".join(f"{k} {v}" for k, v in st["by_state"].items() if v)]
    for w in st["waiting_founder"]:
        lines.append(f"  ⚠ tier {w['tier']} {w['project_type']} · {w['site_location'] or 'n/d'} "
                     f"in attesa da {w['hours']}h (id {w['lead_id']})")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# POST /api/lead-state
# --------------------------------------------------------------------------- #

def handle_state_change(data: dict) -> tuple:
    if not is_owner():
        return {"ok": False, "error": "Azione riservata al titolare."}, 403
    lead_id = (data.get("lead_id") or "").strip()
    state = (data.get("state") or "").strip()
    note = (data.get("note") or "").strip()
    if not lead_id or not state:
        return {"ok": False, "error": "lead_id e state richiesti"}, 400
    if state not in lead_states():
        return {"ok": False, "error": f"stato sconosciuto: {state}"}, 400
    try:
        import lead_ledger
        lead = lead_ledger.update_state(lead_id, state, note=note, force=bool(data.get("force")))
        if lead is None:
            return {"ok": False, "error": "lead non trovato"}, 404
        if state == "do_not_contact" and lead.get("email"):
            try:
                from send_email import add_do_not_contact
                add_do_not_contact(lead["email"], reason="panel: do_not_contact")
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "lead_id": lead_id, "state": lead["state"]}, 200
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}, 409
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500


def _main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Desk lead: sezione pannello / digest")
    p.add_argument("--digest", action="store_true")
    p.add_argument("--text", action="store_true")
    p.add_argument("--section", action="store_true", help="stampa la sezione HTML (forza owner)")
    a = p.parse_args(argv)
    if a.digest:
        print(digest_block())
    elif a.section:
        print(render_section(force=True))
    else:
        print(digest_text() or "(registro vuoto)")
        print(json.dumps({k: v for k, v in stats().items() if k != "waiting_founder"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
