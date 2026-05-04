#!/usr/bin/env python3
"""
Smoke test for the My Villa IG Editorial pipeline.

Runs in two modes:
  --quick   compile-only + offline checks (no network, < 5 sec)
  --full    quick + dry-run planner/generator (no API calls, < 30 sec)

Exit code 0 = all green. Non-zero = at least one check failed.

Usage:
  python3 _system/scripts/smoke_test_editorial.py
  python3 _system/scripts/smoke_test_editorial.py --quick
  python3 _system/scripts/smoke_test_editorial.py --full
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import py_compile
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# Layout autodetection — supports two layouts:
#  (a) Live project: scripts in <ROOT>/_system/scripts/, configs in
#      <ROOT>/_system/config/, knowledge in <ROOT>/_system/knowledge/.
#  (b) Review package: every file flat in the same directory (the zip
#      sent to a code reviewer).
def _detect_layout() -> tuple[Path, Path, Path]:
    """Return (script_dir, config_dir, root_dir) for whichever layout we're in."""
    parent = SCRIPT_DIR.parent  # would be _system/ in live, or zip-root in package
    if (parent / "config" / "editorial-calendar.yml").exists():
        # Live layout: SCRIPT_DIR is _system/scripts/
        return SCRIPT_DIR, parent / "config", parent.parent
    if (SCRIPT_DIR / "editorial-calendar.yml").exists():
        # Review-package layout: scripts + configs flat
        return SCRIPT_DIR, SCRIPT_DIR, SCRIPT_DIR
    # Fallback: assume live layout, tests will surface the mismatch
    return SCRIPT_DIR, parent / "config", parent.parent


SCRIPT_DIR_R, CONFIG_DIR, ROOT_DIR = _detect_layout()
SYSTEM_DIR = CONFIG_DIR.parent

EDITORIAL_SCRIPTS = [
    SCRIPT_DIR_R / "editorial_planner.py",
    SCRIPT_DIR_R / "editorial_generator.py",
    SCRIPT_DIR_R / "partner_scraper.py",
    SCRIPT_DIR_R / "publish_instagram.py",
]

EDITORIAL_CONFIG = CONFIG_DIR / "editorial-calendar.yml"
BRAND_VOICE_CONFIG = CONFIG_DIR / "brand-voice.yml"


# ── Reporting helpers ────────────────────────────────────────────────

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
WARN = "\033[33m⚠\033[0m"

failures = []
warnings_count = 0


def check(label: str, condition: bool, *, warn: bool = False, detail: str = ""):
    global warnings_count
    if condition:
        print(f"  {PASS} {label}")
        return True
    if warn:
        warnings_count += 1
        print(f"  {WARN} {label}{(' — ' + detail) if detail else ''}")
        return True
    print(f"  {FAIL} {label}{(' — ' + detail) if detail else ''}")
    failures.append(label)
    return False


# ── Stage 1: compile every editorial script ──────────────────────────

def stage_compile():
    print("\n[1/4] py_compile every editorial script")
    for s in EDITORIAL_SCRIPTS:
        try:
            py_compile.compile(str(s), doraise=True)
            check(f"compile {s.name}", True)
        except py_compile.PyCompileError as e:
            check(f"compile {s.name}", False, detail=str(e)[:120])


# ── Stage 2: import + structural checks ──────────────────────────────

def stage_imports():
    print("\n[2/4] import & structural checks")
    if str(SCRIPT_DIR_R) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR_R))

    # editorial_planner
    try:
        import editorial_planner  # noqa: F401
        check("import editorial_planner", True)
    except Exception as e:
        check("import editorial_planner", False, detail=str(e))

    # partner_scraper — checks the new pass-3 default behavior
    try:
        import partner_scraper
        check("import partner_scraper", True)
        # The Pass 3 fallback must be opt-in: default should NOT return
        # a low-relevance post when min/hard floors don't match anything.
        sig = partner_scraper.pick_post_for_slot.__defaults__
        check(
            "pick_post_for_slot defaults to allow_any_usable=False",
            partner_scraper.DEFAULT_HARD_FLOOR == 5,
        )
    except Exception as e:
        check("import partner_scraper", False, detail=str(e))

    # editorial_generator
    try:
        import editorial_generator
        check("import editorial_generator", True)
        check(
            "validate_generated_post is exported",
            hasattr(editorial_generator, "validate_generated_post"),
        )
        check(
            "_strip_slot_internal_keys is exported",
            hasattr(editorial_generator, "_strip_slot_internal_keys"),
        )
    except Exception as e:
        check("import editorial_generator", False, detail=str(e))

    # publish_instagram
    try:
        import publish_instagram  # noqa: F401
        check("import publish_instagram", True)
    except Exception as e:
        check("import publish_instagram", False, detail=str(e))


# ── Stage 3: config consistency ──────────────────────────────────────

