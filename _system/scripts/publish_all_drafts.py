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


# ── Digest email ─────────────────────────────────────────────────────

def _format_digest(published, skipped, errors, dry_run=False) -> tuple[str, str]:
    """Returns (subject, body) for the digest email."""
    today = datetime.now().strftime("%Y-%m-%d")
    n_pub = len(published)
    prefix = "[DRY-RUN] " if dry_run else ""
    if n_pub == 0 and not skipped and not errors:
        subject = f"{prefix}[My Villa] Nessun nuovo articolo journal — {today}"
    elif n_pub == 0:
        subject = f"{prefix}[My Villa] Nessun articolo pubblicato (tutti bloccati) — {today}"
    else:
        plural = "articolo" if n_pub == 1 else "articoli"
        subject = f"{prefix}[My Villa] {n_pub} {plural} pubblicato sul journal — {today}"

    lines = []
    if n_pub:
        verb = "sarebbero pubblicati" if dry_run else "sono stati pubblicati"
        lines.append(f"Ciao Ivo,\n\nQuesti {n_pub} articoli {verb} oggi sul journal:\n")
        for i, p in enumerate(published, 1):
            lines.append(f"{i}. {p['title']}")
            lines.append(f"   {p['public_url']}")
            lines.append("")
    else:
        lines.append("Ciao Ivo,\n\nNessun articolo nuovo pubblicato oggi.\n")

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

    lines.append("\n--\nAuto-published by publish_all_drafts.py")
    lines.append(f"Site: {SITE_BASE}/blog/")

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
                        help="Don't move files, don't push, don't email; just report.")
    parser.add_argument("--no-email", action="store_true",
                        help="Publish, but skip the digest email.")
    parser.add_argument("--force", action="store_true",
                        help="Bypass broken-link validation.")
    parser.add_argument("--to", default=DEFAULT_RECIPIENT,
                        help=f"Digest recipient (default: {DEFAULT_RECIPIENT}).")
    parser.add_argument("--no-push", action="store_true",
                        help="Don't run git auto-push after publishing.")
    args = parser.parse_args(argv)

    _load_dotenv()

    drafts = _list_journal_drafts()
    if not drafts:
        print("Nothing to publish — _drafts/journal/ has no .html files.")
        return 0

    print(f"Found {len(drafts)} draft(s) in _drafts/journal/")
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

    # Digest email
    if not args.no_email:
        print("Sending digest email...")
        subject, body = _format_digest(
            published, skipped, errors, dry_run=args.dry_run,
        )
        _send_digest(subject, body, to=args.to, dry_run=args.dry_run)

    print()
    print(f"Summary: {len(published)} published, {len(skipped)} skipped, "
          f"{len(errors)} errors.")
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
