#!/usr/bin/env python3
"""
My Villa — Social @-handle verifier / sanitizer

The content generators were inventing Instagram handles when crediting a
source (e.g. "A new analysis from @eaaorg…"), tagging non-existent or wrong
accounts. A handle merely *existing* doesn't prove it's the RIGHT account, so
we never guess: an @-mention survives ONLY if it is one of ours or a handle
the operator has CONFIRMED in _system/config/source_handles.yml. Everything
else is stripped, and the source is left as plain text.

Two entry points:
  • sanitize(text) -> (clean_text, removed)
      Removes unverified @handles (connector-aware so grammar survives:
      "analysis from @eaaorg breaks down" -> "analysis breaks down"), and
      normalises verified ones (fixes a stray trailing dot, canonical casing).
      Use this as the last gate before anything is published.
  • resolve_source(name) -> handle | None
      Maps a known publication name ("Los Angeles Times") to its verified
      handle ("latimes") so generators can credit it correctly instead of
      guessing — and return None (→ plain name, no @) when unknown.

CLI:
  python3 verify_handles.py check "@eaaorg @latimes"      # per-handle verdict
  python3 verify_handles.py clean  "text with @mentions"  # print sanitized
  python3 verify_handles.py scan   <dir>                  # audit drafts on disk
"""

import re
import sys
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
CONFIG_FILE = SYSTEM_DIR / "config" / "source_handles.yml"

# IG/X handles: start and end with [A-Za-z0-9_], dots only internal (a real
# handle never ends with a dot) — so a sentence period right after a handle
# ("@transsolar.") stays as punctuation instead of being eaten into the match.
_HANDLE_RE = re.compile(r"@(?P<h>[A-Za-z0-9_](?:[A-Za-z0-9_.]{0,38}[A-Za-z0-9_])?)")

# Connector words/punctuation that introduce an attribution; removed together
# with the handle so the sentence still reads ("reported by @x" -> "reported").
_CONNECTORS = (r"from|by|via|per|at|on|according to|courtesy of|h/t|ht|"
               r"reports?|reported|writes?|wrote|says?|said")

_cache = {"mtime": None, "data": None}


def _norm_handle(h: str) -> str:
    """Lowercase, drop leading @, strip trailing dots/punctuation IG can't have."""
    return h.lstrip("@").strip().rstrip(".").rstrip("_").lower()


def _norm_name(name: str) -> str:
    """Loose key for source-name lookup: lowercase alnum only."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _load():
    try:
        st = CONFIG_FILE.stat()
    except OSError:
        return {"allowed": set(), "sources": {}}
    if _cache["mtime"] == st.st_mtime and _cache["data"] is not None:
        return _cache["data"]
    try:
        cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {"allowed": set(), "sources": {}}
    allowed = set()
    for key in ("own", "verified"):
        for h in (cfg.get(key) or []):
            n = _norm_handle(str(h))
            if n:
                allowed.add(n)
    sources = {}
    for name, h in (cfg.get("sources") or {}).items():
        nh = _norm_handle(str(h))
        if nh:
            sources[_norm_name(name)] = nh
            allowed.add(nh)
    data = {"allowed": allowed, "sources": sources}
    _cache["mtime"] = st.st_mtime
    _cache["data"] = data
    return data


def is_allowed(handle: str) -> bool:
    """True if this @handle is ours or operator-verified."""
    return _norm_handle(handle) in _load()["allowed"]


def resolve_source(name: str):
    """Verified handle for a known publication name, else None (→ plain name)."""
    if not name:
        return None
    return _load()["sources"].get(_norm_name(name))


def find_handles(text: str):
    """All @handles in the text, normalised."""
    return [_norm_handle(m.group("h")) for m in _HANDLE_RE.finditer(text or "")]


def sanitize(text: str):
    """Strip unverified @handles (connector-aware), normalise verified ones.

    Returns (clean_text, removed_handles)."""
    if not text:
        return text, []
    removed = []
    SENT = "\x00"  # sentinel marking a handle to delete

    def _repl(m):
        core = _norm_handle(m.group("h"))
        if not core:
            return ""
        if is_allowed(core):
            return "@" + core  # keep, normalised (fixes trailing dot / casing)
        removed.append(core)
        return SENT

    out = _HANDLE_RE.sub(_repl, text)
    if removed:
        # Remove the sentinel together with a leading connector when present,
        # so "analysis from @x breaks" -> "analysis breaks", not "analysis  breaks".
        out = re.sub(r"\s*\b(?:" + _CONNECTORS + r")\b\s*" + SENT, "", out,
                     flags=re.IGNORECASE)
        out = re.sub(r"\s*[—–\-·|:]\s*" + SENT, "", out)   # punctuation connector
        # conjunctions/commas joining a removed handle ("X and @y" -> "X")
        out = re.sub(r"\s*(?:and|&|with|,|;|/)\s*" + SENT, "", out,
                     flags=re.IGNORECASE)
        out = re.sub(r"\s*" + SENT, "", out)               # any leftover sentinel
        # Tidy the wound.
        out = re.sub(r"\(\s*\)", "", out)                  # empty parens
        out = re.sub(r"[ \t]{2,}", " ", out)               # collapsed spaces
        out = re.sub(r"\s+([,.;:!?])", r"\1", out)         # space before punct
        out = re.sub(r"([,;:])\s*([,.;:!?])", r"\2", out)  # doubled punct
        out = re.sub(r"\n[ \t]+", "\n", out)
        out = out.strip()
    return out, removed


# ── CLI ──────────────────────────────────────────────────────────────
def _scan(root: Path):
    """Audit *.md captions on disk: list files whose @handles aren't verified."""
    flagged = 0
    for f in sorted(root.rglob("*.md")):
        try:
            txt = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        bad = sorted({h for h in find_handles(txt) if not is_allowed(h)})
        if bad:
            flagged += 1
            print(f"  {f.relative_to(root)}: " + ", ".join("@" + b for b in bad))
    print(f"\n{flagged} file(s) with unverified handles under {root}")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "check":
        for h in find_handles(sys.argv[2]) or [_norm_handle(sys.argv[2])]:
            print(f"  @{h}: {'VERIFIED' if is_allowed(h) else 'unverified → strip'}")
    elif len(sys.argv) >= 3 and sys.argv[1] == "clean":
        clean, removed = sanitize(sys.argv[2])
        print("CLEAN:", clean)
        print("REMOVED:", ", ".join("@" + r for r in removed) or "(none)")
    elif len(sys.argv) >= 3 and sys.argv[1] == "scan":
        _scan(Path(sys.argv[2]))
    else:
        print(__doc__)
