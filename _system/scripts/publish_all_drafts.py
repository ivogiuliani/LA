#!/usr/bin/env python3
"""
publish_all_drafts.py — auto-publish every journal article in
_drafts/journal/ and send a digest email to the operator.

Pipeline role
-------------
The journal pipeline used to be:
    radar → generate_journal.py → _drafts/journal/ → operator clicks
    "Pubblica" in the dashboard for each one → blog/.
This script collapses the manual step: every draft that passes link
validation is moved to blog/, the indices are rebuilt, the change is
auto-pushed to GitHub, and a single summary email goes to ivolo@me.com
listing what was published with live URLs.

Failure modes (per article):
  - Broken links (validate_links flags them) → article STAYS in
    _drafts/journal/ so the operator can fix and retry. Reported in
    the digest under "skipped".
  - Sidecar JSON missing/malformed → article SKIPPED, reported.
  - Update-script failure (journal_index / sitemap / homepage) →
    publish considered partial, logged but the article is still moved.

Usage
-----
    python3 publish_all_drafts.py                  # publish + email
    python3 publish_all_drafts.py --dry-run        # show what WOULD happen
    python3 publish_all_drafts.py --no-email       # publish but don't email
    python3 publish_all_drafts.py --force          # ignore broken-link check
    python3 publish_all_drafts.py --to other@x.com # override digest recipient

Intended to be called after `generate_journal.py` produces fresh drafts,
typically from a daily cron. Safe to run when the drafts folder is
empty (it just exits with "nothing to publish").
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
ROOT_DIR = SYSTEM_DIR.parent
DRAFTS_DIR = ROOT_DIR / "_drafts" / "journal"
BLOG_DIR = ROOT_DIR / "blog"
SOCIAL_APPROVED = SYSTEM_DIR / "social" / "posts" / "approved"

DEFAULT_RECIPIENT = "ivolo@me.com"
SITE_BASE = "https://myvilla.la"


# ── Helpers ──────────────────────────────────────────────────────────

def _load_dotenv():
    env_file = ROOT_DIR / ".env"
    if not env_file.exists():
        return
    import os
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _list_journal_drafts() -> list[Path]:
    """Return all .html drafts in _drafts/journal/, sorted by mtime asc."""
    if not DRAFTS_DIR.exists():
        return []
    return sorted(DRAFTS_DIR.glob("*.html"), key=lambda p: p.stat().st_mtime)


def _check_links(html_path: Path) -> tuple[bool, list[dict]]:
    """Run the validator. Returns (ok, broken_list).

    Mirrors what approve.py's _handle_approve does before publishing —
    we want the same gate (and the same TRUSTED_UNVERIFIABLE_HOSTS pass)
    when auto-publishing.
    """
    try:
        from validate_links import process_file as validate_links_file
    except ImportError:
        # Validator not installed → fail open (publish anyway, log warning).
        print("  [validate-links] module not importable — skipping check")
        return True, []
    try:
        report = validate_links_file(html_path, fix=False)
    except Exception as e:  # noqa: BLE001
        print(f"  [validate-links] error: {type(e).__name__}: {e} — failing open")
        return True, []
    broken = report.get("broken") or []
    return (not broken), broken


def _run_update_script(script_name: str) -> bool:
    """Shell out to one of the index/sitemap/homepage rebuilders."""
    path = SCRIPT_DIR / script_name
    if not path.exists():
        return True  # nothing to run, treat as success
    try:
        result = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(SCRIPT_DIR),
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            print(f"  [{script_name}] exit {result.returncode}: "
                  f"{(result.stderr or result.stdout)[-300:]}")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  [{script_name}] failed: {type(e).__name__}: {e}")
        return False


def _git_autopush(commit_msg: str) -> tuple[bool, str]:
    """Mirror of approve.py's _git_autopush logic. Returns (ok, sha)."""
    def _g(args, timeout=30):
        return subprocess.run(
            ["git", *args], cwd=str(ROOT_DIR),
            capture_output=True, text=True, timeout=timeout,
        )
    try:
        if not (ROOT_DIR / ".git").exists():
            return False, ""
        if not _g(["remote"], timeout=5).stdout.strip():
            return False, ""
        if _g(["add", "-A"], timeout=15).returncode != 0:
            return False, ""
        if _g(["diff", "--cached", "--quiet"], timeout=5).returncode == 0:
            print("  [autopush] nothing to commit (changes already pushed?)")
            return True, ""
        c = _g(["commit", "-m", commit_msg], timeout=15)
        if c.returncode != 0:
            return False, ""
        p = _g(["push", "origin", "main"], timeout=60)
        if p.returncode != 0:
            print(f"  [autopush] push failed: {p.stderr[-200:]}")
            return False, ""
        sha = _g(["rev-parse", "--short", "HEAD"], timeout=5).stdout.strip()
        return True, sha
    except Exception as e:  # noqa: BLE001
        print(f"  [autopush] {type(e).__name__}: {e}")
        return False, ""


# ── Publish one article ──────────────────────────────────────────────

def _read_sidecar(json_path: Path) -> dict:
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _dismiss_journal_sources(sidecar_data: dict, slug: str):
    """Mark the article's source URLs as user_dismissed so the radar
    doesn't re-suggest them. Mirrors approve.py's behavior on publish.
    """
    if not sidecar_data:
        return
    sources = sidecar_data.get("sources") or []
    urls_titles = []
    seen = set()
    for s in sources:
        u = (s.get("url") or "").strip()
        t = (s.get("title") or "").strip()
        if u and u not in seen:
            seen.add(u)
            urls_titles.append((u, t))
    if not urls_titles:
        return
    dedup_path = SYSTEM_DIR / "radar" / "previously_reported.json"
    try:
        if dedup_path.exists():
            data = json.loads(dedup_path.read_text(encoding="utf-8"))
        else:
            data = {"reported_articles": []}
        articles = data.setdefault("reported_articles", [])
        existing = {a.get("url") for a in articles if a.get("url")}
        added = 0
        today = datetime.now().strftime("%Y-%m-%d")
        for u, t in urls_titles:
            if u in existing:
                continue
            articles.append({
                "date_first_reported": today,
                "source": "user_dismissed",
                "title": t,
                "score": None,
                "cluster": None,
                "action_type": "user_dismissed",
                "url": u,
                "note": f"auto-published journal: {slug}",
            })
            existing.add(u)
            added += 1
        if added:
            tmp = dedup_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(dedup_path)
            print(f"  [dismiss] {added} source URL(s) → previously_reported.json")
    except Exception as e:  # noqa: BLE001
        print(f"  [dismiss] error: {type(e).__name__}: {e}")


def _find_hero_image(slug: str) -> str | None:
    """Public URL of the hero image, or None if no file exists in the
    expected location. Mirrors update_journal_index.find_hero_image."""
    img_dir = BLOG_DIR / "assets" / "img"
    for ext in ("jpg", "jpeg", "png", "webp"):
        p = img_dir / f"{slug}-hero.{ext}"
        if p.exists():
            return f"{SITE_BASE}/blog/assets/img/{slug}-hero.{ext}"
    return None


def _publish_one(html_path: Path, *, force: bool, dry_run: bool) -> dict:
    """Move one draft to blog/, handle sidecar + IG companion.

    Returns a dict with keys: status (published|skipped_links|error),
    title, slug, public_url, reason (if skipped), broken_links (if any).
    Plus rendering fields used by the HTML digest: subtitle, excerpt,
    section, hero_image_url.
    """
    slug = html_path.stem
    sidecar = html_path.with_suffix(".json")
    companion = html_path.with_suffix(".ig.md")
    sidecar_data = _read_sidecar(sidecar)
    title = sidecar_data.get("title") or slug
    subtitle = sidecar_data.get("subtitle") or ""
    excerpt = (
        sidecar_data.get("meta_description")
        or sidecar_data.get("excerpt")
        or ""
    )
    section = sidecar_data.get("tag_label") or sidecar_data.get("section") or ""

    result = {
        "status": "?",
        "title": title,
        "subtitle": subtitle,
        "excerpt": excerpt,
        "section": section,
        "slug": slug,
        "public_url": f"{SITE_BASE}/blog/{slug}.html",
        "hero_image_url": _find_hero_image(slug),
        "reason": "",
        "broken_links": [],
    }

    # ── Link validation gate ──
    if not force:
        ok, broken = _check_links(html_path)
        if not ok:
            result["status"] = "skipped_links"
            result["broken_links"] = broken
            result["reason"] = (
                f"{len(broken)} broken external link(s). Re-run with "
                f"--force to publish anyway, or fix the URLs in the draft."
            )
            return result

    if dry_run:
        result["status"] = "dry_run_ok"
        result["reason"] = "would publish (link check passed)"
        return result

    # ── Move article + sidecar to blog/ ──
    BLOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        # Source URLs → user_dismissed BEFORE moving (sidecar still in
        # _drafts/journal/ at this point).
        _dismiss_journal_sources(sidecar_data, slug)

        shutil.move(str(html_path), str(BLOG_DIR / html_path.name))
        if sidecar.exists():
            shutil.move(str(sidecar), str(BLOG_DIR / sidecar.name))

        # ── IG companion: keep behavior consistent with the manual
        # publish flow — leave the .ig.md in place in _drafts/journal/
        # because the IG account is not yet wired up (see the operator's
        # earlier choice). When IG goes live, change this to move it to
        # _system/social/posts/approved/<date>-ig-journal-<slug>.md.
        if companion.exists():
            print(f"  IG companion left in place: {companion.name}")

        result["status"] = "published"
        return result
    except Exception as e:  # noqa: BLE001
        result["status"] = "error"
        result["reason"] = f"move failed: {type(e).__name__}: {e}"
        return result


