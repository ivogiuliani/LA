#!/usr/bin/env python3
"""
brand_grade.py — "My Villa" warm golden-hour color grade for evergreen
stock images.

Le immagini stock (Unsplash) usate per gli evergreen Instagram arrivano con
tonalità disparate (fredde, neutre, B&N). Questo modulo applica un unico
trattamento fotografico — bilanciamento caldo, split-tone dorato, ombre
sollevate, leggero bloom — così tutta la griglia "parla la stessa lingua"
calda e serena (richiesta utente: adattare lo stock al linguaggio visivo
del brand, tono golden-hour, colori che comunicano positività e serenità).

Puro Pillow, nessuna dipendenza extra (no numpy). Deterministico, gira in
locale, zero API/crediti. Idempotente: marca il JPEG con un commento
(``mvgrade1``) e salta i file già trattati, così non c'è rischio di
doppio-grading se una funzione viene richiamata due volte.

Uso come libreria:
    import brand_grade
    brand_grade.grade_file(Path("img/social/evergreen/foo-hero.jpg"))

Uso CLI (debug / prima-dopo):
    python3 brand_grade.py IN.jpg [OUT.jpg] [strength]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    from PIL import Image, ImageChops, ImageEnhance, ImageFilter
    _PIL_OK = True
except Exception:  # noqa: BLE001 — il grade non deve MAI bloccare la pipeline
    _PIL_OK = False

# Intensità di default = "intermedio" (tarato con l'utente): caldo dorato ma
# ancora naturale. Override opzionale via env per esperimenti, senza toccare
# il codice.
try:
    DEFAULT_STRENGTH = float(os.environ.get("MV_GRADE_STRENGTH", "1.0"))
except (TypeError, ValueError):
    DEFAULT_STRENGTH = 1.0

# Marker scritto nel commento JPEG → riconosce i file già trattati.
GRADE_MARKER = b"mvgrade1"

# Colore dello split-tone golden-hour.
AMBER = (255, 178, 96)


def _lut(lift: float, gain: float, gamma: float, s: float) -> list[int]:
    """LUT per-canale (256 valori) scalata per intensità s (s=0 → identità)."""
    lift_e = lift * s
    gain_e = 1 + (gain - 1) * s
    inv = 1.0 / (1 + (gamma - 1) * s)
    out = []
    for i in range(256):
        v = (i / 255.0) * gain_e + lift_e
        v = 0.0 if v < 0 else 1.0 if v > 1 else v
        if inv != 1.0:
            v = v ** inv
        out.append(int(round((0.0 if v < 0 else 1.0 if v > 1 else v) * 255)))
    return out


def grade_image(img, strength: float = DEFAULT_STRENGTH):
    """Ritorna una copia RGB di ``img`` con il grade My Villa applicato.
    ``strength`` 0 = nessun effetto, 1.0 = intermedio, >1 = più marcato."""
    img = img.convert("RGB")
    s = float(strength)
    if s <= 0:
        return img

    # 1) bilanciamento caldo + neri sollevati (foschia dorata), per canale
    haze = 0.006
    r, g, b = img.split()
    r = r.point(_lut(0.012 + haze, 1.075, 0.99, s))
    g = g.point(_lut(0.004 + haze, 1.015, 1.00, s))
    b = b.point(_lut(-0.012 + haze, 0.905, 1.05, s))
    img = Image.merge("RGB", (r, g, b))

    # 2) split-tone dorato via soft-light ambra (content-adaptive: scalda
    #    senza bruciare le foto già calde; vira le B&N in un caldo seppia)
    amber = Image.new("RGB", img.size, AMBER)
    img = Image.blend(img, ImageChops.soft_light(img, amber), 0.22 * s)

    # 3) contrasto morbido + saturazione contenuta
    img = ImageEnhance.Contrast(img).enhance(1 + 0.06 * s)
    img = ImageEnhance.Color(img).enhance(1 + 0.10 * s)

    # 4) bloom sottile — screen di una copia sfocata e schiarita (glow caldo)
    bloom = ImageEnhance.Brightness(img).enhance(1.28).filter(
        ImageFilter.GaussianBlur(6))
    img = Image.blend(img, ImageChops.screen(img, bloom), 0.07 * s)

    return img


def is_graded(path) -> bool:
    """True se il file porta già il marker del grade (idempotenza)."""
    if not _PIL_OK:
        return False
    try:
        with Image.open(path) as im:
            return im.info.get("comment") == GRADE_MARKER
    except Exception:  # noqa: BLE001
        return False


def grade_file(path, strength: float = DEFAULT_STRENGTH,
               quality: int = 90) -> bool:
    """Applica il grade a un file JPEG IN PLACE. Ritorna True se trattato,
    False se saltato (già graded / Pillow assente / errore). Non solleva
    mai: un fallimento del grade non deve fermare la generazione."""
    if not _PIL_OK:
        return False
    p = Path(path)
    if not p.exists():
        return False
    try:
        if is_graded(p):
            return False
        with Image.open(p) as im:
            out = grade_image(im, strength)
        out.save(p, "JPEG", quality=quality, optimize=True,
                 comment=GRADE_MARKER)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  [grade] skip {p.name}: {type(e).__name__}: {e}")
        return False


def _main(argv) -> int:
    if not _PIL_OK:
        print("Pillow non disponibile")
        return 1
    if len(argv) < 1:
        print("Usage: brand_grade.py IN.jpg [OUT.jpg] [strength]")
        return 1
    src = Path(argv[0])
    dst = Path(argv[1]) if len(argv) > 1 and not _isnum(argv[1]) else src
    s_args = [a for a in argv[1:] if _isnum(a)]
    strength = float(s_args[0]) if s_args else DEFAULT_STRENGTH
    with Image.open(src) as im:
        out = grade_image(im, strength)
    out.save(dst, "JPEG", quality=90, optimize=True, comment=GRADE_MARKER)
    print(f"graded {src.name} → {dst.name} (strength {strength})")
    return 0


def _isnum(x: str) -> bool:
    try:
        float(x)
        return True
    except (TypeError, ValueError):
        return False


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
