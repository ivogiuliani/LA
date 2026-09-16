#!/usr/bin/env python3
"""
backlinklib.py — helper condiviso del motore backlink di My Villa.

Non si lancia da solo. Viene importato da:
  _system/scripts/backlink_check.py, mention_monitor.py,
  resource_page_audit.py, press_submit.py, journalist_requests.py,
  linkedin_founder_drafts.py

Fornisce:
  - percorsi canonici (ROOT, BACKLINKS_DIR, prospects.yml, ledger.jsonl, ...)
  - load_dotenv()          → carica .env di root senza sovrascrivere l'ambiente
  - load_settings()        → _system/config/lead_settings.yml (fonte unica)
  - load_prospects()/save_prospects()  → prospects.yml (ordine preservato)
  - ledger_append(event)   → una riga JSONL per evento (mai PII)
  - fetch(url)             → GET con UA onesto, timeout 15, ritorna (status, html, final_url)
  - find_myvilla_links(html) → [{href, rel, anchor, nofollow}] verso myvilla.la
  - is_own_or_homonym(domain) → True per i nostri domini e per gli omonimi da escludere
  - claude_text(prompt, system, tier, max_tokens) → testo o None, via
    _system/scripts/llm_client.py (Claude Code headless = abbonamento claude.ai;
    MAI API a consumo). Se il modello non è disponibile ritorna None e la
    pipeline degrada (nessuna bozza), mai crash.
  - slugify(), today(), now_iso()
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone, date
from html import unescape
from pathlib import Path
from typing import Any, Optional

BACKLINKS_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = BACKLINKS_DIR.parent
ROOT = SYSTEM_DIR.parent
SCRIPTS_DIR = SYSTEM_DIR / "scripts"
DRAFTS_DIR = ROOT / "_drafts"

PROSPECTS_YML = BACKLINKS_DIR / "prospects.yml"
LEDGER_JSONL = BACKLINKS_DIR / "ledger.jsonl"
STATUS_JSON = BACKLINKS_DIR / "status.json"
MENTIONS_JSON = BACKLINKS_DIR / "mentions.json"
RESOURCE_PAGES_YML = BACKLINKS_DIR / "resource_pages.yml"
RESOURCE_AUDIT_JSON = BACKLINKS_DIR / "resource_audit.json"
SETTINGS_YML = SYSTEM_DIR / "config" / "lead_settings.yml"

USER_AGENT = ("Mozilla/5.0 (compatible; MyVillaBacklinkBot/1.0; "
              "+https://myvilla.la/; info@myvilla.la)")
TIMEOUT = 15

# Domini nostri (mai prospect) + omonimi da escludere nel mention monitor.
OWN_DOMAINS = {"myvilla.la", "www.myvilla.la", "content.myvilla.la"}
HOMONYM_DOMAINS = {
    "villa.edu", "www.villa.edu", "villaforyou.com", "www.villaforyou.com",
    "myvilla.it", "www.myvilla.it", "myprivatevillas.com",
    "www.myprivatevillas.com", "myvilla.com", "www.myvilla.com",
    "myvillas.com", "my-villa.com", "myvilla.co.uk", "myvilla.gr",
}
ALLOWED_STATUSES = ("todo", "drafted", "sent", "live", "lost",
                    "declined", "needs_contact")

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ── tempo ──────────────────────────────────────────────────────────────
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return date.today().isoformat()


# ── env / settings ─────────────────────────────────────────────────────
def load_dotenv() -> None:
    """Carica ROOT/.env senza sovrascrivere variabili già valorizzate."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if v and not os.environ.get(k):
            os.environ[k] = v


