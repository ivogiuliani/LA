#!/usr/bin/env python3
"""
social_digest.py — email quotidiana a Ivo + Giana con l'ABSTRACT di tutti
i contenuti social del pannello: una replica leggibile di ciò che si vede
in localhost:8787 (proposte da approvare + evergreen + commenti ai post
virali + coda di pubblicazione).

Riusa gli stessi scan di approve.py → resta allineato al pannello.
Nessun LLM (solo formattazione) → funziona anche con Anthropic senza crediti.

Destinatari: ivolo@me.com, giana.osman@its.vision  (NON Paolo — quello è
la digest journal). Inviata una volta al giorno (marker anti-doppione,
cross-rail come la digest journal).

CLI:
  python3 social_digest.py            # invia (se non già inviata oggi)
  python3 social_digest.py --dry-run  # stampa l'abstract senza inviare
  python3 social_digest.py --force    # invia anche se già inviata oggi
"""
from __future__ import annotations

import argparse
import html as _html
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
ROOT_DIR = SYSTEM_DIR.parent
MARKER = SYSTEM_DIR / "outreach" / ".last_social_digest_date"
RECIPIENTS = "ivolo@me.com, giana.osman@its.vision"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _already_sent_today():
    try:
        return MARKER.read_text(encoding="utf-8").strip() == _today()
    except OSError:
        return False


def _mark_sent():
    try:
        MARKER.parent.mkdir(parents=True, exist_ok=True)
        MARKER.write_text(_today(), encoding="utf-8")
    except OSError:
        pass


# ── Raccolta contenuti (stessi scan del pannello) ────────────────────
def collect():
    import approve  # riusa gli scan → replica fedele del pannello
    data = {"social": [], "evergreen": [], "ig_viral": [], "x_viral": [],
            "queue": []}
    try:
        data["social"] = approve.scan_social_drafts() or []
    except Exception:  # noqa: BLE001
        pass
    try:
        data["evergreen"] = approve.scan_evergreen() or []
    except Exception:  # noqa: BLE001
        pass
    try:
        igv = approve.scan_ig_viral() or {}
        data["ig_viral"] = igv.get("opportunities", []) or []
    except Exception:  # noqa: BLE001
        pass
    # Virali X/Reddit con commento pronto: dal radar di oggi
    try:
        f = SYSTEM_DIR / "radar" / "reports" / f"radar_{datetime.now().strftime('%Y-%m-%d')}.json"
        if f.exists():
            v = (json.loads(f.read_text(encoding="utf-8"))
                 .get("viral_opportunities", []) or [])
            data["x_viral"] = [x for x in v
                               if (x.get("viral_reply") or {}).get("body")
                               and not (x.get("viral_reply") or {}).get("skip")]
    except Exception:  # noqa: BLE001
        pass
    # Coda di pubblicazione (approvati in attesa)
    appr = SYSTEM_DIR / "social" / "posts" / "approved"
    if appr.exists():
        for p in sorted(appr.glob("*.md")):
            try:
                raw = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", raw, re.DOTALL)
            fm_txt = (m.group(1) if m else "").lower()
            ch = "IG" if ("channel: ig" in fm_txt or "channel: instagram" in fm_txt) \
                else "X" if ("channel: x" in fm_txt or "channel: twitter" in fm_txt) else "?"
            body = (m.group(2).strip() if m else raw.strip())
            data["queue"].append({"channel": ch, "body": body})
    return data


def _snip(s, n=180):
    s = re.sub(r"\s+", " ", (s or "").strip())
    return s[:n] + ("…" if len(s) > n else "")


def _count(data):
    return (len(data["social"]) + len(data["evergreen"]) +
            len(data["ig_viral"]) + len(data["x_viral"]))


