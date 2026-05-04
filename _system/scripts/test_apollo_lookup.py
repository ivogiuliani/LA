#!/usr/bin/env python3
"""
Unit tests for apollo_lookup.

Coverage focuses on the offline-testable parts of the module:
  1. _looks_like_publication_name — guard that prevents wasted API calls
     when the byline is a publication, not a person.
  2. _normalize_for_compare — string normalization used by the guard.
  3. _ORG_ALIASES — sanity checks on the aliases table shape.
  4. is_configured — env-var detection.

Tests that touch the network (the lookup() function and _apollo_match())
are NOT included here; those would require live Apollo credentials and
would burn credits on every CI run. Smoke-test those manually with the
CLI:

    python3 _system/scripts/apollo_lookup.py --name "..." --pub "..."

Run with:
    python3 _system/scripts/test_apollo_lookup.py
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

# Make the sibling module importable when invoked directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from apollo_lookup import (  # noqa: E402  (sys.path tweak above)
    _looks_like_publication_name,
    _normalize_for_compare,
    _ORG_ALIASES,
    is_configured,
)


# ── _looks_like_publication_name ─────────────────────────────────────

class TestPublicationNameGuard(unittest.TestCase):
    """The guard catches bylines that aren't real people.

    Goal: return True for inputs that Apollo will guarantee-miss, so we
    skip the API call. Real-world examples come from observed radar bugs.
    """

    # ── Real journalists (must NOT be flagged) ───────────────────────

    def test_real_reporter_at_trade_pub(self):
        # 2026-04-29 case: real Digital Insurance reporter
        self.assertFalse(_looks_like_publication_name(
            "Michael Shashoua",
            organization="dig-in.com",
            domain="dig-in.com",
        ))

    def test_real_lawyer_at_firm_blog(self):
        # 2026-04-29 case: Daniel Veroff at Merlin Law Group blog
        self.assertFalse(_looks_like_publication_name(
            "Daniel Veroff",
            organization="propertyinsurancecoveragelaw.com",
            domain="propertyinsurancecoveragelaw.com",
        ))

    def test_two_word_name_with_no_context(self):
        self.assertFalse(_looks_like_publication_name("Mario Rossi"))

    # ── Publication names (MUST be flagged) ──────────────────────────

    def test_byline_equals_publication(self):
        # 2026-04-29 case: theoutragedconsumer.com Substack
        self.assertTrue(_looks_like_publication_name(
            "The Outraged Consumer",
            organization="theoutragedconsumer.com",
            domain="theoutragedconsumer.com",
        ))

    def test_byline_equals_publication_via_org(self):
        self.assertTrue(_looks_like_publication_name(
            "LA Times", organization="LA Times", domain="latimes.com",
        ))

    def test_byline_equals_publication_with_the_prefix(self):
        self.assertTrue(_looks_like_publication_name(
            "The LA Times", organization="LA Times", domain="latimes.com",
        ))

    # ── Generic newsroom patterns (MUST be flagged) ──────────────────

    def test_editorial_team(self):
        self.assertTrue(_looks_like_publication_name("Editorial Team"))
        self.assertTrue(_looks_like_publication_name("Editorial Staff"))
        self.assertTrue(_looks_like_publication_name("Editorial Board"))

    def test_newsroom(self):
        self.assertTrue(_looks_like_publication_name("Newsroom"))
        self.assertTrue(_looks_like_publication_name("News Desk"))

    def test_staff_writer(self):
        self.assertTrue(_looks_like_publication_name("Staff Writer"))
        self.assertTrue(_looks_like_publication_name("Staff Reporter"))
        self.assertTrue(_looks_like_publication_name("Staff Writers"))

    def test_press_office(self):
        self.assertTrue(_looks_like_publication_name("Press Office"))
        self.assertTrue(_looks_like_publication_name("Press Team"))

    # ── Empty / defensive ────────────────────────────────────────────

    def test_empty_name(self):
        self.assertFalse(_looks_like_publication_name(""))
        self.assertFalse(_looks_like_publication_name(None))

    def test_whitespace_only(self):
        self.assertFalse(_looks_like_publication_name("   "))


# ── _normalize_for_compare ───────────────────────────────────────────

class TestNormalize(unittest.TestCase):
    """Normalization makes 'The X' compare equal to 'thex.com'."""

    def test_strips_tld(self):
        self.assertEqual(_normalize_for_compare("example.com"), "example")
        self.assertEqual(_normalize_for_compare("example.org"), "example")
        self.assertEqual(_normalize_for_compare("example.io"),  "example")

    def test_strips_punctuation_and_spaces(self):
        self.assertEqual(_normalize_for_compare("La Times"),       "latimes")
        self.assertEqual(_normalize_for_compare("L.A. Times"),     "latimes")
        self.assertEqual(_normalize_for_compare("L-A-Times"),      "latimes")

    def test_strips_leading_the_with_space(self):
        self.assertEqual(_normalize_for_compare("The LA Times"),   "latimes")

    def test_strips_leading_the_glued(self):
        # The whole point of the post-alphanum strip: "the" might have
        # no separator in a domain.
        self.assertEqual(_normalize_for_compare("theoutragedconsumer.com"),
                         "outragedconsumer")
        self.assertEqual(_normalize_for_compare("The Outraged Consumer"),
                         "outragedconsumer")

    def test_byline_and_domain_collide(self):
        # The two should normalize to the same string for the guard to fire.
        self.assertEqual(
            _normalize_for_compare("The Outraged Consumer"),
            _normalize_for_compare("theoutragedconsumer.com"),
        )

    def test_short_strings_keep_the(self):
        # Stripping "the" from a 3-char string would leave "" — keep it.
        self.assertEqual(_normalize_for_compare("the"),  "the")

    def test_empty_input(self):
        self.assertEqual(_normalize_for_compare(""),    "")
        self.assertEqual(_normalize_for_compare(None),  "")


# ── _ORG_ALIASES table ───────────────────────────────────────────────

class TestOrgAliases(unittest.TestCase):
    """Sanity checks on the aliases table — types, no empty values."""

    def test_keys_are_lowercase_domains(self):
        for key in _ORG_ALIASES:
            self.assertEqual(key, key.lower(),
                             f"Alias key {key!r} must be lowercase")
            self.assertIn(".", key,
                          f"Alias key {key!r} should look like a domain")

    def test_values_are_non_empty_string_lists(self):
        for key, val in _ORG_ALIASES.items():
            self.assertIsInstance(val, list,
                                  f"_ORG_ALIASES[{key!r}] must be a list")
            self.assertGreater(len(val), 0,
                               f"_ORG_ALIASES[{key!r}] must have ≥1 alias")
            for alt in val:
                self.assertIsInstance(alt, str,
                                      f"alias {alt!r} must be a string")
                self.assertTrue(alt.strip(),
                                f"alias {alt!r} must not be empty/whitespace")

    def test_known_problem_domains_have_aliases(self):
        # Anchored to the 2026-04-29 incident: these two domains
        # produced misses that motivated the alias mechanism. If the
        # aliases get removed, the regression tests should catch it.
        self.assertIn("dig-in.com",                       _ORG_ALIASES)
        self.assertIn("propertyinsurancecoveragelaw.com", _ORG_ALIASES)


# ── is_configured ────────────────────────────────────────────────────

class TestIsConfigured(unittest.TestCase):
    """Detects whether APOLLO_API_KEY is set in the environment."""

    def setUp(self):
        # Save and clear, restore in tearDown
        self._saved = os.environ.pop("APOLLO_API_KEY", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["APOLLO_API_KEY"] = self._saved
        else:
            os.environ.pop("APOLLO_API_KEY", None)

    def test_returns_false_when_unset(self):
        self.assertFalse(is_configured())

    def test_returns_false_when_empty_or_whitespace(self):
        os.environ["APOLLO_API_KEY"] = ""
        self.assertFalse(is_configured())
        os.environ["APOLLO_API_KEY"] = "   "
        self.assertFalse(is_configured())

    def test_returns_true_when_set(self):
        os.environ["APOLLO_API_KEY"] = "fake_key_for_test"
        self.assertTrue(is_configured())


if __name__ == "__main__":
    unittest.main()