# ── Ready-to-send journalist pitches ─────────────────────────────────
#
# The radar produces email pitches in radar_YYYY-MM-DD.json under
# qualified[]. Each has draft.{subject, body, contact_email,
# email_source}. The dashboard's "Send now" button (one-at-a-time)
# already calls send_email.send_draft and marks the source URL as
# user_dismissed on success. This batch version replicates the same
# flow for every pitch that's "safe to auto-send":
#
#   email_source in {apollo, editorial_scraped, editorial_fallback}
#
# Excluded by policy from auto-send:
#   - apollo_likely : Apollo's "likely match" tag means the operator
#                     should eyeball the journalist name before send.
#   - pattern_guess : ~50% bounce rate historically. Reputation risk.
#   - empty/None    : no contact_email — can't send anyway.
#
# Hard safety nets layered on top (all enforced by send_email already):
#   - dry_run flag in _system/outreach/config.yml
#   - rate_limit_per_hour: 10 (refuses runaway sends; failures show up
#                              as reason='rate_limited' in errors)
#   - blacklist: refuses addresses that previously bounced

SAFE_EMAIL_SOURCES = {"apollo", "editorial_scraped", "editorial_fallback"}


def _publication_reach_label(publication: str, url: str = "") -> str:
    """Human-readable readers/month for a publication. Empty string when
    unknown. Imports the canonical PUBLICATION_REACH table from
    approve.py so the digest stays in sync with what the dashboard
    shows on every radar card.
    """
    try:
        from approve import _lookup_publication_reach, _format_reach
    except Exception:
        return ""
    millions = _lookup_publication_reach(publication or "", url or "")
    if not millions:
        return ""
    label = _format_reach(millions)
    return label or ""

_DAILY_RADAR_RX = re.compile(r"^radar_\d{4}-\d{2}-\d{2}\.json$")


def _find_latest_radar_json():
    reports = SYSTEM_DIR / "radar" / "reports"
    if not reports.exists():
        return None
    cands = sorted(f for f in reports.glob("radar_*.json")
                   if _DAILY_RADAR_RX.match(f.name))
    return cands[-1] if cands else None


def _load_dismissed_urls():
    """Return the set of URLs already marked user_dismissed (so we
    never re-send a pitch on the same article).
    """
    path = SYSTEM_DIR / "radar" / "previously_reported.json"
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    urls = set()
    for a in (data.get("reported_articles") or []):
        u = a.get("url")
        if u:
            urls.add(u)
    return urls


def _mark_pitch_url_dismissed(url, title, recipient):
    """Append the pitch's source URL to previously_reported.json so the
    radar/dashboard never re-suggest it (or auto-resend it).
    Same mechanism approve.py uses on /api/send-email success.
    """
    if not url:
        return
    path = SYSTEM_DIR / "radar" / "previously_reported.json"
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = {"reported_articles": []}
        articles = data.setdefault("reported_articles", [])
        if any(a.get("url") == url for a in articles):
            return  # idempotent
        articles.append({
            "date_first_reported": datetime.now().strftime("%Y-%m-%d"),
            "source": "user_dismissed",
            "title": title or "",
            "score": None,
            "cluster": None,
            "action_type": "user_dismissed",
            "url": url,
            "note": f"auto-sent pitch to {recipient}",
        })
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(path)
    except Exception as e:  # noqa: BLE001
        print(f"  [dismiss-url] {type(e).__name__}: {e}")


