#!/usr/bin/env python3
"""
My Villa — Instagram Editorial Calendar Planner

Builds a monthly calendar of editorial slots from editorial-calendar.yml.
Each slot is a `planned` entry with date+time+pillar+sub_topic, ready to
be picked up by editorial_generator.py.

Cooldown logic prevents repeating the same sub-topic / archetype within
the configured window (uses _system/history/editorial_ledger.json).

Usage:
  python3 editorial_planner.py                    # plan next month from today
  python3 editorial_planner.py --month 2026-05    # plan a specific month
  python3 editorial_planner.py --month 2026-05 --force  # overwrite existing plan
  python3 editorial_planner.py --dry-run          # print plan, don't write
"""

import argparse
import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

# ── Paths ────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
CONFIG_DIR = SYSTEM_DIR / "config"
HISTORY_DIR = SYSTEM_DIR / "history"
SOCIAL_DIR = SYSTEM_DIR / "social"
# Renamed 2026-05-04: was "calendar/", but now most pillars (vision /
# archetype / system) are human-managed and this folder is a hybrid
# plan — partner_echo slots are auto-driven, the rest is reference for
# the human editorial team. The "editorial_plan" name captures both
# uses.
CALENDAR_DIR = SOCIAL_DIR / "editorial_plan"

CONFIG_FILE = CONFIG_DIR / "editorial-calendar.yml"
LEDGER_FILE = HISTORY_DIR / "editorial_ledger.json"

DAY_INDEX = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


# ══════════════════════════════════════════════════════════════════════
# Ledger (cooldown state)
# ══════════════════════════════════════════════════════════════════════

def load_ledger():
    if not LEDGER_FILE.exists():
        return {
            "last_used": {
                # pillar/sub_topic_key → ISO date
            },
            "wednesday_rotation_index": 0,   # 0 = archetype, 1 = system
            "version": 1,
        }
    return json.loads(LEDGER_FILE.read_text())


def save_ledger(ledger):
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER_FILE.write_text(json.dumps(ledger, indent=2, sort_keys=True))


# ══════════════════════════════════════════════════════════════════════
# Config loading
# ══════════════════════════════════════════════════════════════════════

def load_config():
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(
            f"Missing {CONFIG_FILE}. Build it first (see _system/knowledge/content_strategy_instagram.md)."
        )
    return yaml.safe_load(CONFIG_FILE.read_text())


# ══════════════════════════════════════════════════════════════════════
# Topic selection
# ══════════════════════════════════════════════════════════════════════

def _is_in_cooldown(ledger, key, today, cooldown_days):
    """A topic is in cooldown only if it was used recently IN THE PAST
    relative to `today` (the slot being planned). A `last_used` date that
    is AFTER `today` is a stale entry from a previous planning run that
    planned later dates — it should not block the current slot."""
    last = ledger["last_used"].get(key)
    if not last:
        return False
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d").date()
    except ValueError:
        return False
    delta_days = (today - last_dt).days
    if delta_days < 0:
        # last_used is in the future relative to today's slot — not a real cooldown.
        return False
    return delta_days < cooldown_days


def pick_vision_subtopic(config, ledger, slot_date):
    """Pick a Vision sub-topic respecting cooldown."""
    sub_topics = config["pillars"]["vision"].get("sub_topics", [])
    cooldown_days = config["cooldowns"]["vision_subtopic_days"]

    available = [
        st for st in sub_topics
        if not _is_in_cooldown(ledger, f"vision::{st}", slot_date, cooldown_days)
    ]
    if not available:
        # All in cooldown — fall back to longest-since-used
        available = sorted(
            sub_topics,
            key=lambda st: ledger["last_used"].get(f"vision::{st}", "0000-00-00"),
        )
    return available[0] if available else None


def pick_archetype(config, ledger, slot_date):
    """Pick an Archetype respecting cooldown — rotates through all 9."""
    archetypes = config["pillars"]["archetype"].get("archetypes", [])
    cooldown_days = config["cooldowns"]["archetype_days"]

    available = [
        a for a in archetypes
        if not _is_in_cooldown(ledger, f"archetype::{a['name']}", slot_date, cooldown_days)
    ]
    if not available:
        available = sorted(
            archetypes,
            key=lambda a: ledger["last_used"].get(f"archetype::{a['name']}", "0000-00-00"),
        )
    return available[0] if available else None


