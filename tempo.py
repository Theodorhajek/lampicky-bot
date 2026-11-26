"""Rychlost bota: všechny čekací pauzy v UI (psaní, klepání, načítání, scrolování) se násobí `tempo` z config.json
(1 = původní rychlost, 0.5 = dvakrát rychleji). Pauzy hlavní smyčky (kontrola upozornění, výpadky) se nemění."""
import json
import time
from pathlib import Path

try:
    SPEED = float(json.loads((Path(__file__).resolve().parent / "config.json").read_text()).get("tempo", 1))
except Exception:  # noqa: BLE001
    SPEED = 1.0


def sleep(s):
    time.sleep(max(0.05, s * SPEED))
