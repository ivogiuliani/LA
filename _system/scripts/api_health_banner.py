#!/usr/bin/env python3
"""
api_health_banner — shared rendering for the API health banner.

The radar pipeline (radar.py) writes an `api_health` dict into each radar
JSON describing whether each external provider (Anthropic, Apollo, xAI,
Gemini, Google CSE, Apify, …) was reachable during the scan. This module
turns that dict into either:

  - render_api_health_banner_html(api_health, radar_date=None)
        → a <section class="api-health-banner …"> element ready to drop
          into a host page.
  - render_api_health_banner_md(api_health)
        → a Markdown bullet list, one line per provider.

Two CSS themes are bundled because the two host pages style differently:

  - CSS_LIGHT  → home dashboard (approve.py): pastel pills on warm beige
  - CSS_DARK   → radar dashboard:             muted pills on espresso brown

The HTML structure is identical across themes; only the CSS differs, so
adding a new theme means adding a new CSS constant — no rendering changes.

Both consumers (approve.py and generate_radar_report.py) embed CSS inside
their existing <style> blocks via f-string interpolation; the constants
below use plain CSS (no doubled braces) so they paste in cleanly without
needing escapes.

Backward compatibility: older radar JSONs (pre-2026-04-27) lack the
`api_health` key. Both render functions return an empty string in that
case so the host page renders without a guard.
"""

from __future__ import annotations

from html import escape as _html_escape
from typing import Optional


__all__ = [
    "render_api_health_banner_html",
    "render_api_health_banner_md",
    "classify_overall_status",
    "API_LABEL_MAP",
    "STATUS_LABEL_IT",
    "STATUS_SYMBOLS",
    "OVERALL_SYMBOLS",
    "KNOWN_STATUSES",
    "CSS_LIGHT",
    "CSS_DARK",
]


# Human-readable provider labels keyed by what radar.py writes under
# `api_health.<key>`. Add a new entry when a new provider is wired in.
API_LABEL_MAP = {
    "anthropic":  "Anthropic",
    "apollo":     "Apollo",
    "xai_grok":   "xAI / Grok",
    "gemini":     "Gemini",
    "google_cse": "Google CSE",
    "brave":      "Brave Search",
    "apify":      "Apify (partner scrape)",
}

# The four canonical statuses radar.py emits. Anything else from a
# malformed JSON gets coerced to "missing" by _normalize_status so the
# rendered HTML can never inject an arbitrary CSS class.
KNOWN_STATUSES = ("ok", "fail", "missing", "skip")

STATUS_LABEL_IT = {
    "ok":      "operativa",
    "fail":    "errore",
    "missing": "non configurata",
    "skip":    "saltata",
}

STATUS_SYMBOLS = {
    "ok":      "✅",
    "fail":    "❌",
    "missing": "➖",
    "skip":    "⏭",
}

# Emojis for the *overall* status line (one per banner, not per provider).
# Used by both the HTML banner headline (indirectly, via CSS color) and
# by the markdown summary line ("Stato API: ✅ …").
OVERALL_SYMBOLS = {
    "ok":   "✅",
    "fail": "❌",
    "warn": "⚠️",
}


def _normalize_status(raw) -> str:
    """Whitelist incoming status values.

    Accepts only the four statuses radar.py is documented to emit; anything
    else (typo, future status, weird type) is coerced to "missing" so it
    renders as a neutral "not configured" pill instead of injecting an
    arbitrary CSS class name into the output.
    """
    if not isinstance(raw, str):
        return "missing"
    s = raw.strip().lower()
    return s if s in KNOWN_STATUSES else "missing"


def _coerce_entry(info) -> dict:
    """Normalize a single api_health entry so the renderer can rely on dict access.

    Defensive layer for malformed JSON. Accepts:
      - dict   → returned as-is (the canonical shape from radar.py)
      - str    → wrapped as {"status": <str>} so {"anthropic": "ok"} works
      - other  → empty dict, will render as "missing" via the normalizer

    Without this guard, an entry like {"anthropic": "ok"} (string instead
    of dict) crashes the renderer with AttributeError on .get().
    """
    if isinstance(info, dict):
        return info
    if isinstance(info, str):
        return {"status": info}
    return {}


