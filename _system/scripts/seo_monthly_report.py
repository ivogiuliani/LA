#!/usr/bin/env python3
"""Rapporto SEO mensile My Villa (2026-08-26).

Gira il giorno 1 del mese (systemd: myvilla-seo-report.timer). Legge GA4 +
Search Console con la service account (GOOGLE_APPLICATION_CREDENTIALS),
genera un PDF con l'andamento (WeasyPrint, niente browser) e lo manda via
email — mittente Lisa/info@myvilla.la — a Ivo e Paolo con una premessa
sintetica sui numeri del mese appena chiuso.

Idempotente per mese: marker in _system/history/.last_seo_report.

Uso:
  python3 seo_monthly_report.py                # mese scorso, invia email
  python3 seo_monthly_report.py --no-email     # solo PDF
  python3 seo_monthly_report.py --force        # ignora il marker mensile
"""
import argparse
import calendar
import datetime as dt
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT_DIR / ".env")

GA4_URL = ("https://analyticsdata.googleapis.com/v1beta/"
           "properties/526743497:runReport")
GSC_URL = ("https://searchconsole.googleapis.com/webmasters/v3/sites/"
           "https%3A%2F%2Fmyvilla.la%2F/searchAnalytics/query")
HISTORY_START = dt.date(2026, 5, 1)
MARKER = ROOT_DIR / "_system" / "history" / ".last_seo_report"
OUT_DIR = Path(os.environ.get("SEO_REPORT_DIR",
                              str(ROOT_DIR.parent / "seo_reports")))
FONT_DIR = ROOT_DIR / "_system" / "assets" / "fonts"

MESI = ["", "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
        "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]

# palette (tema chiaro del rapporto SEO interattivo)
C = {"paper": "#FAF8F4", "card": "#FDFCFA", "ink": "#211C15",
     "ink2": "#6E6558", "mut": "#948A7C", "line": "#E6DFD3",
     "accent": "#A85A38", "s1": "#2a78d6", "s2": "#eb6834",
     "s3": "#1baf7a", "s4": "#eda100", "sx": "#B9B0A3",
     "grid": "#ECE6DB", "axis": "#C9C0B2", "good": "#006300"}


# ── Google API ──────────────────────────────────────────────────────
def _session():
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession
    key = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    creds = service_account.Credentials.from_service_account_file(
        key, scopes=[
            "https://www.googleapis.com/auth/analytics.readonly",
            "https://www.googleapis.com/auth/webmasters.readonly"])
    return AuthorizedSession(creds)


def ga4(s, body):
    r = s.post(GA4_URL, json=body, timeout=60)
    r.raise_for_status()
    return [{"dims": [d["value"] for d in row.get("dimensionValues", [])],
             "metrics": [m["value"] for m in row.get("metricValues", [])]}
            for row in r.json().get("rows", [])]


def gsc(s, body):
    r = s.post(GSC_URL, json=body, timeout=60)
    r.raise_for_status()
    return r.json().get("rows", [])


# ── SVG helpers (statici, niente JS: WeasyPrint) ───────────────────
def svg_area_weekly(weeks, vals, ymax, yticks, color):
    W, H, pl, pr, pt, pb = 760, 200, 42, 10, 12, 24
    iw, ih = W - pl - pr, H - pt - pb
    n = len(vals)
    x = lambda i: pl + iw * (i / max(1, n - 1))
    y = lambda v: pt + ih * (1 - v / ymax)
    parts = [f'<svg viewBox="0 0 {W} {H}" width="{W}" '
             f'xmlns="http://www.w3.org/2000/svg">']
    for tv in yticks:
        parts.append(f'<line x1="{pl}" x2="{W-pr}" y1="{y(tv):.1f}" '
                     f'y2="{y(tv):.1f}" stroke="{C["grid"]}"/>')
        parts.append(f'<text x="{pl-6}" y="{y(tv)+4:.1f}" text-anchor="end" '
                     f'font-size="10" fill="{C["mut"]}">{tv}</text>')
    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))
    first, last = f"{x(0):.1f},{y(0):.1f}", f"{x(n-1):.1f},{y(0):.1f}"
    parts.append(f'<polygon points="{first} {pts} {last}" '
                 f'fill="{color}" fill-opacity="0.13"/>')
    parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" '
                 'stroke-width="2" stroke-linejoin="round"/>')
    parts.append(f'<circle cx="{x(n-1):.1f}" cy="{y(vals[-1]):.1f}" r="3.5" '
                 f'fill="{color}"/>')
    parts.append(f'<text x="{x(n-1)-4:.1f}" y="{y(vals[-1])-8:.1f}" '
                 f'text-anchor="end" font-size="11" font-weight="700" '
                 f'fill="{C["ink"]}">{vals[-1]}</text>')
    for i in range(0, n, 2):
        parts.append(f'<text x="{x(i):.1f}" y="{H-6}" text-anchor="middle" '
                     f'font-size="9.5" fill="{C["mut"]}">{weeks[i]}</text>')
    parts.append(f'<line x1="{pl}" x2="{W-pr}" y1="{y(0):.1f}" '
                 f'y2="{y(0):.1f}" stroke="{C["axis"]}"/></svg>')
    return "".join(parts)


