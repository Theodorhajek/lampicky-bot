"""Messenger asistent pro prodej lampiček – ovládá aplikaci Messenger v telefonu s Androidem.

  python bot_telefon.py debug   – ukáže, co bot v telefonu "vidí" (nic neposílá)
  python bot_telefon.py dry     – běží, ale odpovědi jen vypisuje, NEPOSÍLÁ
  python bot_telefon.py run     – běží a odpovídá
  python bot_telefon.py dry --catch-up   – při prvním spuštění zpracuje i staré neodpovězené zprávy

Telefon: připojený kabelem, zapnuté Ladění USB, Messenger přihlášený prodejním účtem.
"""

import csv
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import traceback
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ADB_DIR = Path.home() / ".local" / "android" / "platform-tools"
os.environ["PATH"] = f"{ADB_DIR}{os.pathsep}{os.environ['PATH']}"

import uiautomator2 as u2  # noqa: E402  (až po nastavení PATH k adb)

import data  # noqa: E402  (databáze zpráv, komentářů a rozhodnutí – data/lampicky.db)

CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
MODE = sys.argv[1] if len(sys.argv) > 1 else "run"
SEND = MODE == "run"
CATCH_UP = "--catch-up" in sys.argv
PKG = "com.facebook.orca"

# ---------- stav a log ----------

STATE_FILE = HERE / "state_telefon.json"
state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {"initialized": False, "threads": {}}


def save_state():
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def thread(name):
    return state["threads"].setdefault(
        name, {"preview": "", "botSent": [], "botReplies": 0, "pausedUntil": 0, "handedOff": False, "handled": ""}
    )


def log(name, action, text=""):
    now = datetime.now()
    with open(HERE / "log.csv", "a", newline="", encoding="utf-8") as f:
        csv.writer(f, delimiter=";").writerow([now.strftime("%d. %m. %Y %H:%M:%S"), name, action, (text or "").replace("\n", " / ")])
    print(f"[{now:%H:%M:%S}] {name} – {action}{': ' + text if text else ''}", flush=True)


def notify(title, text):
    if sys.platform == "darwin":
        esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(["osascript", "-e", f'display notification "{esc(text)}" with title "{esc(title)}" sound name "Glass"'])
    else:
        print("\a", end="", flush=True)


norm = lambda s: re.sub(r"\s+", " ", s or "").strip()
# porovnání textů bez emoji/interpunkce (telefon čte emoji jako „..“)
same_text = lambda a, b: re.sub(r"[\W_]+", "", a or "").lower() == re.sub(r"[\W_]+", "", b or "").lower()

# ---------- telefon ----------

d = None


def connect():
    global d
    d = u2.connect()
    d.screen_on()
    # vždy na výšku – když telefon leží na boku a otočí se, nesedí rozložení obrazovky
    d.shell("settings put system accelerometer_rotation 0")
    d.shell("settings put system user_rotation 0")
    try:
        d.set_orientation("natural")
        d.freeze_rotation(True)
    except Exception:  # noqa: BLE001
        pass


def nodes():
    """Všechny prvky Messengeru na obrazovce: (třída, text, popis, (x1, y1, x2, y2))."""
    out = []
    for n in ET.fromstring(d.dump_hierarchy()).iter("node"):
        if n.get("package") != PKG:
            continue
        x1, y1, x2, y2 = map(int, re.findall(r"\d+", n.get("bounds")))
        out.append((n.get("class", "").split(".")[-1], n.get("text") or "", n.get("content-desc") or "", (x1, y1, x2, y2)))
    return out


def tap_desc(desc, exact=False, timeout=8):
    sel = d(description=desc) if exact else d(descriptionStartsWith=desc)
    if not sel.wait(timeout=timeout):
        raise RuntimeError(f"nenašel jsem tlačítko „{desc}“")
    sel.click()
    time.sleep(2)


def wake():
    """Rozsvítit a odemknout (přejetím) telefon, pokud zhasl."""
    locked = "isKeyguardShowing=true" in d.shell("dumpsys window").output
    if locked or not d.info.get("screenOn"):
        d.unlock()
        time.sleep(1.5)


# tlačítka, kterými jde bezpečně zavřít vyskakovací okna Messengeru (PIN, novinky, oprávnění…)
DISMISS = ["Zavřít", "Teď ne", "Ne, díky", "Přeskočit", "Později", "Rozumím", "Zrušit"]


def dismiss_popups():
    """Zkusí zavřít známá vyskakovací okna. Vrací True, pokud něco zavřel."""
    for label in DISMISS:
        for sel in (d(description=label), d(text=label)):
            if sel.exists:
                sel.click()
                time.sleep(2)
                return True
    return False


# ---------- zaseknutí: screenshot + obrazovka do problemy/, projdeme je a bota naučíme ----------

PROBLEMS = HERE / "problemy"


def zasek(kind, detail="", exc=None):
    """Uloží, na čem se bot zasekl: screenshot, rozložení obrazovky (XML) a popis do problemy/<čas>-<druh>/.
    Stejný problém se během hodiny ukládá jen jednou (jinak se jen zvýší počet)."""
    try:
        PROBLEMS.mkdir(exist_ok=True)
        index = PROBLEMS / "seznam.json"
        items = json.loads(index.read_text(encoding="utf-8")) if index.exists() else []
        key = f"{kind}|{str(detail)[:80]}"
        recent = next((i for i in reversed(items) if i["key"] == key and i["status"] == "novy"
                       and time.time() - i["lastTs"] < 3600), None)
        if recent:
            recent.update(count=recent["count"] + 1, lastTs=time.time())
        else:
            folder = PROBLEMS / f"{datetime.now():%Y%m%d-%H%M%S}-{re.sub(r'[^a-z0-9]+', '-', unicodedata.normalize('NFKD', kind.lower()).encode('ascii', 'ignore').decode())[:30]}"
            folder.mkdir()
            try:
                d.screenshot(str(folder / "screenshot.png"))
                (folder / "obrazovka.xml").write_text(d.dump_hierarchy(), encoding="utf-8")
                app = d.app_current().get("activity", "")
            except Exception:  # noqa: BLE001
                app = "(telefon nešel přečíst)"
            items.append({"key": key, "kind": kind, "detail": str(detail)[:300], "folder": folder.name, "activity": app,
                          "time": f"{datetime.now():%d. %m. %Y %H:%M}", "lastTs": time.time(), "count": 1, "status": "novy",
                          "error": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-3000:] if exc else ""})
        index.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:  # noqa: BLE001  – ukládání problému nikdy nesmí shodit bota
        print("  (zaseknutí se nepodařilo uložit:", e, ")", flush=True)


def go_home():
    """Messenger na popředí, karta Chaty, seznam úplně nahoře. Při zaseknutí zavře popupy, případně restartuje Messenger."""
    for attempt in range(3):
        try:
            _go_home()
            return
        except RuntimeError:
            if attempt == 0 and dismiss_popups():
                continue
            zasek("Messenger – nenašel jsem seznam chatů, restartuji", f"pokus {attempt + 1}")
            d.app_stop(PKG)  # poslední možnost: Messenger restartovat
            time.sleep(2)
    _go_home()


