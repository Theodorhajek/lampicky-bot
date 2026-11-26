"""Záchrana z neznámé obrazovky: když se skript nedostane tam, kam má (hlavní stránka FB, seznam chatů),
pošle Claudovi seznam prvků na obrazovce a cíl. Claude smí jen „zpět“ nebo klepnout na prvek – a nikdy
na nic, co odesílá, zveřejňuje, potvrzuje, mění účet nebo povoluje (to bot zablokuje sám, i kdyby to Claude chtěl).
Každý krok se zapíše do problemy/zachrana.log, ať se z toho dá udělat pevné pravidlo."""

import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
LOG = HERE / "problemy" / "zachrana.log"

FORBIDDEN = re.compile(
    r"(?i)(přidat|odeslat|poslat|zveřejn|publik|sdílet|potvrd|souhlas|přijmout|povolit|zkontrolovat účet|heslo|přihlásit|"
    r"odhlásit|smazat|odstranit|zablokovat|nahlásit|koupit|zaplatit|objednat|připojit se|pokračovat jako|uložit|"
    r"post|send|share|confirm|accept|allow|agree|log ?in|log ?out|delete|remove|block|report|buy|pay|join)")


def _elements(d):
    root = ET.fromstring(d.dump_hierarchy())
    out = []
    for n in root.iter("node"):
        label = (n.get("content-desc") or n.get("text") or "").strip()
        if n.get("clickable") != "true" or not label:
            continue
        x1, y1, x2, y2 = map(int, re.findall(r"\d+", n.get("bounds"))[:4])
        out.append({"i": len(out), "label": label[:60], "cls": n.get("class", "").split(".")[-1], "box": (x1, y1, x2, y2)})
    return out[:60]


def _log(msg):
    try:
        LOG.parent.mkdir(exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%d.%m. %H:%M:%S} {msg}\n")
    except OSError:
        pass


def rescue(d, goal, done, steps=4):
    """goal = popis cíle pro Clauda, done() = funkce, která pozná, že jsme v cíli. True = povedlo se."""
    _log(f"ZAČÁTEK – cíl: {goal}")
    for step in range(steps):
        if done():
            _log("hotovo")
            return True
        els = _elements(d)
        app = d.app_current().get("package", "")
        prompt = (
            "Ovládáš Android telefon. Jsi v nouzovém režimu: skript se zasekl na neznámé obrazovce.\n"
            f"Cíl: {goal}\nAplikace na popředí: {app}\nKlikatelné prvky (i, třída, popis):\n"
            + "\n".join(f"{e['i']}: {e['cls']} „{e['label']}“" for e in els)
            + "\n\nSmíš jen zavřít/přeskočit dialog, vrátit se zpět, nebo přejít na kartu/ikonu v aplikaci. Nic neodesílej, "
              "nepotvrzuj, nepřihlašuj, neměň nastavení. Odpověz jen JSON: {\"akce\": \"zpet\"} nebo {\"akce\": \"klepni\", \"i\": číslo} "
              "nebo {\"akce\": \"restart\"} (aplikaci zavřít a znovu otevřít) nebo {\"akce\": \"vzdat\"}, plus \"proc\": krátce."
        )
        try:
            r = subprocess.run([CONFIG["claudePath"], "-p", "--output-format", "json", "--model", "sonnet", prompt],
                               capture_output=True, text=True, timeout=120)
            res = json.loads(r.stdout).get("result", "")
            ans = json.loads(re.search(r"\{.*\}", res, re.S).group(0))
        except Exception as e:  # noqa: BLE001
            _log(f"Claude neodpověděl: {e}")
            return False
        act = ans.get("akce")
        _log(f"krok {step + 1}: {ans}")
        if act == "zpet":
            d.press("back")
        elif act == "klepni" and isinstance(ans.get("i"), int) and 0 <= ans["i"] < len(els):
            e = els[ans["i"]]
            if FORBIDDEN.search(e["label"]):
                _log(f"ZAKÁZÁNO klepnout na „{e['label']}“ – místo toho zpět")
                d.press("back")
            else:
                x1, y1, x2, y2 = e["box"]
                d.click((x1 + x2) // 2, (y1 + y2) // 2)
        elif act == "restart" and app:
            d.app_stop(app)
            time.sleep(1)
            return False  # volající aplikaci znovu otevře sám
        else:
            return False
        time.sleep(2.5)
    ok = done()
    _log("hotovo" if ok else "nepovedlo se")
    return ok
