#!/usr/bin/env python3
"""
feature_pitch.py — pitch local press to write a FEATURE about My Villa.

Different from the journalist outreach (which responds to a reporter's
existing article with a story angle). Here we proactively propose that a
local LA/SoCal luxury real-estate / design publication writes a profile
of My Villa.

Target list: _system/outreach/local_press.yml
Per outlet:
  1. Skip if already feature-pitched (feature_pitch_log.jsonl).
  2. Find an editorial email:
       - known_email in the YAML (if set), else
       - editorial_scraper.lookup(contact_url) → verified address.
     VERIFIED (mailto/JSON-LD/contact page) → auto-send.
     PATTERN-GUESS / not found → queue for manual review (never blind-send).
  3. Generate an on-brand feature pitch (Opus, outreach voice).
  4. Send via send_email.send_draft (rate-limited, blacklist-checked).
  5. Record in feature_pitch_log.jsonl.

Cap: cadence.max_per_day from the YAML (default 2) — never a blast.

CLI:
  python3 feature_pitch.py                 # live, up to max_per_day
  python3 feature_pitch.py --dry-run       # generate + show, don't send
  python3 feature_pitch.py --max 1         # override daily cap
  python3 feature_pitch.py --only digs.net # single outlet (test)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# ── Auto-model: tier risolto via model_resolver — upgrade automatico
# ai modelli più recenti appena compaiono su /v1/models (policy Ivo
# 2026-06-10). Fallback hardcoded se il resolver non è importabile:
# il modello non deve MAI bloccare la pipeline.
try:
    import sys as _sys
    if str(SCRIPT_DIR) not in _sys.path:
        _sys.path.insert(0, str(SCRIPT_DIR))
    from model_resolver import resolve as _resolve_model
except Exception:  # noqa: BLE001
    def _resolve_model(tier, _fb={"writer": "claude-fable-5",
                                  "heavy": "claude-opus-4-8",
                                  "balanced": "claude-sonnet-4-6",
                                  "cheap": "claude-haiku-4-5"}):
        return _fb.get(tier, "claude-sonnet-4-6")
MODEL = _resolve_model("heavy")
SYSTEM_DIR = SCRIPT_DIR.parent
ROOT_DIR = SYSTEM_DIR.parent
OUTREACH_DIR = SYSTEM_DIR / "outreach"
TARGET_LIST = OUTREACH_DIR / "local_press.yml"
PITCH_LOG = OUTREACH_DIR / "feature_pitch_log.jsonl"
VOICE_DOC = SYSTEM_DIR / "knowledge" / "outreach_voice.md"



def _load_dotenv():
    env = ROOT_DIR / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        # Override when the env var is missing OR empty. (setdefault
        # would skip a pre-set EMPTY var — which some shells/CI inject
        # for ANTHROPIC_API_KEY — leaving us without the real key.)
        if v and not os.environ.get(k):
            os.environ[k] = v


def _load_outlets() -> tuple[list, dict]:
    import yaml
    data = yaml.safe_load(TARGET_LIST.read_text(encoding="utf-8"))
    return data.get("outlets", []), data.get("cadence", {})


def _already_pitched() -> set:
    """Domains already feature-pitched (any status), from the log."""
    done = set()
    if PITCH_LOG.exists():
        for line in PITCH_LOG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            status = r.get("status") or ""
            # Solo gli esiti DEFINITIVI escludono l'outlet; no_email e
            # queued_review devono poter essere ritentati nei run futuri.
            if r.get("domain") and status not in ("no_email", "queued_review"):
                done.add(r["domain"].lower())
    return done


def _log_pitch(record: dict) -> None:
    record["logged_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with PITCH_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _find_email(outlet: dict) -> tuple[str | None, str]:
    """Return (email, confidence). confidence ∈ {known, verified, guess, none}."""
    known = outlet.get("known_email")
    if known:
        return known, "known"
    # Try the editorial scraper on the contact URL.
    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        import editorial_scraper
        res = editorial_scraper.lookup(
            url=outlet.get("contact_url") or f"https://{outlet['domain']}/",
            domain=outlet["domain"],
        )
        if res and res.get("email"):
            # The scraper scores mailto/contact-page hits as high-confidence.
            conf = ("verified" if res.get("in_mailto") or res.get("score", 0) >= 8 else "guess")
            return res["email"], conf
    except Exception as e:  # noqa: BLE001
        print(f"    [scraper] {outlet['domain']}: {type(e).__name__}: {e}")
    return None, "none"


def _generate_pitch(outlet: dict) -> tuple[str, str] | None:
    """Return (subject, body) for the feature pitch, or None on failure."""
    import urllib.request
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("    [pitch] ANTHROPIC_API_KEY missing")
        return None
    voice = VOICE_DOC.read_text(encoding="utf-8")[:6000] if VOICE_DOC.exists() else ""

    prompt = f"""You are Lisa Monelli, My Villa's media-relations lead, writing a FEATURE-PITCH email.

This proposes that the publication writes a feature / profile ABOUT My Villa — our company, approach, and houses. It is NOT a reaction to one of their articles.

TARGET PUBLICATION: {outlet['name']} ({outlet['domain']})
THEIR BEAT: {outlet.get('beat','luxury real estate & design')}

Brand voice source of truth (tone + the three pillars):
---
{voice}
---