def svg_bars_weekly(weeks, vals, ymax, color):
    W, H, pl, pr, pt, pb = 760, 130, 42, 10, 14, 24
    iw, ih = W - pl - pr, H - pt - pb
    n = len(vals)
    xb = lambda i: pl + iw * (i + 0.5) / n
    y = lambda v: pt + ih * (1 - v / ymax)
    bw = min(22, iw / n - 5)
    parts = [f'<svg viewBox="0 0 {W} {H}" width="{W}" '
             f'xmlns="http://www.w3.org/2000/svg">']
    parts.append(f'<text x="{pl-6}" y="{y(ymax)+4:.1f}" text-anchor="end" '
                 f'font-size="10" fill="{C["mut"]}">{ymax}</text>')
    top_i = max(range(n), key=lambda i: vals[i])
    for i, v in enumerate(vals):
        parts.append(f'<rect x="{xb(i)-bw/2:.1f}" y="{y(v):.1f}" '
                     f'width="{bw:.1f}" height="{max(0.5, y(0)-y(v)):.1f}" '
                     f'rx="2" fill="{color}"/>')
        if i in (top_i, n - 1) and v > 0:
            parts.append(f'<text x="{xb(i):.1f}" y="{y(v)-5:.1f}" '
                         f'text-anchor="middle" font-size="10" '
                         f'font-weight="700" fill="{C["ink"]}">{v}</text>')
    for i in range(0, n, 2):
        parts.append(f'<text x="{xb(i):.1f}" y="{H-6}" text-anchor="middle" '
                     f'font-size="9.5" fill="{C["mut"]}">{weeks[i]}</text>')
    parts.append(f'<line x1="{pl}" x2="{W-pr}" y1="{y(0):.1f}" '
                 f'y2="{y(0):.1f}" stroke="{C["axis"]}"/></svg>')
    return "".join(parts)


def svg_month_stack(months):
    """months: [{label, art, vals:[org, dir, ref, ai, altro]}]"""
    W, H, pl, pr, pt, pb = 760, 250, 42, 10, 20, 40
    iw, ih = W - pl - pr, H - pt - pb
    maxv = max(sum(m["vals"]) for m in months) * 1.1
    y = lambda v: pt + ih * (1 - v / maxv)
    cols = [C["s1"], C["s2"], C["s3"], C["s4"], C["sx"]]
    bw = 78
    parts = [f'<svg viewBox="0 0 {W} {H}" width="{W}" '
             f'xmlns="http://www.w3.org/2000/svg">']
    for i, m in enumerate(months):
        cx = pl + iw * (i + 0.5) / len(months)
        acc = 0
        for ci, v in enumerate(m["vals"]):
            if not v:
                continue
            y0, y1 = y(acc), y(acc + v)
            parts.append(f'<rect x="{cx-bw/2:.0f}" y="{y1:.1f}" width="{bw}" '
                         f'height="{max(0.5, y0-y1-2):.1f}" rx="2" '
                         f'fill="{cols[ci]}"/>')
            if y0 - y1 > 16:
                fill = "#fff" if ci in (0, 1) else C["ink"]
                parts.append(f'<text x="{cx:.0f}" y="{(y0+y1)/2+4:.1f}" '
                             f'text-anchor="middle" font-size="10.5" '
                             f'font-weight="600" fill="{fill}">{v}</text>')
            acc += v
        parts.append(f'<text x="{cx:.0f}" y="{y(acc)-7:.1f}" '
                     f'text-anchor="middle" font-size="11" font-weight="700" '
                     f'fill="{C["ink"]}">{acc}</text>')
        parts.append(f'<text x="{cx:.0f}" y="{H-22}" text-anchor="middle" '
                     f'font-size="11" font-weight="600" '
                     f'fill="{C["ink2"]}">{m["label"]}</text>')
        parts.append(f'<text x="{cx:.0f}" y="{H-8}" text-anchor="middle" '
                     f'font-size="9.5" fill="{C["mut"]}">{m["art"]} art.</text>')
    parts.append("</svg>")
    return "".join(parts)


