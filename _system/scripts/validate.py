#!/usr/bin/env python3
"""
My Villa — Brand Voice & Dedup Validator
Validates generated content against brand rules before publishing

Usage:
  python3 validate.py --posts _system/social/posts/reactive/
  python3 validate.py --articles blog/ --config _system/config/
  python3 validate.py --posts posts/ --articles blog/ --fix  # auto-fix issues
"""

import json
import os
import re
import sys
import argparse
from pathlib import Path

import yaml

try:
    import anthropic
    ANTHROPIC_OK = True
except ImportError:
    ANTHROPIC_OK = False

# ── Paths ────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
CONFIG_DIR = SYSTEM_DIR / "config"


def load_dotenv():
    env_file = SYSTEM_DIR.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            k, v = key.strip(), value.strip()
            if v and (k not in os.environ or not os.environ[k]):
                os.environ[k] = v

load_dotenv()


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


# ══════════════════════════════════════════════════════════════════════
# RULE-BASED VALIDATION (fast, no API)
# ══════════════════════════════════════════════════════════════════════

def load_brand_rules():
    """Load forbidden terms and voice rules from brand-voice.yml."""
    config = load_yaml(CONFIG_DIR / "brand-voice.yml")
    return {
        "forbidden": [t.lower() for t in config.get("forbidden_terms", [])],
        "voice_rules": config.get("voice_rules", []),
        "preferred": config.get("preferred_terms", {}),
    }


def check_forbidden_terms(text, forbidden_terms):
    """Check if text contains any forbidden terms."""
    violations = []
    text_lower = text.lower()
    for term in forbidden_terms:
        if term in text_lower:
            # Find the context
            idx = text_lower.index(term)
            start = max(0, idx - 30)
            end = min(len(text), idx + len(term) + 30)
            context = text[start:end].replace("\n", " ")
            violations.append({
                "type": "forbidden_term",
                "term": term,
                "context": f"...{context}...",
                "severity": "error",
            })
    return violations


def check_tweet_length(text):
    """Check tweet is within 280 chars."""
    if len(text) > 280:
        return [{
            "type": "tweet_too_long",
            "length": len(text),
            "over_by": len(text) - 280,
            "severity": "error",
        }]
    if len(text) > 270:
        return [{
            "type": "tweet_near_limit",
            "length": len(text),
            "severity": "warning",
        }]
    return []


def check_hashtags_x(text):
    """X/Twitter posts should NOT have hashtags."""
    hashtags = re.findall(r'#\w+', text)
    if hashtags:
        return [{
            "type": "hashtags_on_x",
            "hashtags": hashtags,
            "severity": "warning",
            "fix": "Remove hashtags from X posts (clean editorial tone)",
        }]
    return []


def check_false_claims(text):
    """Check for claims that My Villa has built homes."""
    text_lower = text.lower()
    claim_patterns = [
        r"we(?:'ve| have) built",
        r"our (?:first|latest|recent) (?:home|villa|project)",
        r"we completed",
        r"our portfolio (?:includes|features)",
        r"delivered \d+ (?:homes|villas)",
    ]
    violations = []
    for pattern in claim_patterns:
        m = re.search(pattern, text_lower)
        if m:
            violations.append({
                "type": "false_claim",
                "match": m.group(),
                "severity": "error",
                "fix": "My Villa has NOT built any homes yet",
            })
    return violations


def check_ivo_mention(text):
    """Thought leader should be Paolo, not Ivo."""
    if "ivo" in text.lower() and "giuliani" in text.lower():
        return [{
            "type": "wrong_thought_leader",
            "severity": "error",
            "fix": "Use Paolo Mezzalama, not Ivo Giuliani, as public voice",
        }]
    return []


def check_timeline_mention(text):
    """No specific delivery timelines."""
    patterns = [r'\d+\s*months?', r'\d+\s*weeks?', r'18.month', r'20.month']
    text_lower = text.lower()
    violations = []
    for p in patterns:
        m = re.search(p, text_lower)
        if m:
            # Check if it's about delivery
            context_start = max(0, m.start() - 50)
            context = text_lower[context_start:m.end() + 20]
            if any(w in context for w in ["deliver", "build", "construct", "complet", "ready"]):
                violations.append({
                    "type": "timeline_mention",
                    "match": m.group(),
                    "severity": "warning",
                    "fix": "Do not mention specific delivery timelines",
                })
    return violations


def validate_text(text, content_type="general"):
    """Run all rule-based checks on text."""
    rules = load_brand_rules()
    issues = []

    issues.extend(check_forbidden_terms(text, rules["forbidden"]))
    issues.extend(check_false_claims(text))
    issues.extend(check_ivo_mention(text))
    issues.extend(check_timeline_mention(text))

    if content_type == "x_post":
        issues.extend(check_tweet_length(text))
        issues.extend(check_hashtags_x(text))

    return issues


# ══════════════════════════════════════════════════════════════════════
# AI VALIDATION (Claude Sonnet — deeper check)
# ══════════════════════════════════════════════════════════════════════