def _go_home():
    wake()
    if d.app_current().get("package") != PKG:
        d.app_start(PKG, ".auth.StartScreenActivity")  # s aktivitou = bez „monkey“, který zapíná otáčení
        time.sleep(5)
    for _ in range(5):
        if d(descriptionStartsWith="Chaty, ").exists and not d(className="android.widget.EditText").exists:
            break
        if dismiss_popups():
            continue
        d.press("back")
        time.sleep(1)
        if d.app_current().get("package") != PKG:
            d.app_start(PKG, ".auth.StartScreenActivity")  # s aktivitou = bez „monkey“, který zapíná otáčení
            time.sleep(5)
    tap_desc("Chaty, ")
    tap_desc("Chaty, ")  # druhé klepnutí odroluje seznam nahoru


def leave_app():
    """Messenger do pozadí (domovská obrazovka) – jen tak chodí upozornění na nové zprávy."""
    d.press("home")


def messenger_notifications():
    """Otisky aktuálních upozornění Messengeru (bez sahání na obrazovku)."""
    out = subprocess.run(["adb", "shell", "dumpsys", "notification", "--noredact"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30).stdout
    found = set()
    for block in out.split("NotificationRecord(")[1:]:
        if f"pkg={PKG} " not in block.split("\n", 1)[0]:
            continue
        title = re.search(r"android\.title=\S+ \((.*)\)", block)
        text = re.search(r"android\.text=\S+ \((.*)\)", block)
        if title or text:
            found.add(f"{title.group(1) if title else ''}|{text.group(1) if text else ''}")
    return found


SKIP_LIST = re.compile(r"(opustil\(a\) skupinu|do skupiny|Zpráva není dostupná)")


def read_list():
    """Konverzace viditelné na obrazovce: [{name, preview}] – jen chaty s jedním člověkem."""
    out = []
    all_nodes = nodes()
    for cls, text, desc, (x1, y1, x2, y2) in all_nodes:
        if cls != "Button" or x1 > 10 or x2 < 1000 or not desc or "," not in desc:
            continue
        if any(o["name"] and desc.startswith(o["name"] + ",") for o in out):
            continue  # duplicita
        # jméno je v podřízeném prvku s textem „Jméno.“ (případně „Nepřečtené – Jméno.“)
        name = None
        for c2, t2, _, (a1, b1, a2, b2) in all_nodes:
            if t2 and b1 >= y1 and b2 <= y2 and a1 >= 200 and t2.endswith("."):
                name = re.sub(r"^Nepřečtené – ", "", t2)[:-1].strip()
                break
        if not name or not desc.startswith(name + ",") or "," in name or SKIP_LIST.search(desc):
            continue  # skupinový chat / spamová skupina
        preview = desc[len(name):].lstrip(", ")
        preview = re.sub(r", Viděl\(a\) to .*$", "", preview)
        stamp = next((t2.strip() for c2, t2, _, (a1, b1, a2, b2) in all_nodes
                      if t2 and b1 >= y1 and b2 <= y2 and LIST_TIME.match(t2.strip())), "")
        out.append({"name": name, "preview": preview, "late": is_late(stamp)})
    return out


LIST_TIME = re.compile(r"^(\d{1,2}:\d{2}|Po|Út|St|Čt|Pá|So|Ne|\d{1,2}\. ?\d{1,2}\.( ?\d{4})?)$")
WEEKDAYS = ["Po", "Út", "St", "Čt", "Pá", "So", "Ne"]


def is_late(stamp):
    """Čas poslední zprávy ze seznamu chatů („12:22“, „Po“, „14. 10.“) je starší než 24 h?
    U dne bez času se počítá s koncem toho dne, ať se bot neomlouvá zbytečně."""
    today = datetime.now().date()
    if not stamp or ":" in stamp:
        return False  # dnešní zpráva
    if stamp in WEEKDAYS:
        days = (today.weekday() - WEEKDAYS.index(stamp)) % 7 or 7
    else:
        p = [int(x) for x in re.findall(r"\d+", stamp)]
        try:
            day = datetime(p[2] if len(p) > 2 else today.year, p[1], p[0]).date()
        except ValueError:
            return False
        if day > today:
            day = day.replace(year=day.year - 1)
        days = (today - day).days
    return days >= 2


MSG = re.compile(r"^(.+?), (.*), poklepáním zobrazíte datum", re.S)
PHOTO = re.compile(r"(fotka|fotku|obrázek|video).*odeslal\(a\) (.+?) v ", re.I)


DATE = re.compile(r"^(\d{1,2}\. ?\d{1,2}\.|\d{1,2}:\d{2}$|(Po|Út|St|Čt|Pá|So|Ne)\b|Dnes|Včera|.*\bV \d{1,2}:\d{2}$)")
SYSTEM_TEXT = re.compile(r"koncovým šifrováním|Teď už si můžete|Na Facebooku jste přáteli|Zobrazit profil|Nový Facebook profil|^Žije v ")


def read_messages():
    """Zprávy v otevřené konverzaci: [{from: them|me, text, images}]."""
    all_nodes = sorted(nodes(), key=lambda n: n[3][1])
    # spodní hranice konverzace: pole pro psaní nebo lišta „Pokud odpovíte…“ u žádostí
    bottom = min([b[1] for c, t, _, b in all_nodes if c == "EditText" or t.startswith("Pokud odpovíte")] or [10_000])
    out, bubbles = [], []
    for cls, text, desc, (x1, y1, x2, y2) in all_nodes:
        if y1 < 245 or y2 > bottom:
            continue  # hlavička nebo spodní lišta
        side = "them" if x1 < 200 else "me"
        m = MSG.match(desc)
        if m:
            out.append({"from": side, "text": m.group(2).strip(), "images": 0, "y": y1})
            bubbles.append((x1, y1, x2, y2))
        elif cls == "ImageView" and PHOTO.search(desc):
            out.append({"from": side, "text": "", "images": 1, "y": y1})
    # zprávy bez obvyklého popisku (odkazy, sdílené příspěvky…): text v bublině, která ještě není zachycená
    for cls, text, desc, (x1, y1, x2, y2) in all_nodes:
        inside = lambda r: r[0] <= x1 and r[1] <= y1 and r[2] >= x2 and r[3] >= y2
        bubble_side = "them" if 120 <= x1 < 200 else "me" if x1 >= 200 and x2 >= 1040 else None
        if (cls != "ViewGroup" or not text.strip() or y1 < 245 or y2 > bottom or not bubble_side
                or DATE.match(text.strip()) or SYSTEM_TEXT.search(text) or any(inside(r) for r in bubbles)):
            continue
        out.append({"from": bubble_side, "text": text.strip(), "images": 0, "y": y1})
        bubbles.append((x1, y1, x2, y2))
    return sorted(out, key=lambda m: m["y"])


def open_chat(name):
    tap_desc(f"{name}, ")
    time.sleep(2)


PHOTOS = HERE / "fotky"  # skutečné fotky hotových lampiček (název souboru = motiv), bot je posílá zákazníkům


def send_photo(path):
    """Pošle do otevřeného chatu fotku: nahraje ji do galerie (bude první) → Galerie → první fotka → POSLAT."""
    dest = f"/sdcard/Pictures/Lampicky/{datetime.now():%Y%m%d-%H%M%S}-{Path(path).name}"
    subprocess.run(["adb", "push", str(path), dest], check=True, capture_output=True)
    subprocess.run(["adb", "shell", "touch", dest], capture_output=True)
    subprocess.run(["adb", "shell", "am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE", "-d", f"file://{dest}"], capture_output=True)
    time.sleep(3)
    tap_desc("Otevře se fotogalerie.", exact=True)
    time.sleep(4)
    first = d(descriptionStartsWith="Fotka byla pořízena")
    if not first.wait(timeout=8):
        raise RuntimeError("v galerii chatu nejsou fotky (chybí oprávnění?)")
    first.click()
    time.sleep(2)
    send = d(description="POSLAT") if d(description="POSLAT").exists else d(text="POSLAT")
    if not send.exists:
        raise RuntimeError("v galerii chybí tlačítko POSLAT")
    send.click()
    time.sleep(6)
    d.press("back")  # zavřít galerii


def send_message(text):
    box = d(className="android.widget.EditText")
    box.click()
    box.set_text(text)
    time.sleep(1)
    tap_desc("Poslat", exact=True)
    time.sleep(2)
    d.press("back")  # schovat klávesnici


def block_contact(name):
    """Zablokuje člověka v Messengeru (Podrobnosti o konverzaci → Blokovat → potvrdit). Vrací True/False."""
    open_chat(name)
    tap_desc("Podrobnosti o konverzaci", exact=True)
    for _ in range(4):
        if d(description="Blokovat ").exists or d(descriptionStartsWith="Odblokovat").exists:
            break
        d.swipe(540, 1800, 540, 700, 0.3)
        time.sleep(1.5)
    if d(descriptionStartsWith="Odblokovat").exists:
        return True  # už je zablokovaný
    tap_desc("Blokovat ", exact=True)
    for _ in range(3):  # potvrzovací nabídka/dialog
        if d(text="Ještě nejde zablokovat").exists:
            d.press("back")
            return False
        btn = d(textMatches=r"(?i)^blokovat( zprávy.*| na messengeru| uživatele.*)?$", clickable=True)
        if not btn.exists:
            btn = d(descriptionMatches=r"(?i)^blokovat( zprávy.*| na messengeru| uživatele.*)?$", clickable=True)
        if not btn.exists:
            break
        btn.click()
        time.sleep(2.5)
    ok = d(descriptionStartsWith="Odblokovat").exists or d(textStartsWith="Odblokovat").exists
    if not ok:
        print("  blokování – na obrazovce je:", [n[1] or n[2] for n in nodes() if n[1] or n[2]][:20], flush=True)
    return ok


def open_requests(tab):
    go_home()
    tap_desc("Karta s nabídkou")
    tap_desc("Žádosti o zprávy")
    if d(description="Teď ne").exists:
        tap_desc("Teď ne", exact=True)
    tap_desc(tab, exact=True)
    time.sleep(3)


# ---------- Claude (přes předplatné – Claude Code) ----------

REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["reply", "none", "block"]},
        "messages": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "photo": {"type": "string"},
        "handoff": {"type": "boolean"},
        "note": {"type": "string"},
    },
    "required": ["action", "messages", "photo", "handoff", "note"],
    "additionalProperties": False,
}


