#!/usr/bin/env python3
"""
My Villa — Answer page builder: insurable-home-california.html

Cosa fa
-------
Costruisce la pagina "Insurable home in California: what the data says (2026)"
SOLO da fatti già pubblicati nel Journal: legge i sidecar `blog/*.json` con
`_section_id == "insurance"` (che hanno un HTML gemello pubblicato) e usa i
campi `key_data` (numero + etichetta) e `sources` (fonte primaria con URL).
Ogni fatto scelto (lista FACT_PICKS) viene VERIFICATO contro il JSON: se il
numero non esiste più nel sidecar, il fatto viene saltato e loggato — la
pagina non inventa nulla.

Sezioni: definizione (2 frasi), fatti chiave (con data + fonte + articolo),
"How insurers look at a concrete home" (IBHS / Safer from Wildfires / CDI /
FAIR Plan), estimator JS onesto (usa SOLO le percentuali presenti nei
key_data, fonte accanto, "illustrative, not a quote"), checklist Zone 0
(solo se i fatti esistono), FAQ visibili + FAQPage schema coerente, hub dei
33 articoli, CTA verso /private-briefing.html.

Come si lancia
--------------
    python3 _system/scripts/build_answer_page.py            # scrive insurable-home-california.html
    python3 _system/scripts/build_answer_page.py --dry-run  # stampa il report dei fatti, non scrive

Produce
-------
    insurable-home-california.html (root, indicizzabile, canonical, dateModified reale:
    cambia solo se il contenuto cambia; datePublished conservato dalla versione precedente)

Exit code sempre 0.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from datetime import date
from html import escape
from pathlib import Path
from typing import Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent.parent
BLOG_DIR = ROOT_DIR / "blog"
OUT_PATH = ROOT_DIR / "insurable-home-california.html"
TRACKER_JSON = ROOT_DIR / "research" / "data" / "westside-rebuild-permits.json"
SETTINGS_PATH = ROOT_DIR / "_system" / "config" / "lead_settings.yml"

BASE_URL = "https://myvilla.la"
PAGE_URL = f"{BASE_URL}/insurable-home-california.html"
PAGE_TITLE = "Insurable Home in California: What the Data Says (2026)"
BRIEFING_URL = f"{BASE_URL}/private-briefing.html"
TRACKER_URL = f"{BASE_URL}/research/westside-rebuild-tracker.html"
GA4_ID = "G-D6HJX7BNZN"

# ── Fatti scelti (verificati a runtime contro i sidecar) ─────────────
# number: deve corrispondere ESATTAMENTE a key_data[].number del sidecar
# source: sottostringa dell'URL della fonte primaria dentro sources[]
FACT_PICKS: List[Dict[str, str]] = [
    {"id": "fair_plan_hike", "slug": "california-fair-plan-29-percent-rate-increase-2026", "number": "29.1%", "source": "montereyherald.com"},
    {"id": "state_farm_17", "slug": "beverly-hills-insurable-luxury-2026-trousdale-cohen", "number": "17%", "source": "release038-2025"},
    {"id": "state_farm_losses", "slug": "state-farm-investigation-california-insurance-carrier-risk-2026", "number": "$7.6B", "source": "calmatters.org"},
    {"id": "state_farm_paused", "slug": "state-farm-california-status-2026-high-value-home-insurance", "number": "2026", "source": "latentinsure.com"},
    {"id": "fair_plan_limit", "slug": "fair-plan-expansion-luxury-new-construction-california-2026", "number": "$20M", "source": "release028-2025"},
    # NB: le pagine consumer CDI "Safer from Wildfires" (01-consumers/...cfm) sono 404 dal restyling
    # del sito CDI (verificato 2026-09-16): si usano le fonti alternative già presenti nei sidecar.
    {"id": "sfw_12", "slug": "california-insurer-must-renew-fire-safe-homes-bill-2026", "number": "12", "source": "insurancejournal.com"},
    {"id": "sfw_3_layers", "slug": "california-home-insurance-premiums-2026-construction-variable", "number": "3 layers", "source": "coveragecat.com"},
    {"id": "ibhs_50", "slug": "california-insurer-must-renew-fire-safe-homes-bill-2026", "number": "Up to 50%", "source": "ibhs.org/wildfire-prepared-home"},
    {"id": "ibhs_2_tiers", "slug": "fire-resistant-home-insurance-california-47-percent-rise", "number": "2 tiers", "source": "ibhs.org/wildfire-prepared-home"},
    {"id": "fair_stack_164", "slug": "fair-plan-wildfire-discount-stack-california-2026", "number": "16.4%", "source": "substack.com"},
    {"id": "cost_premium_3", "slug": "beverly-hills-insurable-luxury-2026-trousdale-cohen", "number": "~3%", "source": "headwaterseconomics.org"},
    {"id": "ember_90", "slug": "ember-ignition-wildfire-insurable-home-california", "number": "90%", "source": "prnewswire.com"},
    {"id": "aal_33", "slug": "wildfire-risk-models-ibhs-standard-california-insurance", "number": "33%", "source": "propertyguardian.com"},
    {"id": "ucla_losses", "slug": "beverly-hills-insurable-luxury-2026-trousdale-cohen", "number": "$76B–$131B", "source": "anderson.ucla.edu"},
    # Zone 0 checklist facts
    {"id": "zone0_5ft", "slug": "firescaping-california-zone-0-landscaping-insurance", "number": "5 ft", "source": "prnewswire.com"},
    {"id": "zone0_rules", "slug": "ember-cast-defensible-space-california-fire-resistant-home", "number": "Zone 0", "source": "bof.fire.ca.gov"},
    {"id": "embers_vents", "slug": "firescaping-california-zone-0-landscaping-insurance", "number": "Embers", "source": "prnewswire.com"},
]
KEY_FACT_IDS = ["fair_plan_hike", "state_farm_17", "state_farm_losses", "fair_plan_limit", "sfw_12",
                "ibhs_50", "fair_stack_164", "cost_premium_3", "ember_90", "aal_33"]
ZONE0_IDS = ["zone0_rules", "zone0_5ft", "embers_vents"]


def log(msg: str) -> None:
    print(msg, flush=True)


# ── Load ─────────────────────────────────────────────────────────────
def load_insurance_articles() -> List[dict]:
    arts = []
    for jp in sorted(BLOG_DIR.glob("*.json")):
        try:
            d = json.loads(jp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("_section_id") != "insurance":
            continue
        hp = jp.with_suffix(".html")
        if not hp.exists():
            continue  # bozza senza HTML pubblicato
        html = hp.read_text(encoding="utf-8", errors="ignore")
        d["_slug"] = jp.stem
        d["_url"] = f"{BASE_URL}/blog/{jp.stem}.html"
        d["_noindex"] = bool(re.search(r'name="robots"\s+content="noindex', html))
        arts.append(d)
    arts.sort(key=lambda a: a.get("_date", ""), reverse=True)
    return arts


def resolve_fact(pick: dict, by_slug: Dict[str, dict]) -> Optional[dict]:
    art = by_slug.get(pick["slug"])
    if not art:
        log(f"  SKIP {pick['id']}: article {pick['slug']} not published")
        return None
    kd = next((k for k in art.get("key_data", []) if (k.get("number") or "").strip() == pick["number"]), None)
    if not kd:
        log(f"  SKIP {pick['id']}: number '{pick['number']}' not in key_data of {pick['slug']}")
        return None
    src = next((s for s in art.get("sources", []) if pick["source"].lower() in (s.get("url") or "").lower()), None)
    if not src:
        src = next((s for s in art.get("sources", []) if s.get("type") in ("gov", "study")), None) or \
              (art.get("sources") or [None])[0]
        if not src:
            log(f"  SKIP {pick['id']}: no source in {pick['slug']}")
            return None
        log(f"  NOTE {pick['id']}: preferred source not found, using {src.get('name')}")
    return {
        "id": pick["id"],
        "number": kd["number"].strip(),
        "label": " ".join((kd.get("label") or "").split()),
        "date": art.get("_date", ""),
        "article_title": art.get("title", ""),
        "article_url": art["_url"],
        "source_name": src.get("name", "Source"),
        "source_title": src.get("title", ""),
        "source_url": src.get("url", ""),
        "source_type": src.get("type", ""),
    }


def load_tracker_share() -> Optional[dict]:
    try:
        d = json.loads(TRACKER_JSON.read_text(encoding="utf-8"))
        cc = d["summary"]["totals"]["construction_class"]
        total = d["summary"]["totals"]["permits_total"]
        stated = total - cc.get("Not stated", 0)
        nc = cc.get("Type I/II (non-combustible)", 0)
        return {"retrieved": d["meta"]["retrieved"], "total": total, "stated": stated, "non_combustible": nc,
                "pct": (nc / stated * 100) if stated else 0.0}
    except Exception:
        return None


def load_settings() -> dict:
    try:
        import yaml
        return yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


# ── Render helpers ───────────────────────────────────────────────────
def fmt_date(d: str) -> str:
    try:
        y, m, dd = d.split("-")
        return date(int(y), int(m), int(dd)).strftime("%B %-d, %Y")
    except Exception:
        return d


def cite(f: dict) -> str:
    """Inline citation: source link + journal article link."""
    return (f"<span class='cite'>Source: <a class='inline-link' href='{escape(f['source_url'])}' rel='noopener'>{escape(f['source_name'])}</a>"
            f" · <a class='inline-link' href='{escape(f['article_url'])}'>My Villa Journal, {escape(fmt_date(f['date']))}</a></span>")


def fact_card(f: dict) -> str:
    return (
        "<div class='fact'>"
        f"<div class='fact-number'>{escape(f['number'])}</div>"
        f"<div class='fact-label'>{escape(f['label'])}</div>"
        f"<div class='fact-meta'>{fmt_date(f['date'])} · Source: <a class='inline-link' href='{escape(f['source_url'])}' rel='noopener'>{escape(f['source_name'])}</a>"
        f" · <a class='inline-link' href='{escape(f['article_url'])}'>Read the note</a></div>"
        "</div>"
    )


def build_faq(F: Dict[str, dict], settings: dict) -> List[dict]:
    """FAQ text is built only from resolved facts; questions whose facts are missing are dropped."""
    faq = []
    if "sfw_12" in F and "ibhs_50" in F:
        faq.append({
            "q": "What makes a home \"insurable\" in California in 2026?",
            "a": (f"An insurable home is one whose construction and parcel meet the mitigation standards insurers are required to price: "
                  f"the {F['sfw_12']['number']} Safer from Wildfires measures recognised by the California Department of Insurance, and, for the "
                  f"largest discounts, the IBHS Wildfire Prepared Home designation, which admitted carriers have rewarded with premium reductions of "
                  f"{F['ibhs_50']['number'].lower()}."),
        })
    if "fair_stack_164" in F and "ibhs_50" in F:
        faq.append({
            "q": "How much can a hardened home save on wildfire insurance?",
            "a": (f"Two documented figures exist. On a FAIR Plan policy the wildfire-discount stack reaches {F['fair_stack_164']['number']}. "
                  f"With an IBHS Wildfire Prepared Home designation, admitted carriers have offered discounts of {F['ibhs_50']['number'].lower()}. "
                  "Both depend on the carrier, the parcel and the documentation submitted; treat them as ranges, not quotes."),
        })
    if "cost_premium_3" in F:
        faq.append({
            "q": "Does building to the Wildfire Prepared Home Plus standard cost much more?",
            "a": (f"Headwaters Economics and IBHS estimate a construction-cost premium of about {F['cost_premium_3']['number'].replace('~', '')} "
                  "for a home built to the Wildfire Prepared Home Plus level compared with a conventional build."),
        })
    if "fair_plan_hike" in F and "fair_plan_limit" in F:
        faq.append({
            "q": "What is the California FAIR Plan and what changed for high-value homes?",
            "a": (f"The FAIR Plan is the state's insurer of last resort. In 2025 its coverage limit was raised to {F['fair_plan_limit']['number']} per building, "
                  f"and a {F['fair_plan_hike']['number']} rate increase takes effect on October 15, 2026, the largest in the plan's history. "
                  "For an owner of a high-value home it is a fallback, priced accordingly, not a strategy."),
        })
    if "state_farm_paused" in F and "state_farm_17" in F:
        faq.append({
            "q": "Is State Farm writing new home policies in California?",
            "a": (f"As of 2026, State Farm, the state's largest home insurer, has kept new homeowner business paused, after an emergency "
                  f"{F['state_farm_17']['number']} rate increase approved by the Department of Insurance in June 2025."),
        })
    if "zone0_5ft" in F:
        faq.append({
            "q": "What is Zone 0?",
            "a": (f"Zone 0 is the {F['zone0_5ft']['number']} ember-resistant perimeter immediately around the structure, the first zone IBHS and CAL FIRE "
                  "look at. California's Board of Forestry and Fire Protection is codifying it as a non-combustible strip with no vegetation, "
                  "mulch or wood fencing against the walls."),
        })
    price = (settings.get("canonical") or {}).get("price")
    if price:
        faq.append({
            "q": "How does My Villa approach an insurable home?",
            "a": (f"We design reinforced concrete villas for the Los Angeles hills and coast, engineered to meet the Safer from Wildfires measures natively rather than as retrofits, "
                  f"with pricing {price}. The projects shown on our site are concept designs and renders; a private briefing is the first step."),
        })
    return faq


# ── Page ─────────────────────────────────────────────────────────────
def render(arts: List[dict], F: Dict[str, dict], tracker: Optional[dict], settings: dict,
           date_published: str, date_modified: str) -> str:
    key_facts = [F[i] for i in KEY_FACT_IDS if i in F]
    zone0 = [F[i] for i in ZONE0_IDS if i in F]
    faq = build_faq(F, settings)
    n_articles = len(arts)

    description = (
        f"What insurers actually price in California in 2026: {len(key_facts)} sourced facts on FAIR Plan rates, State Farm, "
        "Safer from Wildfires, IBHS Wildfire Prepared Home discounts and construction cost, with an illustrative savings estimator "
        f"and {n_articles} Journal notes."
    )

    # Stat strip: 4 facts
    strip_ids = ["ibhs_50", "fair_plan_hike", "sfw_12", "cost_premium_3"]
    strip = "".join(
        f"<div class='stat-item'><div class='stat-value'>{escape(F[i]['number'])}</div><div class='stat-label'>{escape(F[i]['label'])}</div></div>"
        for i in strip_ids if i in F
    )

    facts_html = "".join(fact_card(f) for f in key_facts)

    # "How insurers look" — prose bound to resolved facts
    def has(*ids):
        return all(i in F for i in ids)

    insurers = []
    if has("ibhs_2_tiers", "ibhs_50"):
        insurers.append(
            "<h3>IBHS Wildfire Prepared Home</h3>"
            f"<p>The Insurance Institute for Business &amp; Home Safety designation has {escape(F['ibhs_2_tiers']['number'])}, Home and Home Plus, "
            "and it is the aggregate standard California carriers point to when they price mitigation. A home certified at the Plus level has been "
            f"offered discounts of {escape(F['ibhs_50']['number'].lower())} by admitted carriers. A reinforced concrete envelope satisfies the structural "
            "items of the checklist by construction, without add-on assemblies; the parcel items (Zone 0, vents, decks) still have to be documented. "
            f"{cite(F['ibhs_50'])}</p>"
        )
    if has("sfw_12", "sfw_3_layers"):
        insurers.append(
            "<h3>Safer from Wildfires and the Department of Insurance</h3>"
            f"<p>Since the Department's regulation took effect, every admitted carrier must recognise {escape(F['sfw_12']['number'])} hardening measures "
            f"organised in {escape(F['sfw_3_layers']['number'])} layers, structure, parcel and community, and reflect them in rates. "
            "For an underwriter, a concrete home is a structure-layer answer to most of the list at once: non-combustible walls, Class A roof, "
            "ember-resistant vents and enclosed eaves are design decisions, not retrofits. What remains is the parcel layer, which is where most "
            f"applications fail. {cite(F['sfw_12'])}</p>"
        )
    if has("fair_plan_hike", "fair_stack_164", "fair_plan_limit"):
        insurers.append(
            "<h3>FAIR Plan</h3>"
            f"<p>The FAIR Plan now covers up to {escape(F['fair_plan_limit']['number'])} per building, which makes it a real fallback for a high-value "
            f"home, and its {escape(F['fair_plan_hike']['number'])} rate increase in October 2026 makes it an expensive one. Its own wildfire discount "
            f"stack tops out at {escape(F['fair_stack_164']['number'])}, roughly a third of what an IBHS-certified home has been offered in the admitted "
            f"market. The gap between the two numbers is the financial case for building to the standard. {cite(F['fair_stack_164'])}</p>"
        )
    if has("state_farm_paused", "state_farm_losses"):
        insurers.append(
            "<h3>Carrier capacity</h3>"
            f"<p>State Farm reported {escape(F['state_farm_losses']['number'])} in insured losses from the January 2025 Los Angeles fires and, as of "
            f"{escape(F['state_farm_paused']['number'])}, has kept new homeowner business paused. Capacity, not only price, is the constraint: a "
            "documented, certifiable home is the submission an underwriter can say yes to. "
            f"{cite(F['state_farm_paused'])}</p>"
        )
    if tracker:
        insurers.append(
            "<h3>What is actually being built</h3>"
            f"<p>Our <a class='inline-link' href='{TRACKER_URL}'>Westside Rebuild Tracker</a>, rebuilt weekly from City of Los Angeles permit data, "
            f"shows that of the {tracker['stated']:,} new-home permits filed since January 2025 that declare a construction type, "
            f"only {tracker['non_combustible']:,} ({tracker['pct']:.1f}%) are Type I or II, the non-combustible category. The rest are wood-frame. "
            f"<span class='cite'>Source: LADBS open data, retrieved {escape(tracker['retrieved'])}.</span></p>"
        )
    insurers_html = "".join(insurers)

    # Estimator (only percentages present in key_data)
    est_rates = []
    if "fair_stack_164" in F:
        est_rates.append({"id": "fair", "label": "FAIR Plan wildfire discount stack", "pct": 16.4, "kind": "documented maximum", "fact": F["fair_stack_164"]})
    if "ibhs_50" in F:
        est_rates.append({"id": "ibhs", "label": "IBHS Wildfire Prepared Home, admitted carriers", "pct": 50.0, "kind": "up to", "fact": F["ibhs_50"]})
    estimator_html = ""
    if est_rates:
        rows = "".join(
            f"<tr><td>{escape(r['label'])}<br><span class='muted'>{escape(r['kind'])} · {cite(r['fact'])}</span></td>"
            f"<td class='num'>{r['pct']:g}%</td><td class='num' id='est-{r['id']}'>—</td></tr>"
            for r in est_rates
        )
        rates_json = json.dumps([{"id": r["id"], "pct": r["pct"]} for r in est_rates])
        estimator_html = f"""
  <h2 id="estimator">Illustrative savings estimator</h2>
  <p>Enter your current annual premium. The estimator applies only the discount percentages published above, nothing else: no modelling, no assumptions about your parcel.</p>
  <div class="estimator">
    <label for="premium">Current annual premium (USD)</label>
    <div class="est-row"><span class="est-prefix">$</span><input type="number" id="premium" min="0" step="100" placeholder="e.g. 24000" inputmode="numeric"></div>
    <div class="table-wrap"><table>
      <thead><tr><th>Discount reference</th><th class="num">Rate</th><th class="num">Annual saving</th></tr></thead>
      <tbody>{rows}</tbody></table></div>
    <p class="disclaimer">Illustrative, not a quote. Discounts are applied by carriers to the wildfire component of a premium, subject to certification, inspection and underwriting; the "up to" figure is a ceiling reported by carriers, not a typical outcome. My Villa is not an insurance broker.</p>
  </div>
  <script>
  (function(){{
    var rates = {rates_json};
    var inp = document.getElementById('premium');
    function fmt(n){{ return '$' + Math.round(n).toLocaleString('en-US'); }}
    function update(){{
      var p = parseFloat(inp.value);
      rates.forEach(function(r){{
        var el = document.getElementById('est-' + r.id);
        if (!el) return;
        if (!p || p <= 0) {{ el.textContent = '—'; return; }}
        el.textContent = (r.id === 'ibhs' ? 'up to ' : '') + fmt(p * r.pct / 100);
      }});
      if (p > 0 && window.gtag) {{ gtag('event', 'estimator_use', {{ 'page': 'insurable-home-california' }}); }}
    }}
    inp.addEventListener('input', update);
  }})();
  </script>
