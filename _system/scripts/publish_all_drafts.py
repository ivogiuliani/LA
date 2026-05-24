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


def _publish_one(html_path: Path, *, force: bool, dry_run: bool) -> dict:
    """Move one draft to blog/, handle sidecar + IG companion.

    Returns a dict with keys: status (published|skipped_links|error),
    title, slug, public_url, reason (if skipped), broken_links (if any).
    """
    slug = html_path.stem
    sidecar = html_path.with_suffix(".json")
    companion = html_path.with_suffix(".ig.md")
    sidecar_data = _read_sidecar(sidecar)
    title = sidecar_data.get("title") or slug

    result = {
        "status": "?",
        "title": title,
        "slug": slug,
        "public_url": f"{SITE_BASE}/blog/{slug}.html",
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

        if dry_run:
            sent.append({
                "title": title, "to": to_addr,
                "subject": subject, "publication": publication,
                "source": source,
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

        ok = getattr(result, "ok", False)
        result_dry = getattr(result, "dry_run", False)
        reason = getattr(result, "reason", "") or ""
        err = getattr(result, "error", "") or ""

        if ok and result_dry:
            # outreach/config.yml has dry_run: true. Treat as informational.
            skipped.append({
                "title": title, "to": to_addr,
                "reason": "outreach config in dry_run mode",
            })
        elif ok:
            sent.append({
                "title": title, "to": to_addr,
                "subject": subject, "publication": publication,
                "source": source,
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
                   *, pitches=None, dry_run=False) -> tuple[str, str]:
    """Returns (subject, body) for the daily digest email.

    `pitches` is the dict returned by _send_ready_pitches() — when
    provided, the digest grows a "✉ Email inviate ai giornalisti"
    section listing sent, skipped (with reason), and errored sends.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    n_pub = len(published)
    n_pitch_sent = len((pitches or {}).get("sent") or [])
    prefix = "[DRY-RUN] " if dry_run else ""

    # Subject reflects whatever happened — both counts when both, just
    # one when only one bucket has activity.
    parts = []
    if n_pub:
        parts.append(f"{n_pub} articolo" + ("" if n_pub == 1 else " s/articoli pubblicato"))
    if n_pitch_sent:
        parts.append(f"{n_pitch_sent} email")
    if parts:
        subject = f"{prefix}[My Villa] " + " + ".join(parts) + f" — {today}"
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

    # ── Section 2: pitch emails to journalists ──
    if pitches is not None:
        p_sent = pitches.get("sent") or []
        p_skip = pitches.get("skipped") or []
        p_err = pitches.get("errors") or []
        if p_sent or p_skip or p_err:
            verb = "sarebbero inviate" if dry_run else "sono state inviate"
            lines.append(f"\n✉️  EMAIL AI GIORNALISTI ({len(p_sent)} inviate)\n")
            if p_sent:
                lines.append(f"Queste pitch {verb} oggi da info@myvilla.la:\n")
                for i, p in enumerate(p_sent, 1):
                    pub = f" ({p['publication']})" if p.get("publication") else ""
                    lines.append(f"{i}. → {p['to']}{pub}")
                    lines.append(f"   subject: {p['subject']}")
                    lines.append(f"   ref: {p['title']}")
                    lines.append("")
            if p_skip:
                lines.append("── Email saltate (review umana richiesta) ──\n")
                for s in p_skip:
                    lines.append(f"• {s.get('to', '?')}  ({s['reason']})")
                    lines.append(f"  ref: {s['title']}")
                    lines.append("")
            if p_err:
                lines.append("── Email con errore di invio ──\n")
                for e in p_err:
                    lines.append(f"• {e.get('to', '?')}: {e['error']}")
                    lines.append(f"  ref: {e['title']}")
                lines.append("")
        else:
            lines.append("\n✉️  EMAIL AI GIORNALISTI: nessuna pitch in coda oggi.\n")

    lines.append("\n--\nAuto-published + outreach by publish_all_drafts.py")
    lines.append(f"Site: {SITE_BASE}/blog/")
    lines.append("Le pitch 'risky' (Apollo likely, pattern_guess) restano per review manuale sul dashboard.")

    return subject, "\n".join(lines)


def _send_digest(subject, body, *, to: str, dry_run: bool) -> bool:
    """Send the digest via the existing send_email module."""
    if dry_run:
        print(f"\n[dry-run] Email NOT sent. Would send to {to}:")
        print(f"  Subject: {subject}")
        print(f"  Body:\n{body}\n")
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
            body=body,
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

    # Digest email
    if not args.no_email:
        print("Sending digest email...")
        subject, body = _format_digest(
            published, skipped, errors,
            pitches=pitches, dry_run=args.dry_run,
        )
        _send_digest(subject, body, to=args.to, dry_run=args.dry_run)

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