def system_prompt():
    info = (HERE / "info.md").read_text(encoding="utf-8")
    lessons = "\n".join(l for l in (HERE / "lekce.md").read_text(encoding="utf-8").splitlines() if l.startswith("- "))
    return f"""Odpovídáš v Messengeru za prodejce ručně dělaných lampiček na zakázku (auta, motorky, loga, cokoliv podle fotky). Zákazníci píšou většinou z Facebook skupin, kde viděli jeho příspěvek.

Píšeš za něj, v první osobě, česky. Vykáš (prodejce píše zákazníkům „Zdravím“, „Dobrý den“, „dejte vědět“). Když zákazník tyká nebo začne „Ahoj“, tykej mu taky (a pozdrav „Ahoj“). Piš úsporně jako člověk na mobilu, ne jako robot ani e-mail. Většinou JEDNA krátká zpráva (pole messages), druhou přidej jen když je opravdu potřeba (nejvýš 2). Každá zpráva max. jedna krátká věta. Čím kratší, tím lepší. Žádné slohovky, odrážky ani formální fráze. Z emoji jen 🙂 (nejvýš jednou), žádné jiné.
- Odpověz jen na to, na co se zákazník ptá. Nic navíc nevysvětluj, neopakuj, co už v konverzaci zaznělo, a nepiš samozřejmosti.
- Nanejvýš jedna otázka, a jen když posouvá objednávku. Žádné výplňové výzvy typu „až se podíváte, dejte vědět", „kdyby něco, napište", „vyberte si, jakého dopravce byste chtěla" – zákazník ví, co dělat.
- Rozměr, napájení, barvy, materiál apod. zmiň, jen když se na ně ptá.
- Fotky: když zákazník chce vidět, jak lampička vypadá, a v SKUTEČNÝCH FOTKÁCH níže je odpovídající motiv, dej do photo název souboru (pošle se po zprávách) a napiš krátce např. „Tohle BMW jsem už jednou dělal 🙂". Když odpovídající fotka není, photo="" a handoff=true (fotku pošle prodejce). Jinak vždy photo="".
- Když zákazník napíše, odkud je, NIKDY nepotvrzuj, že tam pošleš („do Přerova to bez problému pošlu Zásilkovnou nebo PPL" je zbytečné – posíláme kamkoliv v ČR i SK). Prostě pokračuj dalším krokem (adresa/výdejní místo, telefon, e-mail).
- Na potvrzení stačí „OK" nebo „Rozumím." – nikdy „Rozumím, děkuji", „Děkuji za informaci" apod.
- Když zákazník napíše motiv/značku (Kawasaki, BMW, Ferrari…), reaguj přirozeně a pozitivně, např. „Kawasaki by určitě šlo 🙂". Nikdy nepiš nic o tom, odkud vezmeš logo nebo podklady (žádné „logo si seženu sám") – to je jen interní informace pro tebe, aby ses po zákazníkovi nechtěl fotku loga.

Jak prodejce typicky píše (drž se jeho stylu a postupu):
- „Dobrý den, cena je 690 Kč. Odkud jste? Mohl bych vám ji poslat zásilkovnou?"
- „Jakou lampičku byste chtěl?" / „Co byste na lampičku chtěl vygravírovat?"
- „Super, tak mi prosím pošlete adresu výdejního místa, telefonní číslo a e-mail."
- „Jak s platbou? Budete chtít zaplatit celé předem, nebo jen poštovné a zbytek dám na dobírku?"
- „Tak vás poprosím o 105 Kč na účet 1508200788/5500. Předem děkuji a dejte vědět, až pošlete."
- Typická odpověď: „Dobrý den, Kawasaki by určitě šlo 🙂 Cena je 690 Kč + doprava." – a dost. Na „chci vidět fotku" stačí „Jasně, pošlu fotku 🙂".
(Příklady ukazují jen styl – fakta, ceny a podmínky ber výhradně z INFORMACÍ O PRODEJI níže.)
- Na tykání odpovídá tykáním („Ahoj, …"), jinak vyká. Píše krátce, bez formálních obratů, občas „Díky!", „Super", „Skvělý".
Postup objednávky: motiv → cena → doprava (odkud je, zásilkovna?) → adresa výdejny, telefon, e-mail → způsob platby → číslo účtu a částka → předání prodejci.

Cíl: aby zákazník nemusel čekat a objednávka se posunula – odpověz na dotaz, zjisti co chce a postupně sesbírej, co je potřeba k objednávce (podle informací níže). Na nic se neptej dvakrát, pokud už to v konverzaci je.

Pravidla:
- Používej jen informace níže. Nikdy si nevymýšlej ceny, termíny, slevy, parametry ani sliby. Pokud odpověď neznáš, napiš něco jako „tohle vám potvrdím, ozvu se co nejdřív" a nastav handoff=true.
- handoff=true nastav také, když: zákazník reklamuje nebo je nespokojený, chce něco atypického / individuální cenu, smlouvá o ceně nad rámec pravidel, objednávka má všechny potřebné údaje (prodejce ji musí zkontrolovat a vyrobit – zákazníkovi při tom pošli souhrn a platební údaje), zákazník píše, že zaplatil (prodejce musí platbu ověřit), nebo si nejsi jistý.
- Údaje v hranatých závorkách [takhle] jsou nevyplněné – ber je jako neznámé.
- Pokud se zákazník přímo zeptá, jestli píše s robotem, nelži: řekni, že na rychlé odpovědi používáš asistenta a všechno po něm čteš.
- Pokud poslední zpráva nepotřebuje odpověď (např. jen „ok", „díky", palec nahoru) a nic nečeká, dej action="none".
- Pokud jde o spam, podvod nebo zprávu, která s prodejem lampiček nesouvisí, dej action="none".
- Pokud se zákazník jakkoli ptá na autorská práva, licenci, ochrannou známku nebo povolení používat logo/značku (např. „máte povolení to logo používat?"), dej action="block", messages=[] a handoff=false – prodejce takového člověka blokuje a nic mu neodpovídá.
- „[fotka]" znamená, že zákazník poslal obrázek (nevidíš ho) – poděkuj a ber to jako dodanou fotku.
- Vidíš jen posledních pár zpráv konverzace. Pokud chybí kontext, ptej se opatrně nebo předej (handoff=true).
- note: jedna krátká věta pro prodejce, co se v konverzaci děje (např. „chce lampičku s E46, čeká na platbu").

LEKCE Z OPRAV PRODEJCE (mají přednost před vším ostatním):
{lessons}

JAK ODPOVÍDAL PRODEJCE SÁM (skutečné ruční odpovědi – napodob styl a délku):
{data.manual_examples() or "zatím žádné"}

SKUTEČNÉ FOTKY (název souboru = motiv): {", ".join(sorted(f.name for f in PHOTOS.glob("*.jpg"))) or "žádné"}

INFORMACE O PRODEJI:
{info}"""


