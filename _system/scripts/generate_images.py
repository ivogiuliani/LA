#!/usr/bin/env python3
"""
My Villa — Journal Image Generator
Generates header images for Journal articles using DALL-E 3 or Flux

Usage:
  python3 generate_images.py --articles blog/insurance-palisades-rebuild.html
  python3 generate_images.py --from-json radar_article.json --output blog/img/generated/
  python3 generate_images.py --dry-run  # just print prompts
"""

import json
import os
import re
import argparse
from datetime import datetime
from pathlib import Path

import yaml

try:
    import requests
    REQUESTS_OK = True
except ImportError:
    REQUESTS_OK = False

# ── Paths ────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
CONFIG_DIR = SYSTEM_DIR / "config"
HISTORY_DIR = SYSTEM_DIR / "history"
BLOG_DIR = SYSTEM_DIR.parent / "blog"
GENERATED_DIR = BLOG_DIR / "img" / "generated"
STOCK_DIR = BLOG_DIR / "img" / "stock"


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


def load_json(path):
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════════
# PROMPT BUILDING
# ══════════════════════════════════════════════════════════════════════

def build_image_prompt(article_prompt, section, style_config):
    """Build the full image generation prompt with style overrides."""
    base = style_config.get("style", {})
    base_suffix = base.get("base_prompt_suffix", "")
    overrides = base.get("section_overrides", {}).get(section, {})

    mood = overrides.get("mood", "")
    palette = overrides.get("palette", "")

    parts = [article_prompt]
    if mood:
        parts.append(f"Mood: {mood}.")
    if palette:
        parts.append(f"Color palette: {palette}.")
    parts.append(base_suffix.strip())

    return " ".join(parts)


# ══════════════════════════════════════════════════════════════════════
# IMAGE GENERATION
# ══════════════════════════════════════════════════════════════════════

