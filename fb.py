"""Ovládání aplikace Facebook v telefonu: otevřít skupinu a zveřejnit v ní příspěvek s fotkou.

  .venv/bin/python -W ignore fb.py post "Rock&metal-Super zábava 1." "13" posty/<motiv>/3.jpg "text příspěvku" [--jen-pripravit]

Telefon nesmí zároveň ovládat bot_telefon.py – před spuštěním ho zastav (nebo použij frontu v botovi).
"""

import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import uiautomator2 as u2

HERE = Path(__file__).resolve().parent
ADB = str(Path.home() / ".local" / "android" / "platform-tools" / "adb")
FB = "com.facebook.katana"
d = None


def connect():
    global d
    d = u2.connect()
    d.screen_on()
    return d


def bounds(n):
    return list(map(int, re.findall(r"\d+", n.get("bounds"))))


def nodes():
    return [n for n in ET.fromstring(d.dump_hierarchy()).iter("node") if n.get("package") == FB]


def click_desc(desc, timeout=10, **kw):
    sel = d(description=desc, **kw)
    if not sel.wait(timeout=timeout):
        sel = d(text=desc, **kw)
        if not sel.wait(timeout=2):
            raise RuntimeError(f"Facebook: nenašel jsem „{desc}“")
    sel.click()


def discard_composer():
    """Rozepsaný příspěvek (třeba po pádu) zahodit – nikdy ho nezveřejnit."""
    if "Composer" not in (d.app_current().get("activity") or ""):
        return False
    d.press("back")
    time.sleep(3)
    sel = d(description="Zahodit příspěvek")
    if not sel.exists:
        sel = d(text="Zahodit příspěvek")
    if sel.exists:
        sel.click()
    else:
        d.press("back")
    time.sleep(3)
    return True


def home():
    """Facebook nastartovaný načisto, hlavní stránka."""
    d.app_start(FB, stop=True)
    time.sleep(8)
    for _ in range(5):
        if d(description="Hledat").exists:
            return
        if discard_composer():
            continue
        d.press("back")
        time.sleep(1)
    raise RuntimeError("Facebook: nenačetla se hlavní stránka")


