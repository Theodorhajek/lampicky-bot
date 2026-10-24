"""Návrhy pro zákazníky: černobílý návrh motivu na lampičku (logo + pod ním jméno/nápis), 15 × 15 cm.

Bot ho vytvoří, když zákazník chce návrh; v dashboardu ho schválíš a teprve pak se pošle.

  .venv/bin/python navrh.py "BMW" "Raďa"        – vytvoří návrh do navrhy/zakaznici/
"""

import io
import math
import re
import sys
import time
import unicodedata
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
OUT = HERE / "navrhy" / "zakaznici"
LOGOS = HERE / "vzory" / "loga"
NAME_FONT = ("/System/Library/Fonts/Supplemental/Futura.ttc", 2)
LETTER_FONT = ("/System/Library/Fonts/Helvetica.ttc", 1)
SIZE, SS = 1500, 3  # výstup 1500 × 1500 px (= sklo 15 × 15 cm), kreslí se 3× větší kvůli hladkým hranám


def slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:30] or "navrh"


def _bmw_roundel(img, cx, cy, R):
    """Kulaté logo BMW přesně jako na hotové lampičce (černá = gravír = svítí):
    tenký vnější kruh, velká tučná písmena B M W v pásu, tenký vnitřní kruh, svítí čtvrtky vpravo nahoře a vlevo dole,
    tenký kříž přes celý vnitřní kruh."""
    d = ImageDraw.Draw(img)
    line = max(int(R * 0.022), 3)
    r_in = int(R * 0.58)
    d.ellipse([cx - R, cy - R, cx + R, cy + R], outline=0, width=line)
    d.ellipse([cx - r_in, cy - r_in, cx + r_in, cy + r_in], outline=0, width=line)
    q = r_in - line // 2
    # čtvrtky přesně: kruh ∩ obdélník (vpravo nahoře a vlevo dole), pak tenký kříž přes celý vnitřní kruh
    disc = Image.new("L", img.size, 0)
    ImageDraw.Draw(disc).ellipse([cx - q, cy - q, cx + q, cy + q], fill=255)
    quads = Image.new("L", img.size, 0)
    qd = ImageDraw.Draw(quads)
    qd.rectangle([cx, cy - q, cx + q, cy], fill=255)
    qd.rectangle([cx - q, cy, cx, cy + q], fill=255)
    qd.rectangle([cx - line // 2, cy - q, cx + line // 2, cy + q], fill=255)
    qd.rectangle([cx - q, cy - line // 2, cx + q, cy + line // 2], fill=255)
    img.paste(0, (0, 0), ImageChops.multiply(disc, quads))
    font = ImageFont.truetype(LETTER_FONT[0], int(R * 0.30), index=LETTER_FONT[1])
    rm = (R + r_in) / 2
    for ch, ang in zip("BMW", (-127, -90, -53)):
        a = math.radians(ang)
        tile = Image.new("L", (int(R * 0.6), int(R * 0.6)), 0)
        ImageDraw.Draw(tile).text((tile.width / 2, tile.height / 2), ch, font=font, fill=255, anchor="mm")
        tile = tile.rotate(-(ang + 90), resample=Image.BICUBIC)
        img.paste(0, (int(cx + rm * math.cos(a) - tile.width / 2), int(cy + rm * math.sin(a) - tile.height / 2)), tile)


def _logo_file(motif):
    m = slug(motif)
    for f in sorted(LOGOS.glob("*.png")):
        brand = f.stem.split("-")[0]
        if brand in m and "bile" not in f.stem:
            return f
    return None


def _paste_logo(img, f, box):
    """Logo ze souboru převede na čistě černé čáry na bílé a vloží do boxu (x1, y1, x2, y2)."""
    lg = Image.open(f).convert("RGBA")
    bg = Image.new("RGBA", lg.size, (255, 255, 255, 255))
    gray = Image.alpha_composite(bg, lg).convert("L").point(lambda v: 0 if v < 140 else 255)
    bb = Image.eval(gray, lambda v: 255 - v).getbbox()
    if bb:
        gray = gray.crop(bb)
    x1, y1, x2, y2 = box
    scale = min((x2 - x1) / gray.width, (y2 - y1) / gray.height)
    gray = gray.resize((int(gray.width * scale), int(gray.height * scale)), Image.LANCZOS)
    img.paste(gray, (x1 + (x2 - x1 - gray.width) // 2, y1 + (y2 - y1 - gray.height) // 2))


def _gemini_motif(motif):
    """Motif bez loga v knihovně – černobílá linková kresba přes Gemini (bez metadat, jen pixely)."""
    import posty  # noqa: PLC0415
    from google import genai  # noqa: PLC0415
    from google.genai import types  # noqa: PLC0415

    client = genai.Client(api_key=posty.api_key())
    prompt = (f"Black and white line-art design for laser engraving on a square acrylic plate: {motif}. "
              "Pure black thin outlines on pure white background, no shading, no gray, no text, centered, simple and clean, "
              "like a vector logo / stencil. Square image.")
    for model in posty.MODELS:
        try:
            resp = client.models.generate_content(model=model, contents=[prompt], config=types.GenerateContentConfig(
                response_modalities=["IMAGE"], image_config=types.ImageConfig(aspect_ratio="1:1")))
            for part in resp.candidates[0].content.parts:
                if part.inline_data and part.inline_data.data:
                    return Image.open(io.BytesIO(part.inline_data.data)).convert("L").point(lambda v: 0 if v < 140 else 255)
        except Exception as e:  # noqa: BLE001
            print(f"  {model}: {e}", flush=True)
    raise RuntimeError("motiv se nepodařilo vygenerovat")


def make(motif, text="", person="zakaznik"):
    """Vytvoří návrh: motiv nahoře, pod ním text (jméno). Vrací cestu k PNG."""
    big = SIZE * SS
    img = Image.new("L", (big, big), 255)
    has_text = bool(text.strip())
    area = (int(big * 0.1), int(big * 0.08), int(big * 0.9), int(big * (0.72 if has_text else 0.92)))
    if "bmw" in slug(motif) and "motorrad" not in slug(motif):
        R = min(area[2] - area[0], area[3] - area[1]) // 2
        _bmw_roundel(img, big // 2, (area[1] + area[3]) // 2, R)
    elif _logo_file(motif):
        _paste_logo(img, _logo_file(motif), area)
    else:
        art = _gemini_motif(motif)
        tmp = OUT / "_tmp.png"
        OUT.mkdir(parents=True, exist_ok=True)
        art.save(tmp)
        _paste_logo(img, tmp, area)
        tmp.unlink(missing_ok=True)
    if has_text:
        font = ImageFont.truetype(NAME_FONT[0], int(big * 0.12), index=NAME_FONT[1])
        d = ImageDraw.Draw(img)
        while d.textlength(text, font=font) > big * 0.85 and font.size > 40:
            font = ImageFont.truetype(NAME_FONT[0], int(font.size * 0.9), index=NAME_FONT[1])
        d.text((big // 2, int(big * 0.84)), text, font=font, fill=0, anchor="mm")
    OUT.mkdir(parents=True, exist_ok=True)
    f = OUT / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug(person)}-{slug(motif)}.png"
    img.resize((SIZE, SIZE), Image.LANCZOS).convert("RGB").save(f)  # jen pixely, žádná metadata
    return f


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    print(make(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""))