def generate_dalle(prompt, api_key, size="1792x1024", quality="hd"):
    """Generate image using DALL-E 3 via OpenAI API."""
    if not REQUESTS_OK or not api_key:
        return None

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            "https://api.openai.com/v1/images/generations",
            headers=headers,
            json={
                "model": "dall-e-3",
                "prompt": prompt,
                "n": 1,
                "size": size,
                "quality": quality,
                "response_format": "url",
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        image_url = data["data"][0]["url"]
        revised_prompt = data["data"][0].get("revised_prompt", "")
        return {"url": image_url, "revised_prompt": revised_prompt}
    except Exception as e:
        print(f"  [DALL-E] Error: {e}")
        return None


def download_image(url, output_path):
    """Download image from URL to local file."""
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(resp.content)
        print(f"  Saved: {output_path} ({len(resp.content) / 1024:.0f} KB)")
        return True
    except Exception as e:
        print(f"  [Download] Error: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════
# STOCK FALLBACK
# ══════════════════════════════════════════════════════════════════════

def get_stock_image(section, used_assets):
    """Pick an unused stock image for the section."""
    section_dir = STOCK_DIR / section
    if not section_dir.exists():
        return None

    used = set(used_assets.get("used_images", {}).keys())
    candidates = [
        f for f in section_dir.iterdir()
        if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
        and str(f) not in used
    ]

    if not candidates:
        # All used — pick the oldest used one
        all_images = list(section_dir.iterdir())
        if all_images:
            return all_images[0]
        return None

    return candidates[0]


# ══════════════════════════════════════════════════════════════════════
# ARTICLE METADATA EXTRACTION
# ══════════════════════════════════════════════════════════════════════

def extract_from_html(filepath):
    """Extract image_prompt and section from article HTML (if embedded in comments)."""
    content = filepath.read_text()
    # Look for embedded JSON metadata in a comment
    m = re.search(r'<!--\s*ARTICLE_META\s*({.*?})\s*-->', content, re.DOTALL)
    if m:
        try:
            meta = json.loads(m.group(1))
            return meta.get("image_prompt", ""), meta.get("section", "materials")
        except json.JSONDecodeError:
            pass

    # Fallback: use title + section tag
    title = ""
    m = re.search(r'<h1[^>]*>(.+?)</h1>', content, re.DOTALL)
    if m:
        title = re.sub(r'<[^>]+>', '', m.group(1)).strip()

    section = "materials"
    m = re.search(r'<div class="article-hero-tag">(.+?)</div>', content)
    if m:
        tag = m.group(1).lower()
        for key in ("insurance", "materials", "concrete", "permits", "market", "climate"):
            if key in tag:
                section = key
                break

    prompt = f"Editorial header image for article: {title}" if title else ""
    return prompt, section


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="My Villa — Journal Image Generator")
    parser.add_argument("--articles", nargs="*",
                        help="HTML article files to generate images for")
    parser.add_argument("--from-json", nargs="*",
                        help="JSON article files with image_prompt field")
    parser.add_argument("--output", "-o", default=None,
                        help="Output directory (default: blog/img/generated/)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print prompts without generating")
    args = parser.parse_args()

    style_config = load_yaml(CONFIG_DIR / "image-style.yml")
    api_config = style_config.get("api", {})
    provider = api_config.get("provider", "openai")

    output_dir = Path(args.output) if args.output else GENERATED_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load used assets tracker
    used_assets_path = HISTORY_DIR / "used_assets.json"
    used_assets = load_json(used_assets_path) if used_assets_path.exists() else {"used_images": {}}

    items = []

    # From JSON files
    if args.from_json:
        for jf in args.from_json:
            data = load_json(Path(jf))
            if isinstance(data, list):
                items.extend(data)
            elif isinstance(data, dict):
                items.append(data)

    # From HTML articles
    if args.articles:
        for af in args.articles:
            prompt, section = extract_from_html(Path(af))
            if prompt:
                slug = Path(af).stem
                items.append({
                    "slug": slug,
                    "image_prompt": prompt,
                    "section": section,
                })

    if not items:
        print("No articles to process. Use --articles or --from-json.")
        return

    print(f"\nMy Villa — Image Generator")
    print(f"{'='*50}")
    print(f"Provider: {provider} | Items: {len(items)}")

    api_key = os.environ.get("OPENAI_API_KEY", "")

    for item in items:
        slug = item.get("slug", "untitled")
        raw_prompt = item.get("image_prompt", "")
        section = item.get("section", "materials")

        if not raw_prompt:
            print(f"\n[{slug}] No image_prompt — skipping")
            continue

        full_prompt = build_image_prompt(raw_prompt, section, style_config)
        output_path = output_dir / f"{slug}.png"

        print(f"\n[{slug}] Section: {section}")
        print(f"  Prompt: {full_prompt[:120]}...")

        if args.dry_run:
            print(f"  [Dry run] Would save to: {output_path}")
            continue

        # Try AI generation
        success = False
        if api_key and not api_key.startswith("PLACEHOLDER"):
            print(f"  Generating with {provider}...")
            result = generate_dalle(
                full_prompt, api_key,
                size=style_config.get("style", {}).get("resolution", "1792x1024"),
                quality=api_config.get("quality", "hd"),
            )
            if result and result.get("url"):
                success = download_image(result["url"], output_path)
                if success and result.get("revised_prompt"):
                    print(f"  Revised prompt: {result['revised_prompt'][:80]}...")
        else:
            print(f"  No API key — trying stock fallback")

        # Fallback to stock
        if not success and api_config.get("fallback_to_stock", True):
            stock = get_stock_image(section, used_assets)
            if stock:
                import shutil
                shutil.copy2(stock, output_path)
                print(f"  Stock fallback: {stock.name} → {output_path.name}")
                success = True
            else:
                print(f"  No stock images available for section '{section}'")

        # Track usage
        if success:
            used_assets.setdefault("used_images", {})[str(output_path)] = {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "section": section,
                "slug": slug,
            }

    # Save usage tracker
    if not args.dry_run:
        save_json(used_assets_path, used_assets)
        print(f"\nAsset tracker updated: {used_assets_path}")


if __name__ == "__main__":
    main()