def _send_ready_pitches(*, dry_run=False, max_per_run=15):
    """Send all radar pitch emails that are safe to auto-dispatch.

    Returns a dict {sent, skipped, errors} of compact records for the
    digest email.
    """
    radar_json = _find_latest_radar_json()
    if not radar_json:
        return {"sent": [], "skipped": [], "errors": []}

    try:
        radar = json.loads(radar_json.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [pitches] failed to parse {radar_json.name}: {e}")
        return {"sent": [], "skipped": [], "errors": []}

    dismissed = _load_dismissed_urls()

    sent, skipped, errors = [], [], []
    candidates = []
    for it in (radar.get("qualified") or []):
        draft = it.get("draft") or {}
        if (draft.get("type") or "").lower() != "email":
            continue
        to_addr = (draft.get("contact_email") or "").strip()
        if not to_addr:
            continue
        candidates.append(it)

    # Sort by score descending so the best pitches go out before the
    # rate-limit gate kicks in.
    candidates.sort(
        key=lambda x: -(x.get("ai_score") or x.get("preliminary_score") or 0)
    )

    try:
        from send_email import send_draft
    except ImportError as e:
        print(f"  [pitches] send_email module unavailable: {e}")
        return {"sent": [], "skipped": [], "errors": []}

    for i, it in enumerate(candidates):
        draft = it["draft"]
        title = (it.get("title") or "?")[:80]
        to_addr = (draft.get("contact_email") or "").strip()
        source = (draft.get("email_source") or "").strip()
        url = it.get("url") or ""
        subject = (draft.get("subject") or "").strip()
        body = (draft.get("body") or "").strip()
        publication = (it.get("publication") or "").strip()

        # Already covered: don't double-send on the same article.
        if url and url in dismissed:
            skipped.append({
                "title": title, "to": to_addr,
                "reason": "already sent (URL in dedup)",
            })
            continue

        # Policy gate: only safe sources auto-send.
        if source not in SAFE_EMAIL_SOURCES:
            skipped.append({
                "title": title, "to": to_addr,
                "reason": f"risky source ({source or 'unknown'}) — needs manual review",
            })
            continue

        # Sanity: subject + body present.
        if not subject or not body:
            skipped.append({
                "title": title, "to": to_addr,
                "reason": "empty subject or body",
            })
            continue

        # Soft batch cap on top of the per-hour rate limit. Mostly a
        # belt-and-suspenders against a runaway radar that produced
        # an unusual number of qualified items in one run.
        if len(sent) >= max_per_run:
            skipped.append({
                "title": title, "to": to_addr,
                "reason": f"batch cap reached ({max_per_run}) — will retry next run",
            })
            continue

        reach_label = _publication_reach_label(publication, url)

        if dry_run:
            sent.append({
                "title": title, "to": to_addr,
                "subject": subject, "body": body,
                "publication": publication,
                "source": source, "reach": reach_label, "url": url,
            })
            continue

        # Send for real.
        try:
            result = send_draft(to=to_addr, subject=subject, body=body)
        except Exception as e:  # noqa: BLE001
            errors.append({
                "title": title, "to": to_addr,
                "error": f"{type(e).__name__}: {e}",
            })
            continue

        # send_draft returns a dict (asdict of SendResult), NOT a
        # dataclass — so dict access, not getattr. Earlier draft of
        # this code used getattr() which always hit the default and
        # mis-classified successful sends as "unknown error".
        ok = result.get("ok", False)
        result_dry = result.get("dry_run", False)
        reason = result.get("reason") or ""
        err = result.get("error") or ""

        if ok and result_dry:
            # outreach/config.yml has dry_run: true. Treat as informational.
            skipped.append({
                "title": title, "to": to_addr,
                "reason": "outreach config in dry_run mode",
            })
        elif ok:
            sent.append({
                "title": title, "to": to_addr,
                "subject": subject, "body": body,
                "publication": publication,
                "source": source, "reach": reach_label, "url": url,
            })
            # Mark URL so radar/dashboard never re-suggest this article.
            if url:
                _mark_pitch_url_dismissed(url, title, to_addr)
        elif reason == "rate_limited":
            skipped.append({
                "title": title, "to": to_addr,
                "reason": "rate-limited (10/h) — will retry next run",
            })
            # Stop sending further: the hour-bucket is full.
            print(f"  [pitches] rate limit hit at {len(sent)} sent — stopping")
            break
        elif reason == "blacklisted":
            skipped.append({
                "title": title, "to": to_addr,
                "reason": "address blacklisted (previously bounced)",
            })
        else:
            errors.append({
                "title": title, "to": to_addr,
                "error": f"{reason} {err}".strip() or "unknown",
            })

    return {"sent": sent, "skipped": skipped, "errors": errors}


# ── Digest email ─────────────────────────────────────────────────────

def _format_digest(published, skipped, errors,
                   *, pitches=None, dry_run=False) -> tuple[str, str, str]:
    """Returns (subject, plain_body, html_body) for the daily digest.

    The plain body is the legacy text version (kept for email clients
    without HTML support and as the multipart/alternative fallback).
    The HTML body is the brand-aligned visual digest, rendered with
    inline CSS (the only reliable styling in Gmail/Outlook/Apple Mail).

    `pitches` is the dict returned by _send_ready_pitches() — when
    provided, both bodies grow an "Email ai giornalisti" section.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    n_pub = len(published)
    n_pitch_sent = len((pitches or {}).get("sent") or [])
    prefix = "[DRY-RUN] " if dry_run else ""

    # Subject reflects whatever happened — both counts when both, just
    # one when only one bucket has activity.
    parts = []
    if n_pub:
        parts.append("1 articolo pubblicato" if n_pub == 1
                     else f"{n_pub} articoli pubblicati")
    if n_pitch_sent:
        parts.append("1 email inviata" if n_pitch_sent == 1
                     else f"{n_pitch_sent} email inviate")
    if parts:
        subject = f"{prefix}[My Villa] " + " · ".join(parts) + f" — {today}"
    elif skipped or errors or (pitches and (pitches.get("skipped") or pitches.get("errors"))):
        subject = f"{prefix}[My Villa] Nulla pubblicato/inviato (tutto bloccato) — {today}"
    else:
        subject = f"{prefix}[My Villa] Nessuna attività journal/outreach — {today}"

    lines = []
    lines.append("Ciao Ivo,\n")

    # ── Section 1: journal articles ──
    if n_pub:
        verb = "sarebbero pubblicati" if dry_run else "sono stati pubblicati"
        lines.append(f"\n📝 ARTICOLI PUBBLICATI ({n_pub})\n")
        lines.append(f"Questi articoli {verb} oggi sul journal:\n")
        for i, p in enumerate(published, 1):
            lines.append(f"{i}. {p['title']}")
            lines.append(f"   {p['public_url']}")
            lines.append("")
    else:
        lines.append("\n📝 ARTICOLI PUBBLICATI: nessuno oggi.\n")

    if skipped:
        lines.append("\n── Articoli bloccati (link rotti, rimangono in coda) ──\n")
        for s in skipped:
            lines.append(f"• {s['title']}")
            lines.append(f"  motivo: {s['reason']}")
            if s.get("broken_links"):
                for bl in s["broken_links"][:3]:
                    lines.append(f"    - {bl.get('url', '?')} ({bl.get('reason', '?')})")
            lines.append("")

    if errors:
        lines.append("\n── Articoli con errore (intervento manuale necessario) ──\n")
        for e in errors:
            lines.append(f"• {e['title']}: {e['reason']}")
        lines.append("")

    # ── Section 2: pitch emails to journalists (control panel) ──
    if pitches is not None:
        p_sent = pitches.get("sent") or []
        p_skip = pitches.get("skipped") or []
        p_err = pitches.get("errors") or []
        m = _compute_pitch_metrics(p_sent)
        has_today = bool(p_sent or p_skip or p_err)
        has_history = m["sent_total"] > 0
        if has_today or has_history:
            verb = "sarebbero inviate" if dry_run else "sono state inviate"
            lines.append(
                f"\n✉️  EMAIL AI GIORNALISTI — outreach control panel\n"
            )
            # KPI summary line
            deliv_str = (
                f'{m["deliverability_pct"]:.0f}%'
                if m["deliverability_pct"] is not None else "—"
            )
            lines.append(
                f"   Oggi: {m['sent_today']}  ·  "
                f"7gg: {m['sent_week']}  ·  "
                f"Totale: {m['sent_total']}\n"
                f"   Testate: {m['unique_publications']}  ·  "
                f"Risposte: {m['replies_count']}  ·  "
                f"Invalidi: {m['invalid_count']}  ·  "
                f"Consegna: {deliv_str}\n"
            )
            if p_sent:
                lines.append(f"-- Inviate oggi ({len(p_sent)}) --")
                lines.append(f"Queste pitch {verb} oggi da info@myvilla.la:\n")
                for i, p in enumerate(p_sent, 1):
                    pub = f" ({p['publication']})" if p.get("publication") else ""
                    lines.append(f"{i}. → {p['to']}{pub}")
                    lines.append(f"   subject: {p['subject']}")
                    body = (p.get("body") or "").strip()
                    if body:
                        lines.append("")
                        lines.append("   --- testo inviato ---")
                        for bl in body.splitlines():
                            lines.append(f"   {bl}" if bl else "")
                        lines.append("   --- fine ---")
                    lines.append(f"   ref: {p['title']}")
                    lines.append("")
            if m["replies_count"] > 0:
                lines.append(
                    f"-- ⚠ Da gestire — risposte ricevute ({m['replies_count']}) --"
                )
                for r in sorted(
                    m["replies"],
                    key=lambda x: x.get("received_at") or "",
                    reverse=True,
                )[:5]:
                    lines.append(
                        f"• {r.get('from_address','?')}  "
                        f"({(r.get('received_at') or '')[:10]})"
                    )
                    subj = (r.get("subject") or "")[:80]
                    lines.append(f'  "{subj}"')
                if m["replies_count"] > 5:
                    lines.append(
                        f"… +{m['replies_count'] - 5} altre. Tutte in Gmail.\n"
                    )
                else:
                    lines.append("")
            if m["invalid_count"] > 0:
                lines.append(
                    f"-- Indirizzi invalidi ({m['invalid_count']}) --"
                )
                for addr, info in list(m["invalid_addresses"].items())[:3]:
                    reason = (info.get("reason") or "")[:60]
                    lines.append(f"• {addr} — {reason}")
                if m["invalid_count"] > 3:
                    lines.append(
                        f"… +{m['invalid_count'] - 3} altri nel registro"
                    )
                lines.append("")
            if p_skip:
                lines.append(f"-- Saltate oggi ({len(p_skip)}) --")
                for s in p_skip:
                    lines.append(f"• {s.get('to', '?')}  ({s['reason']})")
                lines.append("")
            if p_err:
                lines.append(f"-- Errori invio ({len(p_err)}) --")
                for e in p_err:
                    lines.append(f"• {e.get('to', '?')}: {e['error']}")
                    lines.append(f"  ref: {e['title']}")
                lines.append("")
        else:
            lines.append("\n✉️  EMAIL AI GIORNALISTI: nessuna attività oggi.\n")

    lines.append("\n--\nAuto-published + outreach by publish_all_drafts.py")
    lines.append(f"Site: {SITE_BASE}/blog/")
    lines.append("Le pitch 'risky' (Apollo likely, pattern_guess) restano per review manuale sul dashboard.")

    plain_body = "\n".join(lines)
    html_body = _build_html_digest(
        published, skipped, errors, pitches=pitches, dry_run=dry_run,
    )
    return subject, plain_body, html_body


# ── HTML digest (brand-aligned) ──────────────────────────────────────
#
# Inline CSS only — Gmail strips <style> blocks and external stylesheets
# don't load in most email clients. Single-column 600px-wide layout is
# the safe email standard (renders on phones too).
#
# Brand palette (mirrors the website CSS variables):
#   espresso   #3E2F2B  — primary text, headings
#   terracotta #C2714F  — accents, CTAs, link color
#   tuscan-gold #C4A265 — secondary accent
#   warm-sand  #D4B896  — soft fills
#   sand-white #F0EBE3  — page background

def _html_escape(s: str) -> str:
    return html.escape(str(s or ""), quote=True)


# Domain blacklist: addresses to these belong to *us*, not journalists.
# Used by metrics to avoid counting our own digest/smoke-test sends as
# outreach.
_INTERNAL_DOMAINS = {
    "me.com",        # ivolo@me.com — daily digest recipient
    "myvilla.la",    # info@myvilla.la — our outbound + self test
    "gmail.com",     # ivo.giuliani@gmail.com — smoke tests
    "example.com",   # dry-run smoke test default
}


def _compute_pitch_metrics(today_sent: list) -> dict:
    """Build aggregate KPIs for the outreach control panel.

    Reads three sources of truth:
      • send_log.jsonl       — every Gmail send (real + dry-run)
      • replies_log.jsonl    — replies & bounces detected by the
                                Gmail reply-watcher
      • invalid_addresses.json — hard-bounce registry

    Internal mail (digest to self, smoke tests) is filtered out via
    _INTERNAL_DOMAINS so the counters reflect *actual* journalist
    outreach.

    Today_sent is the live list of pitches sent in the current run —
    counted separately from the historical log to avoid double-counting
    when the log row hasn't been flushed yet.
    """
    outreach_dir = SYSTEM_DIR / "outreach"

    def _domain(addr: str) -> str:
        return addr.split("@", 1)[1].lower() if "@" in addr else ""

    def _is_internal(addr: str) -> bool:
        return _domain(addr) in _INTERNAL_DOMAINS

    # ── send_log.jsonl ─────────────────────────────────────────────
    all_real_sends = []
    sends_today_hist = []
    today_iso = datetime.now().strftime("%Y-%m-%d")
    log_path = outreach_dir / "send_log.jsonl"
    if log_path.exists():
        try:
            for raw in log_path.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not row.get("ok"):
                    continue
                if row.get("dry_run"):
                    continue
                to = (row.get("to") or "").strip().lower()
                if not to or _is_internal(to):
                    continue
                all_real_sends.append(row)
                if (row.get("timestamp") or "").startswith(today_iso):
                    sends_today_hist.append(row)
        except OSError:
            pass

    # ── replies_log.jsonl: separate real replies from bounces ──────
    # Three buckets:
    #   • bounce_events: from mailer-daemon/postmaster
    #   • internal_echoes: from ivolo@me.com etc. (Ivo replying to
    #     his own copy, or any thread chatter inside our domains —
    #     NOT a journalist reply, doesn't need handling)
    #   • real_replies: actual third-party replies (the ones that
    #     show up in the "Da gestire" panel)
    replies_path = outreach_dir / "replies_log.jsonl"
    real_replies = []
    bounce_events = []
    if replies_path.exists():
        try:
            for raw in replies_path.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                fr = (row.get("from_address") or "").lower()
                if "mailer-daemon" in fr or "postmaster" in fr:
                    bounce_events.append(row)
                elif _is_internal(fr):
                    # Ivo replying from his own address — not a
                    # journalist response, skip.
                    continue
                else:
                    real_replies.append(row)
        except OSError:
            pass

    # ── invalid_addresses.json ─────────────────────────────────────
    invalid_addresses = {}
    invalid_path = outreach_dir / "invalid_addresses.json"
    if invalid_path.exists():
        try:
            data = json.loads(invalid_path.read_text(encoding="utf-8"))
            invalid_addresses = data.get("addresses") or {}
        except (OSError, json.JSONDecodeError):
            pass

    # ── rollups ────────────────────────────────────────────────────
    # today_sent (live) is authoritative for "today". If anything
    # snuck into the log already, prefer the live list — same count
    # but cleaner per-row data for display.
    sent_today = len(today_sent or [])

    # past 7 days from the log (excludes today if today's log row
    # hasn't been written yet — but today is already counted above)
    from datetime import timedelta
    week_cutoff = (datetime.now() - timedelta(days=7)).isoformat()
    sent_week = sum(
        1 for s in all_real_sends
        if (s.get("timestamp") or "") >= week_cutoff
    )
    sent_total = len(all_real_sends)

    domains = {_domain(s.get("to") or "") for s in all_real_sends if s.get("to")}
    domains.discard("")
    unique_publications = len(domains)

    recipients = {(s.get("to") or "").lower() for s in all_real_sends}
    recipients.discard("")
    unique_recipients = len(recipients)

    # Deliverability rate: % of sends that didn't bounce.
    bounced_addrs = set(invalid_addresses.keys())
    delivered = sum(
        1 for s in all_real_sends
        if (s.get("to") or "").lower() not in bounced_addrs
    )
    deliverability = (delivered / sent_total * 100) if sent_total else None

    return {
        "sent_today": sent_today,
        "sent_week": sent_week,
        "sent_total": sent_total,
        "unique_publications": unique_publications,
        "unique_recipients": unique_recipients,
        "invalid_count": len(invalid_addresses),
        "invalid_addresses": invalid_addresses,
        "replies_count": len(real_replies),
        "replies": real_replies,
        "bounces_count": len(bounce_events),
        "deliverability_pct": deliverability,
    }


def _html_outreach_panel(pitches: dict, *, dry_run: bool) -> str:
    """Outreach control panel: KPI grid + sent-today + needs-attention.

    Returns HTML table-rows ready to be embedded in the digest <table>.
    Returns empty string when there's literally nothing to say (no
    sends today, no historical data, no replies).
    """
    sent = pitches.get("sent") or []
    p_skip = pitches.get("skipped") or []
    p_err = pitches.get("errors") or []
    m = _compute_pitch_metrics(sent)

    # Touch breakdown for today's sends.
    # "rescue" cold pitches are Touch 1 sends that came from the
    # author-rescue path — they carry a `rescue_of` field with the
    # parent alias they replaced. We surface them separately because
    # they're newsworthy (channel upgrade!) and visually distinct.
    rescue_today = [s for s in sent if s.get("rescue_of")]
    cold_today = [s for s in sent
                  if (s.get("touch_n") or 1) == 1 and not s.get("rescue_of")]
    t2_today = [s for s in sent if (s.get("touch_n") or 1) == 2]
    t3_today = [s for s in sent if (s.get("touch_n") or 1) == 3]

    # Follow-up ledger status (passed from publish_all_drafts via the
    # pitches dict — see _ledger_status injection there).
    ledger_status = pitches.get("_ledger_status") or {}
    in_cadence = ledger_status.get("in_cadence", 0)
    exhausted = ledger_status.get("exhausted", 0)

    has_history = m["sent_total"] > 0
    has_today_activity = bool(sent or p_skip or p_err)
    if not has_history and not has_today_activity:
        return ""

    # ── Section header ─────────────────────────────────────────────
    header = (
        '<tr><td style="padding:28px 32px 4px 32px;'
        'border-top:1px solid #D4B896;">'
        '<h2 style="margin:14px 0 4px;font-family:Georgia,serif;'
        'font-size:20px;font-weight:normal;color:#3E2F2B;'
        'letter-spacing:0.02em;">'
        '<span style="color:#C2714F;">✉</span> Email ai giornalisti'
        '</h2>'
        '<div style="font-family:-apple-system,sans-serif;font-size:12px;'
        'color:#888;letter-spacing:0.04em;margin-bottom:4px;">'
        'Stato outreach giornalistico — oggi e nel tempo'
        '</div></td></tr>'
    )

    # ───────────────────────────────────────────────────────────────
    # BLOCK 1: OGGI — situazione del giorno
    # ───────────────────────────────────────────────────────────────
    today_total = len(sent)
    today_section_title = (
        '<tr><td style="padding:20px 32px 6px 32px;">'
        '<div style="font-family:-apple-system,sans-serif;'
        'font-size:11px;letter-spacing:0.16em;text-transform:uppercase;'
        'color:#5C6B4F;font-weight:700;">'
        '◆ OGGI</div>'
        '</td></tr>'
    )

    # Big "N mail inviate oggi" hero card
    today_hero = (
        '<tr><td style="padding:6px 24px 4px 24px;">'
        '<div style="background:#FAF6F0;border-radius:4px;'
        'border-left:4px solid #5C6B4F;padding:18px 22px;">'
        '<div style="font-family:Georgia,serif;font-size:42px;'
        'font-weight:normal;color:#3E2F2B;line-height:1;">'
        f'{today_total}</div>'
        '<div style="font-family:-apple-system,sans-serif;'
        'font-size:13px;color:#5C6B4F;letter-spacing:0.04em;'
        'margin-top:6px;">'
        f'{"mail inviata" if today_total==1 else "mail inviate"} ai giornalisti'
        '</div></div></td></tr>'
    )

    # Breakdown by type: 4 mini-cards in a row, only showing the ones
    # that have any activity today. Plain-language labels.
    def _mini_card(value: int, label: str, sublabel: str,
                   accent: str) -> str | None:
        if value <= 0:
            return None
        return (
            '<td valign="top" align="center" '
            'style="padding:6px;">'
            '<div style="background:#FFFFFF;border:1px solid #E8DECE;'
            f'border-top:3px solid {accent};border-radius:3px;'
            'padding:10px 8px;">'
            f'<div style="font-family:Georgia,serif;font-size:22px;'
            f'font-weight:normal;color:{accent};line-height:1;">'
            f'{value}</div>'
            '<div style="font-family:-apple-system,sans-serif;'
            'font-size:10px;color:#3E2F2B;font-weight:600;margin-top:4px;'
            'letter-spacing:0.04em;">'
            f'{_html_escape(label)}</div>'
            + (
                '<div style="font-family:-apple-system,sans-serif;'
                'font-size:9px;color:#888;margin-top:2px;'
                'letter-spacing:0.02em;">'
                f'{_html_escape(sublabel)}</div>' if sublabel else ''
            )
            + '</div></td>'
        )

    breakdown_cells = []
    for cell in (
        _mini_card(len(cold_today), "Prime email",
                   "nuovi giornalisti", "#5C6B4F"),
        _mini_card(len(t2_today), "Seconde email",
                   "dopo 7 giorni di silenzio", "#A65E1F"),
        _mini_card(len(t3_today), "Ultime email",
                   "invito a call con Paolo", "#8B3A1F"),
        _mini_card(len(rescue_today), "Giornalisti identificati",
                   "al posto degli alias redazione", "#2E5D6B"),
    ):
        if cell:
            breakdown_cells.append(cell)

    today_breakdown = ""
    if breakdown_cells:
        today_breakdown = (
            '<tr><td style="padding:0 24px 10px 24px;">'
            '<table role="presentation" width="100%" cellspacing="0" '
            'cellpadding="0" border="0"><tr>'
            + "".join(breakdown_cells) +
            '</tr></table></td></tr>'
        )

    # ───────────────────────────────────────────────────────────────
    # BLOCK 2: PIPELINE STATUS — stacked bar + legend
    # ───────────────────────────────────────────────────────────────
    replied_count = m["replies_count"]
    invalid_count = m["invalid_count"]
    pipeline_total = max(
        in_cadence + exhausted + invalid_count + replied_count, 1
    )

    def _bar_segment(value: int, color: str, label_short: str) -> str:
        if value <= 0:
            return ""
        # min 6% so tiny segments still get a label
        pct = max(round(value * 100 / pipeline_total), 6)
        return (
            f'<td width="{pct}%" valign="middle" align="center" '
            f'style="background:{color};color:#FFFFFF;'
            f'font-family:-apple-system,sans-serif;font-size:11px;'
            f'font-weight:600;height:32px;padding:0 4px;'
            f'letter-spacing:0.04em;white-space:nowrap;">'
            f'{value}</td>'
        )

    # Colors picked for fast at-a-glance reading:
    #   green   = active / good (in conversation)
    #   taupe   = neutral closed (3 touches done)
    #   amber   = action needed (replied — go answer them!)
    #   red     = error (bad address)
    # Old palette had three earth-tones that read as one blob.
    PIPE_GREEN = "#6B8E5C"   # fresh sage green (clear positive)
    PIPE_NEUTRAL = "#9B8E7A" # warm taupe (closed, no action)
    PIPE_AMBER = "#D97F4A"   # vivid terracotta (action!)
    PIPE_RED = "#B8463F"     # clean red (error)

    pipeline_bar_cells = (
        _bar_segment(in_cadence, PIPE_GREEN, "")
        + _bar_segment(exhausted, PIPE_NEUTRAL, "")
        + _bar_segment(replied_count, PIPE_AMBER, "")
        + _bar_segment(invalid_count, PIPE_RED, "")
    )

    def _legend_row(value: int, color: str, label: str, hint: str) -> str:
        if value <= 0:
            return ""
        return (
            '<tr>'
            '<td width="14" valign="middle" style="padding:4px 0;">'
            f'<div style="width:10px;height:10px;background:{color};'
            'border-radius:2px;"></div>'
            '</td>'
            '<td valign="middle" style="padding:4px 8px;'
            'font-family:-apple-system,sans-serif;font-size:12px;'
            'color:#3E2F2B;">'
            f'<strong>{value}</strong> {_html_escape(label)} '
            f'<span style="color:#888;font-size:11px;">'
            f'— {_html_escape(hint)}</span>'
            '</td></tr>'
        )

    legend_rows = (
        _legend_row(in_cadence, PIPE_GREEN,
                    "giornalisti in conversazione",
                    "riceveranno seconda o ultima email")
        + _legend_row(exhausted, PIPE_NEUTRAL,
                      "conversazioni concluse",
                      "3 email inviate, stop totale")
        + _legend_row(replied_count, PIPE_AMBER,
                      "hanno risposto",
                      "da gestire a mano in Gmail")
        + _legend_row(invalid_count, PIPE_RED,
                      "indirizzi non validi",
                      "bounce, in blacklist")
    )

    pipeline_section = ""
    if pipeline_total > 1 or in_cadence or exhausted:
        pipeline_section = (
            '<tr><td style="padding:24px 32px 6px 32px;">'
            '<div style="font-family:-apple-system,sans-serif;'
            'font-size:11px;letter-spacing:0.16em;text-transform:uppercase;'
            'color:#3E2F2B;font-weight:700;">'
            f'◆ STATO PIPELINE — {pipeline_total} giornalisti in totale'
            '</div></td></tr>'
            '<tr><td style="padding:6px 32px 4px 32px;">'
            '<table role="presentation" width="100%" cellspacing="0" '
            'cellpadding="0" border="0" '
            'style="border-radius:3px;overflow:hidden;'
            'border:1px solid #D4B896;"><tr>'
            + pipeline_bar_cells +
            '</tr></table></td></tr>'
            '<tr><td style="padding:8px 32px 6px 32px;">'
            '<table role="presentation" cellspacing="0" cellpadding="0" '
            'border="0">'
            + legend_rows +
            '</table></td></tr>'
        )

    # ───────────────────────────────────────────────────────────────
    # BLOCK 3: TOTALI DA SEMPRE — lifetime stats
    # ───────────────────────────────────────────────────────────────
    deliv_str = (
        f'{m["deliverability_pct"]:.0f}%'
        if m["deliverability_pct"] is not None else "—"
    )

    def _stat_cell(value: str, label: str) -> str:
        return (
            '<td width="33%" valign="top" align="center" '
            'style="padding:8px;">'
            '<div style="background:#FFFFFF;border:1px solid #E8DECE;'
            'border-radius:3px;padding:12px 8px;">'
            '<div style="font-family:Georgia,serif;font-size:22px;'
            'font-weight:normal;color:#3E2F2B;line-height:1;">'
            f'{_html_escape(value)}</div>'
            '<div style="font-family:-apple-system,sans-serif;'
            'font-size:10px;color:#888;font-weight:600;margin-top:6px;'
            'letter-spacing:0.06em;text-transform:uppercase;">'
            f'{_html_escape(label)}</div>'
            '</div></td>'
        )

    stats_section = (
        '<tr><td style="padding:24px 32px 6px 32px;">'
        '<div style="font-family:-apple-system,sans-serif;'
        'font-size:11px;letter-spacing:0.16em;text-transform:uppercase;'
        'color:#3E2F2B;font-weight:700;">'
        '◆ TOTALI DA SEMPRE</div>'
        '</td></tr>'
        '<tr><td style="padding:0 24px 8px 24px;">'
        '<table role="presentation" width="100%" cellspacing="0" '
        'cellpadding="0" border="0"><tr>'
        + _stat_cell(str(m["sent_total"]), "Email inviate")
        + _stat_cell(str(m["unique_publications"]), "Testate raggiunte")
        + _stat_cell(deliv_str, "Consegna riuscita")
        + '</tr></table></td></tr>'
    )

    # Order: OGGI first (azione immediata → vede subito cosa è successo),
    # then TOTALI (numeri lifetime), then PIPELINE (stato strutturale
    # — context, va in fondo perché meno actionable).
    kpi_grid = (
        today_section_title + today_hero + today_breakdown
        + stats_section
        + pipeline_section
    )

    # ── Sent today, grouped by touch type ──────────────────────────
    # Cold pitches → T2 follow-ups → T3 final follow-ups. Each group
    # gets its own sub-header so you can tell at a glance what each
    # message is.
    sent_block = ""
    if sent:
        def _group_section(label: str, color: str, items: list) -> str:
            if not items:
                return ""
            sub = (
                f'<tr><td style="padding:14px 32px 4px 32px;">'
                f'<div style="font-family:-apple-system,sans-serif;'
                f'font-size:11px;letter-spacing:0.12em;text-transform:uppercase;'
                f'color:{color};font-weight:600;">{label} ({len(items)})</div>'
                f'</td></tr>'
            )
            return sub + "".join(_html_pitch_row(s, dry_run=dry_run)
                                 for s in items)

        sent_block = (
            _group_section("Prime email inviate oggi — nuovi giornalisti",
                            "#5C6B4F", cold_today)
            + _group_section(
                "🆙 Giornalisti identificati — al posto di alias redazione",
                "#2E5D6B", rescue_today)
            + _group_section(
                "Seconde email — un dato concreto in più (dopo 7 giorni)",
                "#A65E1F", t2_today)
            + _group_section(
                "Ultime email — invito a call con Paolo (dopo 14 giorni)",
                "#8B3A1F", t3_today)
        )

    # ── Da gestire (real replies) ──────────────────────────────────
    handle_block = ""
    if m["replies_count"] > 0:
        # Show up to 5 most recent
        recent = sorted(
            m["replies"],
            key=lambda r: r.get("received_at") or "",
            reverse=True,
        )[:5]
        rows = ""
        for r in recent:
            fr = _html_escape(r.get("from_address") or "?")
            subj = _html_escape((r.get("subject") or "")[:80])
            when = (r.get("received_at") or "")[:10]
            rows += (
                f'<tr><td style="padding:6px 32px;'
                f'font-family:-apple-system,sans-serif;font-size:13px;">'
                f'<span style="color:#C2714F;">↩</span> '
                f'<a href="mailto:{fr}" style="color:#3E2F2B;'
                f'text-decoration:none;font-weight:600;">{fr}</a>'
                f' <span style="color:#888;font-size:11px;">'
                f'· {_html_escape(when)}</span>'
                f'<div style="color:#666;font-size:12px;margin-top:2px;'
                f'font-style:italic;">"{subj}"</div>'
                f'</td></tr>'
            )
        more_note = ""
        if m["replies_count"] > 5:
            more_note = (
                f'<tr><td style="padding:4px 32px 10px;color:#888;'
                f'font-family:-apple-system,sans-serif;font-size:11px;">'
                f'… e altre {m["replies_count"] - 5}. Tutte le risposte '
                f'in Gmail.</td></tr>'
            )
        handle_block = (
            '<tr><td style="padding:14px 32px 4px 32px;">'
            '<div style="font-family:-apple-system,sans-serif;'
            'font-size:11px;letter-spacing:0.12em;text-transform:uppercase;'
            f'color:#C2714F;font-weight:600;">'
            f'📬 Risposte da gestire ({m["replies_count"]}) — '
            'rispondi a mano da Gmail'
            '</div></td></tr>' + rows + more_note
        )

    # ── Rescue drafts pending manual review ────────────────────────
    # The author-rescue path queues pattern-guess hits (low confidence)
    # in rescue_drafts.jsonl instead of auto-sending. Surface them here
    # so the user can pick them up.
    rescue_drafts_block = ""
    rescue_drafts_path = SYSTEM_DIR / "outreach" / "rescue_drafts.jsonl"
    if rescue_drafts_path.exists():
        pending = []
        try:
            for raw in rescue_drafts_path.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    e = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if e.get("status") == "pending_review":
                    pending.append(e)
        except OSError:
            pending = []
        if pending:
            rows = ""
            for e in pending[-5:]:  # most recent 5
                rows += (
                    f'<tr><td style="padding:6px 32px;'
                    f'font-family:-apple-system,sans-serif;font-size:13px;">'
                    f'<span style="color:#A85D3F;">🆙</span> '
                    f'<strong style="color:#3E2F2B;">'
                    f'{_html_escape(e.get("to","?"))}</strong> '
                    f'<span style="color:#888;font-size:11px;">'
                    f'· pattern-guess da {_html_escape(e.get("from_alias","?"))}'
                    f'</span>'
                    f'<div style="color:#666;font-size:12px;margin-top:2px;">'
                    f'{_html_escape((e.get("to_name") or "?"))} — '
                    f'{_html_escape((e.get("publication") or "?"))}'
                    f'</div>'
                    f'</td></tr>'
                )
            more_note = ""
            if len(pending) > 5:
                more_note = (
                    f'<tr><td style="padding:4px 32px 10px;color:#888;'
                    f'font-family:-apple-system,sans-serif;font-size:11px;">'
                    f'… +{len(pending) - 5} altre nel rescue_drafts.jsonl'
                    f'</td></tr>'
                )
            rescue_drafts_block = (
                '<tr><td style="padding:14px 32px 4px 32px;">'
                '<div style="font-family:-apple-system,sans-serif;'
                'font-size:11px;letter-spacing:0.12em;text-transform:uppercase;'
                f'color:#A65E1F;font-weight:600;">'
                f'🆙 Giornalisti identificati — email da validare ({len(pending)})'
                '</div>'
                '<div style="font-family:-apple-system,sans-serif;'
                'font-size:11px;color:#888;margin-top:2px;font-style:italic;">'
                'Nome trovato sull\'articolo, email indovinata dal pattern '
                'comune della testata. Verifica e approva dal dashboard radar.'
                '</div></td></tr>' + rows + more_note
            )

    # ── Invalid addresses (collapsed summary) ──────────────────────
    invalid_block = ""
    if m["invalid_count"] > 0:
        # show first 3 addresses with reason snippet
        items = list(m["invalid_addresses"].items())[:3]
        rows = ""
        for addr, info in items:
            reason = (info.get("reason") or "")[:60]
            rows += (
                f'<tr><td style="padding:4px 32px;color:#888;'
                f'font-family:-apple-system,sans-serif;font-size:12px;">'
                f'• <span style="color:#a85d3f;text-decoration:line-through;">'
                f'{_html_escape(addr)}</span> '
                f'<span style="color:#aaa;font-size:11px;">'
                f'— {_html_escape(reason)}</span>'
                f'</td></tr>'
            )
        if m["invalid_count"] > 3:
            rows += (
                f'<tr><td style="padding:4px 32px;color:#aaa;'
                f'font-family:-apple-system,sans-serif;font-size:11px;'
                f'font-style:italic;">'
                f'… +{m["invalid_count"] - 3} altri indirizzi nel registro'
                f'</td></tr>'
            )
        invalid_block = (
            '<tr><td style="padding:14px 32px 4px 32px;">'
            '<div style="font-family:-apple-system,sans-serif;'
            'font-size:11px;letter-spacing:0.12em;text-transform:uppercase;'
            'color:#a85d3f;font-weight:600;">'
            f'Indirizzi non validi ({m["invalid_count"]}) — '
            f'bounce permanente, non più contattati'
            '</div></td></tr>' + rows
        )

    # ── Saltate / Errori (compact tail) ────────────────────────────
    tail = ""
    if p_skip:
        rows = "".join(
            f'<tr><td style="padding:3px 32px;color:#aaa;'
            f'font-family:-apple-system,sans-serif;font-size:12px;">'
            f'• <a href="mailto:{_html_escape(s.get("to","?"))}" '
            f'style="color:#999;text-decoration:none;">'
            f'{_html_escape(s.get("to","?"))}</a> — '
            f'<span style="color:#bbb;">{_html_escape(s["reason"])}</span>'
            f'</td></tr>'
            for s in p_skip
        )
        tail += (
            '<tr><td style="padding:14px 32px 4px 32px;">'
            '<div style="font-family:-apple-system,sans-serif;'
            'font-size:11px;letter-spacing:0.10em;text-transform:uppercase;'
            f'color:#888;font-weight:600;">Saltate oggi ({len(p_skip)})'
            '</div></td></tr>' + rows
        )
    if p_err:
        rows = "".join(
            f'<tr><td style="padding:3px 32px;color:#a85d3f;'
            f'font-family:-apple-system,sans-serif;font-size:12px;">'
            f'• {_html_escape(e.get("to","?"))}: {_html_escape(e["error"])}'
            f'</td></tr>'
            for e in p_err
        )
        tail += (
            '<tr><td style="padding:14px 32px 4px 32px;">'
            '<div style="font-family:-apple-system,sans-serif;'
            'font-size:11px;letter-spacing:0.10em;text-transform:uppercase;'
            f'color:#a85d3f;font-weight:600;">'
            f'Errori invio ({len(p_err)})</div></td></tr>' + rows
        )

    return (header + kpi_grid + sent_block + handle_block
            + rescue_drafts_block + invalid_block + tail)


def _build_html_digest(published, skipped, errors,
                      *, pitches=None, dry_run=False) -> str:
    today = datetime.now().strftime("%A, %B %-d, %Y")
    dry_banner = ""
    if dry_run:
        dry_banner = (
            '<tr><td style="padding:0 32px 12px 32px;">'
            '<div style="background:#FFF6E0;border:1px solid #C4A265;'
            'color:#6B4A00;padding:10px 14px;border-radius:4px;'
            'font-family:-apple-system,sans-serif;font-size:12px;'
            'letter-spacing:0.04em;text-transform:uppercase;'
            'text-align:center;">'
            'Dry-run preview — nothing sent</div></td></tr>'
        )

    article_cards = ""
    for p in published:
        article_cards += _html_article_card(p, dry_run=dry_run)
    if not published:
        article_cards = (
            '<tr><td style="padding:12px 32px;color:#888;'
            'font-family:-apple-system,sans-serif;font-size:14px;'
            'font-style:italic;">'
            'Nessun articolo pubblicato oggi.</td></tr>'
        )

    skipped_block = ""
    if skipped or errors:
        rows = ""
        for s in skipped:
            rows += (
                f'<tr><td style="padding:6px 32px;color:#a85d3f;'
                f'font-family:-apple-system,sans-serif;font-size:13px;">'
                f'• <strong>{_html_escape(s["title"])}</strong><br>'
                f'<span style="color:#888;font-size:12px;">'
                f'{_html_escape(s["reason"])}</span>'
                f'</td></tr>'
            )
        for e in errors:
            rows += (
                f'<tr><td style="padding:6px 32px;color:#a85d3f;'
                f'font-family:-apple-system,sans-serif;font-size:13px;">'
                f'• <strong>{_html_escape(e["title"])}</strong>: '
                f'{_html_escape(e["reason"])}'
                f'</td></tr>'
            )
        skipped_block = (
            '<tr><td style="padding:18px 32px 6px 32px;">'
            '<div style="font-family:-apple-system,sans-serif;'
            'font-size:11px;letter-spacing:0.12em;text-transform:uppercase;'
            'color:#888;font-weight:600;">'
            'Articoli bloccati o con errore</div></td></tr>' + rows
        )

    pitch_block = ""
    if pitches is not None:
        # Now a full control panel: KPI grid + sent-today + replies to
        # handle + invalid-address registry + skipped/errors tail.
        pitch_block = _html_outreach_panel(pitches, dry_run=dry_run)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>My Villa — Daily digest</title>
</head>
<body style="margin:0;padding:0;background:#F0EBE3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#F0EBE3;">
<tr><td align="center" style="padding:32px 16px;">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" border="0" style="background:#FFFFFF;border-radius:6px;box-shadow:0 2px 10px rgba(62,47,43,0.08);max-width:600px;width:100%;">

  <!-- Brand header -->
  <tr><td align="center" style="padding:40px 32px 24px;border-bottom:1px solid #D4B896;">
    <div style="font-family:Georgia,'Cormorant Garamond',serif;font-size:30px;font-weight:normal;letter-spacing:0.18em;color:#3E2F2B;">
      MY <span style="color:#C2714F;">VILLA</span>
    </div>
    <div style="font-family:-apple-system,sans-serif;font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#888;margin-top:6px;">
      Italian Soul, Californian Body
    </div>
    <div style="margin-top:18px;font-family:-apple-system,sans-serif;font-size:12px;color:#5C6B4F;letter-spacing:0.06em;">
      {_html_escape(today)} — daily journal &amp; outreach digest
    </div>
  </td></tr>

  {dry_banner}

  <!-- Journal articles -->
  <tr><td style="padding:24px 32px 0;">
    <h2 style="margin:0 0 4px;font-family:Georgia,serif;font-size:20px;font-weight:normal;color:#3E2F2B;letter-spacing:0.02em;">
      <span style="color:#C2714F;">📝</span> Articoli pubblicati
      <span style="color:#888;font-size:14px;font-weight:normal;">({len(published)})</span>
    </h2>
  </td></tr>
  {article_cards}
  {skipped_block}

  {pitch_block}

  <!-- Footer -->
  <tr><td align="center" style="padding:28px 32px 32px;border-top:1px solid #D4B896;background:#FAF6F0;">
    <div style="font-family:Georgia,serif;font-size:13px;color:#3E2F2B;letter-spacing:0.04em;">
      <a href="{SITE_BASE}/blog/" style="color:#C2714F;text-decoration:none;border-bottom:1px solid rgba(194,113,79,0.4);">
        myvilla.la/blog
      </a>
    </div>
    <div style="font-family:-apple-system,sans-serif;font-size:10px;color:#999;margin-top:8px;letter-spacing:0.04em;">
      Auto-published &middot; outreach panel aggiornato in tempo reale dal log Gmail
    </div>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""


def _html_article_card(p: dict, *, dry_run: bool) -> str:
    """One row in the articles table — hero image + title + excerpt.

    When `hero_image_url` is missing (the article was generated
    without a hero — rare but possible: image search can fail), we
    fall back to a brand-aligned placeholder block so the digest
    layout stays consistent and the card doesn't look broken.
    """
    hero = p.get("hero_image_url")
    if hero:
        img_block = (
            f'<img src="{_html_escape(hero)}" alt="" '
            f'width="540" '
            f'style="display:block;width:100%;max-width:540px;height:auto;'
            f'border-radius:4px;margin-bottom:14px;border:0;outline:none;"/>'
        )
    else:
        # Placeholder: brand gradient + section label + MV monogram.
        # Inline CSS so Gmail keeps the gradient (some clients strip
        # CSS gradients; the fallback is a solid terracotta block).
        section_text = (p.get("section") or "MY VILLA").upper()
        img_block = (
            f'<div style="display:block;width:100%;max-width:540px;'
            f'height:200px;background:#C2714F;'
            f'background:linear-gradient(135deg,#C2714F 0%,#C4A265 100%);'
            f'border-radius:4px;margin-bottom:14px;'
            f'text-align:center;line-height:200px;'
            f'font-family:Georgia,serif;font-size:13px;'
            f'letter-spacing:0.2em;color:#FAF8F5;'
            f'text-transform:uppercase;">'
            f'<span style="display:inline-block;line-height:1.5;'
            f'vertical-align:middle;">'
            f'<span style="font-family:Georgia,serif;font-size:28px;'
            f'letter-spacing:0.16em;display:block;margin-bottom:6px;">'
            f'MY <span style="color:#FFE8D6;">VILLA</span></span>'
            f'<span style="font-size:11px;opacity:0.9;">'
            f'{_html_escape(section_text)}</span>'
            f'</span></div>'
        )
    section_pill = ""
    if p.get("section"):
        section_pill = (
            f'<span style="display:inline-block;background:#F0EBE3;'
            f'color:#5C6B4F;font-family:-apple-system,sans-serif;'
            f'font-size:10px;letter-spacing:0.12em;text-transform:uppercase;'
            f'padding:3px 10px;border-radius:3px;margin-bottom:10px;">'
            f'{_html_escape(p["section"])}</span>'
        )
    excerpt = p.get("excerpt") or p.get("subtitle") or ""
    excerpt_html = ""
    if excerpt:
        excerpt_html = (
            f'<p style="margin:8px 0 14px;font-family:-apple-system,sans-serif;'
            f'font-size:14px;line-height:1.55;color:#555;">'
            f'{_html_escape(excerpt)}</p>'
        )
    cta_label = "Leggi sul sito →" if not dry_run else "Anteprima →"
    return f"""
  <tr><td style="padding:16px 32px 8px;">
    {img_block}
    {section_pill}
    <h3 style="margin:0;font-family:Georgia,serif;font-size:18px;font-weight:normal;color:#3E2F2B;line-height:1.3;">
      <a href="{_html_escape(p['public_url'])}" style="color:#3E2F2B;text-decoration:none;">
        {_html_escape(p['title'])}
      </a>
    </h3>
    {excerpt_html}
    <a href="{_html_escape(p['public_url'])}" style="display:inline-block;font-family:-apple-system,sans-serif;font-size:12px;color:#C2714F;text-decoration:none;letter-spacing:0.08em;text-transform:uppercase;font-weight:600;border-bottom:1px solid rgba(194,113,79,0.5);padding-bottom:1px;">
      {cta_label}
    </a>
  </td></tr>
  <tr><td style="padding:12px 32px;"><hr style="border:none;border-top:1px solid #F0EBE3;margin:0;"></td></tr>