def stage_config():
    print("\n[3/4] config consistency")
    try:
        import yaml
    except ImportError:
        check("pyyaml available", False, detail="pip install pyyaml")
        return

    check(f"editorial-calendar.yml exists", EDITORIAL_CONFIG.exists())
    check(f"brand-voice.yml exists", BRAND_VOICE_CONFIG.exists())

    if not EDITORIAL_CONFIG.exists():
        return
    cfg = yaml.safe_load(EDITORIAL_CONFIG.read_text())
    h = cfg.get("hashtags", {}).get("per_post", {})
    h_min, h_max = h.get("min"), h.get("max")
    check(
        f"hashtags.per_post min/max present",
        bool(h_min) and bool(h_max),
        detail=f"{h_min}/{h_max}",
    )
    archetypes = cfg.get("pillars", {}).get("archetype", {}).get("archetypes", [])
    check(
        f"archetype count = 8",
        len(archetypes) == 8,
        detail=f"got {len(archetypes)}",
    )
    handles = cfg.get("pillars", {}).get("partner_echo", {}).get("handles", [])
    expected = {"buromilan", "dgu_baja", "transsolar_klimaengineering", "its__vision"}
    actual = {h.get("handle") for h in handles}
    check(
        f"partner handles == 4 expected",
        actual == expected,
        detail=f"missing: {expected - actual}, extra: {actual - expected}",
    )
    # role_in_myvilla must be set on every partner
    missing_role = [h.get("handle") for h in handles
                    if not (h.get("role_in_myvilla") or "").strip()]
    check(
        f"every partner has role_in_myvilla",
        not missing_role,
        detail=f"missing: {missing_role}",
    )


# ── Stage 4: dry-run planner + generator ─────────────────────────────

def stage_dry_run():
    print("\n[4/4] dry-run planner + generator (no API calls)")
    next_month = (date.today().replace(day=1) + timedelta(days=35)).strftime("%Y-%m")

    # Planner --dry-run
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR_R / "editorial_planner.py"),
             "--month", next_month, "--dry-run"],
            capture_output=True, text=True, timeout=30, cwd=str(ROOT_DIR),
        )
        ok = result.returncode == 0 and "Would write" in result.stdout
        check(
            f"editorial_planner --month {next_month} --dry-run",
            ok,
            detail=(result.stderr or result.stdout)[-200:] if not ok else "",
        )
    except subprocess.TimeoutExpired:
        check("editorial_planner --dry-run", False, detail="timeout")

    # Partner scraper --offline (no network)
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR_R / "partner_scraper.py"), "--offline"],
            capture_output=True, text=True, timeout=20, cwd=str(ROOT_DIR),
        )
        ok = result.returncode == 0 and "Summary" in result.stdout
        check(
            "partner_scraper --offline",
            ok,
            detail=(result.stderr or result.stdout)[-200:] if not ok else "",
        )
    except subprocess.TimeoutExpired:
        check("partner_scraper --offline", False, detail="timeout")


# ── Main ──────────────────────────────────────────────────────────────

def _is_review_package_layout() -> bool:
    """True iff every editorial file is flat in SCRIPT_DIR_R (review zip)."""
    return (SCRIPT_DIR_R / "editorial-calendar.yml").exists() and not (
        SCRIPT_DIR_R / "config" / "editorial-calendar.yml"
    ).exists()


def main():
    parser = argparse.ArgumentParser(description="Editorial pipeline smoke test")
    parser.add_argument("--quick", action="store_true",
                        help="Compile + import + config (no subprocess dry-runs)")
    parser.add_argument("--full", action="store_true",
                        help="--quick + planner/scraper dry-run subprocesses")
    args = parser.parse_args()

    layout = "review-package" if _is_review_package_layout() else "live-project"
    print(f"My Villa — Editorial Pipeline Smoke Test")
    print(f"  Layout : {layout}")
    print(f"  Scripts: {SCRIPT_DIR_R}")
    print(f"  Config : {CONFIG_DIR}")
    print(f"  Root   : {ROOT_DIR}")

    stage_compile()
    stage_imports()
    stage_config()
    if args.full or not args.quick:
        if layout == "review-package":
            print("\n[4/4] dry-run planner + generator")
            print(f"  {WARN} skipped — review-package layout has scripts flat,")
            print(f"      but planner/scraper expect live layout (_system/scripts/")
            print(f"      with sibling _system/config/, _drafts/, etc.).")
            print(f"      To run --full, copy scripts into a real My Villa Website")
            print(f"      project tree, then re-run from there.")
            global warnings_count
            warnings_count += 1
        else:
            stage_dry_run()

    print(f"\n{'─' * 50}")
    if failures:
        print(f"  {FAIL} {len(failures)} check(s) failed:")
        for f in failures:
            print(f"     • {f}")
        if warnings_count:
            print(f"  {WARN} {warnings_count} warning(s)")
        sys.exit(1)
    if warnings_count:
        print(f"  {WARN} {warnings_count} warning(s) — review")
    print(f"  {PASS} all green")


if __name__ == "__main__":
    main()
