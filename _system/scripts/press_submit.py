#!/usr/bin/env python3
"""
press_submit.py — submission dei dossier tipologici (concept design) alle
testate di architettura. Generalizzazione di feature_pitch.py, ma:
  - lista testate esterna (_system/outreach/press_submissions.yml);
  - testo dal dossier _drafts/backlinks/dossier_<typology>.md (varianti 300/500/800);
  - DRY-RUN di default: produce solo bozze in _drafts/backlinks/submissions/;
  - invio reale SOLO con --send, SOLO per testate how=email con contatto
    generico verificato, via send_email.send_raw(kind="submission"), con:
      · kill-switch lead_settings.dry_run_kinds (se contiene 'submission' → mai invio reale)
      · tetto lead_settings.budgets_per_day.submission (default 2) + cadence.max_per_day
      · backoff 30 gg per testata (ledger) · esclusiva Dezeen (exclusive_days)
      · gate CAN-SPAM: brand.postal_address vuoto → invio bloccato (solo bozze)
  - le testate how=form ricevono una bozza "copia/incolla" con l'URL del form.

Come si lancia
  python3 _system/scripts/press_submit.py --list
  python3 _system/scripts/press_submit.py --mode dossier --typology courtyard            # bozze per tutte le testate
  python3 _system/scripts/press_submit.py --typology courtyard --outlet dezeen           # una sola
  python3 _system/scripts/press_submit.py --typology courtyard --outlet dezeen --send    # invio reale (se i gate lo permettono)
  (senza --typology usa il calendario cadence.typology_calendar del mese corrente)

Output: _drafts/backlinks/submissions/<date>-<outlet>-<typology>.md (frontmatter kind: submission),
        righe ledger 'submission_draft' / 'submission_sent', prospects.yml status drafted/sent.
Exit 0 sempre.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "backlinks"))
sys.path.insert(0, str(SCRIPT_DIR))
import backlinklib as bl  # noqa: E402

SUBMISSIONS_YML = bl.SYSTEM_DIR / "outreach" / "press_submissions.yml"
DOSSIER_DIR = bl.DRAFTS_DIR / "backlinks"
OUT_DIR = DOSSIER_DIR / "submissions"
TYPOLOGIES = {"courtyard": "The Courtyard House", "hill": "The Hill House",
              "l": "The L House", "deconstructed": "The Deconstructed House"}
# Mappa outlet id → prospect id (per aggiornare prospects.yml)
PROSPECT_MAP = {"dezeen": "dezeen-submit", "archdaily-unbuilt": "archdaily-unbuilt",
                "designboom": "designboom-readers", "divisare": "divisare-submit",
                "metalocus": "metalocus-bowerbird", "e-architect": "e-architect-submit",
                "archeyes": "archeyes-submit", "amazing-architecture": "amazing-architecture-submit",
                "architizer-profile": "architizer-firm-profile", "archello-profile": "archello-profile"}


def load_outlets() -> tuple:
    import yaml
    d = yaml.safe_load(SUBMISSIONS_YML.read_text(encoding="utf-8")) or {}
    return d.get("outlets", []), d.get("cadence", {})


# ── dossier ────────────────────────────────────────────────────────────
def load_dossier(typology: str) -> dict:
    """Legge dossier_<typology>.md e ritorna {title, variants:{800,500,300}, credits, images}."""
    path = DOSSIER_DIR / f"dossier_{typology}.md"
    if not path.exists():
        raise FileNotFoundError(f"dossier mancante: {path}")
    txt = path.read_text(encoding="utf-8")
    # frontmatter opzionale
    fm = {}
    m = re.match(r"^---\n(.*?)\n---\n", txt, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                fm[k.strip()] = v.strip().strip('"')
        txt = txt[m.end():]
    sections = {}
    cur, buf = None, []
    for line in txt.splitlines():
        h = re.match(r"^##\s+(.*)$", line)
        if h:
            if cur:
                sections[cur] = "\n".join(buf).strip()
            cur, buf = h.group(1).strip().lower(), []
        else:
            buf.append(line)
    if cur:
        sections[cur] = "\n".join(buf).strip()
    variants = {}
    for k, v in sections.items():
        mm = re.match(r"variant\s+(\d+)", k)
        if mm:
            variants[int(mm.group(1))] = v
    return {"title": fm.get("title") or TYPOLOGIES.get(typology, typology),
            "typology": typology, "variants": variants,
            "credits": sections.get("credits", ""), "images": sections.get("images", ""),
            "frontmatter": fm}


def pick_variant(dossier: dict, max_words: int) -> str:
    sizes = sorted(dossier["variants"])
    if not sizes:
        return ""
    ok = [s for s in sizes if s <= max_words]
    return dossier["variants"][max(ok) if ok else min(sizes)]


def build_submission(outlet: dict, dossier: dict, settings: dict) -> tuple:
    req = outlet.get("requirements") or {}
    text = pick_variant(dossier, int(req.get("max_words") or 800))
    sig = (settings.get("signatures") or {}).get("prospects") or "the office of Paolo Mezzalama · My Villa\ninfo@myvilla.la · myvilla.la"
    disclaimer = (settings.get("canonical") or {}).get("built_disclaimer") or ""
    subject = f"Concept design submission: {dossier['title']}, an Italian villa in exposed concrete for Los Angeles"
    body = (f"Hello,\n\nWe would like to submit {dossier['title']} for your consideration. "
            f"It is a concept design (unbuilt) by My Villa, an Italian design studio working on "
            f"reinforced-concrete villas for Los Angeles.\n\n{text}\n\n"
            f"Status: concept design, unbuilt. {disclaimer}\n\n"
            f"Credits\n{dossier['credits']}\n\nImages (high-resolution files on request, via download link)\n{dossier['images']}\n\n"
            f"Project page: https://myvilla.la/\n\n"
            f"Would you like the full image set?\n\n{sig.strip()}\n")
    return subject, body


# ── gate e tetti ───────────────────────────────────────────────────────
def _sent_events(days: int = 3650) -> list:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return [e for e in bl.ledger_read() if e.get("event") == "submission_sent" and e.get("ts", "") >= cutoff]


def gates(outlet: dict, outlets: list, cadence: dict, settings: dict) -> Optional[str]:
    """Ritorna il motivo del blocco o None se l'invio reale è ammesso."""
    if outlet.get("how") != "email":
        return "how=form: si invia dal form, non via email"
    if outlet.get("contact_source") != "verified" or not outlet.get("contact") or "@" not in outlet["contact"]:
        return "contatto non verificato (contact_source != verified)"
    if "submission" in (settings.get("dry_run_kinds") or []):
        return "kill-switch: 'submission' è in lead_settings.dry_run_kinds"
    if not (settings.get("brand") or {}).get("postal_address"):
        return "gate CAN-SPAM: brand.postal_address vuoto in lead_settings.yml"
    budget = int((settings.get("budgets_per_day") or {}).get("submission", 2))
    cap = min(budget, int(cadence.get("max_per_day", 2)))
    if len(_sent_events(days=1)) >= cap:
        return f"tetto giornaliero raggiunto ({cap})"
    back = int(cadence.get("backoff_days", 30))
    for e in _sent_events(days=back):
        if e.get("outlet") == outlet["id"]:
            return f"backoff {back} gg: già inviato il {e.get('ts', '')[:10]}"
    # esclusiva: se una testata con exclusive_days>0 ha ricevuto una submission
    # della stessa tipologia negli ultimi N giorni, gli altri aspettano.
    for o in outlets:
        ex = int(o.get("exclusive_days") or 0)
        if ex and o["id"] != outlet["id"]:
            for e in _sent_events(days=ex):
                if e.get("outlet") == o["id"]:
                    return f"esclusiva {o['name']} ({ex} gg) in corso dal {e.get('ts', '')[:10]}"
    return None