"""


def _html_pitch_row(p: dict, *, dry_run: bool) -> str:
    """One row per sent pitch — recipient + publication + reach."""
    reach = p.get("reach") or ""
    reach_pill = ""
    if reach:
        reach_pill = (
            f'<span style="display:inline-block;background:#FFF6E0;'
            f'color:#6B4A00;font-family:-apple-system,sans-serif;'
            f'font-size:11px;font-weight:600;padding:2px 8px;'
            f'border-radius:3px;margin-left:8px;">'
            f'👥 {_html_escape(reach)}</span>'
        )
    pub = p.get("publication") or ""
    pub_html = (
        f'<span style="color:#5C6B4F;font-weight:600;">{_html_escape(pub)}</span>'
        if pub else
        '<span style="color:#888;font-style:italic;">(publication unknown)</span>'
    )

    # Touch indicator pill — distinguishes cold pitch (T1) from
    # follow-up T2 (data bump) and T3 (founder call).
    # A rescued cold pitch is a Touch 1 but visually distinct.
    touch_pill = ""
    touch_n = p.get("touch_n") or 1
    if p.get("rescue_of"):
        touch_pill = (
            f'<span style="display:inline-block;background:#E0F0F5;'
            f'color:#2E5D6B;font-family:-apple-system,sans-serif;'
            f'font-size:10px;font-weight:600;padding:2px 7px;'
            f'border-radius:3px;margin-left:8px;letter-spacing:0.04em;">'
            f'🆙 GIORNALISTA IDENTIFICATO (era {_html_escape(p["rescue_of"])})</span>'
        )
    elif touch_n == 2:
        touch_pill = (
            '<span style="display:inline-block;background:#FFF3E0;'
            'color:#A65E1F;font-family:-apple-system,sans-serif;'
            'font-size:10px;font-weight:600;padding:2px 7px;'
            'border-radius:3px;margin-left:8px;letter-spacing:0.04em;">'
            '🔁 SECONDA EMAIL</span>'
        )
    elif touch_n == 3:
        touch_pill = (
            '<span style="display:inline-block;background:#F3E5DD;'
            'color:#8B3A1F;font-family:-apple-system,sans-serif;'
            'font-size:10px;font-weight:600;padding:2px 7px;'
            'border-radius:3px;margin-left:8px;letter-spacing:0.04em;">'
            '📞 ULTIMA EMAIL</span>'
        )

    # Body of the pitch, rendered as a "letter" block. Plain text → HTML:
    # paragraphs split on double-newline, single newlines become <br>.
    body_block = ""
    body = (p.get("body") or "").strip()
    if body:
        paragraphs = [b.strip() for b in body.split("\n\n") if b.strip()]
        body_paras_html = "".join(
            f'<p style="margin:0 0 10px;font-family:Georgia,serif;'
            f'font-size:13px;line-height:1.65;color:#3E2F2B;'
            f'white-space:pre-wrap;">{_html_escape(para)}</p>'
            for para in paragraphs
        )
        body_block = (
            f'<div style="margin-top:10px;padding:14px 16px;'
            f'background:#FAF6F0;border-left:3px solid #C4A265;'
            f'border-radius:0 4px 4px 0;">'
            f'<div style="font-family:-apple-system,sans-serif;'
            f'font-size:10px;letter-spacing:0.10em;text-transform:uppercase;'
            f'color:#888;font-weight:600;margin-bottom:8px;">'
            f'Testo inviato</div>'
            f'{body_paras_html}'
            f'</div>'
        )

    sent_label = "sarebbe inviata" if dry_run else "inviata"
    return f"""
  <tr><td style="padding:10px 32px;">
    <div style="font-family:-apple-system,sans-serif;font-size:14px;color:#3E2F2B;line-height:1.45;">
      {pub_html}{reach_pill}{touch_pill}
    </div>
    <div style="font-family:-apple-system,sans-serif;font-size:13px;color:#666;margin-top:4px;">
      → <a href="mailto:{_html_escape(p['to'])}" style="color:#C2714F;text-decoration:none;">{_html_escape(p['to'])}</a>
    </div>
    <div style="font-family:-apple-system,sans-serif;font-size:12px;color:#888;margin-top:6px;font-style:italic;">
      “{_html_escape(p.get('subject',''))}”
    </div>
    {body_block}
    <div style="font-family:-apple-system,sans-serif;font-size:11px;color:#aaa;margin-top:8px;">
      rif: {_html_escape(p.get('title',''))[:80]}
    </div>
  </td></tr>
  <tr><td style="padding:8px 32px;"><hr style="border:none;border-top:1px solid #F0EBE3;margin:0;"></td></tr>