def transcript(messages):
    lines = []
    for m in messages[-30:]:
        body = " ".join(x for x in [m["text"], "[fotka]" * min(m["images"], 3)] if x)
        lines.append(f"{'Zákazník' if m['from'] == 'them' else 'Já'}: {body}")
    return "\n".join(lines)


def ask_claude(name, messages, late=False):
    prompt = f"Konverzace se zákazníkem ({name}), nejstarší nahoře:\n\n{transcript(messages)}\n\nNapiš další odpověď za mě."
    commented = data.comments_of(name)
    if commented:
        prompt += f"\n\nTenhle člověk dřív psal komentáře pod mými příspěvky (z toho víš, co chce – neptej se znovu):\n{commented}"
    if late:
        prompt += (" Zákazník čeká na odpověď déle než 24 hodin – první zpráva ať je krátká omluva za zpoždění"
                   " (např. „Dobrý den, omlouvám se za pozdější odpověď.“), pak normálně odpověz.")
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # jen předplatné, nikdy placené API
    proc = subprocess.run(
        [
            CONFIG["claudePath"], "-p", "--output-format", "json",
            "--model", CONFIG["model"], "--effort", CONFIG["effort"],
            "--system-prompt", system_prompt(),
            "--json-schema", json.dumps(REPLY_SCHEMA),
            "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
        ],
        input=prompt, capture_output=True, text=True, encoding="utf-8", cwd=tempfile.gettempdir(), env=env, timeout=180,
    )
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude: {(proc.stderr or proc.stdout)[:300]}")
    if out.get("is_error"):
        raise RuntimeError(f"claude: {out.get('result') or 'chyba'} (vyčerpaný limit předplatného?)")
    return out.get("structured_output") or {"action": "none", "messages": [], "handoff": True, "note": "Claude nevrátil odpověď – vyřiď ručně."}


# ---------- jedna konverzace ----------


def handle_thread(conv):
    name = conv["name"]
    t = thread(name)
    if t.get("blocked"):
        return
    open_chat(name)
    messages = read_messages()
    if not messages:
        log(name, "nepřečetl jsem zprávy", "zkontroluj `debug`")
        return
    try:
        data.save_conversation(name, messages, t["botSent"])
    except Exception as e:  # noqa: BLE001  – databáze nikdy nesmí shodit bota
        print("  (databáze:", e, ")", flush=True)
    last = messages[-1]

    if last["from"] == "me":
        from_bot = any(same_text(s, last["text"]) for s in t["botSent"])
        if not from_bot and last["text"] and time.time() * 1000 > t["pausedUntil"]:
            t.update(pausedUntil=(time.time() + CONFIG["pauseAfterManualReplyHours"] * 3600) * 1000, botReplies=0, handedOff=False)
            log(name, f"odpověděl jsi ručně – bot tu {CONFIG['pauseAfterManualReplyHours']} h mlčí")
        return

    fingerprint = norm(transcript(messages[-5:]))
    if t["handled"] == fingerprint:
        return
    if t["handedOff"]:
        return log(name, "nová zpráva, čeká na tebe (předáno)", last["text"])
    if time.time() * 1000 < t["pausedUntil"]:
        return log(name, "nová zpráva, bot pozastaven (píšeš ručně)", last["text"])
    if t["botReplies"] >= CONFIG["maxBotRepliesInRow"]:
        t["handedOff"] = True
        notify("Lampičky – převezmi konverzaci", f"{name}: bot už odpověděl {t['botReplies']}×")
        return log(name, "limit odpovědí bota – předáno tobě")

    # chvíli počkat, ať zákazník dopíše a nepůsobí to jako automat
    time.sleep(random.uniform(*CONFIG["replyDelaySeconds"]))
    messages = read_messages()
    if not messages or messages[-1]["from"] != "them":
        return

    answer = ask_claude(name, messages, conv.get("late", False))
    try:
        data.save_decision("messenger", name, transcript(messages[-10:]), answer, answer.get("handoff"), answer.get("note", ""))
    except Exception:  # noqa: BLE001
        pass
    t["handled"] = norm(transcript(messages[-5:]))
    msgs = [m.strip() for m in answer.get("messages") or [answer.get("message") or ""] if m.strip()]
    msg = " / ".join(msgs)
    if answer.get("action") == "block":
        if not SEND:
            return log(name, "NÁVRH: zablokovat (neprovedeno)", answer.get("note", ""))
        t["blocked"] = True  # bot mu už nikdy neodpoví, i kdyby blokování selhalo
        if block_contact(name):
            log(name, "ZABLOKOVÁN", answer.get("note", ""))
            notify("Lampičky – zablokováno", f"{name}: ptal se na autorská práva k logu")
        else:
            log(name, "BLOKOVÁNÍ SELHALO – zablokuj ručně", answer.get("note", ""))
            notify("Lampičky – zablokuj ručně", f"{name}: ptal se na autorská práva, blokování se nepovedlo")
        return
    if answer.get("action") == "reply" and msg:
        if SEND:
            for i, m in enumerate(msgs):
                if i:
                    time.sleep(random.uniform(3, 8))  # jako když člověk píše další zprávu
                send_message(m)
            photo = PHOTOS / Path(answer.get("photo") or "").name
            if answer.get("photo") and photo.is_file():
                time.sleep(random.uniform(3, 6))
                send_photo(photo)
                log(name, "odeslána fotka", photo.name)
            t["botSent"] = (t["botSent"] + msgs)[-20:]
            t["botReplies"] += 1
            log(name, "odesláno", msg)
        else:
            log(name, "NÁVRH (neodesláno)", msg)
    else:
        log(name, "bez odpovědi", answer.get("note", ""))
    if answer.get("handoff"):
        t["handedOff"] = SEND
        notify("Lampičky – potřebuju tě", f"{name}: {answer.get('note', '')}")
        log(name, "PŘEDÁNO TOBĚ", answer.get("note", ""))


