#!/usr/bin/env python3
"""
My Villa — Westside Rebuild Tracker (LADBS building permits)

Cosa fa
-------
Interroga il portale open data della Città di Los Angeles (Socrata SODA,
senza token) e costruisce un tracker pubblico dei permessi di NUOVA
COSTRUZIONE residenziale (permit_type = "Bldg-New", sub type "1 or 2 Family
Dwelling") presentati dal 2025-01-08 (giorno dopo l'inizio dell'incendio
Palisades) nelle aree Westside servite da LADBS:

    Pacific Palisades ........ ZIP 90272
    Brentwood ................ ZIP 90049
    Bel Air / Beverly Crest .. ZIP 90077 + 90210 (SOLO la porzione City of LA)
    Encino / Tarzana ......... ZIP 91316, 91356, 91436

Malibu, Topanga e le aree non incorporate NON sono in LADBS (City of Malibu
e LA County rilasciano i propri permessi) e il tracker lo dichiara.

Dataset (verificati il 2026-09-16 con /api/views/<id>.json):
    gwh9-jnip  Building and Safety - Building Permits Submitted from 2020 to Present
    pi9x-tg5x  Building and Safety - Building Permits Issued from 2020 to Present
Usiamo il dataset "Submitted" perché contiene anche i permessi ancora in
plan check (status_desc) e il campo `construction` (Type V-B, V-A, II-B...),
quindi possiamo distinguere legno (Type V) da non combustibile (Type I/II).
Il campo `work_desc` di LADBS inizia con "2025 WILDFIRE REBUILD" per i
permessi di ricostruzione post-incendio: lo usiamo come flag rebuild.

Come si lancia
--------------
    python3 _system/scripts/rebuild_tracker.py            # run reale
    python3 _system/scripts/rebuild_tracker.py --dry-run  # scarica e stampa, non scrive
    python3 _system/scripts/rebuild_tracker.py --offline  # rigenera PNG/HTML dal CSV esistente

Cosa produce (tutto sotto research/, servito da GitHub Pages)
-----------------------------------------------------------
    research/data/westside-rebuild-permits.csv    riga per permesso
    research/data/westside-rebuild-permits.json   meta + aggregati + righe
    research/data/westside-rebuild-history.json   snapshot settimanali (trend)
    research/img/westside-rebuild-tracker.png     grafico 1200 px (matplotlib)
    research/westside-rebuild-tracker.html        pagina pubblica

Exit code sempre 0 (errori non fatali loggati): la pipeline non si ferma.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime
from html import escape
from pathlib import Path
from typing import Dict, List, Optional

# ── Paths ────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent.parent
RESEARCH_DIR = ROOT_DIR / "research"
DATA_DIR = RESEARCH_DIR / "data"
IMG_DIR = RESEARCH_DIR / "img"
CSV_PATH = DATA_DIR / "westside-rebuild-permits.csv"
JSON_PATH = DATA_DIR / "westside-rebuild-permits.json"
HISTORY_PATH = DATA_DIR / "westside-rebuild-history.json"
PNG_PATH = IMG_DIR / "westside-rebuild-tracker.png"
HTML_PATH = RESEARCH_DIR / "westside-rebuild-tracker.html"

BASE_URL = "https://myvilla.la"
PAGE_URL = f"{BASE_URL}/research/westside-rebuild-tracker.html"
PAGE_TITLE = "Westside Rebuild Tracker — LADBS Permits after the January 2025 Fires"
BRIEFING_URL = f"{BASE_URL}/private-briefing.html"
GA4_ID = "G-D6HJX7BNZN"

# ── Data source ──────────────────────────────────────────────────────
SODA_DOMAIN = "https://data.lacity.org"
DATASET_ID = "gwh9-jnip"          # Building Permits Submitted 2020 → present
DATASET_ISSUED_ID = "pi9x-tg5x"   # Building Permits Issued 2020 → present (companion)
DATASET_PAGE = f"{SODA_DOMAIN}/d/{DATASET_ID}"
DATASET_ISSUED_PAGE = f"{SODA_DOMAIN}/d/{DATASET_ISSUED_ID}"
DATASET_NAME = "Building and Safety - Building Permits Submitted from 2020 to Present"
PORTAL_TERMS = f"{SODA_DOMAIN}/terms-of-use"
START_DATE = "2025-01-08"          # day after the Palisades Fire ignition (2025-01-07)
PAGE_SIZE = 5000
TIMEOUT = 90
USER_AGENT = "myvilla-rebuild-tracker/1.0 (+https://myvilla.la/research/)"

AREAS: Dict[str, List[str]] = {
    "Pacific Palisades": ["90272"],
    "Brentwood": ["90049"],
    "Bel Air / Beverly Crest": ["90077", "90210"],
    "Encino / Tarzana": ["91316", "91356", "91436"],
}
AREA_ORDER = list(AREAS.keys())
ZIP_TO_AREA = {z: a for a, zips in AREAS.items() for z in zips}

FIELDS = [
    "permit_nbr", "primary_address", "zip_code", "cpa", "hl", "permit_type",
    "permit_sub_type", "use_desc", "submitted_date", "issue_date", "cofo_date",
    "status_desc", "status_date", "square_footage", "valuation", "construction",
    "height", "work_desc", "lat", "lon",
]

CSV_COLUMNS = [
    "permit_nbr", "area", "zip_code", "community_plan_area", "hillside",
    "use_desc", "submitted_date", "issue_date", "cofo_date", "status",
    "status_desc", "status_date", "construction_type", "construction_class",
    "square_footage", "valuation_usd", "height_ft", "wildfire_rebuild",
    "primary_address", "lat", "lon",
]

# Palette: carta / inchiostro / terracotta (stessi token del sito v1)
PAPER = "#FAF8F5"
LINEN = "#EDE6DC"
INK = "#1a1816"
CHARCOAL = "#2C2C2C"
STONE = "#A09890"
TERRACOTTA = "#C2714F"
SAND = "#D4B896"
OLIVE = "#5C6B4F"
ESPRESSO = "#3E2F2B"
AREA_COLORS = {
    "Pacific Palisades": TERRACOTTA,
    "Brentwood": ESPRESSO,
    "Bel Air / Beverly Crest": OLIVE,
    "Encino / Tarzana": SAND,
}


def log(msg: str) -> None:
    print(msg, flush=True)


# ── Fetch ────────────────────────────────────────────────────────────
def soda_query(where: str, select: Optional[str] = None, order: str = "permit_nbr",
               limit: int = PAGE_SIZE, offset: int = 0) -> list:
    params = {"$where": where, "$order": order, "$limit": str(limit), "$offset": str(offset)}
    if select:
        params["$select"] = select
    url = f"{SODA_DOMAIN}/resource/{DATASET_ID}.json?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_permits() -> list:
    zips = ",".join(f"'{z}'" for z in ZIP_TO_AREA)
    where = (
        f"zip_code in ({zips}) AND submitted_date >= '{START_DATE}T00:00:00' "
        "AND permit_type = 'Bldg-New' AND permit_sub_type = '1 or 2 Family Dwelling'"
    )
    rows: list = []
    offset = 0
    while True:
        page = soda_query(where, select=",".join(FIELDS), offset=offset)
        rows.extend(page)
        log(f"  fetched {len(page)} rows (offset {offset})")
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


# ── Normalize ────────────────────────────────────────────────────────
def _d(value: Optional[str]) -> str:
    """'2026-09-11T00:00:00.000' -> '2026-09-11' (or '')."""
    return (value or "")[:10]


def _num(value: Optional[str]) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def construction_class(construction: str) -> str:
    c = (construction or "").upper()
    if not c:
        return "Not stated"
    # Ordine importante: "TYPE II" è sottostringa di "TYPE III".
    if "TYPE V" in c:
        return "Type V (wood-frame)"
    if "TYPE III" in c or "TYPE IV" in c:
        return "Type III/IV (mixed / heavy timber)"
    if "TYPE I-" in c or "TYPE I " in c or "TYPE II" in c:
        return "Type I/II (non-combustible)"
    return "Other"


def status_bucket(status_desc: str) -> str:
    s = (status_desc or "").lower()
    if s in ("cofo issued", "permit finaled"):
        return "Completed (CofO / finaled)"
    if s == "issued":
        return "Issued (under construction)"
    if any(k in s for k in ("refund", "withdrawn", "expired", "cancel", "no progress", "void")):
        return "Withdrawn / inactive"
    return "In plan check"


STATUS_ORDER = ["In plan check", "Issued (under construction)", "Completed (CofO / finaled)", "Withdrawn / inactive"]
CLASS_ORDER = ["Type V (wood-frame)", "Type I/II (non-combustible)", "Type III/IV (mixed / heavy timber)", "Other", "Not stated"]


def normalize(raw: list) -> list:
    out = []
    seen = set()
    for r in raw:
        nbr = r.get("permit_nbr", "")
        if not nbr or nbr in seen:
            continue
        seen.add(nbr)
        zip_code = (r.get("zip_code") or "")[:5]
        area = ZIP_TO_AREA.get(zip_code)
        if not area:
            continue
        work = (r.get("work_desc") or "").upper()
        lat = _num(r.get("lat"))
        lon = _num(r.get("lon"))
        out.append({
            "permit_nbr": nbr,
            "area": area,
            "zip_code": zip_code,
            "community_plan_area": (r.get("cpa") or "").strip(),
            "hillside": "yes" if (r.get("hl") or "").upper() == "YES" else "no",
            "use_desc": r.get("use_desc") or "",
            "submitted_date": _d(r.get("submitted_date")),
            "issue_date": _d(r.get("issue_date")),
            "cofo_date": _d(r.get("cofo_date")),
            "status": status_bucket(r.get("status_desc") or ""),
            "status_desc": r.get("status_desc") or "",
            "status_date": _d(r.get("status_date")),
            "construction_type": r.get("construction") or "",
            "construction_class": construction_class(r.get("construction") or ""),
            "square_footage": _num(r.get("square_footage")),
            "valuation_usd": _num(r.get("valuation")),
            "height_ft": _num(r.get("height")),
            "wildfire_rebuild": "yes" if "WILDFIRE REBUILD" in work else "no",
            "primary_address": (r.get("primary_address") or "").strip(),
            "lat": round(lat, 4) if lat is not None else None,
            "lon": round(lon, 4) if lon is not None else None,
        })
    out.sort(key=lambda x: (x["submitted_date"], x["permit_nbr"]))
    return out


# ── Aggregate ────────────────────────────────────────────────────────
def month_key(d: str) -> str:
    return d[:7] if d else ""


def month_range(start: str, end: str) -> List[str]:
    y, m = int(start[:4]), int(start[5:7])
    ey, em = int(end[:4]), int(end[5:7])
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def median(values: List[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def aggregate(rows: list, today: str) -> dict:
    months = month_range(START_DATE, today)
    by_month_submitted = {a: Counter() for a in AREA_ORDER}
    by_month_issued = {a: Counter() for a in AREA_ORDER}
    by_area = {}
    for a in AREA_ORDER:
        sub = [r for r in rows if r["area"] == a]
        sfd = [r for r in sub if r["use_desc"] == "Dwelling - Single Family"]
        for r in sub:
            if r["submitted_date"]:
                by_month_submitted[a][month_key(r["submitted_date"])] += 1
            if r["issue_date"]:
                by_month_issued[a][month_key(r["issue_date"])] += 1
        durations = [
            (datetime.strptime(r["issue_date"], "%Y-%m-%d") - datetime.strptime(r["submitted_date"], "%Y-%m-%d")).days
            for r in sub if r["issue_date"] and r["submitted_date"]
        ]
        by_area[a] = {
            "permits_total": len(sub),
            "single_family_dwellings": len(sfd),
            "wildfire_rebuild_flagged": sum(1 for r in sub if r["wildfire_rebuild"] == "yes"),
            "hillside": sum(1 for r in sub if r["hillside"] == "yes"),
            "status": {s: sum(1 for r in sub if r["status"] == s) for s in STATUS_ORDER},
            "construction_class": {c: sum(1 for r in sub if r["construction_class"] == c) for c in CLASS_ORDER},
            "median_days_submitted_to_issued": median(durations),
            "median_sqft_single_family": median([r["square_footage"] for r in sfd]),
            "median_declared_valuation_single_family_usd": median([r["valuation_usd"] for r in sfd]),
            "issued_last_30_days": None,  # filled below
        }
    # last 30 days
    cutoff = (datetime.strptime(today, "%Y-%m-%d").toordinal() - 30)
    for a in AREA_ORDER:
        by_area[a]["issued_last_30_days"] = sum(
            1 for r in rows if r["area"] == a and r["issue_date"]
            and datetime.strptime(r["issue_date"], "%Y-%m-%d").toordinal() >= cutoff
        )
    totals = {
        "permits_total": len(rows),
        "single_family_dwellings": sum(1 for r in rows if r["use_desc"] == "Dwelling - Single Family"),
        "wildfire_rebuild_flagged": sum(1 for r in rows if r["wildfire_rebuild"] == "yes"),
        "status": {s: sum(1 for r in rows if r["status"] == s) for s in STATUS_ORDER},
        "construction_class": {c: sum(1 for r in rows if r["construction_class"] == c) for c in CLASS_ORDER},
    }
    return {
        "months": months,
        "by_month_submitted": {a: {m: by_month_submitted[a].get(m, 0) for m in months} for a in AREA_ORDER},
        "by_month_issued": {a: {m: by_month_issued[a].get(m, 0) for m in months} for a in AREA_ORDER},
        "by_area": by_area,
        "totals": totals,
    }


# ── Outputs: CSV / JSON / history ────────────────────────────────────
def write_csv(rows: list) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in CSV_COLUMNS})


def read_csv() -> list:
    rows = []
    with CSV_PATH.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            for k in ("square_footage", "valuation_usd", "height_ft", "lat", "lon"):
                r[k] = _num(r.get(k))
            rows.append(r)
    return rows


def write_json(meta: dict, agg: dict, rows: list) -> None:
    payload = {"meta": meta, "summary": agg, "permits": rows}
    JSON_PATH.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")


def update_history(today: str, agg: dict) -> list:
    history = []
    if HISTORY_PATH.exists():
        try:
            history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            history = []
    snapshot = {
        "retrieved": today,
        "totals": agg["totals"],
        "by_area": {a: {"permits_total": agg["by_area"][a]["permits_total"],
                        "status": agg["by_area"][a]["status"]} for a in AREA_ORDER},
    }
    history = [h for h in history if h.get("retrieved") != today]
    history.append(snapshot)
    HISTORY_PATH.write_text(json.dumps(history, indent=1), encoding="utf-8")
    return history


# ── Chart ────────────────────────────────────────────────────────────
def render_png(agg: dict, today: str) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
    except Exception as exc:  # matplotlib mancante → non fatale
        log(f"  PNG skipped (matplotlib unavailable: {exc})")
        return False

    months = agg["months"]
    labels = [datetime.strptime(m, "%Y-%m").strftime("%b\n%y") for m in months]
    x = list(range(len(months)))
    serif = "Georgia"
    sans = "Helvetica Neue" if any("Helvetica" in f.name for f in font_manager.fontManager.ttflist) else "DejaVu Sans"

    fig = plt.figure(figsize=(12, 7.2), dpi=100, facecolor=PAPER)
    gs = fig.add_gridspec(1, 3, width_ratios=[2.35, 1, 1], wspace=0.32, left=0.06, right=0.98, top=0.80, bottom=0.22)

    # Panel 1: submitted per month, stacked by area
    ax = fig.add_subplot(gs[0, 0], facecolor=PAPER)
    bottom = [0] * len(months)
    for a in AREA_ORDER:
        vals = [agg["by_month_submitted"][a][m] for m in months]
        ax.bar(x, vals, bottom=bottom, color=AREA_COLORS[a], width=0.72, label=a, linewidth=0)
        bottom = [b + v for b, v in zip(bottom, vals)]
    issued_total = [sum(agg["by_month_issued"][a][m] for a in AREA_ORDER) for m in months]
    ax.plot(x, issued_total, color=INK, linewidth=1.6, marker="o", markersize=3.2, label="Permits issued (all areas)")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=7.5, color=CHARCOAL, family=sans)
    ax.set_title("New-construction permits submitted per month, by area", loc="left", fontsize=11,
                 color=INK, family=serif, pad=10)
    ax.tick_params(axis="y", labelsize=8, colors=CHARCOAL)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(STONE)
    ax.yaxis.grid(True, color=LINEN, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, fontsize=7.5, loc="upper left", ncol=1, labelcolor=CHARCOAL)

    # Panel 2: status pipeline (all areas)
    ax2 = fig.add_subplot(gs[0, 1], facecolor=PAPER)
    st = agg["totals"]["status"]
    names = STATUS_ORDER
    vals = [st.get(n, 0) for n in names]
    cols = [SAND, TERRACOTTA, OLIVE, STONE]
    ax2.barh(range(len(names)), vals, color=cols, height=0.62, linewidth=0)
    ax2.set_yticks(range(len(names)))
    ax2.set_yticklabels([n.replace(" (", "\n(") for n in names], fontsize=7.5, color=CHARCOAL, family=sans)
    ax2.invert_yaxis()
    for i, v in enumerate(vals):
        ax2.text(v + max(vals) * 0.02, i, f"{v:,}", va="center", fontsize=8, color=INK, family=sans)
    ax2.set_xlim(0, max(vals) * 1.25 if vals and max(vals) else 1)
    ax2.set_title("Where the permits stand", loc="left", fontsize=11, color=INK, family=serif, pad=10)
    for s in ("top", "right", "bottom"):
        ax2.spines[s].set_visible(False)
    ax2.spines["left"].set_color(STONE)
    ax2.set_xticks([])

    # Panel 3: construction class (all areas)
    ax3 = fig.add_subplot(gs[0, 2], facecolor=PAPER)
    cc = agg["totals"]["construction_class"]
    cnames = [c for c in CLASS_ORDER if cc.get(c, 0) > 0]
    cvals = [cc[c] for c in cnames]
    ccols = {
        "Type V (wood-frame)": SAND,
        "Type I/II (non-combustible)": TERRACOTTA,
        "Type III/IV (mixed / heavy timber)": OLIVE,
        "Other": STONE,
        "Not stated": LINEN,
    }
    ax3.barh(range(len(cnames)), cvals, color=[ccols[c] for c in cnames], height=0.62, linewidth=0)
    ax3.set_yticks(range(len(cnames)))
    ax3.set_yticklabels([c.replace(" (", "\n(") for c in cnames], fontsize=7.5, color=CHARCOAL, family=sans)
    ax3.invert_yaxis()
    total = sum(cvals) or 1
    for i, v in enumerate(cvals):
        ax3.text(v + max(cvals) * 0.02, i, f"{v:,}  ({v / total * 100:.1f}%)", va="center", fontsize=8, color=INK, family=sans)
    ax3.set_xlim(0, max(cvals) * 1.45 if cvals else 1)
    ax3.set_title("Declared construction type", loc="left", fontsize=11, color=INK, family=serif, pad=10)
    for s in ("top", "right", "bottom"):
        ax3.spines[s].set_visible(False)
    ax3.spines["left"].set_color(STONE)
    ax3.set_xticks([])

    tot = agg["totals"]["permits_total"]
    fig.text(0.06, 0.93, "Westside Rebuild Tracker", fontsize=20, color=INK, family=serif, weight="normal")
    fig.text(0.06, 0.885, f"{tot:,} new 1–2 family building permits submitted to LADBS since {START_DATE} · "
             "Pacific Palisades, Brentwood, Bel Air / Beverly Crest, Encino / Tarzana (City of Los Angeles only)",
             fontsize=8.6, color=CHARCOAL, family=sans)
    fig.text(0.06, 0.085, f"Source: City of Los Angeles, Department of Building and Safety, open data portal data.lacity.org "
             f"(dataset {DATASET_ID}). Retrieved {today}.",
             fontsize=7.4, color=STONE, family=sans)
    fig.text(0.06, 0.058, "Construction type as declared on the permit. Malibu and unincorporated LA County are not in LADBS data. "
             "Current month is partial.",
             fontsize=7.4, color=STONE, family=sans)
    fig.text(0.06, 0.03, "myvilla.la/research/westside-rebuild-tracker.html · chart CC BY 4.0 · underlying records: City of Los Angeles",
             fontsize=7.4, color=STONE, family=sans)
    fig.text(0.98, 0.03, "MY VILLA", fontsize=9, color=SAND, family=serif, ha="right")

    IMG_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(PNG_PATH, dpi=100, facecolor=PAPER)
    plt.close(fig)
    return True


# ── HTML page ────────────────────────────────────────────────────────
def fmt_int(v) -> str:
    return f"{int(v):,}" if v is not None else "—"


def fmt_pct(part, whole) -> str:
    return f"{part / whole * 100:.1f}%" if whole else "—"


def fmt_money(v) -> str:
    return f"${v:,.0f}" if v is not None else "—"


def month_label(m: str) -> str:
    return datetime.strptime(m, "%Y-%m").strftime("%b %Y")


def render_html(agg: dict, meta: dict, history: list) -> str:
    today = meta["retrieved"]
    t = agg["totals"]
    ba = agg["by_area"]
    tot = t["permits_total"] or 1
    pp = ba["Pacific Palisades"]
    non_comb = t["construction_class"].get("Type I/II (non-combustible)", 0)
    wood = t["construction_class"].get("Type V (wood-frame)", 0)
    stated = tot - t["construction_class"].get("Not stated", 0)
    issued_or_done = t["status"]["Issued (under construction)"] + t["status"]["Completed (CofO / finaled)"]

    _t = datetime.strptime(today, "%Y-%m-%d")
    months_since = (_t.year - 2025) * 12 + _t.month - 1 - (1 if _t.day < 7 else 0)
    description = (
        f"Weekly tracker of {tot:,} new-home building permits filed with LADBS in Pacific Palisades, Brentwood, "
        f"Bel Air/Beverly Crest and Encino/Tarzana since the January 2025 fires: status, month, area and declared "
        f"construction type (wood-frame vs non-combustible). Open data, CSV/JSON download. Updated {today}."
    )

    # Tables
    area_rows = ""
    for a in AREA_ORDER:
        d = ba[a]
        area_rows += (
            "<tr>"
            f"<td><strong>{escape(a)}</strong><br><span class='muted'>ZIP {', '.join(AREAS[a])}</span></td>"
            f"<td class='num'>{fmt_int(d['permits_total'])}</td>"
            f"<td class='num'>{fmt_int(d['status']['In plan check'])}</td>"
            f"<td class='num'>{fmt_int(d['status']['Issued (under construction)'])}</td>"
            f"<td class='num'>{fmt_int(d['status']['Completed (CofO / finaled)'])}</td>"
            f"<td class='num'>{fmt_int(d['wildfire_rebuild_flagged'])}</td>"
            f"<td class='num'>{fmt_int(d['construction_class']['Type I/II (non-combustible)'])}"
            f" <span class='muted'>({fmt_pct(d['construction_class']['Type I/II (non-combustible)'], d['permits_total'])})</span></td>"
            f"<td class='num'>{fmt_int(d['median_days_submitted_to_issued'])}</td>"
            f"<td class='num'>{fmt_int(d['median_sqft_single_family'])}</td>"
            "</tr>"
        )

    month_rows = ""
    for m in reversed(agg["months"]):
        subs = [agg["by_month_submitted"][a][m] for a in AREA_ORDER]
        iss = sum(agg["by_month_issued"][a][m] for a in AREA_ORDER)
        month_rows += (
            f"<tr><td>{month_label(m)}</td>"
            + "".join(f"<td class='num'>{v:,}</td>" for v in subs)
            + f"<td class='num'><strong>{sum(subs):,}</strong></td><td class='num'>{iss:,}</td></tr>"
        )

    class_rows = ""
    for c in CLASS_ORDER:
        v = t["construction_class"].get(c, 0)
        if v == 0:
            continue
        class_rows += f"<tr><td>{escape(c)}</td><td class='num'>{v:,}</td><td class='num'>{fmt_pct(v, tot)}</td></tr>"

    status_rows = ""
    for s in STATUS_ORDER:
        v = t["status"].get(s, 0)
        status_rows += f"<tr><td>{escape(s)}</td><td class='num'>{v:,}</td><td class='num'>{fmt_pct(v, tot)}</td></tr>"

    history_rows = ""
    for h in history[-12:]:
        history_rows += (
            f"<tr><td>{escape(h['retrieved'])}</td>"
            f"<td class='num'>{h['totals']['permits_total']:,}</td>"
            f"<td class='num'>{h['totals']['status'].get('In plan check', 0):,}</td>"
            f"<td class='num'>{h['totals']['status'].get('Issued (under construction)', 0):,}</td>"
            f"<td class='num'>{h['totals']['status'].get('Completed (CofO / finaled)', 0):,}</td>"
            f"<td class='num'>{h['totals']['construction_class'].get('Type I/II (non-combustible)', 0):,}</td></tr>"
        )

    apa = (f"My Villa. ({today[:4]}). <em>Westside Rebuild Tracker: LADBS new-construction permits after the "
           f"January 2025 fires</em> [Data set, retrieved {today}]. Los Angeles Open Data, City of Los Angeles "
           f"Department of Building and Safety. {PAGE_URL}")
    embed_code = escape(
        f'<a href="{PAGE_URL}"><img src="{BASE_URL}/research/img/westside-rebuild-tracker.png" '
        f'alt="Westside Rebuild Tracker — LADBS new-home permits since January 2025 (My Villa)" width="1200" '
        f'style="max-width:100%;height:auto"></a>\n<p>Source: <a href="{PAGE_URL}">Westside Rebuild Tracker</a> by My Villa, '
        f'from City of Los Angeles / LADBS open data. Updated {today}.</p>'
    )

    schema = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "Dataset",
                "@id": f"{PAGE_URL}#dataset",
                "name": "Westside Rebuild Tracker — LADBS new-construction permits since January 2025",
                "description": description,
                "url": PAGE_URL,
                "keywords": ["Pacific Palisades rebuild", "building permits", "LADBS", "wildfire rebuild",
                             "Brentwood", "Bel Air", "Encino", "non-combustible construction", "Type V construction"],
                "license": "https://creativecommons.org/licenses/by/4.0/",
                "isAccessibleForFree": True,
                "creator": {"@type": "Organization", "@id": f"{BASE_URL}/#organization", "name": "My Villa", "url": BASE_URL},
                "publisher": {"@type": "Organization", "@id": f"{BASE_URL}/#organization", "name": "My Villa", "url": BASE_URL},
                "isBasedOn": {
                    "@type": "Dataset",
                    "name": DATASET_NAME,
                    "url": DATASET_PAGE,
                    "creator": {"@type": "GovernmentOrganization", "name": "City of Los Angeles, Department of Building and Safety"},
                },
                "temporalCoverage": f"{START_DATE}/{today}",
                "spatialCoverage": {"@type": "Place", "name": "Westside Los Angeles (Pacific Palisades, Brentwood, Bel Air / Beverly Crest, Encino / Tarzana), City of Los Angeles, California"},
                "dateModified": today,
                "variableMeasured": ["permits submitted per month", "permit status", "declared construction type", "wildfire rebuild flag"],
                "distribution": [
                    {"@type": "DataDownload", "encodingFormat": "text/csv", "contentUrl": f"{BASE_URL}/research/data/westside-rebuild-permits.csv"},
                    {"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": f"{BASE_URL}/research/data/westside-rebuild-permits.json"},
                ],
            },
            {
                "@type": "BreadcrumbList",
                "@id": f"{PAGE_URL}#breadcrumb",
                "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": "Home", "item": BASE_URL},
                    {"@type": "ListItem", "position": 2, "name": "Research", "item": f"{BASE_URL}/research/westside-rebuild-tracker.html"},
                    {"@type": "ListItem", "position": 3, "name": "Westside Rebuild Tracker", "item": PAGE_URL},
                ],
            },
        ],
    }

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<!-- Google tag (gtag.js) -->
<script async src="https://www.googletagmanager.com/gtag/js?id={GA4_ID}"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){{dataLayer.push(arguments);}}
  gtag('js', new Date());
  gtag('config', '{GA4_ID}');
</script>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">

<!-- Primary SEO -->
<title>Westside Rebuild Tracker — LADBS Permits after the January 2025 Fires | My Villa Research</title>
<meta name="description" content="{escape(description)}">
<meta name="keywords" content="Pacific Palisades rebuild tracker, Palisades rebuild permits, LADBS building permits 2025, wildfire rebuild Los Angeles data, Brentwood new construction permits, Bel Air building permits, Encino Tarzana hillside permits, Type V wood frame vs non-combustible, rebuild progress Pacific Palisades 2026">
<meta name="author" content="My Villa Research">
<meta name="robots" content="index, follow, max-image-preview:large">
<link rel="canonical" href="{PAGE_URL}">

<!-- Open Graph -->
<meta property="og:type" content="article">
<meta property="og:url" content="{PAGE_URL}">
<meta property="og:title" content="Westside Rebuild Tracker — LADBS permits since January 2025">
<meta property="og:description" content="{escape(description)}">
<meta property="og:image" content="{BASE_URL}/research/img/westside-rebuild-tracker.png">
<meta property="og:site_name" content="My Villa">
<meta property="og:locale" content="en_US">

<!-- Twitter Card -->
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="Westside Rebuild Tracker — LADBS permits since January 2025">
<meta name="twitter:description" content="{escape(description)}">
<meta name="twitter:image" content="{BASE_URL}/research/img/westside-rebuild-tracker.png">

<!-- Geo -->
<meta name="geo.region" content="US-CA">
<meta name="geo.placename" content="Pacific Palisades, Los Angeles">
<meta name="geo.position" content="34.0480;-118.5260">
<meta name="ICBM" content="34.0480, -118.5260">

<!-- Favicon -->
<link rel="icon" type="image/svg+xml" href="../img/logos/favicon.svg">
<link rel="apple-touch-icon" href="../img/logos/apple-touch-icon.png">

<!-- Fonts -->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,500;0,600;1,400;1,500&family=Montserrat:wght@300;400;500;600;700&display=swap" rel="stylesheet">

<!-- Schema.org — Dataset + BreadcrumbList -->
<script type="application/ld+json">
{json.dumps(schema, indent=2, ensure_ascii=False)}
</script>

<style>
*, *::before, *::after {{ margin: 0; padding: 0; box-sizing: border-box; }}
:root {{
  --serif: 'Cormorant Garamond', Georgia, serif;
  --sans: 'Montserrat', 'Helvetica Neue', Arial, sans-serif;
  --offblack: #1a1816; --cream: #FAF8F5; --warm-sand: #D4B896; --terracotta: #C2714F;
  --olive: #5C6B4F; --tuscan-gold: #C4A265; --espresso: #3E2F2B; --charcoal: #2C2C2C;
  --stone-grey: #A09890; --light-linen: #EDE6DC; --line: rgba(44,44,44,0.12);
}}
html {{ scroll-behavior: smooth; -webkit-font-smoothing: antialiased; }}
body {{ font-family: var(--sans); background: var(--cream); color: var(--charcoal); line-height: 1.8; }}
img {{ display: block; max-width: 100%; }}
a {{ color: inherit; text-decoration: none; }}
.nav {{ position: fixed; top: 0; left: 0; right: 0; z-index: 100; display: flex; align-items: center; justify-content: space-between; padding: 20px clamp(24px, 5vw, 120px); background: var(--offblack); }}
.nav-logo-wrap {{ display: flex; flex-direction: column; gap: 2px; }}
.nav-logo {{ font-family: var(--serif); font-size: 18px; font-weight: 400; letter-spacing: 0.25em; color: var(--warm-sand); }}
.nav-payoff {{ font-size: 10px; font-weight: 400; letter-spacing: 0.2em; color: var(--stone-grey); text-transform: uppercase; }}
.nav-cta {{ padding: 10px 20px; border: 1px solid var(--warm-sand); color: var(--warm-sand); font-size: 11px; letter-spacing: 0.15em; text-transform: uppercase; font-weight: 500; transition: all 0.3s; }}
.nav-cta:hover {{ background: var(--warm-sand); color: var(--offblack); }}
.pillar-hero {{ padding: 160px clamp(24px, 5vw, 80px) 40px; max-width: 1100px; margin: 0 auto; text-align: center; }}
.pillar-eyebrow {{ font-size: 11px; letter-spacing: 0.35em; font-weight: 500; color: var(--terracotta); text-transform: uppercase; margin-bottom: 28px; }}
.pillar-title {{ font-family: var(--serif); font-size: clamp(36px, 5.6vw, 66px); font-weight: 400; line-height: 1.08; color: var(--offblack); margin-bottom: 24px; letter-spacing: -0.01em; }}
.pillar-title em {{ font-style: italic; color: var(--terracotta); }}
.pillar-lede {{ font-family: var(--serif); font-size: clamp(19px, 2.2vw, 23px); font-weight: 400; line-height: 1.55; color: rgba(44,44,44,0.82); max-width: 840px; margin: 0 auto; }}
.updated {{ margin-top: 18px; font-size: 11px; letter-spacing: 0.18em; text-transform: uppercase; color: var(--stone-grey); }}
.stat-strip {{ max-width: 1100px; margin: 40px auto 0; padding: 28px clamp(24px, 5vw, 80px); border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 24px; }}
.stat-item {{ text-align: center; }}
.stat-value {{ font-family: var(--serif); font-size: 36px; color: var(--terracotta); font-weight: 400; line-height: 1; margin-bottom: 6px; }}
.stat-label {{ font-family: var(--sans); font-size: 11px; text-transform: uppercase; letter-spacing: 0.15em; color: var(--stone-grey); }}
.pillar-container {{ max-width: 1000px; margin: 0 auto; padding: 50px clamp(24px, 5vw, 60px) 80px; }}
h2 {{ font-family: var(--serif); font-size: clamp(28px, 3.4vw, 38px); font-weight: 500; color: var(--offblack); margin-top: 70px; margin-bottom: 20px; letter-spacing: -0.01em; line-height: 1.2; }}
h3 {{ font-family: var(--serif); font-size: clamp(21px, 2.5vw, 26px); font-weight: 500; color: var(--charcoal); margin-top: 36px; margin-bottom: 12px; }}
p {{ font-size: 16px; line-height: 1.85; color: rgba(44,44,44,0.85); margin-bottom: 20px; }}
.container-lede {{ font-family: var(--serif); font-size: 22px; font-style: italic; color: var(--espresso); line-height: 1.55; margin-bottom: 28px; padding-left: 20px; border-left: 2px solid var(--terracotta); }}
a.inline-link {{ color: var(--terracotta); border-bottom: 1px solid transparent; transition: color 0.2s; }}
a.inline-link:hover {{ color: var(--offblack); border-bottom-color: var(--terracotta); }}
strong {{ color: var(--offblack); font-weight: 600; }}
ul, ol {{ margin: 18px 0 22px 22px; }}
li {{ margin-bottom: 10px; line-height: 1.7; color: rgba(44,44,44,0.85); }}
.chart {{ margin: 30px 0; border: 1px solid var(--line); background: #fff; }}
.chart img {{ width: 100%; height: auto; }}
.table-wrap {{ overflow-x: auto; margin: 20px 0 30px; border: 1px solid var(--line); background: #fff; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13.5px; }}
th, td {{ padding: 10px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
th {{ font-family: var(--sans); font-size: 10.5px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--stone-grey); background: var(--light-linen); font-weight: 600; }}
td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
.muted {{ color: var(--stone-grey); font-size: 12px; }}
.note {{ padding: 20px 24px; background: var(--light-linen); border-left: 3px solid var(--terracotta); border-radius: 4px; margin: 24px 0; font-size: 14.5px; }}
.note p {{ font-size: 14.5px; margin-bottom: 8px; }}
.note p:last-child {{ margin-bottom: 0; }}
.dl-row {{ display: flex; flex-wrap: wrap; gap: 12px; margin: 20px 0 10px; }}
.btn-dl {{ display: inline-block; padding: 12px 22px; border: 1px solid var(--espresso); color: var(--espresso); font-size: 11px; letter-spacing: 0.18em; text-transform: uppercase; font-weight: 500; transition: all 0.3s; }}
.btn-dl:hover {{ background: var(--espresso); color: var(--cream); }}
.cite-box {{ background: #fff; border: 1px solid var(--line); border-radius: 4px; padding: 24px; margin: 24px 0; }}
.cite-box h4 {{ font-family: var(--sans); font-size: 11px; letter-spacing: 0.18em; text-transform: uppercase; color: var(--terracotta); margin-bottom: 10px; }}
.cite-box pre {{ white-space: pre-wrap; word-break: break-all; font-size: 12px; line-height: 1.55; background: var(--light-linen); padding: 14px; border-radius: 3px; margin: 8px 0 18px; }}
.cite-box p {{ font-size: 14px; margin-bottom: 12px; }}
.pillar-cta {{ margin-top: 60px; padding: 46px 36px; background: var(--offblack); color: var(--cream); border-radius: 6px; text-align: center; }}
.pillar-cta h2 {{ color: var(--warm-sand); margin: 0 0 14px 0; font-size: clamp(24px, 3vw, 32px); }}
.pillar-cta p {{ color: rgba(250,248,245,0.82); font-size: 15px; max-width: 600px; margin: 0 auto 24px; }}
.pillar-cta .btn {{ display: inline-block; padding: 14px 32px; border: 1px solid var(--warm-sand); color: var(--warm-sand); font-size: 12px; letter-spacing: 0.22em; text-transform: uppercase; font-weight: 500; transition: all 0.3s; }}
.pillar-cta .btn:hover {{ background: var(--warm-sand); color: var(--offblack); }}
.footer {{ padding: 40px 24px; background: var(--offblack); color: rgba(212,184,150,0.7); text-align: center; font-size: 12px; letter-spacing: 0.05em; }}
.footer a {{ color: var(--warm-sand); }}
.footer-copy {{ margin-bottom: 8px; }}
@media (max-width: 768px) {{
  .nav {{ padding: 16px 20px; }} .nav-payoff {{ display: none; }}
  .pillar-hero {{ padding: 120px 20px 30px; }} .pillar-container {{ padding: 30px 16px 60px; }}
  h2 {{ margin-top: 50px; }} .stat-value {{ font-size: 28px; }}
}}
</style>
</head>
<body>

<nav class="nav">
  <a href="../index.html" class="nav-logo-wrap">
    <span class="nav-logo">MY VILLA</span>
    <span class="nav-payoff">Italian Soul · Californian Body</span>
  </a>
  <a href="{BRIEFING_URL}" class="nav-cta">Request Briefing</a>
</nav>

<section class="pillar-hero">
  <div class="pillar-eyebrow">My Villa Research · Open Data</div>
  <h1 class="pillar-title">Westside Rebuild Tracker: <em>LADBS permits</em> since the January 2025 fires</h1>
  <p class="pillar-lede">Every new-home building permit filed with the Los Angeles Department of Building and Safety in Pacific Palisades, Brentwood, Bel Air / Beverly Crest and Encino / Tarzana since {START_DATE}, by month, status and declared construction type. Rebuilt every Monday from the City's open data.</p>
  <div class="updated">Data retrieved {today} · next refresh Monday 05:30 UTC</div>
</section>

<div class="stat-strip">
  <div class="stat-item"><div class="stat-value">{tot:,}</div><div class="stat-label">New 1–2 family permits filed</div></div>
  <div class="stat-item"><div class="stat-value">{pp['permits_total']:,}</div><div class="stat-label">In Pacific Palisades (90272)</div></div>
  <div class="stat-item"><div class="stat-value">{issued_or_done:,}</div><div class="stat-label">Issued or completed</div></div>
  <div class="stat-item"><div class="stat-value">{fmt_pct(non_comb, stated)}</div><div class="stat-label">Declared non-combustible (Type I/II)</div></div>
</div>

<div class="pillar-container">

  <p class="container-lede">{months_since} months after the Palisades Fire, the rebuild is visible one permit at a time. This page counts them, and shows what the permits themselves say about how the Westside is being rebuilt.</p>

  <div class="chart">
    <img src="img/westside-rebuild-tracker.png" alt="Chart: new-construction permits submitted per month by area, permit status pipeline, and declared construction type — LADBS open data, retrieved {today}" width="1200" height="720" loading="eager">
  </div>

  <h2>What the permits say</h2>
  <p>Of the <strong>{tot:,}</strong> new 1–2 family building permits filed in the four areas since {START_DATE}, <strong>{t['wildfire_rebuild_flagged']:,}</strong> carry the LADBS "2025 wildfire rebuild" work description, almost all of them in Pacific Palisades. <strong>{t['status']['Issued (under construction)']:,}</strong> permits are issued and presumably under construction, <strong>{t['status']['Completed (CofO / finaled)']:,}</strong> have reached a Certificate of Occupancy or final, and <strong>{t['status']['In plan check']:,}</strong> are still in plan check or verification.</p>
  <p>On construction type, the permits are explicit. Where a type is declared, <strong>{fmt_pct(wood, stated)}</strong> ({wood:,} permits) are Type V, the wood-frame category, and <strong>{fmt_pct(non_comb, stated)}</strong> ({non_comb:,}) are Type I or II, the non-combustible category that covers reinforced concrete and steel structures. We publish this share without editorial spin: it is the clearest public measure of how much of the Westside is being rebuilt with the same structural logic as before.</p>

  <h3>By area</h3>
  <div class="table-wrap">
  <table>
    <thead><tr><th>Area</th><th class="num">Permits</th><th class="num">In plan check</th><th class="num">Issued</th><th class="num">Completed</th><th class="num">Wildfire rebuild</th><th class="num">Non-combustible</th><th class="num">Median days to issue</th><th class="num">Median sq ft (SFD)</th></tr></thead>
    <tbody>{area_rows}</tbody>
  </table>
  </div>

  <h3>Status of all permits</h3>
  <div class="table-wrap"><table>
    <thead><tr><th>Status</th><th class="num">Permits</th><th class="num">Share</th></tr></thead>
    <tbody>{status_rows}</tbody></table></div>

  <h3>Declared construction type</h3>
  <div class="table-wrap"><table>
    <thead><tr><th>Construction type (California Building Code)</th><th class="num">Permits</th><th class="num">Share</th></tr></thead>
    <tbody>{class_rows}</tbody></table></div>
  <p class="muted">Type V-A and V-B are wood-frame; Type I and II are non-combustible (concrete, masonry, steel); Type III/IV are mixed or heavy-timber assemblies. The type is declared by the applicant on the permit and is not verified by us.</p>

  <h3>Permits by month</h3>
  <div class="table-wrap"><table>
    <thead><tr><th>Month</th>{"".join(f"<th class='num'>{escape(a)}</th>" for a in AREA_ORDER)}<th class="num">Submitted</th><th class="num">Issued</th></tr></thead>
    <tbody>{month_rows}</tbody></table></div>
  <p class="muted">"Submitted" is the month the application was filed; "Issued" the month the permit was granted (all areas). The current month is partial.</p>

  <h3>Tracker history</h3>
  <p>Each weekly refresh adds a row, so the pipeline can be read over time.</p>
  <div class="table-wrap"><table>
    <thead><tr><th>Snapshot</th><th class="num">Permits</th><th class="num">In plan check</th><th class="num">Issued</th><th class="num">Completed</th><th class="num">Non-combustible</th></tr></thead>
    <tbody>{history_rows}</tbody></table></div>

  <h2>Method</h2>
  <ul>
    <li><strong>Source.</strong> City of Los Angeles Open Data portal, dataset <a class="inline-link" href="{DATASET_PAGE}" rel="noopener">{DATASET_NAME}</a> (id <code>{DATASET_ID}</code>), queried through the public SODA API without a token. The companion <a class="inline-link" href="{DATASET_ISSUED_PAGE}" rel="noopener">Permits Issued</a> dataset ({DATASET_ISSUED_ID}) was used to verify the fields. LADBS refreshes the data weekly.</li>
    <li><strong>Filter.</strong> <code>permit_type = "Bldg-New"</code>, <code>permit_sub_type = "1 or 2 Family Dwelling"</code>, <code>submitted_date ≥ {START_DATE}</code>, ZIP codes {", ".join(sorted(ZIP_TO_AREA))}. Additions, alterations, grading, pools and demolitions are excluded. Accessory dwelling units and garages filed as separate new-building permits are included and shown by use in the CSV.</li>
    <li><strong>Areas.</strong> Pacific Palisades = 90272; Brentwood = 90049; Bel Air / Beverly Crest = 90077 and the City of Los Angeles portion of 90210; Encino / Tarzana = 91316, 91356, 91436. The CSV keeps the LADBS community plan area and hillside flag for each permit.</li>
    <li><strong>Wildfire rebuild flag.</strong> Set when the permit's work description contains "WILDFIRE REBUILD", the wording LADBS uses for post-fire reconstruction.</li>
    <li><strong>Construction type.</strong> Taken from the permit's <code>construction</code> field (California Building Code Types I–V) exactly as declared by the applicant.</li>
    <li><strong>Status.</strong> LADBS <code>status_desc</code> grouped into four buckets: in plan check, issued, completed (CofO issued or permit finaled), withdrawn / inactive.</li>
    <li><strong>Refresh.</strong> Rebuilt automatically every Monday at 05:30 UTC. Retrieval date is printed on the chart and in the JSON.</li>
  </ul>

  <h2>Limits</h2>
  <div class="note">
    <p><strong>Malibu is not here.</strong> Malibu is an incorporated city with its own building department; its permits are not in LADBS data. The same applies to Topanga and other unincorporated areas handled by Los Angeles County, and to the City of Beverly Hills proper (only the City of Los Angeles part of ZIP 90210 appears).</p>
    <p><strong>A permit is not a house.</strong> A submitted permit may be revised, withdrawn or never built; an issued permit marks the start of construction, not its end. Several permits can refer to one lot (main house, ADU, garage).</p>
    <p><strong>Construction type is self-declared</strong> on the application and can change at plan check. We report it as filed.</p>
    <p><strong>The current month is incomplete</strong>, and the portal's weekly refresh may lag City records by several days.</p>
  </div>

  <h2>Download, cite, embed</h2>
  <div class="dl-row">
    <a class="btn-dl" href="data/westside-rebuild-permits.csv" download>Download CSV</a>
    <a class="btn-dl" href="data/westside-rebuild-permits.json" download>Download JSON</a>
    <a class="btn-dl" href="img/westside-rebuild-tracker.png" download>Download chart (PNG)</a>
  </div>
  <div class="cite-box">
    <h4>Cite (APA)</h4>
    <p>{apa}</p>
    <h4>Embed the chart</h4>
    <pre>{embed_code}</pre>
    <h4>License and attribution</h4>
    <p>The underlying permit records are public data of the City of Los Angeles, Department of Building and Safety, published on <a class="inline-link" href="{PORTAL_TERMS}" rel="noopener">data.lacity.org</a> and used under the portal's terms of use. Our aggregation, chart and page are released under <a class="inline-link" href="https://creativecommons.org/licenses/by/4.0/" rel="noopener">CC BY 4.0</a>: reuse freely with a link to this page.</p>
    <p class="muted">Provided "as is", without warranty of any kind. My Villa is not affiliated with the City of Los Angeles. Counts may change as LADBS updates its records; please cite the retrieval date.</p>
  </div>

  <div class="pillar-cta">
    <h2>Rebuilding on the Westside?</h2>
    <p>We design reinforced concrete villas engineered to stay insurable in California's fire zones. A private briefing is a conversation, not a pitch.</p>
    <a href="{BRIEFING_URL}" class="btn">Request a private briefing</a>
  </div>

</div>

<footer class="footer">
  <div class="footer-copy">© {today[:4]} My Villa · <a href="../index.html">Home</a> · <a href="../team.html">Team</a> · <a href="../blog/">Journal</a> · <a href="../insurable-home-california.html">Insurable home data</a> · <a href="../privacy.html">Privacy Policy</a></div>
  <div>Research · Westside Los Angeles · Pacific Palisades · Brentwood · Bel Air · Encino</div>
</footer>

</body>
</html>
"""
    return html


