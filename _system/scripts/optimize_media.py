#!/usr/bin/env python3
"""
optimize_media.py — ricompressione conservativa delle immagini pesanti in img/.

Cosa fa
  - cerca in img/ (ricorsivo) i file .webp/.jpg/.jpeg sopra una soglia (default 400 KB)
    più img/courtyard-interior.webp sempre (era 1,47 MB);
  - ricodifica con PIL a qualità 80, lato massimo 1920 px (proporzioni intatte),
    scrivendo PRIMA in una cartella temporanea;
  - sovrascrive l'originale (stesso nome, stesso formato) SOLO se il risultato è
    più piccolo di almeno il 20%; altrimenti lascia tutto com'è;
  - img/social/** è ESCLUSA di default (asset usati dal pipeline social per gli
    upload IG/X): --include-social per includerla.
  - logga i risparmi file per file e in totale.

Uso
  python3 _system/scripts/optimize_media.py             # esegue
  python3 _system/scripts/optimize_media.py --dry-run   # solo report, nessuna scrittura
  python3 _system/scripts/optimize_media.py --min-kb 300 --quality 78 --max-px 1600
  python3 _system/scripts/optimize_media.py --include-social

Il video hero non viene toccato (niente ffmpeg): index.html lo carica via JS
solo su viewport ≥ 900px e con preload="none" (patch CONV:VIDEO di
inject_conversion_layer.py).

Exit code sempre 0: la pipeline non deve mai fermarsi per un'immagine.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
IMG_DIR = ROOT / "img"
ALWAYS = [IMG_DIR / "courtyard-interior.webp"]
EXTS = {".webp", ".jpg", ".jpeg"}


def human(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.2f} MB"


def candidates(min_bytes: int, include_social: bool) -> List[Path]:
    out = []
    seen = set()
    for p in sorted(IMG_DIR.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in EXTS:
            continue
        if not include_social and "social" in p.relative_to(IMG_DIR).parts:
            continue
        if p.stat().st_size >= min_bytes or p in ALWAYS:
            if p not in seen:
                out.append(p)
                seen.add(p)
    for p in ALWAYS:
        if p.exists() and p not in seen:
            out.append(p)
    return out


def recompress(src: Path, dst: Path, quality: int, max_px: int) -> Optional[Tuple[int, int, int, int]]:
    """Ritorna (w_in, h_in, w_out, h_out) o None se PIL non riesce."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        print("PIL non disponibile: niente da fare")
        return None
    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            w0, h0 = im.size
            if max(w0, h0) > max_px:
                im.thumbnail((max_px, max_px), Image.LANCZOS)
            w1, h1 = im.size
            ext = src.suffix.lower()
            if ext == ".webp":
                has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
                im = im.convert("RGBA" if has_alpha else "RGB")
                im.save(dst, "WEBP", quality=quality, method=6)
            else:
                im = im.convert("RGB")
                im.save(dst, "JPEG", quality=quality, optimize=True, progressive=True)
            return (w0, h0, w1, h1)
    except Exception as exc:
        print(f"  ! {src.relative_to(ROOT)}: errore PIL: {exc}")
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="solo report, nessuna scrittura")
    ap.add_argument("--min-kb", type=int, default=400, help="soglia in KB (default 400)")
    ap.add_argument("--quality", type=int, default=80, help="qualità (default 80)")
    ap.add_argument("--max-px", type=int, default=1920, help="lato massimo in px (default 1920)")
    ap.add_argument("--min-gain", type=float, default=0.20, help="risparmio minimo per sovrascrivere (default 0.20 = 20%%)")
    ap.add_argument("--include-social", action="store_true", help="includi img/social/** (escluso di default)")
    args = ap.parse_args(argv)

    if not IMG_DIR.exists():
        print(f"cartella img non trovata: {IMG_DIR}")
        return 0
    files = candidates(args.min_kb * 1024, args.include_social)
    if not files:
        print("nessun file sopra soglia")
        return 0
    tmpdir = Path(tempfile.mkdtemp(prefix="myvilla-media-"))
    total_before = total_after = 0
    rewritten = 0
    print(f"{'file':64s} {'prima':>9s} {'dopo':>9s} {'-%':>6s}  esito")
    for src in files:
        before = src.stat().st_size
        dst = tmpdir / src.name
        dims = recompress(src, dst, args.quality, args.max_px)
        if dims is None or not dst.exists():
            continue
        after = dst.stat().st_size
        gain = 1 - after / before if before else 0
        rel = str(src.relative_to(ROOT))
        if gain >= args.min_gain:
            verdict = "DRY (sovrascriverei)" if args.dry_run else "sovrascritto"
            if not args.dry_run:
                shutil.copyfile(dst, src)
                rewritten += 1
            total_before += before
            total_after += after
        else:
            verdict = f"lasciato (guadagno {gain*100:.0f}% < {args.min_gain*100:.0f}%)"
            total_before += before
            total_after += before
        w0, h0, w1, h1 = dims
        size_note = f" {w0}x{h0}→{w1}x{h1}" if (w0, h0) != (w1, h1) else ""
        print(f"{rel[:64]:64s} {human(before):>9s} {human(after):>9s} {gain*100:>5.0f}%  {verdict}{size_note}")
        try:
            dst.unlink()
        except OSError:
            pass
    shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"\nTotale: {human(total_before)} → {human(total_after)} "
          f"(risparmio {human(total_before - total_after)}, file riscritti: {rewritten}{' [dry-run]' if args.dry_run else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
