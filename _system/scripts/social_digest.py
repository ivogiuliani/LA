#!/usr/bin/env python3
"""
social_digest.py — email quotidiana a Ivo + Giana con l'ABSTRACT di tutti
i contenuti social del pannello: una replica leggibile e REALISTICA di ciò
che si vede in localhost:8787 (proposte da approvare + evergreen + commenti
ai post virali + coda di pubblicazione).

Ogni post è renderizzato come una "card" il più fedele possibile all'anteprima
del pannello: IMMAGINE inline (allegata via CID — si vede SEMPRE, anche se il
deploy su myvilla.la è in ritardo), caption integrale, hashtag, e — per i
commenti ai virali — autore, metriche di engagement, qualifica del lead
(buyer/partner/brand) col perché, link al post e la risposta pronta.

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
import tempfile
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


# ── Immagini inline (CID) ────────────────────────────────────────────
class ImageRegistry:
    """Normalizza le immagini dei post per l'email: risolve il path locale
    (l'immagine esiste SEMPRE su disco, a prescindere dal deploy), converte
    in JPEG ridimensionato (compatibile con ogni client + email leggera) e
    le registra come allegati inline CID. dedup per file sorgente."""

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.cache: dict[str, str] = {}   # src path → cid
        self.images: dict[str, Path] = {}  # cid → jpeg path
        self._n = 0

    def register(self, web_path: str) -> str:
        """web_path: '/img/x.webp' o '/blog/assets/...'. → cid o '' se assente."""
        if not web_path:
            return ""
        if str(web_path).startswith(("http://", "https://")):
            return ""  # remoto: gestito a parte (src diretto)
        src = (ROOT_DIR / str(web_path).lstrip("/")).resolve()
        if not src.exists():
            return ""
        key = str(src)
        if key in self.cache:
            return self.cache[key]
        try:
            from PIL import Image
            im = Image.open(src)
            if im.mode != "RGB":
                im = im.convert("RGB")
            im.thumbnail((560, 560))
            cid = f"img{self._n}"
            out = self.out_dir / f"{cid}.jpg"
            im.save(out, "JPEG", quality=82, optimize=True)
        except Exception:  # noqa: BLE001 — immagine illeggibile: niente foto
            return ""
        self._n += 1
        self.cache[key] = cid
        self.images[cid] = out
        return cid


# ── Raccolta contenuti (stessi scan del pannello) ────────────────────
def _queue_image(frontmatter: dict) -> str:
    """Anteprima locale per un post in coda (riusa la logica del pannello)."""
    try:
        import approve
        return approve._social_image_preview(frontmatter, local=True)
    except Exception:  # noqa: BLE001
        return ""


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
            fm = {}
            if m:
                for line in m.group(1).splitlines():
                    kv = line.split(":", 1)
                    if len(kv) == 2:
                        fm[kv[0].strip()] = kv[1].strip().strip('"').strip("'")
            fm_txt = (m.group(1) if m else "").lower()
            ch = "IG" if ("channel: ig" in fm_txt or "channel: instagram" in fm_txt) \
                else "X" if ("channel: x" in fm_txt or "channel: twitter" in fm_txt) else "?"
            body = (m.group(2).strip() if m else raw.strip())
            data["queue"].append({"channel": ch, "body": body,
                                  "image_url": _queue_image(fm)})
    return data


def _count(data):
    return (len(data["social"]) + len(data["evergreen"]) +
            len(data["ig_viral"]) + len(data["x_viral"]))


# ── Helper testo ─────────────────────────────────────────────────────
def _snip(s, n=180):
    s = re.sub(r"\s+", " ", (s or "").strip())
    return s[:n] + ("…" if len(s) > n else "")


def _ch(x):
    c = str(x.get("channel", "")).lower()
    return "IG" if c in ("ig", "instagram") else "X" if c in ("x", "twitter") \
        else "Reddit" if c.startswith("reddit") else (c.upper() or "?")


def _clean_viral_text(s):
    """Il radar impacchetta i tweet come 'Author: @x | Text: …' — estrai il
    testo reale per una resa pulita."""
    s = str(s or "")
    m = re.search(r"Text:\s*(.*)", s, re.DOTALL)
    if m:
        s = m.group(1)
    return re.sub(r"\s+", " ", s).strip()


def _fmt_n(v):
    try:
        return f"{int(v):,}".replace(",", ".")
    except (TypeError, ValueError):
        return str(v or 0)


def _topic_of(x):
    mt = re.search(r"-evergreen-([a-z_]+)\.md$", x.get("file", ""))
    return mt.group(1).replace("_", " ") if mt else "evergreen"


# ── Formattazione email ──────────────────────────────────────────────
def format_email(data, reg: ImageRegistry):
    today = datetime.now().strftime("%d/%m/%Y")
    n = _count(data)
    subject = f"My Villa · Social da rivedere: {n} contenuti — {today}"

    # Le proposte journal si accumulano su 7 giorni; il pannello le cappa.
    SOC_CAP = 12
    soc_total = len(data["social"])
    soc_list = data["social"][:SOC_CAP]
    soc_extra = max(0, soc_total - len(soc_list))

    def esc(s):
        return _html.escape(str(s or ""))

    def hl(esc_body):  # evidenzia gli hashtag dentro una caption già escaped
        return re.sub(r"(#\w+)", r'<span style="color:#3b6ea5;">\1</span>',
                      esc_body)

    # ---- PLAIN ----
    P = [f"MY VILLA — Abstract contenuti social del {today}",
         "(replica di ciò che vedi nel pannello di controllo)", ""]

    if data["social"]:
        P.append(f"== PROPOSTE SOCIAL DA APPROVARE ({soc_total}) ==")
        for x in soc_list:
            img = x.get("image_url") or ""
            tag = f"  [img: {Path(img).name}]" if img else ""
            P.append(f"  • [{_ch(x)}] {x.get('type','')}{tag}")
            P.append(f"    {_snip(x.get('body',''), 300)}")
        if soc_extra:
            P.append(f"  … e altri {soc_extra} in magazzino (nel pannello)")
        P.append("")
    if data["evergreen"]:
        P.append(f"== EVERGREEN DAL SITO ({len(data['evergreen'])}) ==")
        for x in data["evergreen"]:
            img = x.get("image_url") or ""
            tag = f"  [img: {Path(img).name}]" if img else ""
            P.append(f"  • [IG · {_topic_of(x)}]{tag}")
            P.append(f"    {_snip(x.get('body',''), 300)}")
        P.append("")
    if data["x_viral"]:
        P.append(f"== COMMENTI AI POST VIRALI — X ({len(data['x_viral'])}) ==")
        for x in data["x_viral"]:
            vr = x.get("viral_reply") or {}
            eng = x.get("engagement") or {}
            P.append(f"  • @{x.get('author','?')}  [{vr.get('lead_type','?')}]"
                     f"  ♥{_fmt_n(eng.get('likes'))} · RT {_fmt_n(eng.get('retweets'))}"
                     f" · {_fmt_n(eng.get('views'))} views")
            P.append(f"    post: {_snip(_clean_viral_text(x.get('snippet') or x.get('title','')), 160)}")
            if vr.get("why"):
                P.append(f"    perché: {_snip(vr.get('why'), 120)}")
            P.append(f"    → risposta pronta: {_snip(vr.get('body',''), 220)}")
            if x.get("url"):
                P.append(f"    link: {x.get('url')}")
        P.append("")
    if data["ig_viral"]:
        P.append(f"== COMMENTI AI POST VIRALI — INSTAGRAM ({len(data['ig_viral'])}) ==")
        for o in data["ig_viral"]:
            au = o.get("username") or o.get("author") or "?"
            cm = o.get("comment") or o.get("suggested_reply") or o.get("reply") or ""
            P.append(f"  • @{au}  ♥{_fmt_n(o.get('likes'))} · {_fmt_n(o.get('comments'))} commenti")
            P.append(f"    post: {_snip(o.get('caption') or o.get('text',''), 160)}")
            P.append(f"    → commento pronto: {_snip(cm, 220)}")
            if o.get("url"):
                P.append(f"    link: {o.get('url')}")
        P.append("")
    if data["queue"]:
        P.append(f"== IN CODA DI PUBBLICAZIONE ({len(data['queue'])}) ==")
        for q in data["queue"]:
            P.append(f"  • [{q['channel']}] {_snip(q.get('body',''), 200)}")
        P.append("")
    P.append("— L'approvazione e la pubblicazione avvengono dal pannello di "
             "controllo. Questa è una panoramica di sola lettura.")
    plain = "\n".join(P)

    # ---- HTML (card realistiche) ----
    def section(title):
        return (f'<h3 style="font-family:Georgia,serif;color:#3a3a3a;'
                f'border-bottom:1px solid #e6dcc8;padding-bottom:6px;'
                f'margin:26px 0 12px;font-size:17px;">{esc(title)}</h3>')

    def img_html(cid, ratio="56%"):
        if not cid:
            return ""
        return (f'<img src="cid:{cid}" width="560" alt="" '
                f'style="width:100%;max-width:560px;display:block;border:0;'
                f'margin:0;">')

    def remote_img_html(url):
        if not url:
            return ""
        return (f'<img src="{esc(url)}" width="560" alt="" '
                f'style="width:100%;max-width:560px;display:block;border:0;">')

    CARD = ('border:1px solid #e6dcc8;border-radius:10px;overflow:hidden;'
            'margin:0 0 16px;background:#fff;')
    META = ('font-size:11px;color:#8a6d3b;text-transform:uppercase;'
            'letter-spacing:.05em;margin:0 0 7px;font-weight:bold;')
    CAP = ('font-size:14px;color:#2b2b2b;line-height:1.55;'
           'white-space:pre-wrap;margin:0;')
    FOOT = 'font-size:11px;color:#9a9a9a;margin:9px 0 0;'

    def post_card(channel_label, meta_extra, body, cid, foot):
        photo = img_html(cid)
        if not photo and "X" in channel_label:
            note = ('<div style="background:#f3f0ea;color:#8a8378;font-size:12px;'
                    'padding:8px 14px;border-bottom:1px solid #efe9dd;">'
                    '𝕏 link-card · l\'immagine non viene pubblicata su X</div>')
        else:
            note = ""
        meta = f'[{channel_label}]'
        if meta_extra:
            meta += f' · {esc(meta_extra)}'
        return (f'<div style="{CARD}">{photo}{note}'
                f'<div style="padding:13px 15px;">'
                f'<div style="{META}">{meta}</div>'
                f'<div style="{CAP}">{hl(esc(body))}</div>'
                + (f'<div style="{FOOT}">{foot}</div>' if foot else "")
                + '</div></div>')

    H = []

    # Proposte social
    if data["social"]:
        H.append(section(f"📱 Proposte da approvare ({soc_total})"))
        for x in soc_list:
            cid = reg.register(x.get("image_url", ""))
            foot = f"{esc(x.get('char_count',''))} caratteri"
            if x.get("date"):
                foot += f" · {esc(x.get('date'))}"
            if x.get("journal_slug"):
                foot += f" · articolo: {esc(x.get('journal_slug'))}"
            H.append(post_card(_ch(x), x.get("type", ""), x.get("body", ""),
                               cid, foot))
        if soc_extra:
            H.append(f'<p style="font-size:13px;color:#999;margin:0 0 16px;">'
                     f'… e altri {soc_extra} in magazzino (nel pannello).</p>')

    # Evergreen
    if data["evergreen"]:
        H.append(section(f"✨ Evergreen dal sito ({len(data['evergreen'])})"))
        for x in data["evergreen"]:
            cid = reg.register(x.get("image_url", ""))
            foot = f"{esc(x.get('char_count',''))} caratteri"
            H.append(post_card(f"IG · {_topic_of(x)}", "brand-awareness",
                               x.get("body", ""), cid, foot))

    # Virali X
    if data["x_viral"]:
        H.append(section(f"💬 Commenti ai post virali — X ({len(data['x_viral'])})"))
        for x in data["x_viral"]:
            vr = x.get("viral_reply") or {}
            eng = x.get("engagement") or {}
            metrics = (f'♥ {_fmt_n(eng.get("likes"))} &nbsp; 🔁 {_fmt_n(eng.get("retweets"))}'
                       f' &nbsp; 💬 {_fmt_n(eng.get("replies"))} &nbsp; 👁 {_fmt_n(eng.get("views"))}')
            lead = esc(vr.get("lead_type", "?"))
            why = (f'<div style="font-size:12px;color:#7a7468;margin:7px 0 0;">'
                   f'<strong>Perché:</strong> {esc(vr.get("why",""))}</div>'
                   if vr.get("why") else "")
            link = (f'<a href="{esc(x.get("url"))}" style="color:#3b6ea5;'
                    f'font-size:12px;text-decoration:none;">apri il post su X ↗</a>'
                    if x.get("url") else "")
            reply = (f'<div style="background:#eef7f0;border-left:3px solid #1f7a4d;'
                     f'padding:9px 12px;margin:10px 0 0;border-radius:0 6px 6px 0;">'
                     f'<div style="font-size:11px;color:#1f7a4d;text-transform:uppercase;'
                     f'letter-spacing:.04em;font-weight:bold;margin-bottom:3px;">'
                     f'Risposta pronta ({esc(vr.get("char_count",""))} car · {esc(vr.get("tone",""))})</div>'
                     f'<div style="font-size:14px;color:#234;line-height:1.5;">'
                     f'{esc(vr.get("body",""))}</div></div>')
            H.append(
                f'<div style="{CARD}padding:13px 15px;">'
                f'<div style="font-size:14px;color:#2b2b2b;margin:0 0 4px;">'
                f'<strong>𝕏 @{esc(x.get("author","?"))}</strong> '
                f'<span style="background:#8a6d3b;color:#fff;font-size:10px;'
                f'padding:2px 7px;border-radius:9px;text-transform:uppercase;'
                f'letter-spacing:.04em;">{lead}</span></div>'
                f'<div style="font-size:11px;color:#9a9a9a;margin:0 0 8px;">{metrics}</div>'
                f'<div style="font-size:13px;color:#444;line-height:1.5;">'
                f'{esc(_clean_viral_text(x.get("snippet") or x.get("title","")))}</div>'
                f'{why}{reply}'
                f'<div style="margin-top:9px;">{link}</div></div>')

    # Virali Instagram
    if data["ig_viral"]:
        H.append(section(f"💬 Commenti ai post virali — Instagram ({len(data['ig_viral'])})"))
        for o in data["ig_viral"]:
            au = o.get("username") or o.get("author") or "?"
            cm = o.get("comment") or o.get("suggested_reply") or o.get("reply") or ""
            thumb = (o.get("display_url") or o.get("image") or
                     o.get("thumbnail") or o.get("thumbnail_url") or "")
            photo = remote_img_html(thumb)
            metrics = f'♥ {_fmt_n(o.get("likes"))} &nbsp; 💬 {_fmt_n(o.get("comments"))}'
            link = (f'<a href="{esc(o.get("url"))}" style="color:#3b6ea5;'
                    f'font-size:12px;text-decoration:none;">apri il post su Instagram ↗</a>'
                    if o.get("url") else "")
            reply = (f'<div style="background:#eef7f0;border-left:3px solid #1f7a4d;'
                     f'padding:9px 12px;margin:10px 0 0;border-radius:0 6px 6px 0;">'
                     f'<div style="font-size:11px;color:#1f7a4d;text-transform:uppercase;'
                     f'letter-spacing:.04em;font-weight:bold;margin-bottom:3px;">'
                     f'Commento pronto</div>'
                     f'<div style="font-size:14px;color:#234;line-height:1.5;">'
                     f'{esc(cm)}</div></div>')
            H.append(
                f'<div style="{CARD}">{photo}'
                f'<div style="padding:13px 15px;">'
                f'<div style="font-size:14px;color:#2b2b2b;margin:0 0 4px;">'
                f'<strong>📷 @{esc(au)}</strong></div>'
                f'<div style="font-size:11px;color:#9a9a9a;margin:0 0 8px;">{metrics}</div>'
                f'<div style="font-size:13px;color:#444;line-height:1.5;">'
                f'{esc(_snip(o.get("caption") or o.get("text",""), 220))}</div>'
                f'{reply}<div style="margin-top:9px;">{link}</div></div></div>')

    # Coda di pubblicazione
    if data["queue"]:
        H.append(section(f"🚀 In coda di pubblicazione ({len(data['queue'])})"))
        for q in data["queue"]:
            cid = reg.register(q.get("image_url", ""))
            H.append(post_card(q["channel"], "approvato", q.get("body", ""),
                               cid, ""))

    html_body = (
        '<div style="max-width:600px;margin:0 auto;font-family:Helvetica,Arial,'
        'sans-serif;color:#3a3a3a;padding:4px 8px;">'
        '<h2 style="font-family:Georgia,serif;margin:0 0 4px;">My Villa · Social da rivedere</h2>'
        f'<p style="color:#8a8378;font-size:13px;margin:0 0 4px;">{esc(today)} — '
        f'replica realistica di ciò che vedi nel pannello di controllo. '
        f'{n} contenuti da rivedere.</p>'
        + "".join(H)
        + '<p style="color:#999;font-size:12px;margin-top:26px;border-top:1px '
        'solid #eee;padding-top:12px;">Approvazione e pubblicazione dal pannello '
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

    with tempfile.TemporaryDirectory(prefix="digest_img_") as tmp:
        reg = ImageRegistry(Path(tmp))
        subject, plain, html_body = format_email(data, reg)
        if dry_run:
            print(f"[dry-run] A: {RECIPIENTS}\nSubject: {subject}\n"
                  f"Immagini inline: {len(reg.images)}\n\n{plain}")
            return True
        try:
            from send_email import send_raw
        except ImportError as e:
            print(f"  [social-digest] send_email non disponibile: {e}")
            return False
        res = send_raw(to=RECIPIENTS, subject=subject, body=plain,
                       html_body=html_body, inline_images=reg.images or None,
                       skip_signature=True, kind="social_digest")
    if getattr(res, "ok", False):
        print(f"  [social-digest] ✓ inviata a {RECIPIENTS} "
              f"({_count(data)} contenuti, {len(reg.images)} immagini inline)")
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
