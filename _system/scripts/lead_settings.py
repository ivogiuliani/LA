#!/usr/bin/env python3
"""
lead_settings.py — loader condiviso di `_system/config/lead_settings.yml`
e del "private dir" (registro lead fuori git).

Usato da: send_email.py (budget/kind/gate), lead_ledger.py, lead_score.py,
lead_ack.py, lead_intake.py, lead_desk.py, lead_api.py, chat_api.py.

    from lead_settings import load_settings, private_dir, get
    cfg = load_settings()              # dict (cache in-process)
    get("brand.booking_url", "")       # dotted path con default
    private_dir()                      # Path, creato se manca (chmod 700)

Private dir: env `MYVILLA_PRIVATE_DIR` (nome env letto da
`private_dir_env` nello YAML), default ~/.myvilla-private.
Nessun dato personale finisce nel repo: tutto ciò che contiene
email/telefoni di lead vive lì.

CLI:
    python3 lead_settings.py            # stampa i valori risolti (senza PII)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = SYSTEM_DIR.parent
SETTINGS_PATH = SYSTEM_DIR / "config" / "lead_settings.yml"

_CACHE: Optional[dict] = None

# Valori di riserva se lo YAML manca o è incompleto: la pipeline non
# deve mai fermarsi per un file di config assente.
_DEFAULTS: dict = {
    "brand": {
        "name": "My Villa",
        "site": "https://myvilla.la",
        "contact_email": "info@myvilla.la",
        "landing_url": "https://myvilla.la/private-briefing.html",
        "thank_you_url": "https://myvilla.la/briefing-received.html",
        "booking_url": "",
        "postal_address": "",
    },
    "canonical": {
        "response_promise": "within one business day",
        "founder_name": "Paolo Mezzalama",
    },
    "signatures": {
        "prospects": "the office of Paolo Mezzalama · My Villa\ninfo@myvilla.la · myvilla.la",
    },
    "alerts": {"to": ["info@myvilla.la"], "pii_level": "minimal"},
    "budgets_per_day": {},
    "dry_run_kinds": [],
    "commercial_kinds": [],
    "private_dir_env": "MYVILLA_PRIVATE_DIR",
    "lead": {
        "retention_months": 24,
        "ack_within_minutes": 5,
        "sla_hours_founder": 24,
        "states": ["new", "acked", "founder_replied", "call_booked",
                   "briefing_done", "won", "lost", "dormant", "do_not_contact"],
    },
    "chat": {"enabled": False,
             "disclosure": "You're chatting with My Villa's AI assistant, not a person."},
    "lead_api": {},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings(*, force: bool = False) -> dict:
    """Ritorna lo YAML fuso con i default. Cache in-process."""
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    data: dict = {}
    try:
        if SETTINGS_PATH.exists():
            data = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 — config rotta ≠ pipeline ferma
        print(f"  [lead_settings] WARN cannot read {SETTINGS_PATH.name}: {exc}",
              file=sys.stderr)
        data = {}
    merged = _deep_merge(_DEFAULTS, data)
    # Override dei destinatari alert da .env (LEAD_ALERT_TO=a@x,b@y): il repo
    # è pubblico, quindi nello YAML resta solo la casella aziendale.
    _load_dotenv_once()
    raw_to = os.environ.get("LEAD_ALERT_TO", "").strip()
    if raw_to:
        merged.setdefault("alerts", {})["to"] = [
            a.strip() for a in raw_to.split(",") if a.strip()]
    _CACHE = merged
    return _CACHE


_DOTENV_LOADED = False


def _load_dotenv_once() -> None:
    """Legge ROOT/.env (KEY=VALUE, senza dipendenze) solo per le chiavi assenti."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    try:
        env_path = SETTINGS_PATH.parent.parent.parent / ".env"
        if not env_path.exists():
            return
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip(); v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:  # noqa: BLE001
        pass


def get(path: str, default: Any = None) -> Any:
    """Accesso con percorso puntato: get('brand.booking_url', '')."""
    node: Any = load_settings()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node if node is not None else default


def private_dir(create: bool = True) -> Path:
    """Directory privata (fuori git) per registro lead, log PII, chat."""
    env_name = get("private_dir_env", "MYVILLA_PRIVATE_DIR") or "MYVILLA_PRIVATE_DIR"
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        # Fallback: .env di progetto (stesso meccanismo degli altri script)
        env_file = PROJECT_ROOT / ".env"
        if env_file.exists():
            try:
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith(env_name + "="):
                        raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
            except OSError:
                pass
    if not raw:
        raw = "~/.myvilla-private"
    p = Path(raw).expanduser()
    if create:
        try:
            p.mkdir(parents=True, exist_ok=True)
            os.chmod(p, 0o700)
        except OSError:
            pass
    return p


def lead_states() -> list:
    return list(get("lead.states", _DEFAULTS["lead"]["states"]))


def _main() -> int:
    cfg = load_settings(force=True)
    view = {
        "settings_path": str(SETTINGS_PATH),
        "private_dir": str(private_dir(create=False)),
        "brand": cfg.get("brand"),
        "budgets_per_day": cfg.get("budgets_per_day"),
        "dry_run_kinds": cfg.get("dry_run_kinds"),
        "commercial_kinds": cfg.get("commercial_kinds"),
        "lead.states": lead_states(),
        "chat.enabled": get("chat.enabled"),
    }
    print(json.dumps(view, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
