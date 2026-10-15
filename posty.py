"""Příspěvky do FB skupin: vygeneruje fotku lampičky s motivem (Gemini), text příspěvku (Claude
přes předplatné) a fotku nahraje do galerie telefonu – příspěvek pak vložíš z telefonu.

  .venv/bin/python -W ignore posty.py "logo kapely Tři sestry" --skupina "Tři sestry"
  .venv/bin/python -W ignore posty.py "logo Ferrari" --logo ~/Downloads/ferrari.png --barva červená --varianty 3

Gemini klíč: proměnná GEMINI_API_KEY, jinak ~/Developer/shopify/.gemini_key (řádek GEMINI_API_KEY=…).
Výstup: posty/<motiv>/1.jpg, 2.jpg… (bez metadat), text.txt  +  v telefonu album „Lampicky“.
"""

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

from google import genai
from google.genai import types
from PIL import Image

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
ADB = Path.home() / ".local" / "android" / "platform-tools" / "adb"
REFERENCES = [HERE / "vzory" / "kabat.jpg", HERE / "vzory" / "bmw.jpg"]  # skutečné lampičky – vzor vzhledu a fotky
KEY_FILE = Path.home() / "Developer" / "shopify" / ".gemini_key"
MODELS = ["gemini-3-pro-image", "gemini-3-pro-image-preview", "gemini-2.5-flash-image"]

IMAGE_PROMPT = """Product photo of a handmade LED night lamp – it must look exactly like the lamps in the first two reference photos (same lamp, same photo setup):
a clear square acrylic plate (15 × 15 cm, slightly rounded corners) standing in an oval light beech-wood base with a small round button on the front.
The motif is laser-engraved into the acrylic as thin glowing lines (line-art / outline engraving, no fill, no print), lit from below by {color} LED light, the plate edges glow too.
Engraved motif: {motif}.{logo_hint}
Scene: identical to the reference photos – realistic smartphone photo, frontal view, lamp centered, standing on a beige carpet in front of a dark wooden plank wall, dark room, the light of the lamp tints the wall and carpet, vertical 4:5 framing.
No text, no watermarks, no hands, no extra objects in front of the lamp."""

POST_PROMPT = """Napiš popisek k fotce do facebookové skupiny „{group}“. Na fotce je LED lampička s motivem: {motif}.
Drž se PŘESNĚ tohoto stylu a délky (vzor): „Pro bratra jsem vyrobil lampičku Tří sester 🙂 Kdyby někdo chtěl taky, klidně napište. Mohu udělat i jiné motivy 🙂“
Tedy: první věta – osobní důvod, proč a pro koho jsem ji vyrobil (bratr, kamarád, táta… střídej), s názvem motivu; pak „Kdyby někdo chtěl taky, klidně napište. Mohu udělat i jiné motivy.“
Emoji: jediné povolené je 🙂 (max jednou, na konci). Žádné jiné emoji. Žádná cena, žádné parametry, žádné hashtagy, nic reklamního.
Vrať jen samotný text."""


def api_key():
    key = os.environ.get("GEMINI_API_KEY") or ""
    if not key and KEY_FILE.exists():
        for line in KEY_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("GEMINI_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"')
    if not key:
        sys.exit(f"Chybí Gemini klíč – nastav GEMINI_API_KEY nebo ho dej do {KEY_FILE}.")
    return key


def slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "motiv"


def generate_image(client, model, motif, color, logo):
    contents = [Image.open(r) for r in REFERENCES]
    hint = ""
    if logo:
        contents.append(Image.open(logo))
        hint = " Use the exact shapes of the logo from the third reference image, converted to engraved outlines – keep its proportions and lettering accurate."
    contents.insert(0, IMAGE_PROMPT.format(motif=motif, color=color, logo_hint=hint))
    resp = client.models.generate_content(
        model=model, contents=contents,
        config=types.GenerateContentConfig(response_modalities=["IMAGE"], image_config=types.ImageConfig(aspect_ratio="4:5")),
    )
    for part in resp.candidates[0].content.parts:
        if part.inline_data and part.inline_data.data:
            return part.inline_data.data
    raise RuntimeError("Gemini nevrátil obrázek")


def write_post(group, motif):
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # jen předplatné
    proc = subprocess.run(
        [CONFIG["claudePath"], "-p", "--model", CONFIG["model"], "--tools", "", "--strict-mcp-config",
         "--setting-sources", "", "--no-session-persistence"],
        input=POST_PROMPT.format(group=group, motif=motif), capture_output=True, text=True,
        encoding="utf-8", cwd=tempfile.gettempdir(), env=env, timeout=180,
    )
    return proc.stdout.strip()


def push_to_phone(files, album="Lampicky"):
    for f in files:
        dest = f"/sdcard/Pictures/{album}/{f.parent.name}-{f.name}"
        subprocess.run([str(ADB), "push", str(f), dest], check=True, capture_output=True)
        subprocess.run([str(ADB), "shell", "am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                        "-d", f"file://{dest}"], capture_output=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("motiv", help='co má být na lampičce, např. "logo kapely Tři sestry"')
    ap.add_argument("--skupina", default="", help="název FB skupiny (pro text příspěvku)")
    ap.add_argument("--logo", help="obrázek loga (PNG/JPG) – výrazně zlepší přesnost loga")
    ap.add_argument("--barva", default="teplá bílá", help="barva světla: teplá bílá, modrá, červená…")
    ap.add_argument("--varianty", type=int, default=2)
    ap.add_argument("--bez-telefonu", action="store_true")
    a = ap.parse_args()

    colors = {"teplá bílá": "warm white", "bílá": "cold white", "modrá": "blue", "červená": "red",
              "zelená": "green", "fialová": "purple", "žlutá": "yellow", "růžová": "pink", "oranžová": "orange"}
    color = colors.get(a.barva, a.barva)
    out = HERE / "posty" / slug(a.motiv)
    out.mkdir(parents=True, exist_ok=True)

    client = genai.Client(api_key=api_key())
    files = []
    for i in range(1, a.varianty + 1):
        for model in MODELS:
            try:
                data = generate_image(client, model, a.motiv, color, a.logo)
                break
            except Exception as e:  # noqa: BLE001
                print(f"  {model}: {e}", flush=True)
        else:
            sys.exit("Obrázek se nepodařilo vygenerovat.")
        f = out / f"{i}.jpg"
        # znovu uložit jen pixely – bez EXIF/C2PA metadat z generátoru (jako screenshot)
        Image.open(io.BytesIO(data)).convert("RGB").save(f, "JPEG", quality=92)
        files.append(f)
        print(f"Obrázek {i}: {f}  ({model})", flush=True)

    text = write_post(a.skupina or a.motiv, a.motiv)
    (out / "text.txt").write_text(text + "\n", encoding="utf-8")
    print(f"\nTEXT PŘÍSPĚVKU:\n{text}\n")

    if not a.bez_telefonu:
        push_to_phone(files)
        print("Fotky jsou v telefonu v Galerii, album „Lampicky“.")


if __name__ == "__main__":
    main()
