"""Kde se bot zasekl – screenshoty a obrazovky v problemy/. Čas od času je projdeme a bota opravíme.

  .venv/bin/python problemy.py              – nevyřešené problémy
  .venv/bin/python problemy.py vse          – všechny včetně vyřešených
  .venv/bin/python problemy.py vyreseno 3   – označí problém č. 3 jako vyřešený
"""

import json
import sys
from pathlib import Path

INDEX = Path(__file__).resolve().parent / "problemy" / "seznam.json"


def main():
    items = json.loads(INDEX.read_text(encoding="utf-8")) if INDEX.exists() else []
    cmd = sys.argv[1] if len(sys.argv) > 1 else "seznam"
    if cmd == "vyreseno":
        items[int(sys.argv[2]) - 1]["status"] = "vyreseno"
        INDEX.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    elif cmd not in ("seznam", "vse"):
        sys.exit(__doc__)
    shown = 0
    for i, p in enumerate(items, 1):
        if cmd == "vse" or p["status"] == "novy":
            shown += 1
            print(f"{i:3}. [{p['status']}] {p['time']} ({p['count']}×) {p['kind']} – {p['detail'][:120]}\n       problemy/{p['folder']}/")
    if not shown:
        print("Žádné nevyřešené problémy 🙂")


if __name__ == "__main__":
    main()