def pick_system_subtopic(config, ledger, slot_date):
    """Pick a System sub-topic respecting cooldown."""
    sub_topics = config["pillars"]["system"].get("sub_topics", [])
    cooldown_days = config["cooldowns"]["system_subtopic_days"]

    available = [
        st for st in sub_topics
        if not _is_in_cooldown(ledger, f"system::{st}", slot_date, cooldown_days)
    ]
    if not available:
        available = sorted(
            sub_topics,
            key=lambda st: ledger["last_used"].get(f"system::{st}", "0000-00-00"),
        )
    return available[0] if available else None


def pick_partner_handle(config, ledger, slot_date):
    """Pick a partner handle to repost from, respecting per-handle cooldown."""
    handles = config["pillars"]["partner_echo"].get("handles", [])
    cooldown_days = config["cooldowns"]["partner_per_handle_days"]

    available = [
        h for h in handles
        if not _is_in_cooldown(ledger, f"partner::{h['handle']}", slot_date, cooldown_days)
    ]
    if not available:
        available = sorted(
            handles,
            key=lambda h: ledger["last_used"].get(f"partner::{h['handle']}", "0000-00-00"),
        )
    return available[0] if available else None


# ══════════════════════════════════════════════════════════════════════
# Calendar generation
# ══════════════════════════════════════════════════════════════════════

def _resolve_pillar(slot_def, week_index, ledger):
    """Resolve which pillar a slot uses (handles pillar_rotation alternation)."""
    if "pillar" in slot_def:
        return slot_def["pillar"]

    if "pillar_rotation" in slot_def:
        rotation = slot_def["pillar_rotation"]
        # Use ledger's wednesday_rotation_index + week offset for stable alternation
        idx = (ledger.get("wednesday_rotation_index", 0) + week_index) % len(rotation)
        return rotation[idx]

    return None


def build_slot(slot_def, slot_date, week_index, config, ledger):
    """Build a single slot dict for the calendar."""
    pillar_key = _resolve_pillar(slot_def, week_index, ledger)
    if not pillar_key:
        return None

    pillar = config["pillars"].get(pillar_key, {})
    slot = {
        "date": slot_date.strftime("%Y-%m-%d"),
        "day": slot_date.strftime("%A").lower(),
        "time": slot_def.get("time", "09:00"),
        "timezone": config["account"].get("timezone", "America/Los_Angeles"),
        "pillar": pillar_key,
        "pillar_name": pillar.get("name", pillar_key),
        "format": pillar.get("format", "single_image"),
        "caption_target_chars": pillar.get("caption_target_chars", 220),
        "visual_hint": pillar.get("visual_hint", ""),
        "status": "planned",
        "slug": "",
    }

    # Pick the specific sub-topic for this pillar
    if pillar_key == "vision":
        st = pick_vision_subtopic(config, ledger, slot_date)
        if st:
            slot["sub_topic"] = st
            slot["slug"] = _slugify(st)[:48]
            ledger["last_used"][f"vision::{st}"] = slot["date"]

    elif pillar_key == "archetype":
        a = pick_archetype(config, ledger, slot_date)
        if a:
            slot["sub_topic"] = a["name"]
            slot["archetype_definition"] = a.get("definition", "")
            slot["archetype_masters"] = a.get("masters", [])
            slot["slug"] = _slugify(f"archetype-{a['name']}")
            slot["slides"] = config["pillars"]["archetype"].get("slides", 6)
            ledger["last_used"][f"archetype::{a['name']}"] = slot["date"]

    elif pillar_key == "system":
        st = pick_system_subtopic(config, ledger, slot_date)
        if st:
            slot["sub_topic"] = st
            slot["slug"] = _slugify(st)[:48]
            ledger["last_used"][f"system::{st}"] = slot["date"]

    elif pillar_key == "partner_echo":
        h = pick_partner_handle(config, ledger, slot_date)
        if h:
            slot["partner_handle"] = h["handle"]
            slot["partner_focus"] = h.get("focus", "")
            slot["partner_role_in_myvilla"] = (h.get("role_in_myvilla") or "").strip()
            slot["slug"] = f"partner-{h['handle']}"
            slot["status"] = "planned_pending_scrape"   # P2 dependency
            ledger["last_used"][f"partner::{h['handle']}"] = slot["date"]

    return slot


