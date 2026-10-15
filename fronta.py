"""Fronta příspěvků do FB skupin – zpracovává ji bot_telefon.py (nejvýš maxPostsPerDay příspěvků za den,
mezi příspěvky pauza postGapMinutes). Když v skupině ještě nejsi, bot se nejdřív přidá a zkusí to znovu za 3 h.

  .venv/bin/python fronta.py seznam                         – co je ve frontě a v jakém stavu
  .venv/bin/python fronta.py nahrat plany/motorky.json      – přidá do fronty celý plán
  .venv/bin/python fronta.py pridat "Skupina" "12 tis." posty/logo-ktm/2.jpg "text příspěvku"
  .venv/bin/python fronta.py smazat 3                       – smaže úlohu č. 3
  .venv/bin/python fronta.py vycistit                       – smaže hotové (zveřejněné) úlohy

Plán (JSON): [{"group": "název skupiny", "members": "12 tis.", "photos": ["posty/.../2.jpg", …], "text": "…"}]
Když "text" chybí, vezme se posty/<motiv>/text.txt vedle fotky.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUEUE = HERE / "fronta.json"


def load():
    return json.loads(QUEUE.read_text(encoding="utf-8")) if QUEUE.exists() else []


def save(jobs):
    QUEUE.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")


def add(jobs, group, members, photos, text=None):
    photos = [photos] if isinstance(photos, str) else photos
    for photo in photos:
        if not (HERE / photo).exists():
            sys.exit(f"Fotka {photo} neexistuje.")
    if not text:
        text = (HERE / photos[0]).parent.joinpath("text.txt").read_text(encoding="utf-8").strip()
    jobs.append({"group": group, "members": members, "photos": photos, "text": text, "status": "ceka", "due": 0})


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "seznam"
    jobs = load()
    if cmd == "nahrat":
        for p in json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")):
            add(jobs, p["group"], p["members"], p.get("photos") or p["photo"], p.get("text"))
        save(jobs)
    elif cmd == "pridat":
        add(jobs, sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5] if len(sys.argv) > 5 else None)
        save(jobs)
    elif cmd == "smazat":
        jobs.pop(int(sys.argv[2]) - 1)
        save(jobs)
    elif cmd == "vycistit":
        save([j for j in jobs if j["status"] not in ("zverejneno",)])
        jobs = load()
    elif cmd != "seznam":
        sys.exit(__doc__)
    for i, j in enumerate(jobs, 1):
        print(f"{i:2}. [{j['status']}] {j['group']} ({j['members']}) – {', '.join(Path(x).parent.name.replace('logo-', '') for x in j.get('photos') or [j['photo']])}"
              + (f" – {j['last']}" if j.get("last") else "") + (f" – {j['error']}" if j.get("error") else ""))
    if not jobs:
        print("Fronta je prázdná.")


if __name__ == "__main__":
    main()