def classify_overall_status(api_health) -> tuple[str, str]:
    """Classify the overall health of the API set.

    Returns ``(status_key, italian_label)`` where ``status_key`` is one of:

      - ``"ok"``   → every provider succeeded
      - ``"fail"`` → at least one provider failed (worst case wins)
      - ``"warn"`` → no failures but the run is degraded (something missing
                     or skipped)

    The Italian label is the same human-readable text used in the HTML
    banner headline so the markdown digest, the HTML banner, and any
    future consumer all stay phrased the same way.

    Note: ``"skip"`` is treated as warn-level, not ok-level. ``radar.py``
    documents skip as "feature disabled because secondary config is
    missing", which is information the operator wants surfaced — not
    silently rolled into a green banner.
    """
    if not api_health or not isinstance(api_health, dict):
        return "warn", "Stato API sconosciuto"

    statuses = {
        _normalize_status(_coerce_entry(info).get("status"))
        for info in api_health.values()
    }

    # Worst-wins precedence
    if "fail" in statuses:
        return "fail", "Una o più API in errore — radar parziale"
    if "missing" in statuses:
        return "warn", "Configurazione API parziale"
    if statuses <= {"ok"}:
        return "ok", "Tutte le API attive e funzionanti"
    if "skip" in statuses:
        # Only ok + skip remain at this point.
        return "warn", "Nessuna API in errore, alcune saltate"
    return "warn", "Stato API parziale"


def render_api_health_banner_html(
    api_health: Optional[dict],
    radar_date: Optional[str] = None,
) -> str:
    """Render the API health banner as a <section> element.

    Returns "" when `api_health` is empty, None, or not a dict — callers
    can drop the result straight into a template without a guard clause.

    Args:
      api_health: Dict mapping provider key → entry. Each entry is normally
                  ``{"status": ..., "detail": ..., "env_var": ...}`` but the
                  renderer also accepts bare strings (treated as just the
                  status) and silently drops anything else, so a malformed
                  JSON cannot crash the dashboard.
      radar_date: Optional ISO date string (e.g. "2026-04-28"). When given,
                  appended as a small right-aligned date pill so operators
                  can tell at a glance how stale the data is.
    """
    if not api_health or not isinstance(api_health, dict):
        return ""

    pills = []
    for name, info in api_health.items():
        info = _coerce_entry(info)
        status = _normalize_status(info.get("status"))  # whitelisted → safe in CSS class
        label = API_LABEL_MAP.get(name, str(name))
        detail = (info.get("detail") or "")
        if not isinstance(detail, str):
            detail = str(detail)
        detail = detail.strip()
        env_var = info.get("env_var", "")
        if not isinstance(env_var, str):
            env_var = str(env_var)
        tooltip_parts = [f"{label}: {STATUS_LABEL_IT[status]}"]
        if detail:
            tooltip_parts.append(detail)
        if env_var:
            tooltip_parts.append(f"env: {env_var}")
        tooltip = _html_escape(" — ".join(tooltip_parts))
        pills.append(
            f'<span class="api-pill api-{status}" title="{tooltip}">'
            f'<span class="api-dot"></span>{_html_escape(label)}</span>'
        )

    head_status, head_text = classify_overall_status(api_health)

    date_html = (
        f'<span class="api-health-date">radar {_html_escape(str(radar_date))}</span>'
        if radar_date else ""
    )
    return (
        f'<section class="api-health-banner banner-{head_status}" '
        f'aria-label="API health check">'
        f'<span class="api-health-headline">'
        f'<span class="api-dot"></span>{_html_escape(head_text)}</span>'
        f'<span class="api-health-pills">{"".join(pills)}</span>'
        f'{date_html}'
        f'</section>'
    )


def render_api_health_banner_md(api_health: Optional[dict]) -> str:
    """Render the banner as a Markdown bullet list.

    One bullet per provider, status icon + label + detail + env-var (only
    shown when missing or failing, since that's the actionable case).
    """
    if not api_health or not isinstance(api_health, dict):
        return ""
    bullets = []
    for name, info in api_health.items():
        info = _coerce_entry(info)
        status = _normalize_status(info.get("status"))
        label = API_LABEL_MAP.get(name, str(name))
        detail = (info.get("detail") or "")
        if not isinstance(detail, str):
            detail = str(detail)
        detail = detail.strip()
        env_var = info.get("env_var", "")
        if not isinstance(env_var, str):
            env_var = str(env_var)
        sym = STATUS_SYMBOLS[status]
        line = f"  - {sym} **{label}** — {status}"
        if detail:
            line += f" ({detail})"
        if env_var and status in ("missing", "fail"):
            line += f" [`{env_var}`]"
        bullets.append(line)
    return "\n".join(bullets)