# ---------- komentáře pod příspěvky ve skupinách ----------

COMMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "comment_reply": {"type": "string"},
        "send_dm": {"type": "boolean"},
        "dm": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["comment_reply", "send_dm", "dm", "note"],
    "additionalProperties": False,
}

COMMENT_PROMPT = """Spravuješ komentáře pod příspěvky prodejce ručně dělaných LED lampiček ve facebookových skupinách (rock, punk, kapely…). Příspěvky jsou osobní: „Pro bratra jsem vyrobil lampičku Tří sester 🙂 Kdyby někdo chtěl taky, klidně napište. Mohu udělat i jiné motivy“.
CÍL: dostat zájemce do soukromých zpráv. V komentářích se nic nedomlouvá.

Dostaneš jeden komentář (nebo odpověď na můj komentář). Vrať:
- comment_reply: krátká veřejná odpověď pod komentář (nebo "" = neodpovídat). Vykej („Napište mi zprávu a domluvíme se 🙂“); tykej jen tomu, kdo sám tyká nebo píše „Ahoj“/„Čau“ („Napiš mi zprávu…“). Kdo píše „Zdravím“, „Dobrý den“ nebo nejde poznat – vykej. Max 1 krátká věta. Emoji jen 🙂 (nebo žádné), nikdy jiné. Obměňuj, ať to nevypadá jako robot, např. „Napiš mi zprávu a domluvíme se 🙂“, „Napiš SZ a domluvíme se“, „Pošli zprávu, domluvíme se 🙂“, „Klidně napiš do zpráv 🙂“.
- send_dm: true, pokud má člověk o lampičku zájem (ptá se na cenu, chce ji, ptá se kde koupit, na jiný motiv…). Pak mu prodejce rovnou napíše do Messengeru, aby tam měl zprávu, až chat otevře.
- dm: úvodní zpráva do Messengeru (když send_dm). Oslovení stejně jako v comment_reply (vykat, tykat jen tomu, kdo tyká / „Ahoj“): např. „Dobrý den, lampička stojí 690 Kč + doprava 🙂 Jakou byste chtěl?“ Píšeš KRÁTCE – jedna věta + nejvýš jedna otázka, emoji jen 🙂: např. „Ahoj, lampička stojí 690 Kč + doprava 🙂 Jakou bys chtěl?“ Nevyjmenovávej dopravce ani parametry, pokud se na ně neptal. Když se ptá na něco konkrétního (barva, motiv, rozměr – sklo 15 × 15 cm, barvy světla se přepínají), odpověz na to krátce. Nepiš, odkud vezmeš logo. Nepiš, že ses dozvěděl z komentáře robotem apod.
- note: krátce pro prodejce, co se stalo.

PRAVIDLA:
- Dotaz na cenu („cena?", „kolik?", „za kolik?", „cena" …): v komentáři cenu NEPIŠ, jen ať napíše zprávu. send_dm=true.
- Jen když je někdo naštvaný nebo trvá na tom, ať je cena veřejně v komentářích (např. „napiš sem cenu, ať se každý nemusí ptát"): comment_reply přesně ve stylu „690, napiš zprávu 🙂" – jen číslo 690, NIC o dopravě ani dalším. send_dm=true.
- V komentáři NIKDY nepiš, že jsi mu už napsal / poslal zprávu / „mrkni do zpráv".
- Pochvala („pěkné", „super", „super“): krátké poděkování („Díky! 🙂") nebo nic; send_dm=false.
- Odpověď ve vlákně, kde člověk jen oznamuje (např. „jsem v Norsku do listopadu", „ozvu se"): krátce „OK, ozvi se pak 🙂", send_dm=false.
- Odpovědi musí být krátké, jako od člověka na mobilu.
- Označení kamaráda bez dalšího textu: nic, send_dm=false.
- Dotaz na autorská práva / licenci / povolení k logu: comment_reply="", send_dm=false.
- Hejt, trolling, spam, nesouvisející: comment_reply="", send_dm=false.
- Nikdy si nevymýšlej fakta. Fakta pro dm: cena 690 Kč + doprava (Zásilkovna nebo PPL), na lampičku jde udělat jakýkoliv motiv."""


def ask_claude_comment(item):
    kind = "Odpověď na můj komentář" if item["reply"] else "Komentář pod mým příspěvkem"
    if item.get("thread"):
        kind += f" (celé vlákno, nejstarší nahoře):\n{item['thread']}\n\nOdpovídáš na poslední"
    prompt = (f"{kind} od {item['name']}: „{item['text']}“\n(skupina: {item.get('group') or '?'};"
              f" můj příspěvek: ručně dělané LED lampičky s gravírovaným motivem)")
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    proc = subprocess.run(
        [
            CONFIG["claudePath"], "-p", "--output-format", "json",
            "--model", CONFIG["model"], "--effort", CONFIG["effort"],
            "--system-prompt", COMMENT_PROMPT, "--json-schema", json.dumps(COMMENT_SCHEMA),
            "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
        ],
        input=prompt, capture_output=True, text=True, encoding="utf-8", cwd=tempfile.gettempdir(), env=env, timeout=180,
    )
    out = json.loads(proc.stdout)
    if out.get("is_error") or not out.get("structured_output"):
        raise RuntimeError(f"claude: {out.get('result') or 'bez odpovědi'}")
    return out["structured_output"]


