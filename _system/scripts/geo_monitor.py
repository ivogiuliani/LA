#!/usr/bin/env python3
"""
geo_monitor.py — GEO (Generative Engine Optimization) monitor.

Pone le domande "da cliente" di _system/research/geo/queries.yml a due
motori di risposta AI con ricerca web e misura se My Villa viene citata:

  (a) Claude + WebSearch via llm_client.complete(tier="balanced",
      web_search=True) → Claude Code headless (abbonamento claude.ai,
      MAI API a consumo). La CLI restituisce testo con URL: le fonti
      vengono estratte dal testo con regex (link markdown + URL nudi).
  (b) Gemini via REST (requests) su
      generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash
      con tools [{"google_search": {}}] e GEMINI_API_KEY
      (fallback gemini-2.0-flash; se anche quello fallisce la query è saltata
      con log — la pipeline non si ferma mai).

Per ogni risposta salva: testo, URL citati, cited_myvilla (myvilla.la nel
testo o nelle fonti), mentioned_myvilla (il nome nel testo) e le
affermazioni SBAGLIATE su My Villa (sedi, "built homes", prezzi…) rilevate
con un secondo passaggio Claude cheap + json_schema (solo se il nome compare).

Output:
  _system/research/geo/runs/<date>.json   run completo (una voce per query × motore)
  _system/research/geo/summary.json       citation share per motore + storico
Costo: Claude = 0 (abbonamento; il "cost_reported_usd" della CLI è solo
informativo e NON viene fatturato); Gemini = stima token × listino.

Uso:
  python3 _system/scripts/geo_monitor.py                 # baseline/run completo
  python3 _system/scripts/geo_monitor.py --dry-run       # nessuna chiamata ai modelli
  python3 _system/scripts/geo_monitor.py --engine claude # solo un motore
  python3 _system/scripts/geo_monitor.py --limit 3       # prime 3 query
  python3 _system/scripts/geo_monitor.py --date 2026-09-16
  python3 _system/scripts/geo_monitor.py --engine claude --limit 1 --no-save  # test: non tocca run/summary

Se Claude Code non è disponibile (limite d'uso, CLI, auth) il motore claude e
il fact-check vengono saltati con log: la pipeline non si ferma mai.

Schedulato: .github/workflows/geo-monitor.yml (lunedì 07:00 UTC).
Exit code: sempre 0.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
ROOT_DIR = SYSTEM_DIR.parent
GEO_DIR = SYSTEM_DIR / "research" / "geo"
RUNS_DIR = GEO_DIR / "runs"
QUERIES = GEO_DIR / "queries.yml"
SUMMARY = GEO_DIR / "summary.json"
SETTINGS = SYSTEM_DIR / "config" / "lead_settings.yml"

sys.path.insert(0, str(SCRIPT_DIR))
try:
    from llm_client import complete, complete_json, LLMUnavailable, LLMRefused  # noqa: E402
    _LLM_IMPORT_ERROR: Optional[str] = None
except Exception as _exc:  # noqa: BLE001
    complete = complete_json = None  # type: ignore
    _LLM_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"

    class LLMUnavailable(RuntimeError):  # type: ignore
        pass

    class LLMRefused(RuntimeError):  # type: ignore
        pass

BRAND_RE = re.compile(r"my\s?villa|myvilla\.la", re.I)
DOMAIN_RE = re.compile(r"myvilla\.la", re.I)
_MD_LINK_RE = re.compile(r"\[([^\]]{1,200})\]\((https?://[^\s)]+)\)")
_URL_RE = re.compile(r"https?://[^\s<>()\[\]\"'`]+")

# Listino indicativo Gemini (USD per 1M token; grounding per 1000 ricerche).
# Solo per la STIMA nel log: aggiornare se cambia il pricing.
# Claude passa dall'abbonamento (Claude Code): costo 0, nessun listino.
PRICE = {
    "gemini": (0.30, 2.50),
    "gemini_grounding_per_1k": 35.0,
}

SYSTEM_ANSWER = "Answer as a knowledgeable assistant; cite sources."


# ── .env minimale (stesso approccio di radar.py) ─────────────────────
def load_dotenv() -> None:
    env_file = ROOT_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if v and not os.environ.get(k):
            os.environ[k] = v


def log(msg: str) -> None:
    print(f"[geo] {msg}", flush=True)


def _clean_url(u: str) -> str:
    return u.rstrip(".,;:!?'\"*_")


def extract_urls(text: str) -> List[Dict[str, str]]:
    """URL citati nel testo della CLI: prima i link markdown (con titolo), poi
    gli URL nudi. Dedup, ordine di apparizione."""
    urls: List[Dict[str, str]] = []
    seen = set()

    def _add(url: str, title: str = "") -> None:
        url = _clean_url(url or "")
        if url and url not in seen:
            seen.add(url)
            urls.append({"url": url, "title": (title or "").strip()})

    for m in _MD_LINK_RE.finditer(text or ""):
        _add(m.group(2), m.group(1))
    for m in _URL_RE.finditer(text or ""):
        _add(m.group(0))
    return urls


def canonical_facts() -> str:
    try:
        cfg = yaml.safe_load(open(SETTINGS, encoding="utf-8"))
        c = cfg["canonical"]
        return (
            "CANONICAL FACTS ABOUT MY VILLA (myvilla.la):\n"
            "- My Villa is the Los Angeles practice of IT'S Architecture, an Italian studio with "
            "offices in Rome and Paris; the LA office is opening. It is NOT a general contractor.\n"
            f"- {c['built_disclaimer']}\n"
            f"- Founder: {c['founder_name']}, {c['founder_title']}. {c['architect_of_record_note']}\n"
            f"- Pricing: {c['price']}. Timeline: {c['timeline']}.\n"
            f"- Fire performance claim: {c['fire_rating']}; insurance claim: {c['insurance_claim']}.\n"
            "- Product: custom reinforced concrete luxury villas (double-skin precast, fair-faced "
            "concrete) for Malibu, Beverly Hills and the LA Westside; partners Transsolar, "
            "BURO MILAN, DGU.\n"
        )
    except Exception as e:  # noqa: BLE001
        log(f"lead_settings non leggibile ({e}); fact-check con fatti minimi")
        return ("CANONICAL FACTS: My Villa is the Los Angeles practice of IT'S Architecture "
                "(Rome, Paris). It has not yet delivered a villa. Pricing from $1,500/sq ft.")


# ── Engine A: Claude + WebSearch (Claude Code, abbonamento) ─────────
_CLAUDE_QUESTION_SUFFIX = ("\n\nSearch the web once before answering. In the answer, cite the "
                           "sources you used with their full URLs (markdown links or plain URLs).")


def ask_claude(question: str, model: Optional[str] = None) -> Dict[str, Any]:
    """Una domanda a Claude con ricerca web via llm_client (tier balanced).
    Solleva LLMUnavailable/LLMRefused se il modello non è raggiungibile."""
    if complete is None:
        raise LLMUnavailable(f"llm_client non importabile ({_LLM_IMPORT_ERROR})")
    t0 = time.time()
    r = complete(question + _CLAUDE_QUESTION_SUFFIX, system=SYSTEM_ANSWER, tier="balanced",
                 model=model, web_search=True, max_tokens=1500)
    text = (r.text or "").strip()
    usage = r.usage or {}
    raw = r.raw or {}
    return {
        "text": text,
        "urls": extract_urls(text),
        "search_error": None,
        "model": r.model,
        "backend": r.backend,
        "usage": {"input_tokens": usage.get("input_tokens", 0) or 0,
                  "output_tokens": usage.get("output_tokens", 0) or 0,
                  "cache_read_tokens": usage.get("cache_read_input_tokens", 0) or 0,
                  "turns": raw.get("num_turns")},
        "cost_usd": 0.0,   # abbonamento: non fatturato
        "cost_reported_usd": raw.get("total_cost_usd"),   # solo informativo (listino CLI)
        "latency_s": round(time.time() - t0, 1),
    }


# ── Engine B: Gemini + Google Search grounding ───────────────────────
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash"]


def ask_gemini(key: str, question: str) -> Dict[str, Any]:
    last_err = ""
    for model in GEMINI_MODELS:
        t0 = time.time()
        endpoint = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model}:generateContent?key={key}")
        try:
            r = requests.post(
                endpoint, headers={"Content-Type": "application/json"},
                json={
                    "systemInstruction": {"parts": [{"text": SYSTEM_ANSWER}]},
                    "contents": [{"parts": [{"text": question}]}],
                    "tools": [{"google_search": {}}],
                    "generationConfig": {"maxOutputTokens": 1500},
                },
                timeout=60)
            if r.status_code >= 400:
                last_err = f"{model}: HTTP {r.status_code} {r.text[:200]}"
                log(f"  gemini {last_err} → provo il modello successivo")
                continue
            data = r.json()
        except Exception as e:  # noqa: BLE001 — l'URL contiene ?key=, non loggarlo
            last_err = f"{model}: {type(e).__name__}"
            log(f"  gemini {last_err}")
            continue
        cands = data.get("candidates") or []
        if not cands:
            last_err = f"{model}: nessun candidate ({str(data)[:160]})"
            continue
        cand = cands[0]
        text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
        gm = cand.get("groundingMetadata", {}) or {}
        urls = []
        seen = set()
        for ch in gm.get("groundingChunks", []) or []:
            w = ch.get("web", {}) or {}
            u, t = w.get("uri", ""), w.get("title", "")
            if u and u not in seen:
                seen.add(u)
                urls.append({"url": u, "title": t})
        um = data.get("usageMetadata", {}) or {}
        in_tok = um.get("promptTokenCount", 0) or 0
        out_tok = um.get("candidatesTokenCount", 0) or 0
        grounded = 1 if gm else 0
        cost = (in_tok * PRICE["gemini"][0] + out_tok * PRICE["gemini"][1]) / 1e6 \
            + grounded * PRICE["gemini_grounding_per_1k"] / 1000
        return {
            "text": text.strip(),
            "urls": urls,
            "search_queries": gm.get("webSearchQueries", []),
            "model": model,
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok, "grounded": grounded},
            "cost_usd": round(cost, 4),
            "latency_s": round(time.time() - t0, 1),
        }
    raise RuntimeError(last_err or "gemini: nessun modello disponibile")


# ── Fact-check pass (Claude cheap + json_schema) ─────────────────────
_FACT_SCHEMA = {
    "type": "object",
    "properties": {
        "wrong_claims": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"claim": {"type": "string"}, "issue": {"type": "string"}},
                      "required": ["claim", "issue"]},
        },
    },
    "required": ["wrong_claims"],
}


def fact_check(answer_text: str, facts: str, model: Optional[str] = None) -> Dict[str, Any]:
    """Elenca le affermazioni su My Villa che contraddicono i fatti canonici.
    Solleva LLMUnavailable/LLMRefused se il modello non è raggiungibile."""
    if complete_json is None:
        raise LLMUnavailable(f"llm_client non importabile ({_LLM_IMPORT_ERROR})")
    prompt = (
        f"{facts}\n\nBelow is an AI assistant's answer that mentions My Villa. List every "
        "statement ABOUT MY VILLA that CONTRADICTS the canonical facts (office locations, "
        "claims of completed/built/delivered homes, prices, timelines, founder, services). "
        "Do NOT flag statements that are merely absent from the facts, and do NOT flag "
        "\"designs\" or \"designs and builds\" unless the answer claims specific completed "
        "houses. Ignore statements about other companies. Reply ONLY with JSON: "
        '{"wrong_claims": [{"claim": "<quoted or paraphrased>", "issue": "<why wrong>"}]} '
        "— an empty list if nothing is wrong.\n\nANSWER:\n" + answer_text[:6000]
    )
    data = complete_json(prompt, _FACT_SCHEMA, tier="cheap", model=model, max_tokens=800)
    claims: List[Dict[str, str]] = []
    for c in data.get("wrong_claims", []) or []:
        if isinstance(c, dict):
            claims.append({"claim": str(c.get("claim", "")), "issue": str(c.get("issue", ""))})
    return {"wrong_claims": claims, "model": model or "cheap", "cost_usd": 0.0}


# ── Orchestrazione ───────────────────────────────────────────────────
def analyse(entry: Dict[str, Any]) -> None:
    text = entry.get("text", "") or ""
    urls = entry.get("urls", []) or []
    in_sources = any(DOMAIN_RE.search((u.get("url", "") + " " + u.get("title", "")))
                     for u in urls)
    entry["mentioned_myvilla"] = bool(BRAND_RE.search(text))
    entry["cited_myvilla"] = bool(DOMAIN_RE.search(text)) or in_sources
    entry["myvilla_urls"] = [u["url"] for u in urls
                             if DOMAIN_RE.search(u.get("url", "") + " " + u.get("title", ""))]


def update_summary(run: Dict[str, Any]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {"history": []}
    if SUMMARY.exists():
        try:
            summary = json.load(open(SUMMARY, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    stats: Dict[str, Any] = {}
    for eng in ("claude", "gemini"):
        rows = [r for r in run["results"] if r["engine"] == eng and not r.get("error")]
        n = len(rows)
        cited = sum(1 for r in rows if r.get("cited_myvilla"))
        mentioned = sum(1 for r in rows if r.get("mentioned_myvilla"))
        wrong = sum(len(r.get("fact_check", {}).get("wrong_claims", [])) for r in rows)
        errors = sum(1 for r in run["results"] if r["engine"] == eng and r.get("error"))
        stats[eng] = {
            "answered": n, "errors": errors, "cited": cited, "mentioned": mentioned,
            "citation_share": round(cited / n, 3) if n else None,
            "mention_share": round(mentioned / n, 3) if n else None,
            "wrong_claims": wrong,
            "brand_queries_cited": sum(1 for r in rows if r["group"] == "brand" and r.get("cited_myvilla")),
            "brand_queries": sum(1 for r in rows if r["group"] == "brand"),
        }
    row = {"date": run["date"], "engines": stats, "cost_est_usd": run["cost_est_usd"],
           "queries": run["query_count"]}
    summary["history"] = [h for h in summary.get("history", []) if h.get("date") != run["date"]]
    summary["history"].append(row)
    summary["history"].sort(key=lambda h: h["date"])
    summary["latest"] = row
    summary["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    SUMMARY.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description="GEO monitor: citazioni di My Villa nei motori AI")
    ap.add_argument("--dry-run", action="store_true", help="nessuna chiamata ai modelli")
    ap.add_argument("--engine", choices=["claude", "gemini", "both"], default="both")
    ap.add_argument("--limit", type=int, default=0, help="solo le prime N query")
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--no-save", action="store_true",
                    help="non scrivere runs/<date>.json né summary.json (test)")
    args = ap.parse_args()

    load_dotenv()
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        queries = yaml.safe_load(open(QUERIES, encoding="utf-8"))["queries"]
    except Exception as e:  # noqa: BLE001
        log(f"queries.yml non leggibile: {e}")
        return 0
    if args.limit:
        queries = queries[: args.limit]
    log(f"{len(queries)} query · engine={args.engine} · date={args.date}"
        + (" · DRY-RUN" if args.dry_run else ""))

    if args.dry_run:
        for q in queries:
            print(f"  {q['id']} [{q['group']}] {q['text']}")
        return 0

    engines = ["claude", "gemini"] if args.engine == "both" else [args.engine]
    # Claude (motore + fact-check) passa da llm_client → Claude Code, abbonamento.
    claude_ok = complete is not None
    if claude_ok:
        try:
            from llm_client import status as llm_status, model_for
            st = llm_status()
            if not st.get("cli"):
                raise LLMUnavailable("Claude Code CLI non trovata")
            log(f"claude: backend={st.get('backend')} · answer={model_for('balanced')} · "
                f"fact-check={model_for('cheap')} · billed=0 (abbonamento)")
        except Exception as e:  # noqa: BLE001
            log(f"Claude non disponibile ({e}) → engine claude e fact-check saltati")
            claude_ok = False
    else:
        log(f"llm_client non importabile ({_LLM_IMPORT_ERROR}) → engine claude e fact-check saltati")
    if not claude_ok:
        engines = [e for e in engines if e != "claude"]
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if "gemini" in engines and not gemini_key:
        log("GEMINI_API_KEY mancante → engine gemini saltato")
        engines = [e for e in engines if e != "gemini"]

    facts = canonical_facts()
    results: List[Dict[str, Any]] = []
    total_cost = 0.0
    for q in queries:
        for eng in engines:
            entry: Dict[str, Any] = {"query_id": q["id"], "group": q["group"],
                                     "query": q["text"], "engine": eng, "error": None}
            if eng == "claude" and not claude_ok:
                entry["error"] = "LLMUnavailable: Claude Code non disponibile (skip)"
                results.append(entry)
                continue
            try:
                if eng == "claude":
                    entry.update(ask_claude(q["text"]))
                else:
                    entry.update(ask_gemini(gemini_key, q["text"]))
            except (LLMUnavailable, LLMRefused) as e:
                # limite d'uso / CLI / auth: inutile insistere sulle query successive
                entry["error"] = f"{type(e).__name__}: {str(e)[:200]}"
                log(f"  {q['id']} {eng}: {entry['error']} → motore claude e fact-check saltati")
                claude_ok = False
                results.append(entry)
                continue
            except Exception as e:  # noqa: BLE001
                entry["error"] = f"{type(e).__name__}: {str(e)[:200]}"
                log(f"  {q['id']} {eng}: ERRORE {entry['error']}")
                results.append(entry)
                continue
            analyse(entry)
            total_cost += entry.get("cost_usd", 0.0)
            if entry["mentioned_myvilla"] and claude_ok:
                try:
                    fc = fact_check(entry["text"], facts)
                    entry["fact_check"] = fc
                    total_cost += fc.get("cost_usd", 0.0)
                except (LLMUnavailable, LLMRefused) as e:
                    entry["fact_check"] = {"wrong_claims": [], "error": str(e)[:200]}
                    log(f"  fact-check: {type(e).__name__} → saltato per il resto del run")
                    claude_ok = False
                except Exception as e:  # noqa: BLE001
                    entry["fact_check"] = {"wrong_claims": [], "error": str(e)[:200]}
            flag = "CITED" if entry["cited_myvilla"] else ("mentioned" if entry["mentioned_myvilla"] else "-")
            wrong = len(entry.get("fact_check", {}).get("wrong_claims", []))
            log(f"  {q['id']} {eng:6s} {flag:9s} urls={len(entry['urls']):2d} "
                f"wrong_claims={wrong} cost=${entry.get('cost_usd', 0):.4f} "
                f"({entry.get('latency_s', 0)}s)")
            results.append(entry)

    run = {
        "date": args.date,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engines": engines,
        "query_count": len(queries),
        "cost_est_usd": round(total_cost, 3),
        "results": results,
    }
    if args.no_save:
        log(f"--no-save: run non scritto · costo stimato ${total_cost:.3f} (Claude: 0, abbonamento)")
        for r in results:
            log(f"  {r['query_id']} {r['engine']}: error={r.get('error')} "
                f"cited={r.get('cited_myvilla')} mentioned={r.get('mentioned_myvilla')} "
                f"urls={len(r.get('urls') or [])} text[:200]={(r.get('text') or '')[:200]!r}")
        return 0
    out = RUNS_DIR / f"{args.date}.json"
    out.write_text(json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8")
    row = update_summary(run)
    log(f"salvato {out.relative_to(ROOT_DIR)} · costo stimato ${total_cost:.3f} (Claude: 0, abbonamento)")
    for eng, st in row["engines"].items():
        if st["answered"] or st["errors"]:
            log(f"  {eng}: {st['answered']} risposte, {st['errors']} errori, "
                f"citation share {st['citation_share']}, mention share {st['mention_share']}, "
                f"wrong claims {st['wrong_claims']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