VALIDATION_PROMPT = """\
You are a brand voice validator for My Villa (luxury reinforced concrete villas, LA).

Check this content against brand rules:
1. No forbidden terms: bunker, fortress, dream home, anti-fire, fear language
2. No false claims (My Villa has NOT built any homes yet)
3. Paolo Mezzalama is the public voice (not Ivo Giuliani)
4. No specific delivery timelines
5. Value before product (data/insight first, then My Villa)
6. No financial data (fees, margins, valuations)
7. Tone: editorial, informed, never salesy

Content type: {content_type}
Content:
---
{content}
---

Return JSON:
{{
  "pass": true/false,
  "issues": [
    {{"type": "string", "description": "string", "severity": "error|warning", "suggestion": "string"}}
  ],
  "tone_score": 1-10,
  "tone_notes": "brief assessment"
}}

Return ONLY valid JSON."""


def ai_validate(text, content_type="general", model="claude-sonnet-4-6"):
    """Use Claude Sonnet for deeper brand voice validation."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not ANTHROPIC_OK or not api_key or api_key.startswith("sk-ant-PLACEHOLDER"):
        return None

    client = anthropic.Anthropic(api_key=api_key)
    prompt = VALIDATION_PROMPT.format(content_type=content_type, content=text[:3000])

    try:
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        result_text = response.content[0].text.strip()
        if result_text.startswith("```"):
            result_text = re.sub(r'^```json?\n?', '', result_text)
            result_text = re.sub(r'\n?```$', '', result_text)
        return json.loads(result_text)
    except Exception as e:
        print(f"  [AI Validate] Error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════
# FILE PROCESSING
# ══════════════════════════════════════════════════════════════════════

def validate_social_post(filepath):
    """Validate a social post markdown file."""
    content = filepath.read_text()

    # Parse frontmatter
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {"file": filepath.name, "issues": [{"type": "invalid_format", "severity": "error"}]}

    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        meta = {}

    body = parts[2].strip()
    channel = meta.get("channel", "unknown")
    content_type = "x_post" if channel == "x" else "ig_caption"

    issues = validate_text(body, content_type)

    return {
        "file": filepath.name,
        "channel": channel,
        "char_count": len(body),
        "issues": issues,
        "pass": not any(i["severity"] == "error" for i in issues),
    }


def validate_article(filepath):
    """Validate a Journal article HTML file."""
    content = filepath.read_text()

    # Extract text content (strip HTML tags)
    text = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL)
    text = re.sub(r'<style[^>]*>.*?</style>', '', content, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()

    issues = validate_text(text, "article")

    return {
        "file": filepath.name,
        "issues": issues,
        "pass": not any(i["severity"] == "error" for i in issues),
    }


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="My Villa — Brand Voice & Dedup Validator")
    parser.add_argument("--posts", nargs="*",
                        help="Directories or files with social posts to validate")
    parser.add_argument("--articles", nargs="*",
                        help="Directories or files with Journal articles to validate")
    parser.add_argument("--config", default=None,
                        help="Config directory (default: _system/config/)")
    parser.add_argument("--model", default="claude-sonnet-4-6",
                        help="Model for AI validation")
    parser.add_argument("--ai", action="store_true",
                        help="Enable AI validation (uses API credits)")
    parser.add_argument("--fix", action="store_true",
                        help="Auto-fix simple issues (hashtag removal, etc.)")
    args = parser.parse_args()

    print(f"\nMy Villa — Brand Voice Validator")
    print(f"{'='*50}")

    results = []
    errors = 0
    warnings = 0

    # Validate social posts
    if args.posts:
        for post_path in args.posts:
            p = Path(post_path)
            files = list(p.glob("*.md")) if p.is_dir() else [p]
            for f in files:
                result = validate_social_post(f)
                results.append(result)
                for issue in result["issues"]:
                    if issue["severity"] == "error":
                        errors += 1
                    else:
                        warnings += 1

    # Validate articles
    if args.articles:
        for article_path in args.articles:
            p = Path(article_path)
            files = [f for f in (list(p.glob("*.html")) if p.is_dir() else [p])
                     if f.name != "index.html"]
            for f in files:
                result = validate_article(f)
                results.append(result)
                for issue in result["issues"]:
                    if issue["severity"] == "error":
                        errors += 1
                    else:
                        warnings += 1

    # AI validation (if enabled, only on files with issues or randomly)
    if args.ai:
        for result in results:
            if not result.get("pass", True):
                filepath = Path(result["file"])
                if filepath.exists():
                    content = filepath.read_text()
                    ai_result = ai_validate(content, model=args.model)
                    if ai_result:
                        result["ai_validation"] = ai_result

    # Print results
    print(f"\nResults: {len(results)} files validated")
    print(f"  Errors: {errors}")
    print(f"  Warnings: {warnings}")

    for result in results:
        status = "PASS" if result.get("pass", True) else "FAIL"
        icon = "+" if status == "PASS" else "x"
        print(f"\n  [{icon}] {result['file']}")
        if result.get("channel"):
            print(f"      Channel: {result['channel']} | Chars: {result.get('char_count', '?')}")
        for issue in result.get("issues", []):
            sev = "ERROR" if issue["severity"] == "error" else "WARN"
            desc = issue.get("term", issue.get("match", issue.get("type", "")))
            fix = issue.get("fix", issue.get("context", ""))
            print(f"      [{sev}] {issue['type']}: {desc}")
            if fix:
                print(f"             → {fix}")

    # Exit code
    if errors > 0:
        print(f"\n{'='*50}")
        print(f"VALIDATION FAILED — {errors} error(s) found")
        sys.exit(1)
    else:
        print(f"\n{'='*50}")
        print(f"VALIDATION PASSED")
        if warnings:
            print(f"  ({warnings} warning(s) — review recommended)")
        sys.exit(0)


if __name__ == "__main__":
    main()