def check_comments():
    """Projde upozornění na komentáře pod mými příspěvky, otevře příspěvek a vyřídí komentáře, na které ještě
    nikdo neodpověděl (bot ani ty ručně). Odpovědi pod komentáře napíše hned, zprávy do Messengeru až potom."""
    import fb  # noqa: PLC0415

    fb.d = d
    done = state.setdefault("comments", {})
    seen = state.setdefault("commentNotifs", {})
    fb.open_notifications()
    notifs = fb.all_comment_notifications()
    # nová upozornění + nedávná znovu jednou za 6 h (pod příspěvek mohly přibýt odpovědi a FB upozornění nezmění);
    # co už je vyřízené, si bot pamatuje (comments), takže nikomu neodpoví dvakrát
    stale = lambda k: not isinstance(seen.get(k), (int, float)) or time.time() - seen[k] > 6 * 3600
    todo = [n for i, n in enumerate(notifs) if n["key"] not in seen or (i < 6 and stale(n["key"]))]
    for notif in todo:
        fb.open_notification(notif["key"])
        all_comments = fb.read_post_comments()
        try:
            for c in all_comments:
                if c["name"] != fb.ME:
                    data.save_seen_comment(notif["group"], c["name"], c["text"], c["indent"])
        except Exception:  # noqa: BLE001
            pass
        if not all_comments:  # nic nenačteno – radši zkusit příště, než to označit jako vyřízené
            log(notif["group"] or "komentáře", "pod příspěvkem jsem nic nenačetl, zkusím příště", notif["key"])
            continue
        comments = [c for c in all_comments
                    if c["name"] != fb.ME and not c["answered"] and not fb.too_old(c.get("age"))
                    and (not c["indent"] or c.get("toMe"))  # komentáře + odpovědi určené Martinovi
                    and f"{c['name']}|{c['text'][:80]}" not in done]
        merged = {}  # víc komentářů od jednoho člověka = jedna odpověď a jedna zpráva
        for c in comments:
            if c["name"] in merged:
                merged[c["name"]]["text"] += " / " + c["text"]
                merged[c["name"]].setdefault("also", []).append(f"{c['name']}|{c['text'][:80]}")
            else:
                merged[c["name"]] = dict(c, first=c["text"])
        dms, failed = [], False
        # 1) rozmyslet odpovědi na všechny komentáře (telefon mezitím stojí na příspěvku)
        plan = []
        for c in merged.values():
            name = c["name"]
            item = {"name": name, "text": c["text"], "reply": c["indent"], "group": notif["group"], "key": notif["key"],
                    "thread": thread_context(all_comments, c)}
            try:
                answer = ask_claude_comment(item)
            except Exception as e:  # noqa: BLE001
                failed = True
                comment_error(name, c["text"], e)
                continue
            try:
                data.save_comment(notif["group"], name, c["text"], c["indent"], answer["comment_reply"].strip(),
                                  answer["dm"].strip() if answer["send_dm"] else "", answer.get("note", ""))
                data.save_decision("comment", name, item.get("thread") or c["text"], answer, False, answer.get("note", ""))
            except Exception:  # noqa: BLE001
                pass
            plan.append((c, answer))
        # 2) odpovědět na místě, odspodu nahoru (po přečtení je bot dole a řazení je „Nejnovější“ → dole nejstarší)
        pos = lambda c: next((i for i, x in enumerate(all_comments) if (x["name"], x["text"]) == (c["name"], c["first"])), 0)
        plan.sort(key=lambda ca: -pos(ca[0]))
        for c, answer in plan:
            name, ckey = c["name"], f"{c['name']}|{c['first'][:80]}"
            reply, dm = answer["comment_reply"].strip(), answer["dm"].strip()
            mark = lambda answer=answer, c=c, ckey=ckey: [done.__setitem__(k, answer.get("note", "")) for k in [ckey] + c.get("also", [])] and save_state()
            if not SEND:
                log(name, "KOMENTÁŘ – NÁVRH (neodesláno)", f"„{c['text']}“ → {reply or '—'} | DM: {dm if answer['send_dm'] else '—'}")
                continue
            t = thread(name)
            if answer["send_dm"] and dm and not t.get("dmFromComment") and not t.get("blocked") and not t.get("handled"):
                dms.append((c, dm))
            if not reply:
                mark()
                log(name, "komentář bez veřejné odpovědi", f"„{c['text']}“ – {answer.get('note', '')}")
                continue
            try:
                try:
                    fb.reply_to_comment(name, c["first"], reply)
                except RuntimeError:
                    fb.open_notification(notif["key"])  # stránka se mezitím rozhodila – jednou znovu otevřít
                    fb.sort_newest()
                    fb.reply_to_comment(name, c["first"], reply)
                mark()  # vyřízeno až po odeslání – při chybě to bot zkusí příště znovu
                log(name, "komentář – odpověděl jsem", f"„{c['text']}“ → {reply}")
                time.sleep(random.uniform(4, 10))
            except Exception as e:  # noqa: BLE001
                failed = True  # upozornění neoznačit jako vyřízené – příště se komentář zkusí znovu
                if is_connection_error(e):
                    raise
                comment_error(name, c["text"], e)
        retry = state.setdefault("dmRetry", [])
        retry += [{"key": notif["key"], "name": c["name"], "first": c["first"], "dm": dm, "tries": 0} for c, dm in dms]
        if SEND and not failed:
            seen[notif["key"]] = time.time()
            save_state()
    send_comment_dms(fb)


def send_comment_dms(fb):
    """Zprávy do Messengeru lidem z komentářů (i ty, které minule selhaly – nejvýš 3 pokusy)."""
    retry = state.setdefault("dmRetry", [])
    for job in list(retry):
        name = job["name"]
        t = thread(name)
        if t.get("dmFromComment") or t.get("blocked"):
            retry.remove(job)
            continue
        try:
            time.sleep(random.uniform(20, 60))
            fb.open_notification(job["key"])
            result = fb.message_commenter(name, job["first"], job["dm"])
            t["dmFromComment"] = True
            retry.remove(job)
            if result == "existing":
                log(name, "komentář – s člověkem už máme chat, zprávu neposílám")
            else:
                t["botSent"] = (t["botSent"] + [job["dm"]])[-20:]
                t["botReplies"] += 1
                log(name, "komentář – poslal jsem zprávu", job["dm"])
        except Exception as e:  # noqa: BLE001
            if is_connection_error(e):
                raise
            job["tries"] += 1
            if job["tries"] >= 3:
                retry.remove(job)
                comment_error(name, job["first"], e)
            else:
                log(name, f"zpráva z komentáře nevyšla ({job['tries']}×), zkusím příště", str(e))
        save_state()


def thread_context(all_comments, c):
    """Celé vlákno, do kterého komentář patří (původní komentář + odpovědi až po tenhle), pro Clauda."""
    i = next((k for k, x in enumerate(all_comments) if x is c or (x["name"], x["text"]) == (c["name"], c.get("first", c["text"]))), None)
    if i is None or not c["indent"]:
        return ""
    start = i
    while start > 0 and all_comments[start]["indent"]:
        start -= 1
    return "\n".join(f"{'  ' if x['indent'] else ''}{'Já' if x['name'] == 'Martin Hejduk' else x['name']}: {x['text']}"
                     for x in all_comments[start:i + 1])


def comment_error(name, text, e):
    log(name, "KOMENTÁŘ – CHYBA, vyřiď ručně", f"„{text}“: {e}")
    zasek("Facebook – komentář/zpráva z komentáře", str(e), e)
    with open(HERE / "errors.log", "a", encoding="utf-8") as f:
        f.write(f"\n=== {datetime.now():%d. %m. %Y %H:%M:%S} – komentář od {name}: „{text}“\n")
        f.write("".join(traceback.format_exception(type(e), e, e.__traceback__)))
    notify("Lampičky – komentář vyřiď ručně", f"{name}: „{text}“"[:200])