def esc(t):
    return (str(t).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


# ── main ────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    today = dt.date.today()
    first_this = today.replace(day=1)
    last_prev = first_this - dt.timedelta(days=1)
    first_prev = last_prev.replace(day=1)
    ym = f"{first_prev:%Y-%m}"

    if MARKER.exists() and MARKER.read_text().strip() == ym \
            and not args.force:
        print(f"[seo-report] {ym} già inviato — skip (--force per rifare)")
        return 0

    mese_nome = MESI[first_prev.month]
    print(f"[seo-report] Rapporto {mese_nome} {first_prev.year}")
    s = _session()
    end = min(today - dt.timedelta(days=1), last_prev)  # GSC ha ~2gg lag
    start_hist = max(HISTORY_START,
                     first_prev - dt.timedelta(weeks=25))

    # GSC giornaliero → settimane (solo settimane complete)
    daily = gsc(s, {"startDate": str(start_hist), "endDate": str(last_prev),
                    "dimensions": ["date"], "rowLimit": 400})
    wk = defaultdict(lambda: [0, 0])
    for row in daily:
        d = dt.date.fromisoformat(row["keys"][0])
        monday = d - dt.timedelta(days=d.weekday())
        wk[monday][0] += row.get("clicks", 0)
        wk[monday][1] += row.get("impressions", 0)
    mondays = sorted(m for m in wk if m + dt.timedelta(days=6) <= last_prev)
    weeks_lbl = [f"{m.day} {MESI[m.month][:3]}" for m in mondays]
    clicks_w = [round(wk[m][0]) for m in mondays]
    impr_w = [round(wk[m][1]) for m in mondays]

    # mese chiuso vs precedente
    def month_tot(d0, d1):
        rows = gsc(s, {"startDate": str(d0), "endDate": str(d1),
                       "dimensions": [], "rowLimit": 1})
        if rows:
            return (round(rows[0].get("clicks", 0)),
                    round(rows[0].get("impressions", 0)))
        return (0, 0)
    m_click, m_impr = month_tot(first_prev, last_prev)
    pp_last = first_prev - dt.timedelta(days=1)
    pp_first = pp_last.replace(day=1)
    p_click, p_impr = month_tot(pp_first, pp_last)

    # GA4: canali per mese (storia) — sessioni
    mc_rows = ga4(s, {
        "dateRanges": [{"startDate": str(HISTORY_START),
                        "endDate": str(last_prev)}],
        "dimensions": [{"name": "yearMonth"},
                       {"name": "sessionDefaultChannelGroup"}],
        "metrics": [{"name": "sessions"}], "limit": 200})
    mc = defaultdict(dict)
    for r in mc_rows:
        mc[r["dims"][0]][r["dims"][1]] = int(r["metrics"][0])

    # articoli pubblicati per mese (dai sidecar del journal)
    art_month = defaultdict(int)
    for j in (ROOT_DIR / "blog").glob("*.json"):
        if j.stem == "index" or j.stem.startswith("category"):
            continue
        try:
            d0 = json.load(open(j)).get("_date") or ""
        except Exception:
            continue
        if d0[:7]:
            art_month[d0[:7].replace("-", "")] += 1

    months_data = []
    for ymk in sorted(mc):
        ch = mc[ymk]
        org = ch.get("Organic Search", 0)
        dirc = ch.get("Direct", 0)
        ref = ch.get("Referral", 0)
        ai = ch.get("AI Assistant", 0)
        altro = sum(ch.values()) - org - dirc - ref - ai
        months_data.append({
            "label": MESI[int(ymk[4:6])][:3],
            "art": art_month.get(ymk, 0),
            "vals": [org, dirc, ref, ai, max(0, altro)]})
    cur = mc.get(f"{first_prev:%Y%m}", {})
    cur_tot = sum(cur.values()) or 1
    organic_share = round(cur.get("Organic Search", 0) / cur_tot * 100)

    # ── geografia (richiesta Ivo 2026-08-26: contano USA, e la
    # California in particolare) — mese chiuso ──
    geo_countries = ga4(s, {
        "dateRanges": [{"startDate": str(first_prev),
                        "endDate": str(last_prev)}],
        "dimensions": [{"name": "country"}],
        "metrics": [{"name": "sessions"}],
        "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
        "limit": 8})
    geo_tot = sum(int(r["metrics"][0]) for r in geo_countries) or 1
    us_sessions = next((int(r["metrics"][0]) for r in geo_countries
                        if r["dims"][0] == "United States"), 0)
    us_share = round(us_sessions / geo_tot * 100)

    geo_states = ga4(s, {
        "dateRanges": [{"startDate": str(first_prev),
                        "endDate": str(last_prev)}],
        "dimensions": [{"name": "region"}],
        "metrics": [{"name": "sessions"}],
        "dimensionFilter": {"filter": {
            "fieldName": "country",
            "stringFilter": {"value": "United States"}}},
        "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
        "limit": 6})
    ca_sessions = next((int(r["metrics"][0]) for r in geo_states
                        if r["dims"][0] == "California"), 0)
    ca_share_us = round(ca_sessions / us_sessions * 100) if us_sessions else 0

    # visibilità in ricerca dagli USA (GSC usa codici ISO-3 minuscoli)
    gsc_us = gsc(s, {"startDate": str(first_prev), "endDate": str(last_prev),
                     "dimensions": ["country"], "rowLimit": 50})
    gsc_impr_tot = sum(r.get("impressions", 0) for r in gsc_us) or 1
    gsc_us_row = next((r for r in gsc_us if r["keys"][0] == "usa"), {})
    gsc_us_impr_share = round(
        gsc_us_row.get("impressions", 0) / gsc_impr_tot * 100)

    # top pagine e query del mese
    pages = gsc(s, {"startDate": str(first_prev), "endDate": str(last_prev),
                    "dimensions": ["page"], "rowLimit": 12})
    queries = gsc(s, {"startDate": str(first_prev), "endDate": str(last_prev),
                      "dimensions": ["query"], "rowLimit": 10})

    # ── HTML ──
    def delta_str(cur_v, prev_v):
        if prev_v <= 0:
            return "n/d"
        pct = (cur_v - prev_v) / prev_v * 100
        return f"{pct:+.0f}%"

    ymax_i = max(impr_w + [10]) * 1.15
    yticks = [0, round(ymax_i / 3 / 50) * 50 or 50,
              round(ymax_i * 2 / 3 / 50) * 50 or 100]
    n_art = art_month.get(f"{first_prev:%Y%m}", 0)

    def hl(name, target):
        if name == target:
            return (f'<b style="color:{C["accent"]}">{esc(name)}</b>')
        return esc(name)

    country_rows = "".join(
        f"<tr><td>{hl(r['dims'][0], 'United States')}</td>"
        f"<td class='n'>{int(r['metrics'][0])}</td>"
        f"<td class='n'>{int(r['metrics'][0]) / geo_tot * 100:.0f}%</td></tr>"
        for r in geo_countries)
    state_rows = "".join(
        f"<tr><td>{hl(r['dims'][0], 'California')}</td>"
        f"<td class='n'>{int(r['metrics'][0])}</td></tr>"
        for r in geo_states) or "<tr><td colspan='2'>—</td></tr>"

    page_rows = "".join(
        f"<tr><td>{esc(r['keys'][0].replace('https://myvilla.la', '') or '/')}"
        f"</td><td class='n'>{r.get('clicks', 0):.0f}</td>"
        f"<td class='n'>{r.get('impressions', 0):.0f}</td>"
        f"<td class='n'>{r.get('position', 0):.0f}</td></tr>"
        for r in pages)
    query_rows = "".join(
        f"<tr><td>{esc(r['keys'][0])}</td>"
        f"<td class='n'>{r.get('clicks', 0):.0f}</td>"
        f"<td class='n'>{r.get('impressions', 0):.0f}</td>"
        f"<td class='n'>{r.get('position', 0):.1f}</td></tr>"
        for r in queries)
    legend = "".join(
        f'<span><i style="background:{c}"></i>{n}</span>'
        for n, c in [("Organico", C["s1"]), ("Diretto", C["s2"]),
                     ("Referral", C["s3"]), ("AI", C["s4"]),
                     ("Altro", C["sx"])])

    fraunces = (FONT_DIR / "fraunces-var.woff2").as_uri()
    publics = (FONT_DIR / "publicsans-var.woff2").as_uri()
    html = f"""<meta charset="utf-8">
<style>
@font-face{{font-family:Fraunces;src:url("{fraunces}");
  font-weight:100 900}}
@font-face{{font-family:"Public Sans";src:url("{publics}");
  font-weight:100 900}}
@page{{size:A4;margin:14mm 12mm;
  @bottom-right{{content:"My Villa · rapporto SEO {mese_nome} "
  "{first_prev.year} · pag. " counter(page);
  font-family:"Public Sans";font-size:8px;color:{C["mut"]}}}}}
body{{font-family:"Public Sans",sans-serif;font-size:10.5pt;
  line-height:1.55;color:{C["ink"]};background:{C["paper"]};margin:0}}
h1{{font-family:Fraunces,serif;font-size:26pt;font-weight:600;
  margin:6px 0 4px}}
h2{{font-family:Fraunces,serif;font-size:15pt;font-weight:600;
  margin:22px 0 4px}}
.kick{{color:{C["accent"]};font-size:7.5pt;font-weight:700;
  letter-spacing:.16em;text-transform:uppercase}}
.sub{{color:{C["ink2"]};margin:0 0 6px}}
.tiles{{width:100%;border-collapse:separate;border-spacing:8px 0;
  margin:14px -8px 4px}}
.tile{{background:{C["card"]};border:0.6pt solid {C["line"]};
  border-radius:5px;padding:11px 13px;width:25%;vertical-align:top}}
.tile .v{{font-family:Fraunces,serif;font-size:19pt;font-weight:700}}
.tile .l{{color:{C["ink2"]};font-size:8pt;line-height:1.4;margin-top:3px}}
.tile .d{{color:{C["good"]};font-size:8pt;font-weight:600;margin-top:2px}}
.chart{{background:{C["card"]};border:0.6pt solid {C["line"]};
  border-radius:5px;padding:12px 12px 6px;margin-top:8px;
  break-inside:avoid}}
h2{{break-after:avoid}}
table.data{{break-inside:avoid}}
.chart h4{{margin:0 0 6px;font-size:9.5pt}}
.legend{{font-size:8.5pt;color:{C["ink2"]};margin:0 0 6px}}
.legend span{{margin-right:14px}}
.legend i{{display:inline-block;width:8px;height:8px;border-radius:2px;
  margin-right:5px}}
table.data{{width:100%;border-collapse:collapse;font-size:8.8pt;
  background:{C["card"]};border:0.6pt solid {C["line"]};
  border-radius:5px;margin-top:8px}}
table.data th{{font-size:7pt;letter-spacing:.1em;text-transform:uppercase;
  color:{C["mut"]};text-align:left;padding:7px 10px 5px;
  border-bottom:0.6pt solid {C["line"]}}}
table.data td{{padding:5px 10px;border-bottom:0.4pt solid {C["line"]}}}
td.n,th.n{{text-align:right;font-variant-numeric:tabular-nums}}
.foot{{color:{C["mut"]};font-size:8pt;margin-top:18px;
  border-top:0.6pt solid {C["line"]};padding-top:8px}}
svg{{max-width:100%}}
</style>
<div class="kick">My Villa · Rapporto SEO automatico</div>
<h1>{mese_nome.capitalize()} {first_prev.year}</h1>
<p class="sub">myvilla.la — ricerca Google e canali di traffico. Generato
il {today:%d/%m/%Y} da Google Analytics 4 e Search Console (API).</p>

<table class="tiles"><tr>
<td class="tile"><div class="v">{m_impr:,}</div>
  <div class="l">Impression su Google a {mese_nome}</div>
  <div class="d">{delta_str(m_impr, p_impr)} vs {MESI[pp_first.month]}</div></td>
<td class="tile"><div class="v">{m_click}</div>
  <div class="l">Click da Google a {mese_nome}</div>
  <div class="d">{delta_str(m_click, p_click)} vs {MESI[pp_first.month]}</div></td>
<td class="tile"><div class="v">{organic_share}%</div>
  <div class="l">Quota di sessioni da ricerca organica</div></td>
<td class="tile"><div class="v">{n_art}</div>
  <div class="l">Articoli pubblicati dal journal</div></td>
</tr></table>

<h2>Impression per settimana</h2>
<div class="chart">{svg_area_weekly(weeks_lbl, impr_w,
                                    ymax_i, yticks, C["s1"])}</div>
<h2>Click per settimana</h2>
<div class="chart">{svg_bars_weekly(weeks_lbl, clicks_w,
                                    max(clicks_w + [5]) * 1.3, C["s1"])}</div>

<h2>Sessioni per canale, mese per mese</h2>
<div class="chart"><div class="legend">{legend}</div>
{svg_month_stack(months_data)}</div>

<h2>Da dove arriva il traffico — {mese_nome}</h2>
<p class="sub">Sessioni dagli USA: <b>{us_sessions}</b> ({us_share}% del
totale) — di cui California: <b>{ca_sessions}</b> ({ca_share_us}% delle
sessioni USA). Impression Google generate da ricerche USA:
{gsc_us_impr_share}%.</p>
<table style="width:100%;border-collapse:separate;border-spacing:8px 0;
margin:0 -8px"><tr>
<td style="width:55%;vertical-align:top">
<table class="data"><tr><th>Paese</th><th class="n">Sessioni</th>
<th class="n">%</th></tr>{country_rows}</table></td>
<td style="width:45%;vertical-align:top">
<table class="data"><tr><th>Stati USA</th><th class="n">Sessioni</th></tr>
{state_rows}</table></td>
</tr></table>

<h2>Pagine più viste in ricerca — {mese_nome}</h2>
<table class="data"><tr><th>Pagina</th><th class="n">Click</th>
<th class="n">Impression</th><th class="n">Pos.</th></tr>{page_rows}</table>

<h2>Query principali — {mese_nome}</h2>
<table class="data"><tr><th>Query</th><th class="n">Click</th>
<th class="n">Impression</th><th class="n">Pos.</th></tr>{query_rows}</table>

<div class="foot">Rapporto generato automaticamente il primo del mese.
Fonti: Google Analytics Data API (proprietà 526743497) e Search Console
API (https://myvilla.la/), service account myvilla-report. Settimane
complete lun–dom.</div>
"""

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = OUT_DIR / f"rapporto-seo-{ym}.pdf"
    from weasyprint import HTML
    HTML(string=html, base_url=str(ROOT_DIR)).write_pdf(str(pdf_path))
    print(f"[seo-report] PDF: {pdf_path} "
          f"({pdf_path.stat().st_size // 1024} KB)")

    if args.no_email:
        print("[seo-report] --no-email: non invio")
        return 0

    top_page = ""
    for r in pages:
        p = r["keys"][0].replace("https://myvilla.la", "")
        if p not in ("/", ""):
            top_page = (f"{p} ({r.get('impressions', 0):.0f} impression)")
            break

    body = f"""Ciao Ivo, ciao Paolo,

in allegato il rapporto SEO di {mese_nome} per myvilla.la. In sintesi:

- Impression su Google: {m_impr:,} ({delta_str(m_impr, p_impr)} rispetto a {MESI[pp_first.month]})
- Click: {m_click} ({delta_str(m_click, p_click)})
- Quota di traffico da ricerca organica: {organic_share}% delle sessioni
- Sessioni dagli USA: {us_sessions} ({us_share}% del totale), di cui California {ca_sessions} ({ca_share_us}% delle sessioni USA)
- Articoli pubblicati dal journal: {n_art}
- Pagina piu vista in ricerca: {top_page or "homepage"}

Nel PDF trovate l'andamento settimana per settimana, i canali di traffico
mese per mese e le query su cui il sito sta comparendo."""

    from send_email import send_raw
    result = send_raw(
        to="ivolo@me.com",
        cc="paolo.mezzalama@its.vision",
        subject=f"My Villa — Rapporto SEO {mese_nome} {first_prev.year}",
        body=body,
        attachments=[pdf_path],
        skip_rate_limit=True,      # interno: non consuma il budget outreach
        kind="seo_report",
    )
    if result.ok:
        MARKER.parent.mkdir(parents=True, exist_ok=True)
        MARKER.write_text(ym)
        print(f"[seo-report] email inviata (id {result.message_id})")
        return 0
    print(f"[seo-report] ERRORE invio: {result.reason or result.error}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