"""


def _send_digest(subject, plain_body, html_body, *, to: str, dry_run: bool) -> bool:
    """Send the digest via the existing send_email module.

    Both `plain_body` and `html_body` are passed: send_email composes a
    multipart/alternative MIME so HTML-capable clients get the branded
    layout and text-only clients fall back to plain.
    """
    if dry_run:
        print(f"\n[dry-run] Email NOT sent. Would send to {to}:")
        print(f"  Subject: {subject}")
        print(f"  Plain body:\n{plain_body[:600]}...")
        return True
    try:
        from send_email import send_raw
    except ImportError as e:
        print(f"  [digest] send_email not available: {e}")
        return False
    try:
        # skip_signature=True — the digest already has its own footer
        # and we don't want the journalist-outreach "Lisa Monelli" block
        # appended to an internal notification.
        result = send_raw(
            to=to,
            subject=subject,
            body=plain_body,
            html_body=html_body,
            skip_signature=True,
            kind="journal_digest",
        )
        # SendResult is a @dataclass (see send_email.py), so use
        # attribute access — NOT dict .get().
        if getattr(result, "ok", False):
            msg_id = getattr(result, "message_id", None) or "?"
            print(f"  ✓ digest sent to {to} (msg {msg_id})")
            return True
        err = getattr(result, "error", "") or ""
        reason = getattr(result, "reason", "") if hasattr(result, "reason") else ""
        print(f"  ✗ digest send failed: {reason} {err}".strip())
        return False
    except Exception as e:  # noqa: BLE001
        print(f"  [digest] error: {type(e).__name__}: {e}")
        return False


# ── Main ─────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't move files, don't push, don't send mails; just report.")
    parser.add_argument("--no-email", action="store_true",
                        help="Publish, but skip the digest email.")
    parser.add_argument("--no-pitches", action="store_true",
                        help="Skip the auto-send of journalist pitches.")
    parser.add_argument("--force", action="store_true",
                        help="Bypass broken-link validation.")
    parser.add_argument("--to", default=DEFAULT_RECIPIENT,
                        help=f"Digest recipient (default: {DEFAULT_RECIPIENT}).")
    parser.add_argument("--no-push", action="store_true",
                        help="Don't run git auto-push after publishing.")
    args = parser.parse_args(argv)

    _load_dotenv()

    drafts = _list_journal_drafts()
    # We intentionally DON'T early-return when drafts is empty anymore —
    # there may still be journalist pitches to auto-send from the radar
    # even when no journal articles need publishing.
    if drafts:
        print(f"Found {len(drafts)} draft(s) in _drafts/journal/")
    else:
        print("No journal drafts to publish.")
    print()

    published, skipped, errors = [], [], []
    for draft in drafts:
        print(f"── {draft.name} ──")
        r = _publish_one(draft, force=args.force, dry_run=args.dry_run)
        status = r["status"]
        if status == "published":
            print(f"  ✓ published → {r['public_url']}")
            published.append(r)
        elif status == "dry_run_ok":
            print(f"  ✓ (dry-run) would publish → {r['public_url']}")
            published.append(r)
        elif status == "skipped_links":
            print(f"  ⚠ skipped: {r['reason']}")
            skipped.append(r)
        else:
            print(f"  ✗ error: {r['reason']}")
            errors.append(r)
        print()

    # Rebuild indices and push if at least one article moved (or dry run for preview).
    pushed_sha = ""
    if published and not args.dry_run:
        print("Rebuilding indices...")
        for s in ("update_journal_index.py", "update_sitemap.py",
                  "update_homepage_journal.py"):
            ok = _run_update_script(s)
            print(f"  {'✓' if ok else '✗'} {s}")
        print()

        if not args.no_push:
            print("Auto-push to GitHub...")
            plural = "s" if len(published) > 1 else ""
            commit_msg = (
                f"Auto-publish {len(published)} journal article{plural}\n\n"
                + "\n".join(f"  - {p['title']}" for p in published)
                + "\n\nPosted automatically by publish_all_drafts.py."
            )
            ok, pushed_sha = _git_autopush(commit_msg)
            print(f"  {'✓ pushed ' + pushed_sha if ok else '✗ push failed'}")
            print()

    # Send journalist pitches that are safe to auto-dispatch (after
    # publishing journal articles — order matters because publishing
    # marks source URLs as user_dismissed, which would otherwise cause
    # the same article's pitch to be skipped here as 'already sent').
    pitches = None
    if not args.no_pitches:
        print("Sending ready journalist pitches...")
        pitches = _send_ready_pitches(dry_run=args.dry_run)
        s = pitches["sent"]
        sk = pitches["skipped"]
        er = pitches["errors"]
        print(f"  Sent: {len(s)}    Skipped: {len(sk)}    Errors: {len(er)}")
        for item in s:
            print(f"    ✓ → {item['to']}  ({item['title'][:50]})")
        for item in sk:
            print(f"    ⤳ skip {item.get('to','?')}: {item['reason']}")
        for item in er:
            print(f"    ✗ {item.get('to','?')}: {item['error']}")
        print()

        # Follow-up engine — 3-touch cadence for journalists who never
        # replied to the cold pitch. Touches 2 (data bump @ +7d) and 3
        # (founder call @ +14d) run automatically; STOP after T3.
        try:
            sys.path.insert(0, str(SCRIPT_DIR))
            import followup_engine
            print("Running follow-up engine (3-touch cadence)...")
            fu_result = followup_engine.run(dry_run=args.dry_run)
            fs = fu_result.get("sent") or []
            fsk = fu_result.get("skipped") or []
            fer = fu_result.get("errors") or []
            print(f"  Follow-ups sent: {len(fs)}  skipped: {len(fsk)}  errors: {len(fer)}")
            for it in fs:
                print(f"    ✓ T{it['touch_n']} → {it['to']}  ({it.get('publication','?')})")
            for it in fsk:
                print(f"    ⤳ T{it.get('touch_n','?')} → {it.get('to','?')}: {it['reason']}")
            for it in fer:
                print(f"    ✗ T{it.get('touch_n','?')} → {it.get('to','?')}: {it['error']}")
            print()

            # Merge follow-ups into the pitches dict so the digest builder
            # sees them in the same control panel. Keep them flagged with
            # touch_n so the renderer can show "Follow-up T2" labels.
            for it in fs:
                # Re-use the same shape as cold pitches so _html_pitch_row
                # renders them uniformly. The touch_n field is the marker
                # that distinguishes them in the digest.
                pitches.setdefault("sent", []).append({
                    "to": it["to"],
                    "publication": it.get("publication") or "",
                    "reach": _publication_reach_label(
                        it.get("publication") or "", it.get("source_url", "")),
                    "subject": it["subject"],
                    "body": it.get("body", ""),
                    "title": it.get("title", ""),
                    "url": it.get("source_url", ""),
                    "touch_n": it["touch_n"],   # ← marker
                })
            for it in fsk:
                pitches.setdefault("skipped", []).append({
                    "to": it["to"],
                    "reason": it["reason"],
                    "touch_n": it.get("touch_n"),
                })
            for it in fer:
                pitches.setdefault("errors", []).append({
                    "to": it["to"],
                    "error": it["error"],
                    "touch_n": it.get("touch_n"),
                })
            pitches["_ledger_status"] = fu_result.get("ledger_status_counts") or {}
            pitches["_ledger_total"] = fu_result.get("ledger_total_contacts") or 0
        except ImportError as e:
            print(f"  [followup] engine not available: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  [followup] engine error: {type(e).__name__}: {e}")
        print()

    # Digest email
    if not args.no_email:
        print("Sending digest email...")
        subject, plain_body, html_body = _format_digest(
            published, skipped, errors,
            pitches=pitches, dry_run=args.dry_run,
        )
        _send_digest(subject, plain_body, html_body,
                     to=args.to, dry_run=args.dry_run)

    print()
    pitch_sent = len((pitches or {}).get("sent") or [])
    pitch_skip = len((pitches or {}).get("skipped") or [])
    pitch_err = len((pitches or {}).get("errors") or [])
    print(
        f"Summary: {len(published)} published, {len(skipped)} skipped, "
        f"{len(errors)} publish errors  |  "
        f"{pitch_sent} pitches sent, {pitch_skip} skipped, {pitch_err} errors."
    )
    return 0 if not (errors or pitch_err) else 2


if __name__ == "__main__":
    sys.exit(main())
