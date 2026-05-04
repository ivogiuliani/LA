#!/usr/bin/env python3
"""
Unit tests for api_health_banner.

Coverage focuses on the fragile bits called out in code review:
  1. classify_overall_status precedence (skip is warn, fail beats missing,
     etc.) — was the source of a bug where ok+skip showed as fully OK.
  2. _normalize_status whitelist — guards against arbitrary CSS class
     injection from a malformed JSON.
  3. XSS escaping of the detail field — operator sees the same string in
     a <span title="…"> attribute and any unescaped quote breaks the HTML.
  4. Non-dict entries / non-dict api_health — defensive coercion so a
     malformed radar JSON cannot crash the dashboard.

Run with:
    python3 -m unittest _system/scripts/test_api_health_banner.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Make the sibling module importable when this file is invoked directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from api_health_banner import (  # noqa: E402  (sys.path tweak above)
    KNOWN_STATUSES,
    classify_overall_status,
    render_api_health_banner_html,
    render_api_health_banner_md,
    _coerce_entry,
    _normalize_status,
)


# ── classify_overall_status ──────────────────────────────────────────

class TestClassifyOverallStatus(unittest.TestCase):
    """Worst-wins precedence; skip is warn, not ok."""

    def test_empty_or_none_returns_warn(self):
        self.assertEqual(classify_overall_status(None)[0],  "warn")
        self.assertEqual(classify_overall_status({})[0],    "warn")

    def test_non_dict_returns_warn(self):
        # Lists, strings, ints — anything not a dict — should not crash.
        self.assertEqual(classify_overall_status([])[0],          "warn")
        self.assertEqual(classify_overall_status("ok")[0],        "warn")
        self.assertEqual(classify_overall_status(42)[0],          "warn")

    def test_all_ok_is_ok(self):
        api = {"a": {"status": "ok"}, "b": {"status": "ok"}}
        key, label = classify_overall_status(api)
        self.assertEqual(key, "ok")
        self.assertIn("attive", label.lower())

    def test_skip_alone_is_warn_not_ok(self):
        # The bug the reviewer caught: skip=disabled-feature is meaningful,
        # not noise to roll into a green banner.
        api = {"a": {"status": "skip"}}
        key, label = classify_overall_status(api)
        self.assertEqual(key, "warn")
        self.assertIn("salt", label.lower())  # "saltate"

    def test_ok_plus_skip_is_warn(self):
        api = {"a": {"status": "ok"}, "b": {"status": "skip"}}
        key, _ = classify_overall_status(api)
        self.assertEqual(key, "warn")

    def test_fail_beats_everything(self):
        # Fail > missing > skip > ok.
        api = {
            "a": {"status": "ok"},
            "b": {"status": "skip"},
            "c": {"status": "missing"},
            "d": {"status": "fail"},
        }
        key, _ = classify_overall_status(api)
        self.assertEqual(key, "fail")

    def test_missing_beats_skip(self):
        # Missing means "should be configured, isn't" — actionable.
        # Skip means "intentionally disabled" — informational.
        api = {"a": {"status": "missing"}, "b": {"status": "skip"}}
        key, _ = classify_overall_status(api)
        self.assertEqual(key, "warn")
        # Both are warn-level but the missing branch wins for label text.
        _, label = classify_overall_status(api)
        self.assertIn("Configurazione", label)

    def test_unknown_status_is_treated_as_missing(self):
        # Defensive: unrecognized statuses must not bypass classification.
        api = {"a": {"status": "totally_made_up_status"}}
        key, _ = classify_overall_status(api)
        self.assertEqual(key, "warn")


# ── _normalize_status ────────────────────────────────────────────────

class TestNormalizeStatus(unittest.TestCase):

    def test_known_statuses_pass_through(self):
        for s in KNOWN_STATUSES:
            self.assertEqual(_normalize_status(s), s)

    def test_unknown_status_falls_back_to_missing(self):
        self.assertEqual(_normalize_status("DEPRECATED"),       "missing")
        self.assertEqual(_normalize_status("rate-limited"),     "missing")
        self.assertEqual(_normalize_status(""),                 "missing")

    def test_non_string_falls_back_to_missing(self):
        # ints, None, lists — should never raise.
        self.assertEqual(_normalize_status(None),    "missing")
        self.assertEqual(_normalize_status(0),       "missing")
        self.assertEqual(_normalize_status(["ok"]),  "missing")

    def test_case_and_whitespace_normalized(self):
        self.assertEqual(_normalize_status("  OK  "),  "ok")
        self.assertEqual(_normalize_status("Fail"),    "fail")

    def test_status_used_in_html_class_is_safe(self):
        # The whole point of the whitelist is preventing CSS class injection.
        # Verify that a malicious status never makes it into the rendered HTML.
        evil = '"></span><script>alert(1)</script>'
        api = {"a": {"status": evil}}
        html = render_api_health_banner_html(api)
        # Either the script tag escaped to entities, or the status was
        # dropped to "missing" — never a literal <script> in the output.
        self.assertNotIn("<script>", html)
        self.assertIn("api-missing", html)  # whitelisted fallback applied


# ── XSS escaping in tooltip / label ──────────────────────────────────

class TestEscaping(unittest.TestCase):

    def test_detail_is_escaped_in_tooltip(self):
        api = {
            "a": {
                "status": "fail",
                "detail": '<script>alert("xss")</script>',
                "env_var": "FOO",
            },
        }
        html = render_api_health_banner_html(api)
        self.assertNotIn("<script>", html)
        self.assertNotIn('alert("xss")', html)
        # The escaped form should be present.
        self.assertIn("&lt;script&gt;", html)

    def test_quote_in_detail_does_not_break_title_attribute(self):
        # title="…" must survive a quote in the detail.
        api = {"a": {"status": "fail", "detail": 'broken " quote'}}
        html = render_api_health_banner_html(api)
        # No raw double-quote inside the title attribute.
        self.assertIn("&quot;", html)
        # The <span> still closes correctly.
        self.assertIn("</span>", html)

    def test_provider_name_with_html_chars_is_escaped(self):
        # Unknown providers fall back to displaying the raw key, so an
        # operator who adds {"<b>spam</b>": ...} to a JSON shouldn't break
        # the page.
        api = {"<b>spam</b>": {"status": "ok"}}
        html = render_api_health_banner_html(api)
        self.assertNotIn("<b>spam</b>", html)
        self.assertIn("&lt;b&gt;spam&lt;/b&gt;", html)


# ── _coerce_entry / non-dict entries ─────────────────────────────────

class TestCoerceEntry(unittest.TestCase):

    def test_dict_passes_through(self):
        d = {"status": "ok", "detail": "fine"}
        self.assertIs(_coerce_entry(d), d)

    def test_string_becomes_status_dict(self):
        self.assertEqual(_coerce_entry("ok"),   {"status": "ok"})
        self.assertEqual(_coerce_entry("fail"), {"status": "fail"})

    def test_other_types_become_empty_dict(self):
        self.assertEqual(_coerce_entry(None),   {})
        self.assertEqual(_coerce_entry(42),     {})
        self.assertEqual(_coerce_entry([]),     {})

    def test_renderer_handles_string_entries(self):
        # The bug: {"anthropic": "ok"} previously crashed with AttributeError.
        api = {"anthropic": "ok", "google_cse": "fail"}
        # Should not raise.
        html = render_api_health_banner_html(api)
        self.assertIn("api-ok",   html)
        self.assertIn("api-fail", html)

    def test_renderer_handles_mixed_malformed_entries(self):
        api = {
            "anthropic":  {"status": "ok", "detail": "fine"},  # canonical
            "apollo":     "ok",        # bare string
            "gemini":     None,        # → empty → status "missing"
            "google_cse": ["broken"],  # → empty → status "missing"
            "xai_grok":   {"status": 42},  # non-string status → "missing"
        }
        # No crash; banner renders.
        html = render_api_health_banner_html(api)
        md = render_api_health_banner_md(api)
        # All five providers should produce a pill / bullet.
        self.assertEqual(html.count("api-pill"), 5)
        self.assertEqual(md.count("\n  -"),       4)  # 5 bullets, 4 newlines between

    def test_md_renderer_handles_non_dict_api_health(self):
        # render_api_health_banner_md should also be defensive at the top.
        self.assertEqual(render_api_health_banner_md(None),    "")
        self.assertEqual(render_api_health_banner_md([]),      "")
        self.assertEqual(render_api_health_banner_md("oops"),  "")


if __name__ == "__main__":
    unittest.main()
