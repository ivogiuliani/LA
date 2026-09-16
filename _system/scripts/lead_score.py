#!/usr/bin/env python3
"""
lead_score.py — tier A/B/C + score 0-100 per un lead.

Due passi:
  1. REGOLE deterministiche da lead_settings.yml → lead.scoring
     (aree tier-1, timeline, rebuild, red flag fornitori). Sempre eseguite.
  2. RIFINITURA Claude (model_resolver.resolve("balanced")) sui SOLI campi
     project_type / timeline / site_location / message — mai nome, email,
     telefono. Output JSON {tier, score, reasons[], needs_human, confidence}.
     Se l'API manca/fallisce → si tengono le regole (needs_human=True se
     il caso è ambiguo). Confidence < soglia → needs_human=True.

    from lead_score import score_lead
    result = score_lead(lead_dict, use_llm=True)
    # → {"tier": "A", "score": 82, "reasons": [...], "needs_human": False,
    #    "confidence": 0.9, "method": "rules+llm", "vendor": False}

CLI (test senza PII):
    python3 lead_score.py --project-type "New custom build" --timeline "Ready now" \
        --site "Malibu" --message "We own a lot on Point Dume" [--no-llm]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Optional

from lead_settings import get as cfg_get, PROJECT_ROOT

SCORE_FIELDS = ("project_type", "timeline", "site_location", "message", "how_found")


def _load_env_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _contains_any(text: str, needles: list) -> Optional[str]:
    t = (text or "").lower()
    for n in needles or []:
        if str(n).lower() in t:
            return str(n)
    return None


# --------------------------------------------------------------------------- #
# Step 1 — regole
# --------------------------------------------------------------------------- #

def score_rules(lead: dict) -> dict:
    sc = cfg_get("lead.scoring", {}) or {}
    tier1 = sc.get("tier1_areas") or []
    cal = sc.get("california_areas") or []
    fast = [s.lower() for s in (sc.get("fast_timelines") or [])]
    mid = [s.lower() for s in (sc.get("mid_timelines") or [])]
    rebuild_kw = sc.get("rebuild_keywords") or []
    flags = sc.get("vendor_red_flags") or []

    project = (lead.get("project_type") or "").strip()
    timeline = (lead.get("timeline") or "").strip()
    site = (lead.get("site_location") or "").strip()
    message = (lead.get("message") or "").strip()
    blob = " ".join([project, timeline, site, message])

    reasons: list = []
    score = 30
    ambiguous = False

    # Red flag fornitori → C secco
    flag = _contains_any(message, flags) or _contains_any(project, flags)
    if flag:
        return {"tier": "C", "score": 5, "reasons": [f"vendor/solicitation signal: '{flag}'"],
                "needs_human": False, "vendor": True, "method": "rules"}

    # Area
    area_t1 = _contains_any(site, tier1) or _contains_any(message, tier1)
    area_ca = _contains_any(site, cal) or _contains_any(message, cal)
    if area_t1:
        score += 35
        reasons.append(f"tier-1 area: {area_t1}")
    elif area_ca:
        score += 20
        reasons.append(f"California area: {area_ca}")
    elif site:
        score += 5
        reasons.append(f"area outside known lists: '{site[:40]}'")
        ambiguous = True
    else:
        reasons.append("no site location given")
        ambiguous = True

    # Timeline
    tl = timeline.lower()
    if tl in fast:
        score += 25
        reasons.append(f"timeline ≤ 12 months ({timeline})")
    elif tl in mid:
        score += 12
        reasons.append(f"timeline 12–24 months ({timeline})")
    elif tl:
        score += 0
        reasons.append(f"timeline: {timeline}")
    else:
        ambiguous = True

    # Rebuild after fire
    rebuild = (project.lower().startswith("rebuild") or
               _contains_any(blob, rebuild_kw) is not None)
    if rebuild:
        score += 10
        reasons.append("rebuild after fire")

    # Progetto
    pl = project.lower()
    if pl.startswith("new custom") or rebuild:
        score += 5
    elif "future site" in pl or "land search" in pl:
        score -= 5
        reasons.append("no site yet (land search)")
    elif "general" in pl:
        score -= 10
        reasons.append("general interest")

    # Messaggio: sostanza
    if len(message) >= 120:
        score += 5
        reasons.append("substantive message")
    elif not message:
        reasons.append("empty message")

    score = max(0, min(100, score))

    # Tier
    if area_t1 and tl in fast:
        tier = "A"
    elif (area_t1 or area_ca) and (tl in fast or tl in mid):
        tier = "B"
    elif rebuild and (area_t1 or area_ca):
        tier = "B"
    elif tl == "exploring" or (not area_t1 and not area_ca):
        tier = "C"
    else:
        tier = "B" if score >= 45 else "C"

    return {"tier": tier, "score": score, "reasons": reasons,
            "needs_human": ambiguous and tier != "C", "vendor": False,
            "method": "rules"}


# --------------------------------------------------------------------------- #
# Step 2 — rifinitura Claude
# --------------------------------------------------------------------------- #

_SYSTEM = """You triage inbound enquiries for My Villa, a design-led practice proposing
reinforced-concrete luxury villas in Los Angeles (Malibu, Beverly Hills, Bel Air,
Brentwood, Pacific Palisades and similar). You see ONLY project fields — never the
person's identity. Classify the enquiry:

- Tier A: tier-1 Los Angeles luxury area AND wants to start within 12 months (or owns a lot / lost a home in the fires and is ready).
- Tier B: California luxury area OR 12–24 month horizon OR rebuild after fire.
- Tier C: just exploring, outside California, students, press, vendors, agencies,
  SEO/link/web-design solicitations, spam.

Return ONLY a JSON object:
{"tier":"A|B|C","score":0-100,"reasons":["short reason", ...],
 "needs_human":true|false,"confidence":0.0-1.0,"vendor":true|false}
needs_human=true when the text is ambiguous, contradictory, in another language you
cannot read, or hints at something unusual (press, partnership, legal). Be conservative
on tier A. Do not invent facts."""


def refine_with_llm(lead: dict, rules: dict) -> Optional[dict]:
    key = _load_env_key()
    if not key:
        return None
    try:
        import anthropic
        from model_resolver import resolve
    except Exception:  # noqa: BLE001
        return None
    payload = {k: (lead.get(k) or "")[:1500] for k in SCORE_FIELDS}
    payload["rules_verdict"] = {"tier": rules["tier"], "score": rules["score"],
                                "reasons": rules["reasons"]}
    try:
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model=resolve("balanced"),
            max_tokens=400,
            system=_SYSTEM,
            messages=[{"role": "user", "content":
                       "Enquiry fields (JSON):\n" + json.dumps(payload, ensure_ascii=False)}],
        )
        text = "".join(getattr(b, "text", "") for b in resp.content)
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        data = json.loads(m.group(0))
        tier = str(data.get("tier", "")).upper()[:1]
        if tier not in ("A", "B", "C"):
            return None
        return {
            "tier": tier,
            "score": max(0, min(100, int(data.get("score", rules["score"])))),
            "reasons": [str(r) for r in (data.get("reasons") or [])][:6],
            "needs_human": bool(data.get("needs_human", False)),
            "confidence": float(data.get("confidence", 0.5)),
            "vendor": bool(data.get("vendor", False)),
        }
    except Exception as exc:  # noqa: BLE001 — l'API non ferma mai l'intake
        print(f"  [lead_score] LLM refinement skipped: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return None


def score_lead(lead: dict, *, use_llm: bool = True) -> dict:
    rules = score_rules(lead)
    if rules.get("vendor"):
        rules["confidence"] = 0.95
        return rules
    result = dict(rules)
    result["confidence"] = 0.55 if rules["needs_human"] else 0.75
    if use_llm:
        llm = refine_with_llm(lead, rules)
        if llm:
            threshold = float(cfg_get("lead.scoring.needs_human_below_confidence", 0.6) or 0.6)
            merged_reasons = rules["reasons"] + [r for r in llm["reasons"] if r not in rules["reasons"]]
            result = {
                "tier": llm["tier"],
                "score": int(round((llm["score"] * 0.6) + (rules["score"] * 0.4))),
                "reasons": merged_reasons[:8],
                "needs_human": llm["needs_human"] or llm["confidence"] < threshold,
                "confidence": llm["confidence"],
                "vendor": llm["vendor"] or rules.get("vendor", False),
                "method": "rules+llm",
            }
            # Le regole restano un pavimento: un lead A per regole non scende a C per l'LLM senza revisione umana
            if rules["tier"] == "A" and llm["tier"] == "C":
                result["needs_human"] = True
                result["reasons"].append("rules=A vs llm=C: human check")
    return result


def _main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Scoring lead (test senza PII)")
    p.add_argument("--project-type", default="")
    p.add_argument("--timeline", default="")
    p.add_argument("--site", default="")
    p.add_argument("--message", default="")
    p.add_argument("--how-found", default="")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--json", help="dict JSON con i campi (alternativa ai flag)")
    a = p.parse_args(argv)
    lead = json.loads(a.json) if a.json else {
        "project_type": a.project_type, "timeline": a.timeline,
        "site_location": a.site, "message": a.message, "how_found": a.how_found}
    print(json.dumps(score_lead(lead, use_llm=not a.no_llm), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