# ── Main ─────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="Westside Rebuild Tracker (LADBS open data)")
    parser.add_argument("--dry-run", action="store_true", help="fetch and summarise, write nothing")
    parser.add_argument("--offline", action="store_true", help="rebuild PNG/JSON/HTML from the existing CSV")
    args = parser.parse_args()

    today = date.today().isoformat()
    log(f"Westside Rebuild Tracker — {today}")
    try:
        if args.offline:
            if not CSV_PATH.exists():
                log("  ERROR: no CSV to rebuild from; run without --offline first")
                return 0
            rows = read_csv()
            log(f"  loaded {len(rows)} rows from {CSV_PATH.name}")
        else:
            log(f"  querying {SODA_DOMAIN}/resource/{DATASET_ID} ...")
            raw = fetch_permits()
            rows = normalize(raw)
            log(f"  {len(rows)} permits after normalisation")
            if len(rows) < 100:
                log("  WARN: suspiciously few rows — keeping previous outputs untouched")
                return 0

        agg = aggregate(rows, today)
        for a in AREA_ORDER:
            d = agg["by_area"][a]
            log(f"  {a:<26} {d['permits_total']:>5}  issued {d['status']['Issued (under construction)']:>5}  "
                f"non-comb {d['construction_class']['Type I/II (non-combustible)']:>3}")
        if args.dry_run:
            log("  dry-run: nothing written")
            return 0

        meta = {
            "title": PAGE_TITLE,
            "page": PAGE_URL,
            "retrieved": today,
            "source": {
                "publisher": "City of Los Angeles, Department of Building and Safety (LADBS)",
                "portal": SODA_DOMAIN,
                "dataset_id": DATASET_ID,
                "dataset_name": DATASET_NAME,
                "dataset_url": DATASET_PAGE,
                "terms": PORTAL_TERMS,
            },
            "filter": {
                "permit_type": "Bldg-New",
                "permit_sub_type": "1 or 2 Family Dwelling",
                "submitted_date_from": START_DATE,
                "areas": AREAS,
            },
            "license": "Aggregation CC BY 4.0 (My Villa); underlying records public data of the City of Los Angeles",
            "disclaimer": "Provided as is, without warranty. Construction type as declared on the permit. Malibu and unincorporated LA County are not covered by LADBS.",
        }
        if not args.offline:
            write_csv(rows)
            log(f"  wrote {CSV_PATH.relative_to(ROOT_DIR)}")
        write_json(meta, agg, rows)
        log(f"  wrote {JSON_PATH.relative_to(ROOT_DIR)}")
        history = update_history(today, agg)
        if render_png(agg, today):
            log(f"  wrote {PNG_PATH.relative_to(ROOT_DIR)}")
        HTML_PATH.write_text(render_html(agg, meta, history), encoding="utf-8")
        log(f"  wrote {HTML_PATH.relative_to(ROOT_DIR)}")
    except urllib.error.URLError as exc:
        log(f"  network error, outputs untouched: {exc}")
    except Exception:
        log("  unexpected error, outputs untouched:")
        traceback.print_exc()
    return 0


if __name__ == "__main__":
    sys.exit(main())
