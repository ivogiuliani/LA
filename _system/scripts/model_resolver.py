#!/usr/bin/env python3
"""
model_resolver.py — selezione automatica del miglior modello Claude.

Policy (decisa con Ivo, 2026-06-10): "passaggio automatico delle API a
modelli superiori appena arrivano". Ogni script della pipeline chiede
un TIER funzionale, non un model id:

    from model_resolver import resolve
    model = resolve("writer")     # oggi → claude-fable-5

Tier → famiglie (in ordine di preferenza) → si sceglie SEMPRE la
versione più alta disponibile nella prima famiglia che ha modelli:

    writer    [fable, opus]   articoli journal (brand-critical) —
                              Fable è la famiglia top: appena esce
                              fable-6 si passa da soli a fable-6
    heavy     [opus, fable]   pitch giornalisti, social, revise radar
    balanced  [sonnet]        scoring, validazione, follow-up, caption
    cheap     [haiku]         scoring di massa, scraper

Come funziona:
  1. GET https://api.anthropic.com/v1/models (lista ufficiale dei
     modelli disponibili per la NOSTRA chiave — niente guessing).
  2. Cache su disco 24h (_system/config/.model_cache.json): una sola
     chiamata al giorno, il resto della pipeline legge la cache.
  3. Parse della versione dall'id: claude-{famiglia}-{major}[-{minor}]
     [-YYYYMMDD]. Versione più alta vince; a parità, l'alias senza
     data batte lo snapshot datato.
  4. Se la risoluzione di un tier CAMBIA rispetto all'ultima volta,
     stampa "MODEL UPGRADE" (finisce nel log giornaliero della
     pipeline, visibile nel digest di debug).
  5. Famiglie NUOVE sconosciute (es. un ipotetico claude-nova-1) NON
     vengono adottate alla cieca: vengono segnalate nel log perché
     potrebbero essere modelli specializzati. L'auto-upgrade vale
     DENTRO le famiglie note (fable/opus/sonnet/haiku) e per le loro
     versioni future.
  6. Fallback robusto: API giù → cache anche scaduta → costanti
     hardcoded (gli id validi di oggi). La pipeline non si ferma mai
     per colpa del resolver.

CLI:
    python3 model_resolver.py            # tabella risoluzioni
    python3 model_resolver.py --refresh  # forza refresh cache
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
ROOT_DIR = SYSTEM_DIR.parent
CACHE_PATH = SYSTEM_DIR / "config" / ".model_cache.json"

# Tier → famiglie in ordine di preferenza.
TIERS: dict[str, list[str]] = {
    "writer": ["fable", "opus"],
    "heavy": ["opus", "fable"],
    "balanced": ["sonnet"],
    "cheap": ["haiku"],
}

# ── SOSPENSIONE TEMPORANEA MODELLI ──────────────────────────────────
# Famiglie/id temporaneamente NON operativi lato Anthropic. Esclusi da
# TUTTA la risoluzione (discovery, cache E fallback): il tier ripiega
# automaticamente sulla famiglia successiva.
#   >>> PER RIATTIVARE: togliere la voce da SUSPENDED_FAMILIES. <<<
# 2026-06-13 (Ivo): Fable 5 sospeso → "writer" ripiega su Opus 4.8
#   ("heavy" era già opus). Rimuovere "fable" appena Fable 5 torna live.
SUSPENDED_FAMILIES: set[str] = {"fable"}
SUSPENDED_IDS: set[str] = set()

# Miglior id "di sicurezza" per famiglia, usato quando API e cache sono
# entrambe indisponibili (id verificati validi al 2026-06-10).
FAMILY_FALLBACK: dict[str, str] = {
    "fable": "claude-fable-5",
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}

KNOWN_FAMILIES = set(FAMILY_FALLBACK)


def _is_suspended(model_id: str) -> bool:
    """True se l'id (o la sua famiglia) è temporaneamente sospeso."""
    if model_id in SUSPENDED_IDS:
        return True
    m = _ID_RE.match(model_id or "")
    return bool(m and m.group("family") in SUSPENDED_FAMILIES)


def _tier_fallback(tier: str) -> str:
    """Fallback del tier che salta le famiglie sospese."""
    for fam in TIERS[tier]:
        if fam not in SUSPENDED_FAMILIES and fam in FAMILY_FALLBACK:
            return FAMILY_FALLBACK[fam]
    # caso-limite: tutte le famiglie del tier sospese → la preferita
    return FAMILY_FALLBACK.get(TIERS[tier][0], "claude-opus-4-8")


# Fallback finale per tier (suspension-aware). Interfaccia invariata:
# il resto del modulo continua a leggere FALLBACKS[tier].
FALLBACKS: dict[str, str] = {tier: _tier_fallback(tier) for tier in TIERS}

# minor = max 2 cifre: senza il limite, lo snapshot datato senza minor
# (claude-opus-4-20250514) veniva parsato come minor=20250514 e
# "batteva" qualunque versione reale.
_ID_RE = re.compile(
    r"^claude-(?P<family>[a-z]+)-(?P<major>\d+)"
    r"(?:-(?P<minor>\d{1,2}))?"
    r"(?:-(?P<date>\d{8}))?$"
)


