#!/usr/bin/env python3
"""
linkedin_founder_drafts.py — una bozza di post LinkedIn a settimana per Paolo.

Cosa fa
  Prende gli articoli del Journal degli ultimi 7 giorni (blog/*.json con HTML
  gemello), sceglie un dato (key_data) e genera con Claude (tier "heavy") un
  post in prima persona di 150-220 parole, con il link all'articolo in fondo.
  Salva _drafts/linkedin_founder/<date>.md (frontmatter: status review, article,
  datum). Non pubblica nulla: Paolo copia/incolla dal suo profilo.

Come si lancia
  python3 _system/scripts/linkedin_founder_drafts.py            # bozza della settimana
  python3 _system/scripts/linkedin_founder_drafts.py --days 14  # finestra più ampia
  python3 _system/scripts/linkedin_founder_drafts.py --dry-run  # stampa, non salva
  python3 _system/scripts/linkedin_founder_drafts.py --force    # anche se esiste già una bozza questa settimana
Exit 0 sempre.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "backlinks"))
import backlinklib as bl  # noqa: E402

OUT_DIR = bl.DRAFTS_DIR / "linkedin_founder"

SYSTEM_PROMPT = """You write LinkedIn posts for Paolo Mezzalama, founder of My Villa (myvilla.la): an Italian design team's project for private villas in exposed reinforced concrete in Los Angeles. He is an architect registered in Italy and France (say it that way if you mention his title; never "licensed in California").
Voice: first person, direct, curious, the tone of an architect thinking out loud to peers and future clients. No hype, no emojis, no hashtags spam (at most two hashtags at the very end), no bullet lists.
Rules:
- 150 to 220 words.
- Open with the datum (number + what it measures + source), then what it changes for how houses are designed in LA, then one concrete observation from our work (call our projects "concept designs"; never say we have built or delivered a villa).
- Exactly one link, on the last line, to the article URL provided, formatted as: "Full note on the Journal: <url>".
- Forbidden: bunker, fortress, dream home, protect your family, survive the next fire, fear-based framing. Do not mention prices or internal financial data.
- "IT'S Architecture" only if strictly needed, capital S.
Output only the post text."""


def pick_article(days: int) -> tuple:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    items = [d for d in bl.load_journal() if (d.get("_date") or "") >= cutoff and d.get("key_data")]
    if not items:
        items = [d for d in bl.load_journal(limit=10) if d.get("key_data")]
    # preferisci sezioni commerciali (market/insurance/materials) e dati numerici
    prio = {"insurance": 0, "market": 1, "materials": 2, "concrete_arch": 3, "climate": 4, "permits": 5}
    items.sort(key=lambda d: (prio.get(d.get("_section_id"), 9), d.get("_date") or ""), reverse=False)
    for d in items:
        for k in d["key_data"]:
            num = str(k.get("number", "")).strip()
            if any(ch.isdigit() for ch in num):
                return d, k
    d = items[0]
    return d, d["key_data"][0]


def run(*, days: int = 7, dry_run: bool = False, force: bool = False) -> dict:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    week_start = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    existing = [p for p in OUT_DIR.glob("*.md") if p.stem >= week_start]
    if existing and not force:
        print(f"linkedin_founder_drafts: bozza già presente questa settimana ({existing[0].name}); usa --force")
        return {"skipped": True}
    d, k = pick_article(days)
    src = (d.get("sources") or [{}])[0]
    datum = f"{k.get('number')} — {str(k.get('label', '')).replace(chr(10), ' ')} (source: {src.get('name', 'see article')})"
    prompt = (f"ARTICLE: {d.get('title')}\nURL: {d['_url']}\nDATE: {d.get('_date')}\nSECTION: {d.get('_section_name')}\n"
              f"DATUM TO OPEN WITH: {datum}\n\nOUR PERSPECTIVE (from the article, for context):\n{(d.get('our_perspective') or '')[:1200]}\n\n"
              f"EXCERPT:\n{(d.get('excerpt') or '')[:600]}\n")
    text = bl.claude_text(prompt, system=SYSTEM_PROMPT, tier="heavy", max_tokens=700)
    if not text:
        print("linkedin_founder_drafts: generazione fallita (chiave assente o errore API)")
        return {"ok": False}
    words = len(text.split())
    out = (f"---\nkind: linkedin_founder\nstatus: review\narticle: {d['_url']}\narticle_title: \"{d.get('title', '')}\"\n"
           f"datum: \"{datum}\"\nwords: {words}\ncreated: {bl.today()}\nsender: paolo\n---\n\n{text}\n")
    path = OUT_DIR / f"{bl.today()}.md"
    if dry_run:
        print(out)
        return {"ok": True, "dry_run": True, "words": words}
    path.write_text(out, encoding="utf-8")
    bl.ledger_append({"event": "linkedin_founder_draft", "article": d["_slug"], "words": words,
                      "draft": str(path.relative_to(bl.ROOT))})
    print(f"linkedin_founder_drafts: ✎ {path.relative_to(bl.ROOT)} ({words} parole, articolo {d['_slug']})")
    return {"ok": True, "path": str(path), "words": words}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Bozza settimanale LinkedIn per Paolo.")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    try:
        run(days=a.days, dry_run=a.dry_run, force=a.force)
    except Exception as e:  # noqa: BLE001
        print(f"linkedin_founder_drafts: errore non fatale: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
