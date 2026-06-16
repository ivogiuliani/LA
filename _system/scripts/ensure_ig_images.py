#!/usr/bin/env python3
"""
ensure_ig_images.py — garantisce che OGNI post Instagram abbia
un'immagine già scelta dal sistema, così il pannello mostra sempre
l'anteprima (mai "⚠ Nessuna immagine").

Per ogni post IG (companion blog/*.ig.md + reattivi in _drafts/social e
posts/reactive) che NON ha un'immagine usabile — né `image:` esistente
in locale, né un hero d'articolo risolvibile via `journal_slug` — sceglie
un'immagine Unsplash orientata al linguaggio fotografico delle guidelines
(golden hour, minimal, simmetrico), la scarica in img/social/ e scrive il
campo `image:` nel frontmatter.

Idempotente (skip-if-image-ok) → costo steady-state ~0. Si lancia in
pipeline (daily_publish.sh) e a mano:
    python3 ensure_ig_images.py            # processa tutti
    python3 ensure_ig_images.py --dry-run  # mostra solo chi è senza img
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
ROOT_DIR = SYSTEM_DIR.parent
BLOG_IMG = ROOT_DIR / "blog" / "assets" / "img"
IMG_SOCIAL = ROOT_DIR / "img" / "social"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from social_guidelines import IMAGE_STYLE_HINT
except Exception:  # noqa: BLE001
    IMAGE_STYLE_HINT = "golden hour minimalist architecture symmetric soft shadows"

FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)


def _ig_post_paths():
    """Tutti i post IG candidati (companion + reattivi)."""
    out = []
    for f in sorted(BLOG_IMG.parent.parent.glob("*.ig.md")):  # blog/*.ig.md
        out.append(f)
    for d in (ROOT_DIR / "_drafts" / "social",
              SYSTEM_DIR / "social" / "posts" / "reactive"):
        if d.exists():
            out += [f for f in sorted(d.glob("*.md")) if "-ig-" in f.name]
    return out


def _parse(path: Path):
    raw = path.read_text(encoding="utf-8")
    m = FM_RE.match(raw)
    if not m:
        return None, "", raw
    fm = {}
    for ln in m.group(1).splitlines():
        kv = ln.split(":", 1)
        if len(kv) == 2:
            fm[kv[0].strip()] = kv[1].strip().strip('"').strip("'")
    return fm, m.group(1), m.group(2).strip()


def _is_ig(fm: dict) -> bool:
    return str(fm.get("channel", fm.get("platform", ""))).lower() in ("ig", "instagram")


def _has_usable_image(fm: dict) -> bool:
    img = str(fm.get("image") or "").strip()
    if img.startswith(("http://", "https://")):
        return True
    if img and (ROOT_DIR / img.lstrip("/")).exists():
        return True
    slug = str(fm.get("journal_slug") or "").strip()
    if slug:
        for ext in ("jpg", "jpeg", "png", "webp"):
            if (BLOG_IMG / f"{slug}-hero.{ext}").exists():
                return True
    return False


def _slug_for(fm: dict, path: Path) -> str:
    s = fm.get("journal_slug") or fm.get("slug") or path.name
    for suf in (".ig.md", ".md"):
        if s.endswith(suf):
            s = s[:-len(suf)]
    s = re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")
    return s or "social"


def ensure_one(path: Path, *, dry_run: bool = False) -> str:
    """→ 'ok' (già a posto) | 'picked' | 'skip-nonig' | 'fail'."""
    fm, fm_text, body = _parse(path)
    if fm is None or not _is_ig(fm):
        return "skip-nonig"
    if _has_usable_image(fm):
        return "ok"
    if dry_run:
        print(f"  [senza immagine] {path.name}")
        return "picked"
    slug = _slug_for(fm, path)
    query = fm.get("title") or slug.replace("-", " ") or "italian villa los angeles"
    q_full = f"{query} {IMAGE_STYLE_HINT}".strip()
    try:
        from image_picker import fetch_candidates, download_candidate
    except Exception as e:  # noqa: BLE001
        print(f"  [ensure-ig] image_picker non disponibile: {e}")
        return "fail"
    cands = fetch_candidates(q_full, count=4,
                             fallback_queries=[query, "los angeles concrete villa golden hour"])
    if not cands:
        print(f"  [ensure-ig] nessun candidato per {path.name}")
        return "fail"
    hero = download_candidate(cands[0], slug, IMG_SOCIAL)
    if not hero or not hero.get("local_path"):
        return "fail"
    rel = Path(hero["local_path"]).resolve().relative_to(ROOT_DIR).as_posix()
    lines, saw = [], False
    for ln in fm_text.splitlines():
        if ln.strip().startswith("image:"):
            lines.append(f"image: {rel}")
            saw = True
        else:
            lines.append(ln)
    if not saw:
        lines.append(f"image: {rel}")
    path.write_text("---\n" + "\n".join(lines) + "\n---\n\n" + body + "\n",
                    encoding="utf-8")
    print(f"  [ensure-ig] ✓ {path.name} → {rel}")
    return "picked"


def main(argv=None) -> int:
    dry = "--dry-run" in (argv or sys.argv[1:])
    paths = _ig_post_paths()
    n_ok = n_pick = n_fail = 0
    for p in paths:
        r = ensure_one(p, dry_run=dry)
        if r == "ok":
            n_ok += 1
        elif r == "picked":
            n_pick += 1
        elif r == "fail":
            n_fail += 1
    print(f"  [ensure-ig] IG: {n_ok} già con immagine, "
          f"{n_pick} {'da sistemare' if dry else 'immagini scelte'}, {n_fail} falliti")
    return 0


if __name__ == "__main__":
    sys.exit(main())