def _load_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    env_file = ROOT_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _fetch_models() -> list[dict] | None:
    """Lista modelli da /v1/models. None su qualunque errore."""
    key = _load_api_key()
    if not key:
        return None
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/models?limit=200",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
        models = data.get("data") or []
        return models if models else None
    except Exception:  # noqa: BLE001 — qualunque errore → fallback cache
        return None


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass  # cache best-effort, mai fatale


def _parse_id(model_id: str):
    """→ (family, version_tuple, is_dated) oppure None se non-parsabile."""
    m = _ID_RE.match(model_id or "")
    if not m:
        return None
    family = m.group("family")
    major = int(m.group("major"))
    minor = int(m.group("minor") or 0)
    return family, (major, minor), bool(m.group("date"))


def _best_of_family(model_ids: list[str], family: str) -> str | None:
    """Id con versione più alta nella famiglia; alias batte snapshot."""
    best = None  # (version, undated_pref, id)
    for mid in model_ids:
        parsed = _parse_id(mid)
        if not parsed or parsed[0] != family:
            continue
        _, version, is_dated = parsed
        key = (version, 0 if is_dated else 1)  # undated vince a parità
        if best is None or key > best[0]:
            best = (key, mid)
    return best[1] if best else None


def _resolve_all(model_ids: list[str]) -> dict[str, str]:
    # Anche se /v1/models elencasse un modello sospeso (sospensione
    # lato server = id presente ma inutilizzabile), lo scartiamo qui.
    live_ids = [m for m in model_ids if not _is_suspended(m)]
    out = {}
    for tier, families in TIERS.items():
        chosen = None
        for fam in families:
            if fam in SUSPENDED_FAMILIES:
                continue
            chosen = _best_of_family(live_ids, fam)
            if chosen:
                break
        out[tier] = chosen or FALLBACKS[tier]
    return out


def _get_resolution(force_refresh: bool = False) -> dict[str, str]:
    """Risoluzione tier→model con cache 24h + notifiche upgrade."""
    cache = _load_cache()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Una cache è valida solo se NON contiene modelli ora sospesi: una
    # risoluzione salvata ieri con Fable, dopo la sospensione, va
    # ricalcolata anche se la data è di oggi. Difesa indipendente dal
    # refresh manuale (il cloud non lancia --refresh).
    cached = cache.get("resolution") or {}
    cache_clean = bool(cached) and not any(
        _is_suspended(m) for m in cached.values())

    if not force_refresh and cache.get("date") == today and cache_clean:
        return cached

    models = _fetch_models()
    if models is None:
        # API giù: usa la cache solo se "pulita", altrimenti fallback
        # (FALLBACKS è già suspension-aware).
        if cache_clean:
            return cached
        return dict(FALLBACKS)

    model_ids = [m.get("id", "") for m in models]
    resolution = _resolve_all(model_ids)

    # Notifica UPGRADE quando un tier cambia modello.
    prev = cache.get("resolution") or {}
    for tier, mid in resolution.items():
        if prev.get(tier) and prev[tier] != mid:
            print(f"  [model] ⬆ MODEL UPGRADE {tier}: {prev[tier]} → {mid}")

    # Famiglie nuove non ancora gestite → segnala, non adottare.
    seen_families = set()
    for mid in model_ids:
        parsed = _parse_id(mid)
        if parsed:
            seen_families.add(parsed[0])
    unknown = sorted(seen_families - KNOWN_FAMILIES)
    if unknown and unknown != cache.get("unknown_families"):
        print(f"  [model] ℹ nuove famiglie disponibili (non auto-adottate, "
              f"valutare): {', '.join(unknown)}")

    _save_cache({
        "date": today,
        "resolution": resolution,
        "model_ids": model_ids,
        "unknown_families": unknown,
        "refreshed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    return resolution


def resolve(tier: str, *, force_refresh: bool = False) -> str:
    """API pubblica: model id per il tier ('writer'|'heavy'|'balanced'|'cheap')."""
    if tier not in TIERS:
        raise ValueError(f"tier sconosciuto: {tier!r} (validi: {list(TIERS)})")
    try:
        return _get_resolution(force_refresh).get(tier) or FALLBACKS[tier]
    except Exception:  # noqa: BLE001 — il resolver non deve MAI rompere la pipeline
        return FALLBACKS[tier]


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Risoluzione tier→modello Claude")
    p.add_argument("--refresh", action="store_true", help="Ignora la cache 24h")
    args = p.parse_args(argv)
    res = _get_resolution(force_refresh=args.refresh)
    print("Tier        Modello risolto")
    print("─" * 45)
    for tier in TIERS:
        marker = " (fallback)" if res.get(tier) == FALLBACKS.get(tier) and \
            not _load_cache().get("model_ids") else ""
        print(f"{tier:10}  {res.get(tier)}{marker}")
    cache = _load_cache()
    if cache.get("refreshed_at"):
        print(f"\nCache: {CACHE_PATH.name} (refresh {cache['refreshed_at']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