# ── main ───────────────────────────────────────────────────────────────
def run(*, typology: Optional[str], outlet_id: Optional[str], send: bool, mode: str = "dossier") -> dict:
    outlets, cadence = load_outlets()
    settings = bl.load_settings()
    if not typology:
        typology = (cadence.get("typology_calendar") or {}).get(bl.today()[:7], "courtyard")
    if typology not in TYPOLOGIES:
        print(f"press_submit: tipologia sconosciuta {typology!r} (attese: {', '.join(TYPOLOGIES)})")
        return {}
    dossier = load_dossier(typology)
    targets = sorted([o for o in outlets if not outlet_id or o["id"] == outlet_id],
                     key=lambda o: o.get("priority", 99))
    print(f"press_submit [{mode}] tipologia={typology} testate={len(targets)} "
          f"{'SEND' if send else 'dry-run (bozze)'}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pdata = bl.load_prospects()
    by_id = {p.get("id"): p for p in pdata.get("prospects", [])}
    out = {"drafted": [], "sent": [], "blocked": []}
    for o in targets:
        subject, body = build_submission(o, dossier, settings)
        why = gates(o, outlets, cadence, settings) if send else None
        to = o.get("contact") if o.get("how") == "email" and o.get("contact_source") == "verified" else ""
        path = OUT_DIR / f"{bl.today()}-{o['id']}-{typology}.md"
        fm = (f"---\nkind: submission\noutlet: {o['id']}\noutlet_name: \"{o['name']}\"\nhow: {o.get('how')}\n"
              f"url: {o.get('url')}\nto: \"{to}\"\ntypology: {typology}\nexclusive_days: {o.get('exclusive_days', 0)}\n"
              f"requirements: \"{(o.get('requirements') or {}).get('images', '')}\"\nstatus: review\n"
              f"created: {bl.today()}\nsender: office\n---\n\n")
        path.write_text(fm + f"Subject: {subject}\n\n{body}", encoding="utf-8")
        bl.ledger_append({"event": "submission_draft", "outlet": o["id"], "typology": typology,
                          "draft": str(path.relative_to(bl.ROOT))})
        pid = PROSPECT_MAP.get(o["id"])
        # needs_contact resta tale: la bozza esiste ma manca il destinatario.
        if pid in by_id and by_id[pid].get("status") == "todo":
            by_id[pid]["status"] = "drafted"
            by_id[pid]["drafted_at"] = bl.today()
        out["drafted"].append(o["id"])
        print(f"  ✎ bozza {path.name}")
        if not send:
            continue
        if why:
            print(f"  ⏸ {o['name']}: {why}")
            out["blocked"].append({"outlet": o["id"], "why": why})
            continue
        try:
            from send_email import send_raw  # type: ignore
            res = send_raw(to=to, subject=subject, body=body, skip_signature=True, kind="submission")
            ok = bool(getattr(res, "ok", False)) and not getattr(res, "dry_run", False)
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {o['name']}: send_email error {type(e).__name__}: {e}")
            ok = False
        if ok:
            bl.ledger_append({"event": "submission_sent", "outlet": o["id"], "typology": typology,
                              "message_id": getattr(res, "message_id", None)})
            if pid in by_id:
                by_id[pid]["status"] = "sent"
                by_id[pid]["sent_at"] = bl.today()
            out["sent"].append(o["id"])
            print(f"  ✓ inviato a {o['name']}")
        else:
            reason = getattr(res, "reason", None) if "res" in dir() else "error"
            out["blocked"].append({"outlet": o["id"], "why": f"send_email: {reason}"})
            print(f"  ✗ {o['name']}: non inviato ({reason})")
    bl.save_prospects(pdata)
    print(f"press_submit: {len(out['drafted'])} bozze, {len(out['sent'])} inviate, {len(out['blocked'])} bloccate")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Submission dossier tipologici alle testate.")
    ap.add_argument("--list", action="store_true", help="elenca le testate e i requisiti")
    ap.add_argument("--mode", default="dossier", choices=["dossier"])
    ap.add_argument("--typology", choices=sorted(TYPOLOGIES))
    ap.add_argument("--outlet", help="id testata (vedi --list)")
    ap.add_argument("--dry-run", action="store_true", default=True, help="default: solo bozze")
    ap.add_argument("--send", action="store_true", help="invio reale (solo email verificate, con tetti e gate)")
    a = ap.parse_args(argv)
    try:
        if a.list:
            outlets, cadence = load_outlets()
            print(f"{len(outlets)} testate · max/day {cadence.get('max_per_day')} · backoff {cadence.get('backoff_days')} gg")
            for o in sorted(outlets, key=lambda o: o.get("priority", 99)):
                req = o.get("requirements") or {}
                c = o.get("contact") or "—"
                print(f"  {o.get('priority', '?'):>2}. {o['id']:22} {o['how']:5} unbuilt={o.get('accepts_unbuilt')} "
                      f"excl={o.get('exclusive_days', 0)}d words<={req.get('max_words')} "
                      f"contact[{o.get('contact_source')}]={c}")
            return 0
        run(typology=a.typology, outlet_id=a.outlet, send=a.send, mode=a.mode)
    except Exception as e:  # noqa: BLE001
        print(f"press_submit: errore non fatale: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