# ── Formattazione email ──────────────────────────────────────────────
def format_email(data):
    today = datetime.now().strftime("%d/%m/%Y")
    n = _count(data)
    subject = f"My Villa · Social da rivedere: {n} contenuti — {today}"

    # Il blocco proposte social (companion journal) si accumula su 7 giorni;
    # il pannello lo mostra capato. Stesso qui: top 12 + nota magazzino.
    SOC_CAP = 12
    soc_total = len(data["social"])
    soc_list = data["social"][:SOC_CAP]
    soc_extra = max(0, soc_total - len(soc_list))

    # ---- plain ----
    P = [f"MY VILLA — Abstract contenuti social del {today}",
         "(replica di ciò che vedi nel pannello di controllo)", ""]

    def _ch(x):
        c = str(x.get("channel", "")).lower()
        return "IG" if c in ("ig", "instagram") else "X" if c in ("x", "twitter") else c.upper()

    if data["social"]:
        P.append(f"== PROPOSTE SOCIAL DA APPROVARE ({soc_total}) ==")
        for x in soc_list:
            P.append(f"  [{_ch(x)}] {x.get('type','')}: {_snip(x.get('body',''))}")
        if soc_extra:
            P.append(f"  … e altri {soc_extra} in magazzino (nel pannello)")
        P.append("")
    if data["evergreen"]:
        P.append(f"== EVERGREEN DAL SITO ({len(data['evergreen'])}) ==")
        for x in data["evergreen"]:
            mt = re.search(r"-evergreen-([a-z_]+)\.md$", x.get("file", ""))
            topic = mt.group(1).replace("_", " ") if mt else "evergreen"
            P.append(f"  [IG · {topic}] {_snip(x.get('body',''))}")
        P.append("")
    if data["x_viral"]:
        P.append(f"== COMMENTI AI POST VIRALI — X ({len(data['x_viral'])}) ==")
        for x in data["x_viral"]:
            vr = x.get("viral_reply") or {}
            P.append(f"  @{x.get('author','?')} ({vr.get('lead_type','?')}): "
                     f"{_snip(x.get('title') or x.get('snippet',''), 90)}")
            P.append(f"     → risposta: {_snip(vr.get('body',''), 160)}")
        P.append("")
    if data["ig_viral"]:
        P.append(f"== COMMENTI AI POST VIRALI — INSTAGRAM ({len(data['ig_viral'])}) ==")
        for o in data["ig_viral"]:
            au = o.get("username") or o.get("author") or "?"
            cm = o.get("comment") or o.get("suggested_reply") or o.get("reply") or ""
            P.append(f"  @{au}: {_snip(o.get('caption') or o.get('text',''), 90)}")
            P.append(f"     → commento: {_snip(cm, 160)}")
        P.append("")
    if data["queue"]:
        P.append(f"== IN CODA DI PUBBLICAZIONE ({len(data['queue'])}) ==")
        for q in data["queue"]:
            P.append(f"  [{q['channel']}] {_snip(q.get('body',''), 120)}")
        P.append("")
    P.append("— L'approvazione e la pubblicazione avvengono dal pannello di "
             "controllo. Questa è una panoramica di sola lettura.")
    plain = "\n".join(P)

    # ---- HTML ----
    def esc(s):
        return _html.escape(str(s or ""))

    def block(title, rows):
        if not rows:
            return ""
        items = "".join(
            f'<li style="margin:0 0 10px;">{r}</li>' for r in rows)
        return (f'<h3 style="font-family:Georgia,serif;color:#3a3a3a;'
                f'border-bottom:1px solid #e6dcc8;padding-bottom:4px;'
                f'margin:22px 0 10px;">{esc(title)}</h3>'
                f'<ul style="list-style:none;padding:0;margin:0;'
                f'font-size:14px;color:#444;line-height:1.5;">{items}</ul>')

    soc_rows = [f'<strong style="color:#8a6d3b;">[{_ch(x)}]</strong> '
                f'{esc(x.get("type",""))} — {esc(_snip(x.get("body","")))}'
                for x in soc_list]
    if soc_extra:
        soc_rows.append(f'<em style="color:#999;">… e altri {soc_extra} in '
                        f'magazzino (nel pannello)</em>')
    eg_rows = []
    for x in data["evergreen"]:
        mt = re.search(r"-evergreen-([a-z_]+)\.md$", x.get("file", ""))
        topic = mt.group(1).replace("_", " ") if mt else "evergreen"
        eg_rows.append(f'<strong style="color:#5c6b4f;">✨ {esc(topic)}</strong> — '
                       f'{esc(_snip(x.get("body","")))}')
    xv_rows = []
    for x in data["x_viral"]:
        vr = x.get("viral_reply") or {}
        xv_rows.append(
            f'<strong>𝕏 @{esc(x.get("author","?"))}</strong> '
            f'<em>({esc(vr.get("lead_type","?"))})</em>: '
            f'{esc(_snip(x.get("title") or x.get("snippet",""),90))}'
            f'<br><span style="color:#1f7a4d;">↳ {esc(_snip(vr.get("body",""),160))}</span>')
    igv_rows = []
    for o in data["ig_viral"]:
        au = o.get("username") or o.get("author") or "?"
        cm = o.get("comment") or o.get("suggested_reply") or o.get("reply") or ""
        igv_rows.append(
            f'<strong>📷 @{esc(au)}</strong>: '
            f'{esc(_snip(o.get("caption") or o.get("text",""),90))}'
            f'<br><span style="color:#1f7a4d;">↳ {esc(_snip(cm,160))}</span>')
    q_rows = [f'<strong>[{esc(q["channel"])}]</strong> {esc(_snip(q.get("body",""),120))}'
              for q in data["queue"]]

    html_body = (
        '<div style="max-width:640px;margin:0 auto;font-family:Helvetica,Arial,'
        'sans-serif;color:#3a3a3a;">'
        f'<h2 style="font-family:Georgia,serif;">My Villa · Social da rivedere</h2>'
        f'<p style="color:#8a8378;font-size:13px;">{esc(today)} — replica di ciò '
        f'che vedi nel pannello di controllo. {n} contenuti.</p>'
        + block(f"📱 Proposte da approvare ({soc_total})", soc_rows)
        + block(f"✨ Evergreen dal sito ({len(data['evergreen'])})", eg_rows)
        + block(f"💬 Commenti ai virali — X ({len(data['x_viral'])})", xv_rows)
        + block(f"💬 Commenti ai virali — Instagram ({len(data['ig_viral'])})", igv_rows)
        + block(f"🚀 In coda di pubblicazione ({len(data['queue'])})", q_rows)
        + '<p style="color:#999;font-size:12px;margin-top:24px;border-top:1px '
        'solid #eee;padding-top:10px;">Approvazione e pubblicazione dal pannello '
        'di controllo. Questa è una panoramica di sola lettura.</p></div>')
    return subject, plain, html_body