"""

    zone0_html = ""
    if len(zone0) >= 2:
        items = "".join(f"<li><strong>{escape(f['number'])}</strong> — {escape(f['label'])}. {cite(f)}</li>" for f in zone0)
        zone0_html = f"""
  <h2 id="zone-0">Zone 0: the perimeter insurers check first</h2>
  <p>Only the points below are documented in our notes; the full Board of Forestry regulation is longer and still being finalised.</p>
  <ul class="checklist">{items}</ul>
"""

    faq_html = "".join(
        f"<div class='faq-item'><div class='faq-question'>{escape(q['q'])}</div><div class='faq-answer'><p>{escape(q['a'])}</p></div></div>"
        for q in faq
    )

    hub_items = "".join(
        f"<li><a class='inline-link' href='{escape(a['_url'])}'><span class='hub-date'>{escape(a.get('_date', ''))}</span>{escape(a.get('title', ''))}</a></li>"
        for a in arts
    )

    schema = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "Article",
                "@id": f"{PAGE_URL}#article",
                "headline": PAGE_TITLE,
                "description": description,
                "url": PAGE_URL,
                "mainEntityOfPage": PAGE_URL,
                "datePublished": date_published,
                "dateModified": date_modified,
                "inLanguage": "en",
                "author": {"@type": "Organization", "@id": f"{BASE_URL}/#organization", "name": "My Villa", "url": BASE_URL},
                "publisher": {"@type": "Organization", "@id": f"{BASE_URL}/#organization", "name": "My Villa", "url": BASE_URL,
                              "logo": {"@type": "ImageObject", "url": f"{BASE_URL}/img/logos/apple-touch-icon.png"}},
                "image": f"{BASE_URL}/img/hero.png",
                "about": ["home insurance California", "wildfire insurance discounts", "IBHS Wildfire Prepared Home", "California FAIR Plan", "Safer from Wildfires"],
                "citation": [f["source_url"] for f in key_facts],
            },
            {
                "@type": "FAQPage",
                "@id": f"{PAGE_URL}#faq",
                "mainEntity": [
                    {"@type": "Question", "name": q["q"], "acceptedAnswer": {"@type": "Answer", "text": q["a"]}} for q in faq
                ],
            },
            {
                "@type": "ItemList",
                "@id": f"{PAGE_URL}#journal",
                "name": "My Villa Journal — Insurance & Insurability notes",
                "numberOfItems": n_articles,
                "itemListElement": [
                    {"@type": "ListItem", "position": i + 1, "name": a.get("title", ""), "url": a["_url"]} for i, a in enumerate(arts)
                ],
            },
            {
                "@type": "BreadcrumbList",
                "@id": f"{PAGE_URL}#breadcrumb",
                "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": "Home", "item": BASE_URL},
                    {"@type": "ListItem", "position": 2, "name": "Insurable Home in California", "item": PAGE_URL},
                ],
            },
        ],
    }

    return f"""<!DOCTYPE html>
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
<title>{escape(PAGE_TITLE)} | My Villa</title>
<meta name="description" content="{escape(description)}">
<meta name="keywords" content="insurable home California, California home insurance 2026, wildfire insurance discount, IBHS Wildfire Prepared Home, Safer from Wildfires, California FAIR Plan rate increase 2026, State Farm California, fire-resistant home insurance, reinforced concrete home insurance, Zone 0 California">
<meta name="author" content="My Villa Research">
<meta name="robots" content="index, follow, max-image-preview:large">
<link rel="canonical" href="{PAGE_URL}">

