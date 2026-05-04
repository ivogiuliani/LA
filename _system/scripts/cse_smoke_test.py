#!/usr/bin/env python3
"""
Google Custom Search Engine — smoke test.

Run this AFTER you've pasted the new GOOGLE_CSE_ENGINE_ID into .env to
verify the API key + engine ID combo actually returns results, without
having to wait for a full radar scan.

Usage:
    python3 _system/scripts/cse_smoke_test.py
    python3 _system/scripts/cse_smoke_test.py --query "luxury villa los angeles"

Exit codes:
    0 — query worked, got results
    1 — query worked, got no results (engine is not configured to
        "Search the entire web" or the query has no hits)
    2 — config error (missing key, placeholder cx, etc.)
    3 — HTTP / API error (will print Google's response verbatim so you
        can read the actual cause)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

_PLACEHOLDER_CX = {"", "PENDING", "TODO", "TBD", "CHANGEME", "YOUR_CX_HERE"}


def _load_dotenv():
    """Tiny .env loader (no external deps)."""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--query", "-q",
        default="luxury villa los angeles",
        help="Test query (default: 'luxury villa los angeles')",
    )
    parser.add_argument(
        "--num", "-n",
        type=int, default=3,
        help="Number of results to display (default: 3)",
    )
    args = parser.parse_args(argv)

    _load_dotenv()
    key = os.environ.get("GOOGLE_CSE_API_KEY", "").strip()
    cx = os.environ.get("GOOGLE_CSE_ENGINE_ID", "").strip()

    print("=" * 60)
    print("Google CSE smoke test")
    print("=" * 60)
    print(f"  API key: {'set (' + key[:6] + '...' + key[-4:] + ')' if key else '✗ MISSING'}")
    print(f"  Engine ID (cx): {cx or '✗ MISSING'}")
    print(f"  Query: {args.query!r}")
    print()

    if not key:
        print("✗ GOOGLE_CSE_API_KEY is not set in .env. Aborting.")
        return 2
    if not cx or cx.upper() in _PLACEHOLDER_CX:
        print(f"✗ GOOGLE_CSE_ENGINE_ID is {cx!r} (placeholder).")
        print()
        print("Fix: create a Programmable Search Engine at")
        print("     https://programmablesearchengine.google.com/")
        print("     and paste the resulting ID into .env.")
        return 2

    url = "https://www.googleapis.com/customsearch/v1?" + urllib.parse.urlencode({
        "key": key, "cx": cx, "q": args.query, "num": args.num,
    })

    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        print(f"✗ HTTP {e.code} — Google rejected the request.")
        print()
        print("Response body (verbatim):")
        print(body[:1500])
        print()
        # Common errors → friendly hints
        if "API key not valid" in body:
            print("Hint: the GOOGLE_CSE_API_KEY in .env is wrong or revoked.")
        elif "Invalid Value" in body and "cx" in body.lower():
            print("Hint: the GOOGLE_CSE_ENGINE_ID is wrong (typo, or you copied")
            print("      the public URL instead of the cx value).")
        elif "billing" in body.lower():
            print("Hint: enable billing for the Google Cloud project that owns")
            print("      the API key (free tier = 100 queries/day still requires")
            print("      a billing account).")
        elif "Custom Search API has not been used" in body or "disabled" in body.lower():
            print("Hint: enable 'Custom Search API' in the Google Cloud console")
            print("      for the project that owns the API key.")
        return 3
    except Exception as e:
        print(f"✗ Network error: {type(e).__name__}: {e}")
        return 3

    items = data.get("items", []) or []
    total = (data.get("searchInformation", {}) or {}).get("totalResults", "?")
    print(f"✓ API responded — totalResults={total}, returned {len(items)} items")
    print()

    if not items:
        print("⚠️  Got 0 results. Possible causes:")
        print("    • The engine is restricted to specific sites and none match")
        print("      your query. Open the engine settings on")
        print("      https://programmablesearchengine.google.com/ and toggle")
        print("      'Search the entire web' ON.")
        print("    • Your query genuinely has no hits (try --query 'pizza').")
        return 1

    for i, item in enumerate(items, 1):
        print(f"  {i}. {item.get('title', '')[:80]}")
        print(f"     {item.get('link', '')}")
        print(f"     {item.get('snippet', '')[:100]}")
        print()

    print("✓ Smoke test passed. Re-run the radar:")
    print("    python3 _system/scripts/radar.py --lookback 10")
    return 0


if __name__ == "__main__":
    sys.exit(main())