def send(dry_run=False, force=False):
    if not force and _already_sent_today():
        print("  [social-digest] già inviata oggi — skip")
        return True
    data = collect()
    if _count(data) == 0 and not force:
        print("  [social-digest] nessun contenuto social oggi — niente email")
        return True
    subject, plain, html_body = format_email(data)
    if dry_run:
        print(f"[dry-run] A: {RECIPIENTS}\nSubject: {subject}\n\n{plain}")
        return True
    try:
        from send_email import send_raw
    except ImportError as e:
        print(f"  [social-digest] send_email non disponibile: {e}")
        return False
    res = send_raw(to=RECIPIENTS, subject=subject, body=plain,
                   html_body=html_body, skip_signature=True,
                   kind="social_digest")
    if getattr(res, "ok", False):
        print(f"  [social-digest] ✓ inviata a {RECIPIENTS} "
              f"({_count(data)} contenuti)")
        _mark_sent()
        return True
    print(f"  [social-digest] ✗ invio fallito: {getattr(res,'error','?')}")
    return False


def main(argv=None):
    p = argparse.ArgumentParser(description="Digest social a Ivo + Giana")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    args = p.parse_args(argv)
    # carica .env per le credenziali Gmail
    try:
        from send_email import _load_dotenv  # type: ignore
        _load_dotenv()
    except Exception:  # noqa: BLE001
        pass
    ok = send(dry_run=args.dry_run, force=args.force)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