<!-- Open Graph -->
<meta property="og:type" content="article">
<meta property="og:url" content="{PAGE_URL}">
<meta property="og:title" content="{escape(PAGE_TITLE)}">
<meta property="og:description" content="{escape(description)}">
<meta property="og:image" content="{BASE_URL}/img/hero.png">
<meta property="og:site_name" content="My Villa">
<meta property="og:locale" content="en_US">
<meta property="article:published_time" content="{date_published}">
<meta property="article:modified_time" content="{date_modified}">

<!-- Twitter Card -->
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{escape(PAGE_TITLE)}">
<meta name="twitter:description" content="{escape(description)}">
<meta name="twitter:image" content="{BASE_URL}/img/hero.png">

<!-- Geo -->
<meta name="geo.region" content="US-CA">
<meta name="geo.placename" content="Los Angeles">

<!-- Favicon -->
<link rel="icon" type="image/svg+xml" href="img/logos/favicon.svg">
<link rel="apple-touch-icon" href="img/logos/apple-touch-icon.png">

<!-- Fonts -->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,500;0,600;1,400;1,500&family=Montserrat:wght@300;400;500;600;700&display=swap" rel="stylesheet">

<!-- Schema.org — Article + FAQPage + ItemList + BreadcrumbList -->
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
.stat-strip {{ max-width: 1100px; margin: 40px auto 0; padding: 28px clamp(24px, 5vw, 80px); border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 24px; }}
.stat-item {{ text-align: center; }}
.stat-value {{ font-family: var(--serif); font-size: 36px; color: var(--terracotta); font-weight: 400; line-height: 1; margin-bottom: 6px; }}
.stat-label {{ font-family: var(--sans); font-size: 11px; text-transform: uppercase; letter-spacing: 0.15em; color: var(--stone-grey); }}
.pillar-container {{ max-width: 900px; margin: 0 auto; padding: 50px clamp(24px, 5vw, 60px) 80px; }}
h2 {{ font-family: var(--serif); font-size: clamp(28px, 3.4vw, 38px); font-weight: 500; color: var(--offblack); margin-top: 70px; margin-bottom: 20px; letter-spacing: -0.01em; line-height: 1.2; }}
h3 {{ font-family: var(--serif); font-size: clamp(21px, 2.5vw, 26px); font-weight: 500; color: var(--charcoal); margin-top: 36px; margin-bottom: 12px; }}
p {{ font-size: 16px; line-height: 1.85; color: rgba(44,44,44,0.85); margin-bottom: 20px; }}
.container-lede {{ font-family: var(--serif); font-size: 22px; font-style: italic; color: var(--espresso); line-height: 1.55; margin-bottom: 28px; padding-left: 20px; border-left: 2px solid var(--terracotta); }}
a.inline-link {{ color: var(--terracotta); border-bottom: 1px solid transparent; transition: color 0.2s; }}
a.inline-link:hover {{ color: var(--offblack); border-bottom-color: var(--terracotta); }}
strong {{ color: var(--offblack); font-weight: 600; }}
ul, ol {{ margin: 18px 0 22px 22px; }}
li {{ margin-bottom: 10px; line-height: 1.7; color: rgba(44,44,44,0.85); }}
.cite {{ display: block; font-size: 12px; color: var(--stone-grey); margin-top: 4px; }}
.muted {{ color: var(--stone-grey); font-size: 12px; }}
.facts {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 18px; margin: 28px 0; }}
.fact {{ background: #fff; border: 1px solid var(--line); border-left: 3px solid var(--terracotta); border-radius: 4px; padding: 22px 22px 18px; }}
.fact-number {{ font-family: var(--serif); font-size: 34px; color: var(--terracotta); line-height: 1; margin-bottom: 8px; }}
.fact-label {{ font-size: 14.5px; line-height: 1.5; color: var(--offblack); margin-bottom: 10px; }}
.fact-meta {{ font-size: 12px; color: var(--stone-grey); line-height: 1.6; }}
.estimator {{ background: #fff; border: 1px solid var(--line); border-radius: 4px; padding: 26px; margin: 20px 0 30px; }}
.estimator label {{ display: block; font-size: 11px; letter-spacing: 0.18em; text-transform: uppercase; color: var(--stone-grey); margin-bottom: 8px; }}
.est-row {{ display: flex; align-items: center; gap: 8px; margin-bottom: 18px; }}
.est-prefix {{ font-family: var(--serif); font-size: 26px; color: var(--espresso); }}
.estimator input {{ font-family: var(--sans); font-size: 20px; padding: 10px 14px; border: 1px solid var(--line); border-radius: 3px; width: 100%; max-width: 280px; background: var(--cream); color: var(--offblack); }}
.estimator input:focus {{ outline: 2px solid var(--warm-sand); }}
.disclaimer {{ font-size: 12.5px; color: var(--stone-grey); margin: 12px 0 0; line-height: 1.6; }}
.table-wrap {{ overflow-x: auto; margin: 10px 0 10px; border: 1px solid var(--line); background: #fff; }}
table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
th, td {{ padding: 12px 14px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
th {{ font-family: var(--sans); font-size: 10.5px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--stone-grey); background: var(--light-linen); font-weight: 600; }}
td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; font-family: var(--serif); font-size: 20px; color: var(--offblack); }}
th.num {{ font-family: var(--sans); font-size: 10.5px; color: var(--stone-grey); }}
.checklist {{ list-style: none; margin-left: 0; }}
.checklist li {{ padding: 12px 0 12px 30px; border-bottom: 1px solid var(--line); position: relative; }}
.checklist li::before {{ content: ''; position: absolute; left: 0; top: 20px; width: 14px; height: 14px; border: 1px solid var(--terracotta); border-radius: 2px; }}
.faq-block {{ margin-top: 20px; }}
.faq-item {{ border-bottom: 1px solid var(--line); padding: 20px 0; }}
.faq-question {{ font-family: var(--serif); font-size: 22px; font-weight: 500; color: var(--offblack); margin-bottom: 10px; line-height: 1.3; }}
.faq-answer p {{ font-size: 15px; line-height: 1.75; color: rgba(44,44,44,0.82); margin-bottom: 0; }}
.hub {{ margin-top: 20px; padding: 30px; background: var(--light-linen); border-radius: 4px; }}
.hub ul {{ margin-left: 0; list-style: none; columns: 1; }}
.hub li {{ padding: 9px 0; border-bottom: 1px solid rgba(44,44,44,0.08); margin: 0; }}
.hub li:last-child {{ border-bottom: none; }}
.hub a.inline-link {{ display: block; font-family: var(--serif); font-size: 17px; color: var(--espresso); border: none; line-height: 1.35; }}
.hub a.inline-link:hover {{ color: var(--terracotta); }}
.hub-date {{ display: inline-block; font-family: var(--sans); font-size: 10px; letter-spacing: 0.12em; color: var(--terracotta); margin-right: 10px; vertical-align: middle; }}
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
  <a href="index.html" class="nav-logo-wrap">
    <span class="nav-logo">MY VILLA</span>
    <span class="nav-payoff">Italian Soul · Californian Body</span>
  </a>
  <a href="{BRIEFING_URL}" class="nav-cta">Request Briefing</a>
</nav>

<section class="pillar-hero">
  <div class="pillar-eyebrow">Insurance &amp; Insurability · Data brief</div>
  <h1 class="pillar-title">Insurable home in California: <em>what the data says</em> (2026)</h1>
  <p class="pillar-lede">Every figure on this page was first published in a My Villa Journal note with a primary source. We collect them here so an owner, an architect or a broker can see, on one page, what California insurers are pricing this year.</p>
  <div class="updated">Published {fmt_date(date_published)} · Updated {fmt_date(date_modified)} · {n_articles} source notes</div>
</section>

<div class="stat-strip">{strip}</div>

<div class="pillar-container">

  <h2 id="definition" style="margin-top:20px">Definition</h2>
  <p class="container-lede">An insurable home in California is a house whose structure and parcel meet the wildfire-mitigation standards that insurers are required to recognise and price, so that an admitted carrier can write it at a discounted rate instead of pushing it to the FAIR Plan. In 2026 that means the Safer from Wildfires measures as a floor and the IBHS Wildfire Prepared Home designation as the reference for the largest discounts.</p>

  <h2 id="facts">Key facts, with sources</h2>
  <p>Each card shows the number as published, the date of our note, and the primary source. Nothing here is modelled by us.</p>
  <div class="facts">{facts_html}</div>

  <h2 id="insurers">How insurers look at a concrete home</h2>
  {insurers_html}
{estimator_html}{zone0_html}
  <h2 id="faq">Frequently asked questions</h2>
  <div class="faq-block">{faq_html}</div>

  <h2 id="journal">All {n_articles} Journal notes on insurance &amp; insurability</h2>
  <div class="hub"><ul>{hub_items}</ul></div>
  <p class="muted" style="margin-top:12px">Also see the <a class="inline-link" href="blog/category/insurance.html">Insurance &amp; Insurability</a> category and the <a class="inline-link" href="{TRACKER_URL}">Westside Rebuild Tracker</a>.</p>

  <div class="pillar-cta">
    <h2>Planning an insurable home?</h2>
    <p>We design reinforced concrete villas for the Los Angeles hills and coast, engineered to meet the standards above by construction. A private briefing is a conversation, not a pitch.</p>
    <a href="{BRIEFING_URL}" class="btn">Request a private briefing</a>
  </div>

</div>

<footer class="footer">
  <div class="footer-copy">© {date_modified[:4]} My Villa · <a href="index.html">Home</a> · <a href="team.html">Team</a> · <a href="blog/">Journal</a> · <a href="research/westside-rebuild-tracker.html">Rebuild Tracker</a> · <a href="privacy.html">Privacy Policy</a></div>
  <div>Luxury home builder · Malibu · Beverly Hills · Bel Air · Brentwood · Hidden Hills · Calabasas · Westside Los Angeles</div>
</footer>

</body>
</html>
"""


def strip_dates(html: str) -> str:
    """Remove the date fields so two renders can be compared for real content changes."""
    html = re.sub(r'"date(Published|Modified)":\s*"[^"]*"', '', html)
    html = re.sub(r'article:(published|modified)_time" content="[^"]*"', '', html)
    html = re.sub(r'Published [^·]+· Updated [^·]+·', '', html)
    html = re.sub(r'© \d{4}', '', html)
    return html


def main() -> int:
    parser = argparse.ArgumentParser(description="Build insurable-home-california.html from Journal sidecars")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    today = date.today().isoformat()
    try:
        arts = load_insurance_articles()
        by_slug = {a["_slug"]: a for a in arts}
        log(f"Insurance notes published: {len(arts)} ({sum(1 for a in arts if a['_noindex'])} noindex, still linked)")
        F: Dict[str, dict] = {}
        for pick in FACT_PICKS:
            f = resolve_fact(pick, by_slug)
            if f:
                F[f["id"]] = f
        log(f"Facts resolved: {len(F)}/{len(FACT_PICKS)}")
        missing_key = [i for i in KEY_FACT_IDS if i not in F]
        if missing_key:
            log(f"  missing key facts: {missing_key}")
        if len([i for i in KEY_FACT_IDS if i in F]) < 8:
            log("  ERROR: fewer than 8 key facts resolved — page not written")
            return 0
        zone0_ok = len([i for i in ZONE0_IDS if i in F]) >= 2
        log(f"Zone 0 checklist: {'included' if zone0_ok else 'OMITTED (facts missing)'}")
        tracker = load_tracker_share()
        settings = load_settings()

        date_published = today
        date_modified = today
        previous = OUT_PATH.read_text(encoding="utf-8") if OUT_PATH.exists() else ""
        if previous:
            m = re.search(r'"datePublished":\s*"(\d{4}-\d{2}-\d{2})"', previous)
            if m:
                date_published = m.group(1)
            m2 = re.search(r'"dateModified":\s*"(\d{4}-\d{2}-\d{2})"', previous)
            prev_modified = m2.group(1) if m2 else today
            candidate = render(arts, F, tracker, settings, date_published, prev_modified)
            if strip_dates(candidate) == strip_dates(previous):
                date_modified = prev_modified
                log(f"  content unchanged — dateModified kept at {prev_modified}")

        html = render(arts, F, tracker, settings, date_published, date_modified)
        if args.dry_run:
            log(f"  dry-run: would write {OUT_PATH.name} ({len(html):,} bytes), FAQ {len(build_faq(F, settings))}, hub {len(arts)}")
            return 0
        OUT_PATH.write_text(html, encoding="utf-8")
        log(f"Wrote {OUT_PATH.relative_to(ROOT_DIR)} ({len(html):,} bytes) · datePublished {date_published} · dateModified {date_modified}")
    except Exception:
        log("Unexpected error — page untouched:")
        traceback.print_exc()
    return 0


if __name__ == "__main__":
    sys.exit(main())