def open_group(name, members):
    """Najde skupinu přes hledání (jméno + počet členů, např. „13“ z „13 tis.“) a otevře ji."""
    home()
    click_desc("Hledat")
    time.sleep(3)
    d(className="android.widget.EditText").set_text(name)
    time.sleep(1)
    d.press("enter")
    time.sleep(6)
    d(descriptionStartsWith="Výsledky vyhledávání pro Skupiny").click()
    time.sleep(6)
    for _ in range(6):
        for n in nodes():
            t = (n.get("content-desc") or "").replace("\xa0", " ")
            x1, y1, x2, y2 = bounds(n)
            if n.get("class", "").endswith("Button") and t.startswith(name + ",") and members in t and y2 - y1 > 100:
                d.click(540, (y1 + y2) // 2)
                time.sleep(7)
                return
        d.swipe(540, 1900, 540, 700, 0.3)
        time.sleep(3)
    raise RuntimeError(f"Facebook: skupinu „{name}“ jsem nenašel")


def push_photo(path):
    """Nahraje fotku do telefonu jako nejnovější (v galerii bude první)."""
    dest = f"/sdcard/Pictures/Lampicky/{datetime.now():%Y%m%d-%H%M%S}-{Path(path).parent.name}-{Path(path).name}"
    subprocess.run([ADB, "push", str(path), dest], check=True, capture_output=True)
    subprocess.run([ADB, "shell", "touch", dest], capture_output=True)
    subprocess.run([ADB, "shell", "am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE", "-d", f"file://{dest}"], capture_output=True)
    time.sleep(3)


def compose(text, photos):
    """Ve skupině otevře nový příspěvek, vloží text a fotky (jedna cesta nebo seznam). Nezveřejňuje."""
    photos = [photos] if isinstance(photos, (str, Path)) else list(photos)
    for p in reversed(photos):  # poslední nahraná = v galerii první → pořadí v příspěvku jako v seznamu
        push_photo(p)
        time.sleep(1.5)
    click_desc("Napište něco…")
    time.sleep(5)
    d(className="android.widget.EditText").set_text(text) if d(className="android.widget.EditText").exists else d(className="android.widget.AutoCompleteTextView").set_text(text)
    time.sleep(2)
    if d(description="Galerie").exists is False:
        d.press("back")  # schovat klávesnici
        time.sleep(2)
    if d(description="Hotovo").exists:  # editor textu na pozadí
        click_desc("Hotovo")
        time.sleep(3)
    click_desc("Galerie")
    time.sleep(5)
    if d(description="Povolit přístup").exists:
        subprocess.run([ADB, "shell", "pm", "grant", FB, "android.permission.READ_MEDIA_IMAGES"], capture_output=True)
        click_desc("Povolit přístup")
        time.sleep(4)
    for i in range(1, len(photos) + 1):
        d(className="android.widget.CompoundButton", descriptionStartsWith=f"Fotka, položka {i},").click()
        time.sleep(1.5)
    click_desc("Další")
    time.sleep(6)
    if not d(description="Přidat", className="android.widget.Button").exists:
        raise RuntimeError("Facebook: příspěvek se nepřipravil (chybí tlačítko Přidat)")


def publish():
    d(description="Přidat", className="android.widget.Button").click()
    time.sleep(10)
    for _ in range(36):  # fotky se nahrávají („Zveřejňování… Facebook nechte otevřený“) – počkat max 3 min
        if not (d(textStartsWith="Zveřejňování").exists or d(descriptionStartsWith="Zveřejňování").exists):
            break
        time.sleep(5)
    time.sleep(3)


def screenshot(name):
    out = HERE / "posty" / "screeny"
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"{datetime.now():%Y%m%d-%H%M%S}-{name}.png"
    f.write_bytes(subprocess.run([ADB, "exec-out", "screencap", "-p"], capture_output=True).stdout)
    return f


def membership():
    """Na otevřené skupině: 'member' / 'pending' / 'none'."""
    descs = " | ".join((n.get("content-desc") or n.get("text") or "") for n in nodes())
    if "Přidal(a) se" in descs or "Napište něco…" in descs:
        return "member"
    if "Zrušit žádost" in descs:
        return "pending"
    return "none"


def join_open_group():
    """Na otevřené skupině klepne Přidat se. Otázky/pravidla nevyplňuje (zavře je). Vrací stav členství."""
    for sel in (d(description="Přidat se", className="android.widget.Button"), d(descriptionStartsWith="Přidat se do skupiny"), d(text="Přidat se")):
        if sel.exists:
            sel.click()
            time.sleep(6)
            break
    if d(description="Zavřít").exists and (d(textContains="Otázky").exists or d(descriptionContains="Otázky").exists or d(descriptionContains="Pravidla").exists):
        d(description="Zavřít").click()
        time.sleep(3)
    return membership()


def join_or_post(group, members, photos, text):
    """Otevře skupinu; když nejsem člen, přidá se. Když jsem člen, zveřejní příspěvek.
    Vrací (stav, screenshot): 'posted' / 'posted-pending' (čeká na schválení adminem) / 'pending' / 'none'."""
    open_group(group, members)
    state = membership()
    if state == "none":
        state = join_open_group()
    if state != "member":
        return state, None
    if not d(description="Napište něco…").exists and (d(description="Co prodáváte?").exists or d(text="Co prodáváte?").exists):
        return "sell-group", None  # bazarová skupina – jen inzeráty s cenou, tam nepostujeme
    compose(text, photos)
    publish()
    shot = screenshot("zverejneno")
    descs = " | ".join((n.get("content-desc") or n.get("text") or "") for n in nodes())
    return ("posted-pending" if "Ke schválení" in descs or "odpovědí se správci" in descs else "posted"), shot


def post(group, members, photo, text, only_prepare=False):
    connect()
    open_group(group, members)
    compose(text, photo)
    shot = screenshot("pripraveno")
    if only_prepare:
        return shot
    publish()
    return screenshot("zverejneno")


# ---------- komentáře pod mými příspěvky ----------

COMMENT_RE = re.compile(r"^(?P<name>.+?) (?P<kind>okomentoval\(a\)|okomentoval|okomentovala|odpověděl\(a\)|odpověděl|odpověděla)\b.*?„(?P<text>.*?)“")


def open_notifications():
    home()
    d(descriptionStartsWith="Upozornění").click()
    time.sleep(5)


def comment_notifications():
    """Komentáře/odpovědi v kartě Upozornění: [{key, name, text, reply, bounds}]."""
    out = []
    for n in nodes():
        t = (n.get("content-desc") or "").replace("\xa0", " ")
        if not n.get("class", "").endswith("Button") or "„" not in t or t.startswith("Spravovat"):
            continue
        m = COMMENT_RE.search(t)
        if m:
            out.append({"key": re.sub(r"\s+", " ", t)[:160], "name": m["name"].strip(), "text": m["text"].strip(),
                        "reply": m["kind"].startswith("odpověd"), "bounds": bounds(n)})
    return out


def _find_comment(name, text):
    """Na otevřeném příspěvku najde komentář (jméno + začátek textu), vrací jeho bounds."""
    snippet = re.sub(r"\W+", "", text[:25]).lower()
    for _ in range(6):
        for n in nodes():
            t = n.get("content-desc") or n.get("text") or ""
            if name in t and snippet and snippet in re.sub(r"\W+", "", t).lower():
                return bounds(n)
        d.swipe(540, 1700, 540, 1000, 0.3)
        time.sleep(2)
    return None


def reply_to_comment(name, text, reply):
    """Na otevřeném příspěvku odpoví pod komentářem daného člověka."""
    b = _find_comment(name, text)
    if not b:
        raise RuntimeError(f"komentář od {name} jsem na příspěvku nenašel")
    best = None  # tlačítko Odpovědět nejblíž pod komentářem
    for n in nodes():
        t = (n.get("content-desc") or n.get("text") or "").strip()
        if t == "Odpovědět" or t.startswith("Odpovědět na komentář"):
            y = bounds(n)[1]
            if b[1] - 10 <= y <= b[3] + 250 and (best is None or y < bounds(best)[1]):
                best = n
    if best is None:
        raise RuntimeError(f"u komentáře od {name} chybí tlačítko Odpovědět")
    x1, y1, x2, y2 = bounds(best)
    d.click((x1 + x2) // 2, (y1 + y2) // 2)
    time.sleep(3)
    box = d(className="android.widget.AutoCompleteTextView")
    if not box.exists:
        box = d(className="android.widget.EditText")
    box.set_text(reply)
    time.sleep(2)
    click_desc("Odeslat")
    time.sleep(5)


def message_commenter(name, text, message):
    """Z komentáře otevře profil člověka → Zpráva → pošle zprávu v Messengeru."""
    b = _find_comment(name, text)
    target = None
    for n in nodes():
        if (n.get("content-desc") or n.get("text") or "") == name and n.get("clickable") == "true":
            target = n
            if b and abs(bounds(n)[1] - b[1]) < 300:
                break
    if target is None:
        raise RuntimeError(f"jméno {name} u komentáře nejde otevřít")
    x1, y1, x2, y2 = bounds(target)
    d.click((x1 + x2) // 2, (y1 + y2) // 2)
    time.sleep(7)
    for label in ("Zpráva", "Poslat zprávu", "Napsat zprávu"):
        if d(description=label).exists or d(text=label).exists:
            click_desc(label)
            break
    else:
        raise RuntimeError(f"na profilu {name} není tlačítko Zpráva")
    time.sleep(8)
    if d.app_current().get("package") != "com.facebook.orca":
        raise RuntimeError("po klepnutí na Zpráva se neotevřel Messenger")
    box = d(className="android.widget.EditText")
    if not box.wait(timeout=10):
        raise RuntimeError("v Messengeru chybí pole pro zprávu")
    box.click()
    box.set_text(message)
    time.sleep(1)
    d(description="Poslat").click()
    time.sleep(3)
    d.press("back")


if __name__ == "__main__":
    if len(sys.argv) >= 6 and sys.argv[1] == "post":
        print(post(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], "--jen-pripravit" in sys.argv))
    else:
        print(__doc__)