Write the email. Rules:
- Subject: short, specific to this publication, no clickbait.
- Body ≤ 160 words, warm, human, editorial peer-to-peer tone, no stacked superlatives.
- One idea: My Villa would be a strong feature for THEIR specific readers because we bring European construction resilience + Italian livability in exposed reinforced concrete (cemento a vista) to LA — exactly as insurability and fire-resilience reshape what a luxury home means here. Tie the relevance to their beat.
- One concrete low-friction offer (first look at a current build / renders + photos / short talk with our founder Paolo Mezzalama).
- One open question at the end.
- FOUNDER NAME: he is "Paolo Mezzalama". Refer to him as "Paolo" or "Paolo Mezzalama" — NEVER invent or use any other surname.
- Standard signature:
Best,
Lisa Monelli
My Villa Media Team
info@myvilla.la · myvilla.la

Output ONLY the email as:
Subject: <subject>
<blank line>
<body>"""

    body = json.dumps({
        "model": MODEL, "max_tokens": 700,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        import urllib.error
        with urllib.request.urlopen(req, timeout=60) as r:
            txt = json.load(r)["content"][0]["text"].strip()
    except Exception as e:  # noqa: BLE001
        print(f"    [pitch] generation failed: {e}")
        return None

    # Split "Subject: ..." from body.
    subject, _, rest = txt.partition("\n")
    subject = subject.replace("Subject:", "").strip()
    return subject, rest.strip()


def run(*, dry_run=False, max_per_run=None, only=None) -> dict:
    _load_dotenv()
    outlets, cadence = _load_outlets()
    cap = max_per_run if max_per_run is not None else cadence.get("max_per_day", 2)
    done = _already_pitched()

    # Order by priority (1 first), preserving file order within a tier.
    outlets = sorted(outlets, key=lambda o: o.get("priority", 99))
    if only:
        outlets = [o for o in outlets if o["domain"].lower() == only.lower()]

    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        from send_email import send_draft
    except ImportError as e:
        print(f"  [feature] send_email unavailable: {e}")
        return {"sent": [], "queued": [], "skipped": []}

    sent, queued, skipped = [], [], []
    for outlet in outlets:
        dom = outlet["domain"].lower()
        if len(sent) >= cap:
            break
        if dom in done and not only:
            skipped.append({"outlet": outlet["name"], "reason": "già contattata"})
            continue

        email, conf = _find_email(outlet)
        if not email:
            print(f"  ~ {outlet['name']}: nessuna email editoriale trovata — skip")
            queued.append({"outlet": outlet["name"], "domain": dom,
                           "reason": "email non trovata"})
            if not dry_run:
                _log_pitch({"domain": dom, "outlet": outlet["name"],
                            "status": "no_email"})
            continue

        gen = _generate_pitch(outlet)
        if not gen:
            skipped.append({"outlet": outlet["name"], "reason": "pitch gen fallita"})
            continue
        subject, pbody = gen

        # Pattern-guess → never blind-send; queue for manual review.
        if conf == "guess":
            print(f"  ~ {outlet['name']}: email pattern-guess ({email}) — coda review")
            queued.append({"outlet": outlet["name"], "domain": dom, "to": email,
                           "subject": subject, "body": pbody,
                           "reason": "email da validare (pattern guess)"})
            if not dry_run:
                _log_pitch({"domain": dom, "outlet": outlet["name"], "to": email,
                            "status": "queued_review", "subject": subject})
            continue

        if dry_run:
            print(f"\n  ── [DRY-RUN] {outlet['name']} → {email} ({conf}) ──")
            print(f"  Subject: {subject}")
            print("  " + pbody.replace("\n", "\n  ")[:400] + " …")
            sent.append({"outlet": outlet["name"], "to": email, "subject": subject,
                         "body": pbody, "confidence": conf, "dry_run": True})
            continue

        result = send_draft(to=email, subject=subject, body=pbody)
        ok = result.get("ok", False)
        reason = result.get("reason") or ""
        if ok and not result.get("dry_run"):
            print(f"  ✓ {outlet['name']} → {email}")
            sent.append({"outlet": outlet["name"], "to": email, "subject": subject,
                         "body": pbody, "confidence": conf,
                         "message_id": result.get("message_id")})
            _log_pitch({"domain": dom, "outlet": outlet["name"], "to": email,
                        "status": "sent", "subject": subject,
                        "message_id": result.get("message_id")})
        elif reason == "rate_limited":
            print(f"  ⏸ rate limit raggiunto a {len(sent)} invii — stop")
            break
        else:
            err = result.get("error") or reason or "unknown"
            print(f"  ✗ {outlet['name']} → {email}: {err}")
            skipped.append({"outlet": outlet["name"], "reason": err})

    return {"sent": sent, "queued": queued, "skipped": skipped}


def main(argv=None):
    p = argparse.ArgumentParser(description="Feature-pitch local press on My Villa.")
    p.add_argument("--dry-run", action="store_true", help="Generate + show, don't send.")
    p.add_argument("--max", type=int, default=None, help="Override daily cap.")
    p.add_argument("--only", default=None, help="Single outlet domain (test).")
    args = p.parse_args(argv)

    res = run(dry_run=args.dry_run, max_per_run=args.max, only=args.only)
    print(f"\nFeature pitch: {len(res['sent'])} inviati, "
          f"{len(res['queued'])} in coda review, {len(res['skipped'])} skip.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