def _slugify(text):
    import re
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return s


def _month_dates(year, month):
    """Yield every date in (year, month)."""
    d = date(year, month, 1)
    while d.month == month:
        yield d
        d += timedelta(days=1)


def plan_month(year, month, config, ledger):
    """Build slots for every editorial day in the month."""
    slots = []
    schedule = config["cadence"]["schedule"]
    schedule_by_day = {s["day"].lower(): s for s in schedule}

    # Track which Wednesday this is in the month, for archetype/system rotation
    wednesday_count = 0

    for d in _month_dates(year, month):
        day_name = d.strftime("%A").lower()
        if day_name not in schedule_by_day:
            continue

        slot_def = schedule_by_day[day_name]

        # Determine week index for rotation
        if day_name == "wednesday":
            week_index = wednesday_count
            wednesday_count += 1
        else:
            # Use ISO week-of-month so ordering is stable
            week_index = (d.day - 1) // 7

        slot = build_slot(slot_def, d, week_index, config, ledger)
        if slot:
            slots.append(slot)

    # Bump persistent rotation index for next planning cycle
    ledger["wednesday_rotation_index"] = (
        ledger.get("wednesday_rotation_index", 0) + wednesday_count
    ) % 2

    return slots


# ══════════════════════════════════════════════════════════════════════
# Output
# ══════════════════════════════════════════════════════════════════════

def write_calendar(year, month, slots, dry_run=False):
    """Write slots to _system/social/editorial_plan/YYYY-MM.yml."""
    CALENDAR_DIR.mkdir(parents=True, exist_ok=True)

    out = {
        "month": f"{year:04d}-{month:02d}",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "slot_count": len(slots),
        "slots": slots,
    }

    target = CALENDAR_DIR / f"{year:04d}-{month:02d}.yml"

    if dry_run:
        print(f"\n[dry-run] Would write {target}")
        print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))
        return target

    target.write_text(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))
    print(f"✓ Wrote {len(slots)} slots → {target.relative_to(SYSTEM_DIR.parent)}")
    return target


def print_summary(slots):
    """Print a short text summary table of the calendar."""
    print(f"\n  Date          Day       Time   Pillar         Topic")
    print(f"  {'-'*12}  {'-'*8}  {'-'*5}  {'-'*13}  {'-'*40}")
    for s in slots:
        topic = s.get("sub_topic") or s.get("partner_handle", "—")
        if len(topic) > 40:
            topic = topic[:37] + "..."
        print(f"  {s['date']}  {s['day'][:8]:8}  {s['time']:5}  "
              f"{s['pillar']:13}  {topic}")
    print()


# ══════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="MyVilla — IG Editorial Planner")
    parser.add_argument("--month", help="YYYY-MM (default: next month from today)")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing plan for that month")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan without writing files")
    args = parser.parse_args()

    # Resolve target month
    if args.month:
        try:
            year, month = map(int, args.month.split("-"))
        except ValueError:
            parser.error("--month must be YYYY-MM (e.g. 2026-05)")
    else:
        # Default: next month
        today = date.today()
        if today.month == 12:
            year, month = today.year + 1, 1
        else:
            year, month = today.year, today.month + 1

    target = CALENDAR_DIR / f"{year:04d}-{month:02d}.yml"
    if target.exists() and not args.force and not args.dry_run:
        print(f"⚠ Plan already exists: {target.relative_to(SYSTEM_DIR.parent)}")
        print(f"  Use --force to overwrite, or --dry-run to preview.")
        return

    config = load_config()
    ledger = load_ledger()

    print(f"\nMy Villa — IG Editorial Planner")
    print(f"{'=' * 50}")
    print(f"  Month: {year}-{month:02d}")
    print(f"  Config: {CONFIG_FILE.relative_to(SYSTEM_DIR.parent)}")
    print(f"  Ledger: {LEDGER_FILE.relative_to(SYSTEM_DIR.parent) if LEDGER_FILE.exists() else '(new)'}")

    slots = plan_month(year, month, config, ledger)
    print_summary(slots)

    write_calendar(year, month, slots, dry_run=args.dry_run)

    if not args.dry_run:
        save_ledger(ledger)
        print(f"  Ledger updated: {len(ledger['last_used'])} entries tracked")


if __name__ == "__main__":
    main()