def load_settings() -> dict:
    try:
        import yaml
        return yaml.safe_load(SETTINGS_YML.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        print(f"  [backlinklib] lead_settings.yml non leggibile: {e}")
        return {}


# ── prospects ──────────────────────────────────────────────────────────
def load_prospects() -> dict:
    """Ritorna il dict completo del YAML ({'prospects': [...], ...})."""
    import yaml
    if not PROSPECTS_YML.exists():
        return {"prospects": []}
    data = yaml.safe_load(PROSPECTS_YML.read_text(encoding="utf-8")) or {}
    data.setdefault("prospects", [])
    return data


def save_prospects(data: dict) -> None:
    """Riscrive prospects.yml preservando l'ordine dei campi (yaml sort_keys=False)."""
    import yaml
    header = (
        "# My Villa — Backlink prospects (motore backlink, Fase 2)\n"
        "# Regole: contact SOLO caselle generiche/form verificati; mai nominativi;\n"
        "# contact_source known|verified|to_verify; status todo|drafted|sent|live|lost|declined|needs_contact.\n"
        "# Aggiornato automaticamente da backlink_check.py e mention_monitor.py (campi status/last_checked/notes).\n"
        f"# Ultima riscrittura: {now_iso()}\n\n"
    )
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110)
    PROSPECTS_YML.write_text(header + body, encoding="utf-8")


def prospect_ids(data: dict) -> set:
    return {p.get("id") for p in data.get("prospects", []) if p.get("id")}


# ── ledger ─────────────────────────────────────────────────────────────
def ledger_append(event: dict) -> None:
    """Appende un evento al ledger. MAI inserire email/nomi di persone."""
    event = dict(event)
    event.setdefault("ts", now_iso())
    LEDGER_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def ledger_read() -> list:
    out = []
    if not LEDGER_JSONL.exists():
        return out
    for line in LEDGER_JSONL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ── http ───────────────────────────────────────────────────────────────
