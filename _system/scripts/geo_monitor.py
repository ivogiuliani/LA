#!/usr/bin/env python3
"""
geo_monitor.py — GEO (Generative Engine Optimization) monitor.

Pone le domande "da cliente" di _system/research/geo/queries.yml a due
motori di risposta AI con ricerca web e misura se My Villa viene citata:

  (a) Claude + web search server tool
      {"type": "web_search_20260209", "name": "web_search", "max_uses": 1}
      via anthropic SDK, modello = model_resolver.resolve("balanced")
  (b) Gemini via REST (requests) su
      generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash
      con tools [{"google_search": {}}] e GEMINI_API_KEY
      (fallback gemini-2.0-flash; se anche quello fallisce la query è saltata
      con log — la pipeline non si ferma mai).

Per ogni risposta salva: testo, URL citati, cited_myvilla (myvilla.la nel
testo o nelle fonti), mentioned_myvilla (il nome nel testo) e le
affermazioni SBAGLIATE su My Villa (sedi, "built homes", prezzi…) rilevate
con un secondo passaggio Claude cheap (solo se il nome compare: costo zero
altrimenti).

Output:
  _system/research/geo/runs/<date>.json   run completo (una voce per query × motore)
  _system/research/geo/summary.json       citation share per motore + storico
Costo stimato (token × listino, indicativo) nel log e nel run.

Uso:
  python3 _system/scripts/geo_monitor.py                 # baseline/run completo
  python3 _system/scripts/geo_monitor.py --dry-run       # nessuna chiamata API
  python3 _system/scripts/geo_monitor.py --engine claude # solo un motore
  python3 _system/scripts/geo_monitor.py --limit 3       # prime 3 query
  python3 _system/scripts/geo_monitor.py --date 2026-09-16

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
    from model_resolver import resolve  # noqa: E402
except Exception:  # noqa: BLE001
    def resolve(tier):  # type: ignore
        return {"balanced": "claude-sonnet-4-5", "cheap": "claude-haiku-4-5"}.get(tier, "claude-sonnet-4-5")

BRAND_RE = re.compile(r"my\s?villa|myvilla\.la", re.I)
DOMAIN_RE = re.compile(r"myvilla\.la", re.I)

# Listino indicativo (USD per 1M token; web search per 1000 ricerche).
# Solo per la STIMA nel log: aggiornare se cambia il pricing.
PRICE = {
    "sonnet": (3.0, 15.0), "haiku": (1.0, 5.0), "opus": (15.0, 75.0),
    "fable": (15.0, 75.0), "gemini": (0.30, 2.50),
    "claude_search_per_1k": 10.0, "gemini_grounding_per_1k": 35.0,
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


def _price_family(model: str) -> str:
    for fam in ("sonnet", "haiku", "opus", "fable"):
        if fam in model:
            return fam
    return "sonnet"


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


# ── Engine A: Claude + web search ────────────────────────────────────
def ask_claude(client: Any, model: str, question: str) -> Dict[str, Any]:
    t0 = time.time()
    resp = client.messages.create(
        model=model,
        max_tokens=1500,
        system=SYSTEM_ANSWER,
        messages=[{"role": "user", "content": question}],
        tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 1}],
    )
    text_parts: List[str] = []
    urls: List[Dict[str, str]] = []
    search_error: Optional[str] = None
    seen = set()

    def _add(url: str, title: str = "") -> None:
        if url and url not in seen:
            seen.add(url)
            urls.append({"url": url, "title": title or ""})

    for block in resp.content:
        btype = getattr(block, "type", "")
        if btype == "text":
            text_parts.append(getattr(block, "text", "") or "")
            for cit in (getattr(block, "citations", None) or []):
                _add(getattr(cit, "url", "") or "", getattr(cit, "title", "") or "")
        elif btype == "web_search_tool_result":
            content = getattr(block, "content", None)
            if isinstance(content, list):
                for r in content:
                    _add(getattr(r, "url", "") or "", getattr(r, "title", "") or "")
            else:  # oggetto errore
                search_error = str(getattr(content, "error_code", None) or content)
    usage = getattr(resp, "usage", None)
    in_tok = getattr(usage, "input_tokens", 0) or 0
    out_tok = getattr(usage, "output_tokens", 0) or 0
    stu = getattr(usage, "server_tool_use", None)
    searches = getattr(stu, "web_search_requests", 0) if stu else 0
    fam = _price_family(model)
    cost = (in_tok * PRICE[fam][0] + out_tok * PRICE[fam][1]) / 1e6 \
        + searches * PRICE["claude_search_per_1k"] / 1000
    return {
        "text": "\n".join(text_parts).strip(),
        "urls": urls,
        "search_error": search_error,
        "model": model,
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok, "web_searches": searches},
        "cost_usd": round(cost, 4),
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


# ── Fact-check pass (Claude cheap) ───────────────────────────────────
def fact_check(client: Any, model: str, answer_text: str, facts: str) -> Dict[str, Any]:
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
    resp = client.messages.create(model=model, max_tokens=800,
                                  messages=[{"role": "user", "content": prompt}])
    txt = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", txt, re.S)
    claims: List[Dict[str, str]] = []
    if m:
        try:
            claims = json.loads(m.group(0)).get("wrong_claims", []) or []
        except Exception:  # noqa: BLE001
            claims = [{"claim": "(unparsed)", "issue": txt[:300]}]
    usage = getattr(resp, "usage", None)
    fam = _price_family(model)
    cost = ((getattr(usage, "input_tokens", 0) or 0) * PRICE[fam][0]
            + (getattr(usage, "output_tokens", 0) or 0) * PRICE[fam][1]) / 1e6
    return {"wrong_claims": claims, "model": model, "cost_usd": round(cost, 4)}


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
    ap.add_argument("--dry-run", action="store_true", help="nessuna chiamata API")
    ap.add_argument("--engine", choices=["claude", "gemini", "both"], default="both")
    ap.add_argument("--limit", type=int, default=0, help="solo le prime N query")
    ap.add_argument("--date", default=date.today().isoformat())
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
    client = None
    model_balanced = model_cheap = ""
    if "claude" in engines or True:  # il fact-check usa sempre Claude
        try:
            import anthropic  # noqa: WPS433
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise RuntimeError("ANTHROPIC_API_KEY mancante")
            client = anthropic.Anthropic()
            model_balanced = resolve("balanced")
            model_cheap = resolve("cheap")
            log(f"claude: answer={model_balanced} · fact-check={model_cheap}")
        except Exception as e:  # noqa: BLE001
            log(f"Claude non disponibile ({e}) → engine claude e fact-check saltati")
            client = None
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
            try:
                if eng == "claude":
                    entry.update(ask_claude(client, model_balanced, q["text"]))
                else:
                    entry.update(ask_gemini(gemini_key, q["text"]))
            except Exception as e:  # noqa: BLE001
                entry["error"] = f"{type(e).__name__}: {str(e)[:200]}"
                log(f"  {q['id']} {eng}: ERRORE {entry['error']}")
                results.append(entry)
                continue
            analyse(entry)
            total_cost += entry.get("cost_usd", 0.0)
            if entry["mentioned_myvilla"] and client is not None:
                try:
                    fc = fact_check(client, model_cheap, entry["text"], facts)
                    entry["fact_check"] = fc
                    total_cost += fc.get("cost_usd", 0.0)
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
    out = RUNS_DIR / f"{args.date}.json"
    out.write_text(json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8")
    row = update_summary(run)
    log(f"salvato {out.relative_to(ROOT_DIR)} · costo stimato ${total_cost:.3f}")
    for eng, st in row["engines"].items():
        if st["answered"] or st["errors"]:
            log(f"  {eng}: {st['answered']} risposte, {st['errors']} errori, "
                f"citation share {st['citation_share']}, mention share {st['mention_share']}, "
                f"wrong claims {st['wrong_claims']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
