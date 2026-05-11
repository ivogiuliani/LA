#!/usr/bin/env python3
"""
publish_radar_team.py — wrap the daily radar HTML in a password gate
and write it to team/radar/index.html so collaborators can read the
full radar (news + pitch drafts + scores + contacts) at
https://myvilla.la/team/radar/.

Threat model (intentionally weak, by operator's choice "Strada 1")
------------------------------------------------------------------
- The password is hardcoded (SHA-256 hashed) in the page.
- The HTML is in the PUBLIC GitHub repo ivogiuliani/LA. A determined
  attacker who browses the repo can pull the file directly and
  bypass the JS gate entirely.
- This stops:
    * casual web visitors who land on myvilla.la/team/radar/
    * crawlers (noindex + robots.txt Disallow)
- This does NOT stop:
    * anyone who browses the github.com/ivogiuliani/LA repo source
    * anyone who brute-forces the SHA-256 of an 8-char common word
- For real auth, see the Cloudflare Access path discussed with the
  operator. This file is the agreed-upon soft gate for trusted
  collaborators only.

Usage
-----
    python3 publish_radar_team.py
    python3 publish_radar_team.py --radar _system/radar/reports/radar_2026-05-04.json
    python3 publish_radar_team.py --password ivopaolo --output team/radar/index.html

Called automatically at the end of generate_radar_report.py via
publish_team_radar() so the team view is refreshed on every daily run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
ROOT_DIR = SYSTEM_DIR.parent

# Reuse the same renderer that powers the local /radar dashboard so the
# team view stays in lockstep with what the operator sees.
sys.path.insert(0, str(SCRIPT_DIR))
from generate_radar_report import render_dashboard  # noqa: E402

DEFAULT_PASSWORD = "ivopaolo"


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _gate_block(password_hash: str) -> str:
    """The HTML chunk we inject right before </head> in the radar page.

    Includes:
      - robots noindex/nofollow/noarchive/nocache meta
      - <style> that hides the entire html element until auth passes
      - <script> that prompts for the password, hashes with crypto.subtle,
        compares, and reveals the page on match. sessionStorage caches
        the auth so a reload during the same browser session doesn't
        re-prompt.
    """
    return f"""
  <meta name="robots" content="noindex,nofollow,noarchive,nocache">
  <style id="mv-auth-gate-style">
    html {{ visibility: hidden !important; }}
    html.mv-team-authed {{ visibility: visible !important; }}
  </style>
  <script id="mv-auth-gate-script">
  (function() {{
    const CORRECT_HASH = '{password_hash}';
    const SESSION_KEY  = 'myvilla-team-auth';

    function reveal() {{
      sessionStorage.setItem(SESSION_KEY, '1');
      document.documentElement.classList.add('mv-team-authed');
    }}

    function deny() {{
      // Replace the whole document with a minimal access-denied page
      // so the protected content is not even in the rendered tree.
      document.documentElement.innerHTML =
        '<body style="font-family:-apple-system,sans-serif;padding:3rem;' +
        'visibility:visible;color:#666;background:#faf8f5">' +
        '<h1 style="color:#333">Access denied</h1>' +
        '<p>Reload the page to retry.</p></body>';
      document.documentElement.classList.add('mv-team-authed');
    }}

    function ask() {{
      const p = prompt('My Villa team radar\\n\\nEnter password:');
      if (p === null || p === '') {{ deny(); return; }}
      if (!window.crypto || !window.crypto.subtle) {{
        alert('Your browser is too old for the auth gate. Use a modern browser.');
        deny();
        return;
      }}
      const enc = new TextEncoder().encode(p);
      window.crypto.subtle.digest('SHA-256', enc).then(function(buf) {{
        const arr = Array.from(new Uint8Array(buf));
        const hex = arr.map(function(b) {{ return b.toString(16).padStart(2,'0'); }}).join('');
        if (hex === CORRECT_HASH) {{ reveal(); }}
        else {{ alert('Wrong password. Try again.'); ask(); }}
      }});
    }}

    if (sessionStorage.getItem(SESSION_KEY) === '1') {{
      reveal();
    }} else if (document.readyState === 'loading') {{
      document.addEventListener('DOMContentLoaded', ask);
    }} else {{
      ask();
    }}
  }})();
  </script>
"""


def wrap_with_password_gate(html: str, password_hash: str) -> str:
    """Insert the gate block immediately before </head> in the input HTML."""
    block = _gate_block(password_hash)
    if "</head>" in html:
        return html.replace("</head>", block + "\n</head>", 1)
    # Fallback — prepend the block. The page won't get the visibility
    # cascade right, but at least the script runs.
    return block + html


def publish_team_radar(
    radar_data: dict,
    date_str: str,
    *,
    output_path: Path | None = None,
    password: str = DEFAULT_PASSWORD,
) -> Path:
    """Render + gate the radar, then write to disk. Returns output path.

    Designed to be called from generate_radar_report.py at the end of a
    successful render. Failures should bubble up to the caller so the
    main script can log them without blocking the rest of the pipeline.
    """
    html = render_dashboard(radar_data, date_str)
    gated = wrap_with_password_gate(html, _sha256(password))
    out = Path(output_path) if output_path else (ROOT_DIR / "team" / "radar" / "index.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(gated, encoding="utf-8")
    return out


def _find_latest_radar() -> Path | None:
    reports = SYSTEM_DIR / "radar" / "reports"
    if not reports.exists():
        return None
    candidates = sorted(reports.glob("radar_*.json"))
    return candidates[-1] if candidates else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--radar",
        help="Path to a radar JSON. Default: most recent in _system/radar/reports/.",
    )
    parser.add_argument(
        "--output",
        default=str(ROOT_DIR / "team" / "radar" / "index.html"),
        help="Output HTML path (default: team/radar/index.html).",
    )
    parser.add_argument(
        "--password", default=DEFAULT_PASSWORD,
        help="Password for the soft gate (default: 'ivopaolo'). "
             "Stored as SHA-256 in the page, never as plaintext.",
    )
    args = parser.parse_args(argv)

    radar_path = Path(args.radar) if args.radar else _find_latest_radar()
    if not radar_path or not radar_path.exists():
        print("No radar JSON found.", file=sys.stderr)
        return 1
    try:
        data = json.loads(radar_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Malformed radar JSON {radar_path}: {e}", file=sys.stderr)
        return 1
    date_str = data.get("date", radar_path.stem.replace("radar_", ""))
    out = publish_team_radar(
        data, date_str,
        output_path=Path(args.output),
        password=args.password,
    )
    size_kb = out.stat().st_size / 1024
    print(f"  [team-radar] wrote {out} ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