def fetch(url: str, *, timeout: int = TIMEOUT, max_bytes: int = 2_500_000):
    """GET semplice. Ritorna (status:int, html:str, final_url:str).
    status 0 = errore di rete. Non solleva mai."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.8,it;q=0.6",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(max_bytes)
            ctype = r.headers.get("Content-Type", "")
            charset = "utf-8"
            m = re.search(r"charset=([\w-]+)", ctype)
            if m:
                charset = m.group(1)
            try:
                html = raw.decode(charset, errors="replace")
            except LookupError:
                html = raw.decode("utf-8", errors="replace")
            return r.status, html, r.geturl()
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200_000).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            body = ""
        return e.code, body, url
    except Exception as e:  # noqa: BLE001
        return 0, f"__error__:{type(e).__name__}:{e}", url


_A_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
_ATTR_RE = re.compile(r"""([a-zA-Z_:-]+)\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))""")
_TAG_RE = re.compile(r"<[^>]+>")


def _attrs(s: str) -> dict:
    out = {}
    for m in _ATTR_RE.finditer(s):
        out[m.group(1).lower()] = m.group(3) or m.group(4) or m.group(5) or ""
    return out


def find_myvilla_links(html: str, host: str = "myvilla.la") -> list:
    """Tutti gli <a href> verso `host` (qualsiasi sottodominio/percorso)."""
    links = []
    for m in _A_RE.finditer(html or ""):
        attrs = _attrs(m.group(1))
        href = unescape(attrs.get("href", "")).strip()
        if not href:
            continue
        low = href.lower()
        if host not in low:
            continue
        if not (low.startswith("http") or low.startswith("//")):
            continue
        rel = (attrs.get("rel") or "").lower()
        anchor = unescape(_TAG_RE.sub("", m.group(2))).strip()
        anchor = re.sub(r"\s+", " ", anchor)
        links.append({
            "href": href,
            "rel": rel or "dofollow",
            "nofollow": any(t in rel.split() for t in ("nofollow", "ugc", "sponsored")),
            "anchor": anchor[:160],
        })
    return links


def extract_external_links(html: str, base_host: str) -> list:
    """Tutti gli href http(s) esterni al dominio della pagina (dedup, ordine)."""
    from urllib.parse import urlparse
    seen, out = set(), []
    base = base_host.lower().lstrip("www.")
    for m in _A_RE.finditer(html or ""):
        attrs = _attrs(m.group(1))
        href = unescape(attrs.get("href", "")).strip()
        if not href.lower().startswith(("http://", "https://")):
            continue
        try:
            h = (urlparse(href).hostname or "").lower()
        except ValueError:
            continue
        if not h or h.lstrip("www.") == base:
            continue
        if href in seen:
            continue
        seen.add(href)
        anchor = unescape(_TAG_RE.sub("", m.group(2))).strip()
        out.append({"href": href, "anchor": re.sub(r"\s+", " ", anchor)[:120]})
    return out


def domain_of(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def is_own_or_homonym(domain: str) -> bool:
    d = (domain or "").lower()
    if not d:
        return True
    if d in OWN_DOMAINS or d in HOMONYM_DOMAINS:
        return True
    bare = d[4:] if d.startswith("www.") else d
    return bare in OWN_DOMAINS or bare in HOMONYM_DOMAINS


# ── testo ──────────────────────────────────────────────────────────────
def slugify(text: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:maxlen].rstrip("-") or "item"


def html_to_text(html: str) -> str:
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html or "", flags=re.I | re.S)
    html = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", html, flags=re.I)
    txt = unescape(_TAG_RE.sub(" ", html))
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n\s*\n+", "\n\n", txt)
    return txt.strip()


# ── Claude (via llm_client → Claude Code, abbonamento) ─────────────────
def resolve_model(tier: str = "heavy") -> str:
    """model_resolver.resolve(tier) con fallback: mai bloccare la pipeline.
    Mantenuta per compatibilità: claude_text() usa i tier di llm_client."""
    try:
        from model_resolver import resolve  # type: ignore
        return resolve(tier)
    except Exception as e:  # noqa: BLE001
        print(f"  [backlinklib] model_resolver non disponibile ({e}); fallback")
        return {"heavy": "claude-opus-4-8", "writer": "claude-opus-4-8",
                "balanced": "claude-sonnet-4-6", "cheap": "claude-haiku-4-5"}.get(tier, "claude-sonnet-4-6")


def claude_text(prompt: str, *, system: str = "", tier: str = "heavy",
                max_tokens: int = 1200) -> Optional[str]:
    """Chiamata semplice via llm_client.complete (Claude Code, abbonamento).
    None se il modello non è disponibile (LLMUnavailable/LLMRefused) o fallisce:
    chi chiama salta la generazione, la pipeline non si ferma."""
    load_dotenv()
    try:
        from llm_client import complete, LLMUnavailable, LLMRefused  # type: ignore
    except Exception as e:  # noqa: BLE001
        print(f"  [backlinklib] llm_client non importabile ({type(e).__name__}: {e}): skip generazione")
        return None
    try:
        r = complete(prompt, system=system or None, tier=tier, max_tokens=max_tokens)
        return (r.text or "").strip() or None
    except (LLMUnavailable, LLMRefused) as e:
        print(f"  [backlinklib] Claude non disponibile ({type(e).__name__}: {e}): skip generazione")
        return None
    except Exception as e:  # noqa: BLE001
        print(f"  [backlinklib] Claude error: {type(e).__name__}: {e}")
        return None


# ── Journal (blog/*.json) ──────────────────────────────────────────────
def load_journal(limit: Optional[int] = None) -> list:
    """Sidecar JSON degli articoli pubblicati (con HTML gemello), più recenti prima."""
    items = []
    blog = ROOT / "blog"
    for jp in blog.glob("*.json"):
        if not (blog / (jp.stem + ".html")).exists():
            continue
        try:
            d = json.loads(jp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        d["_slug"] = jp.stem
        d["_url"] = f"https://myvilla.la/blog/{jp.stem}.html"
        items.append(d)
    items.sort(key=lambda d: d.get("_date") or "", reverse=True)
    return items[:limit] if limit else items


def key_data_lines(items: list, max_per_article: int = 3) -> list:
    """['<number> — <label> (source: <name>) → <url>', ...] per prompt di Claude."""
    out = []
    for d in items:
        srcs = d.get("sources") or []
        src_name = (srcs[0].get("name") if srcs and isinstance(srcs[0], dict) else "") or ""
        for k in (d.get("key_data") or [])[:max_per_article]:
            num = str(k.get("number", "")).strip()
            lab = str(k.get("label", "")).replace("\n", " ").strip()
            if num and lab:
                out.append(f"{num} — {lab} (source: {src_name or 'see article'}) → {d['_url']}")
    return out