def facebook_comment_notifications():
    """Otisky upozornění aplikace Facebook, které vypadají jako komentář/odpověď."""
    out = subprocess.run(["adb", "shell", "dumpsys", "notification", "--noredact"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30).stdout
    found = set()
    for block in out.split("NotificationRecord(")[1:]:
        if "pkg=com.facebook.katana " not in block.split("\n", 1)[0]:
            continue
        text = re.search(r"android\.text=\S+ \((.*)\)", block)
        if text and re.search(r"okomentoval|odpověděl|komentář", text.group(1)):
            found.add(text.group(1))
    return found


# ---------- fronta příspěvků do skupin (fronta.json) ----------

QUEUE_FILE = HERE / "fronta.json"


QUESTIONS_SCHEMA = {
    "type": "object",
    "properties": {"skip": {"type": "boolean"}, "answers": {"type": "array", "items": {"type": "string"}}, "reason": {"type": "string"}},
    "required": ["skip", "answers", "reason"],
    "additionalProperties": False,
}


def answer_group_questions(texts, fields):
    """Claude vyhodnotí formulář s otázkami skupiny (texty z obrazovky) a navrhne odpovědi podle o_mne.md."""
    about = (HERE / "o_mne.md").read_text(encoding="utf-8")
    system = f"""Pomáháš se žádostí o členství ve facebookové skupině. Dostaneš texty z obrazovky formuláře (otázky, pravidla, tlačítka)
a počet textových polí. Vrať answers = odpovědi na otázky v pořadí shora dolů (jedna na každé textové pole), česky, krátce, přirozeně, jako člověk.
Odpovídej pravdivě jen podle informací O MNĚ. Nikdy si nevymýšlej (třeba jakou mám motorku, kde bydlím, kolik mi je).
skip=true (a do reason napiš proč) když: na otázku nejde pravdivě odpovědět z O MNĚ, pravidla zakazují prodej / reklamu / nabízení výrobků,
skupina je bazar/inzerce, nebo formuláři nerozumíš. Souhlas s pravidly bot zaškrtne sám.

O MNĚ:
{about}"""
    prompt = f"Texty na obrazovce:\n" + "\n".join(f"- {t}" for t in texts[:80]) + f"\n\nPočet textových polí: {fields}"
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    proc = subprocess.run(
        [CONFIG["claudePath"], "-p", "--output-format", "json", "--model", CONFIG["model"], "--effort", CONFIG["effort"],
         "--system-prompt", system, "--json-schema", json.dumps(QUESTIONS_SCHEMA),
         "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence"],
        input=prompt, capture_output=True, text=True, encoding="utf-8", cwd=tempfile.gettempdir(), env=env, timeout=180,
    )
    out = json.loads(proc.stdout).get("structured_output") or {"skip": True, "answers": [], "reason": "Claude nevrátil odpověď"}
    zasek("Otázky při přidání do skupiny" + (" – přeskočeno" if out["skip"] else " – vyplněno"),
          f"{out.get('reason', '')} | odpovědi: {out.get('answers')}")  # uložit ke kontrole
    return out


def run_queue():
    """Provede nejvýš jednu splatnou úlohu z fronty: přidat se do skupiny / zveřejnit příspěvek."""
    if not QUEUE_FILE.exists():
        return False
    jobs = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    now = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    posts_today = state.setdefault("postsByDay", {}).get(today, 0)
    if posts_today >= CONFIG.get("maxPostsPerDay", 4):
        return False  # denní limit příspěvků z profilu vyčerpán – pokračuje se zítra
    job = next((j for j in jobs if j["status"] in ("ceka", "ceka-na-clenstvi") and j.get("due", 0) <= now), None)
    if not job:
        return False
    import fb  # noqa: PLC0415

    fb.d = d
    try:
        status, shot = fb.join_or_post(job["group"], job["members"], [str(HERE / p) for p in job.get("photos") or [job["photo"]]], job["text"],
                                       answerer=answer_group_questions)
    except Exception as e:  # noqa: BLE001
        if is_connection_error(e):
            raise  # výpadek telefonu – příspěvek se zkusí znovu, nepočítá se jako nepovedený pokus
        status, shot = "chyba", None
        zasek(f"Facebook – příspěvek do skupiny", f"{job['group']}: {e}", e)  # dřív, než se příspěvek zahodí
        job["error"] = str(e)
        job["tries"] = job.get("tries", 0) + 1
        try:
            fb.discard_composer()  # nedokončený příspěvek zahodit, ať se nezveřejní napůl
        except Exception:  # noqa: BLE001
            pass
    job["last"] = datetime.now().strftime("%d. %m. %H:%M")
    if status in ("posted", "posted-pending"):
        state["postsByDay"][today] = posts_today + 1
        save_state()
        job["status"] = "zverejneno" if status == "posted" else "ceka-na-schvaleni-prispevku"
        job["screenshot"] = str(shot)
        gap = CONFIG.get("postGapMinutes", [20, 40])
        for j in jobs:  # další příspěvek nejdřív za 20–40 min
            if j["status"] == "ceka":
                j["due"] = max(j.get("due", 0), time.time() + random.uniform(*gap) * 60)
        log(job["group"], "PŘÍSPĚVEK ZVEŘEJNĚN" if status == "posted" else "PŘÍSPĚVEK ČEKÁ NA SCHVÁLENÍ ADMINEM", job["text"])
    elif status == "no-permission":
        job["status"] = "nelze-postovat"
        log(job["group"], "ve skupině nemáme oprávnění zveřejňovat – vyřazeno z fronty")
        notify("Lampičky – skupina vyřazena", f"{job['group']}: nemáme oprávnění zveřejňovat příspěvky")
    elif status == "skipped-questions":
        job["status"] = "otazky-vyres-rucne"
        job["error"] = fb.LAST_SKIP_REASON
        log(job["group"], "otázky ke členství – bot neodpověděl, vyřiď ručně", fb.LAST_SKIP_REASON)
        notify("Lampičky – otázky ke skupině", f"{job['group']}: {fb.LAST_SKIP_REASON}"[:200])
    elif status == "sell-group":
        job["status"] = "preskoceno-bazar"
        log(job["group"], "bazarová skupina (jen inzeráty s cenou) – přeskakuju")
    elif status in ("pending", "none"):
        job["status"] = "ceka-na-clenstvi"
        job["checks"] = job.get("checks", 0) + 1
        if job["checks"] > 12:
            job["status"] = "vzdano-nepřijali"
        job["due"] = time.time() + 3 * 3600  # členství zkontrolovat znovu za 3 h
        for j in jobs:  # mezi přidáváním do skupin taky pauza
            if j["status"] == "ceka":
                j["due"] = max(j.get("due", 0), time.time() + random.uniform(3, 8) * 60)
        log(job["group"], "žádost o členství odeslána/čeká – zkusím za 3 h")
    else:
        job["status"] = "ceka" if job["tries"] < 3 else "chyba"
        job["due"] = time.time() + 30 * 60
        log(job["group"], "PŘÍSPĚVEK – CHYBA", job.get("error", ""))
        if job["status"] == "chyba":
            notify("Lampičky – příspěvek se nepovedl", f"{job['group']}: {job.get('error', '')}"[:200])
    QUEUE_FILE.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


# ---------- hlavní smyčka ----------


def check_list(source, opener):
    opener()
    convs = read_list()
    if not state["initialized"] and not CATCH_UP:
        for c in convs:
            thread(c["name"])["preview"] = c["preview"]
        return len(convs)
    for c in convs:
        t = thread(c["name"])
        if t["preview"] == c["preview"]:
            continue
        t["preview"] = c["preview"]
        try:
            handle_thread(c)
        except Exception as e:  # noqa: BLE001
            if is_connection_error(e):
                t["preview"] = ""
                raise  # výpadek telefonu není chyba konverzace – vyřeší hlavní smyčka
            t["errors"] = t.get("errors", 0) + 1
            log(c["name"], "CHYBA", str(e))
            zasek("Messenger – chyba v konverzaci", str(e), e)
            with open(HERE / "errors.log", "a", encoding="utf-8") as f:
                f.write(f"\n=== {datetime.now():%d. %m. %Y %H:%M:%S} – konverzace {c['name']}, chyba č. {t['errors']}\n")
                f.write("".join(traceback.format_exception(type(e), e, e.__traceback__)))
            if t["errors"] >= 3:  # pořád to padá – nechat na tobě, ať se bot necyklí
                t.update(handedOff=True, errors=0)
                log(c["name"], "PŘEDÁNO TOBĚ – opakovaná chyba")
                notify("Lampičky – převezmi konverzaci", f"{c['name']}: bot tu opakovaně padá ({e})"[:200])
            else:
                t["preview"] = ""  # zkusit znovu při další kontrole
        else:
            t["errors"] = 0
        save_state()
        opener()  # vrátit se na seznam
    return len(convs)


def main():
    connect()
    if MODE == "debug":
        go_home()
        convs = read_list()
        print("\nCHATY:", json.dumps(convs, ensure_ascii=False, indent=1))
        if convs:
            open_chat(convs[0]["name"])
            print(f"\nZPRÁVY v „{convs[0]['name']}“ (them = zákazník, me = já):")
            print(json.dumps(read_messages(), ensure_ascii=False, indent=1))
        for tab in ("MOŽNÁ ZNÁTE", "SPAM"):
            open_requests(tab)
            print(f"\nŽÁDOSTI – {tab}:", json.dumps(read_list(), ensure_ascii=False, indent=1))
        go_home()
        return

    print("Bot běží a ODPOVÍDÁ. Zastavíš ho Ctrl+C." if SEND else "Zkušební režim – odpovědi jen vypisuju, nic neposílám.", flush=True)
    rnd = lambda key, default: random.uniform(*CONFIG.get(key, default)) * 60
    next_chats = 0  # hned po startu jedna kontrola všeho
    next_requests = 0
    seen_notifs = messenger_notifications()
    seen_fb = facebook_comment_notifications()
    next_comments = 0  # hned po startu jednou projít komentáře
    failures = 0
    offline_since = None
    while True:
        try:
            # bez internetu v telefonu nechodí zprávy ani nejde nic odeslat – počkat a dát vědět
            if not phone_online():
                if offline_since is None:
                    offline_since = time.time()
                    print(f"[{datetime.now():%H:%M:%S}] TELEFON JE BEZ INTERNETU – čekám", flush=True)
                    log("telefon", "BEZ INTERNETU")
                    subprocess.run(["adb", "shell", "svc", "wifi", "enable"], capture_output=True)  # kdyby se Wi-Fi vypnula
                if time.time() - offline_since > 120 and not getattr(main, "offline_notified", False):
                    notify("Lampičky – telefon je bez internetu", "Bot nemůže číst ani posílat zprávy. Zkontroluj Wi-Fi / hotspot.")
                    main.offline_notified = True
                time.sleep(60)
                continue
            if offline_since is not None:
                print(f"[{datetime.now():%H:%M:%S}] internet je zpátky (výpadek {round((time.time() - offline_since) / 60)} min) – projdu všechno", flush=True)
                log("telefon", "internet zpátky")
                offline_since, main.offline_notified = None, False
                next_chats = next_requests = next_comments = 0  # dohnat, co mezitím přišlo
            now = time.time()
            notifs = messenger_notifications()
            new = notifs - seen_notifs
            seen_notifs = notifs
            if new:
                print(f"[{datetime.now():%H:%M:%S}] upozornění: {'; '.join(sorted(new))[:150]}", flush=True)
            quiet = in_quiet_hours()
            if new or (now >= next_chats and not quiet):
                time.sleep(random.uniform(3, 10))  # jako člověk, co vezme telefon do ruky
                n = check_list("chaty", go_home)
                next_chats = time.time() + rnd("fullCheckEveryMinutes", [20, 40])
                if not state["initialized"] or (time.time() >= next_requests and not quiet):
                    for tab in ("MOŽNÁ ZNÁTE", "SPAM"):
                        n += check_list(tab, lambda tab=tab: open_requests(tab))
                    next_requests = time.time() + rnd("requestsEveryMinutes", [60, 90])
                if not state["initialized"]:
                    state["initialized"] = True
                    save_state()
                    print(f"Zapamatováno {n} konverzací. Odpovídat budu jen na nové zprávy.", flush=True)
                leave_app()
                seen_notifs = messenger_notifications()  # upozornění, která jsme právě vyřídili
            fb_now = facebook_comment_notifications()
            fb_new = fb_now - seen_fb
            seen_fb = fb_now
            if fb_new or (time.time() >= next_comments and not quiet):
                if fb_new:
                    print(f"[{datetime.now():%H:%M:%S}] Facebook: {'; '.join(sorted(fb_new))[:150]}", flush=True)
                time.sleep(random.uniform(3, 10))
                check_comments()
                next_comments = time.time() + rnd("commentsEveryMinutes", [30, 60])
                leave_app()
                seen_fb = facebook_comment_notifications()
                seen_notifs = messenger_notifications()
            if not quiet and run_queue():  # příspěvky do skupin (fronta.json), max 4 za den
                leave_app()
                seen_fb = facebook_comment_notifications()
                seen_notifs = messenger_notifications()
            failures = 0
            time.sleep(CONFIG.get("notificationCheckSeconds", 20))
        except Exception as e:  # noqa: BLE001
            if is_connection_error(e) and wait_for_phone(120):
                # krátký výpadek USB / restart uiautomatoru – telefon je zpátky, pokračovat hned
                print(f"[{datetime.now():%H:%M:%S}] telefon byl chvíli odpojený, pokračuju", flush=True)
                try:
                    connect()
                    leave_app()
                    continue
                except Exception:  # noqa: BLE001
                    pass
            failures += 1
            wait = min(60 * 2 ** (failures - 1), 30 * 60)  # 1, 2, 4… max 30 min – žádné zběsilé opakování
            report_error(e, failures, wait)
            time.sleep(wait)
            try:
                connect()
                leave_app()
            except Exception:  # noqa: BLE001
                pass


def phone_online():
    """Má telefon internet? (ping na 8.8.8.8, pro jistotu i 1.1.1.1)"""
    for host in ("8.8.8.8", "1.1.1.1"):
        out = subprocess.run(["adb", "shell", "ping", "-c", "1", "-W", "3", host], capture_output=True, text=True, timeout=20).stdout
        if " 1 received" in out:
            return True
    return False


def is_connection_error(e):
    text = f"{type(e).__name__} {e}"
    return any(x in text for x in ("not found", "RemoteDisconnected", "Connection", "offline", "AdbError", "not online"))


def wait_for_phone(seconds):
    """Počká, až se telefon znovu objeví v adb (krátké výpadky USB). True = je zpátky."""
    end = time.time() + seconds
    while time.time() < end:
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
        if re.search(r"^\S+\s+device$", out, re.M):
            time.sleep(3)
            return True
        time.sleep(3)
    return False


def in_quiet_hours():
    start, end = CONFIG.get("quietHours", [23, 7])
    h = datetime.now().hour
    return (h >= start or h < end) if start > end else start <= h < end


def report_error(e, failures, wait):
    """Chybu zapíše do errors.log i s tím, co bylo na obrazovce, a při opakování dá vědět na Mac."""
    stamp = datetime.now()
    try:
        screen = " | ".join((n[1] or n[2])[:60] for n in nodes() if n[1] or n[2])[:1500]
        app = d.app_current().get("activity", "")
    except Exception:  # noqa: BLE001
        screen, app = "(obrazovku nešlo přečíst)", ""
    if "not found" not in str(e):  # odpojený telefon není problém s ovládáním
        zasek("Obecná chyba bota", str(e), e)
    with open(HERE / "errors.log", "a", encoding="utf-8") as f:
        f.write(f"\n=== {stamp:%d. %m. %Y %H:%M:%S} – chyba č. {failures} za sebou, další pokus za {wait // 60} min\n")
        f.write(f"aktivita: {app}\nobrazovka: {screen}\n")
        f.write("".join(traceback.format_exception(type(e), e, e.__traceback__)))
    print(f"[{stamp:%H:%M:%S}] CHYBA ({failures}× za sebou): {e} – zkusím znovu za {wait // 60} min, detail v errors.log", flush=True)
    if failures in (3, 6):
        notify("Lampičky – bot se zasekl", f"{failures}× za sebou: {str(e)[:120]}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        save_state()
        print("\nBot zastaven.")