# ── CSS constants ────────────────────────────────────────────────────
# Plain CSS — no f-string brace doubling. Host pages either inject these
# into their existing <style> block (via {API_HEALTH_CSS} substitution
# from an outer f-string) or could serve them as a static asset.

CSS_LIGHT = """
/* ── API Health banner — light theme (home dashboard) ─── */
.api-health-banner {
  display: flex;
  align-items: center;
  gap: 18px;
  flex-wrap: wrap;
  padding: 10px 22px;
  font-size: 13px;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  border-bottom: 1px solid rgba(0,0,0,0.08);
}
.api-health-banner.banner-ok   { background: #EAF4EC; color: #1F4D2A; }
.api-health-banner.banner-fail { background: #FBE9E7; color: #7A1F12; }
.api-health-banner.banner-warn { background: #FFF6E0; color: #6B4A00; }
.api-health-headline {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  letter-spacing: 0.2px;
}
.api-health-pills { display: inline-flex; flex-wrap: wrap; gap: 6px; }
.api-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 500;
  background: rgba(255,255,255,0.55);
  border: 1px solid rgba(0,0,0,0.08);
  color: inherit;
  cursor: help;
}
.api-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
  opacity: 0.85;
  flex-shrink: 0;
}
.api-pill.api-ok      .api-dot { background: #2E8B47; }
.api-pill.api-fail    .api-dot { background: #C0392B; }
.api-pill.api-missing .api-dot { background: #B8860B; }
.api-pill.api-skip    .api-dot { background: #8A8A8A; }
.api-health-date {
  margin-left: auto;
  font-size: 12px;
  opacity: 0.75;
  font-variant-numeric: tabular-nums;
}
@media (max-width: 720px) {
  .api-health-banner { padding: 10px 14px; gap: 10px; }
  .api-health-date   { margin-left: 0; }
}
"""

CSS_DARK = """
/* ── API Health banner — dark theme (radar dashboard) ─── */
.api-health-banner {
  display: flex;
  align-items: center;
  gap: 18px;
  padding: 10px 40px;
  background: #211d18;
  border-bottom: 1px solid #3a3530;
  flex-wrap: wrap;
  font-family: 'Helvetica Neue', Arial, sans-serif;
}
.api-health-banner.banner-ok   { background: #1f2a1f; }
.api-health-banner.banner-fail { background: #2a1c1c; }
.api-health-banner.banner-warn { background: #2a261c; }
.api-health-headline {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--gold-light);
  font-weight: 600;
}
.banner-ok   .api-health-headline { color: #6dcc8a; }
.banner-fail .api-health-headline { color: #e07b7b; }
.banner-warn .api-health-headline { color: var(--gold-light); }
.api-health-headline .api-dot { width: 9px; height: 9px; border-radius: 50%; }
.banner-ok   .api-health-headline .api-dot { background: #5a9b72; box-shadow: 0 0 8px rgba(90,155,114,0.7); }
.banner-fail .api-health-headline .api-dot { background: #c0392b; box-shadow: 0 0 8px rgba(192,57,43,0.7); }
.banner-warn .api-health-headline .api-dot { background: var(--gold-light); box-shadow: 0 0 8px rgba(212,184,122,0.6); }
.api-health-pills { display: inline-flex; flex-wrap: wrap; gap: 6px; }
.api-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  border-radius: 14px;
  background: rgba(255,255,255,0.04);
  color: #c0b8a8;
  font-size: 11px;
  letter-spacing: 0.04em;
  cursor: help;
  transition: background 0.15s;
}
.api-pill:hover { background: rgba(255,255,255,0.08); }
.api-pill .api-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.api-pill.api-ok      .api-dot { background: #5a9b72; box-shadow: 0 0 6px rgba(90,155,114,0.5); }
.api-pill.api-fail    .api-dot { background: #c0392b; box-shadow: 0 0 6px rgba(192,57,43,0.5); }
.api-pill.api-missing .api-dot { background: #6b6b6b; }
.api-pill.api-skip    .api-dot { background: var(--gold-light); }
.api-pill.api-ok   { color: #b8d4c0; }
.api-pill.api-fail { color: #e8a8a8; }
.api-pill.api-skip { color: var(--gold-light); }
@media (max-width: 720px) {
  .api-health-banner   { padding: 8px 14px; gap: 10px; }
  .api-health-headline { width: 100%; font-size: 10px; }
  .api-pill            { font-size: 10px; padding: 2px 8px; }
}
"""
