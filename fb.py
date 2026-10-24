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

import psani  # psaní po znacích s náhodnými pauzami (jako člověk)

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
    d.app_start(FB, ".LoginActivity", stop=True)  # s aktivitou = bez „monkey“, který zapíná otáčení
    for _ in range(5):
        if d(descriptionMatches="^(Hledat|Search)$").wait(timeout=20):  # FB se někdy načítá pomalu; občas je anglicky
            return
        if d.app_current().get("package") != FB:  # „zpět“ nás vyhodilo z aplikace – znovu otevřít
            d.app_start(FB, ".LoginActivity")
            continue
        if discard_composer():
            continue
        d.press("back")
        time.sleep(1)
    raise RuntimeError("Facebook: nenačetla se hlavní stránka")


def open_group(name, members):
    """Najde skupinu přes hledání (jméno + počet členů, např. „13“ z „13 tis.“) a otevře ji."""
    home()
    for _ in range(3):  # klepnutí na lupu občas nezabere (stránka se ještě načítá / zrovna se překresluje)
        try:
            d(descriptionMatches="^(Hledat|Search)$").click()
        except Exception:  # noqa: BLE001  – StaleObjectException: prvek se mezitím překreslil
            time.sleep(2)
            continue
        if d(className="android.widget.EditText").wait(timeout=8):
            break
        time.sleep(2)
    else:
        raise RuntimeError("Facebook: neotevřelo se hledání")
    time.sleep(1)
    d(className="android.widget.EditText").click()
    psani.human_type(d, name)
    time.sleep(1)
    d.press("enter")
    time.sleep(6)
    tab = d(descriptionMatches="^(Výsledky vyhledávání pro Skupiny|Search results for Groups).*")
    if tab.wait(timeout=10):
        tab.click()
    time.sleep(6)
    # rozhoduje název; počet členů se mění („42 tis.“ → „42,1 tis.“), takže jen pomáhá vybrat mezi stejnými názvy
    want = (re.match(r"\d+", members or "") or [""])[0]
    fallback = None
    for _ in range(6):
        for n in nodes():
            t = (n.get("content-desc") or "").replace("\xa0", " ")
            x1, y1, x2, y2 = bounds(n)
            if not (n.get("class", "").endswith("Button") and t.startswith(name + ",") and y2 - y1 > 100):
                continue
            count = re.search(r"·\s*([\d\s,]+)", t)
            if want and count and count.group(1).strip().startswith(want):
                d.click(540, (y1 + y2) // 2)
                time.sleep(7)
                return
            fallback = fallback or (y1 + y2) // 2
        if fallback:
            d.click(540, fallback)
            time.sleep(7)
            return
        d.swipe(540, 1900, 540, 700, 0.3)
        time.sleep(3)
    open_my_group(name)  # skupiny, kde už jsem členem, hledání často neukáže – zkusit seznam „Vaše skupiny“


def open_my_group(name):
    """Nabídka → Skupiny → Vaše skupiny → najde skupinu podle přesného názvu (posouvá) a otevře ji."""
    home()
    d(descriptionMatches="^(Nabídka|Menu)$").click()
    time.sleep(3)
    if not d(descriptionStartsWith="Skupiny, ").exists and d(description="Zobrazit další").exists:
        d(description="Zobrazit další").click()
        time.sleep(2)
    d(descriptionStartsWith="Skupiny, ").click()
    time.sleep(5)
    d(descriptionStartsWith="Vaše skupiny, ").click()
    time.sleep(5)
    for _ in range(20):
        for n in nodes():
            t = (n.get("content-desc") or n.get("text") or "").replace("\xa0", " ").strip()
            if t == name and n.get("class", "").endswith(("Button", "ViewGroup")):
                x1, y1, x2, y2 = bounds(n)
                d.click((x1 + x2) // 2, (y1 + y2) // 2)
                time.sleep(7)
                return
        d.swipe(540, 1800, 540, 900, 0.3)
        time.sleep(2)
    raise RuntimeError(f"Facebook: skupinu „{name}“ jsem nenašel (ani ve Vašich skupinách)")


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
    box = d(className="android.widget.EditText") if d(className="android.widget.EditText").exists else d(className="android.widget.AutoCompleteTextView")
    box.click()
    psani.human_type(d, text)
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
    """Na otevřené skupině: 'member' / 'pending' / 'none' / 'no-permission' (člen, ale nesmí postovat).
    Pozor: veřejné skupiny ukazují „Napište něco…“ i nečlenům – rozhoduje tlačítko Přidat se / Přidal(a) se."""
    all_nodes = nodes()
    descs = " | ".join((n.get("content-desc") or n.get("text") or "") for n in all_nodes)
    if "nemáte oprávnění" in descs:
        return "no-permission"
    if "Zrušit žádost" in descs:
        return "pending"
    if "Přidat se do skupiny" in descs or any((n.get("content-desc") or n.get("text")) == "Přidat se" for n in all_nodes):
        return "none"
    if "Přidal(a) se" in descs or "Napište něco…" in descs:
        return "member"
    return "none"


def questions_screen():
    """Je otevřený formulář s otázkami / pravidly skupiny? Vrací texty na obrazovce (nebo None)."""
    all_nodes = nodes()
    texts = [(n.get("content-desc") or n.get("text") or "").strip() for n in all_nodes]
    joined = " | ".join(texts)
    fields = [n for n in all_nodes if n.get("class", "").endswith(("EditText", "AutoCompleteTextView"))]
    if not fields and not re.search(r"Odpovězte|otázk|Otázk|Pravidla skupiny|Souhlasím", joined):
        return None
    return {"texts": [t for t in texts if t], "fields": len(fields)}


def fill_questions(answers):
    """Vyplní odpovědi do prázdných polí (shora dolů, i po posunutí), zaškrtne souhlas s pravidly a odešle žádost."""
    todo = [a for a in answers if a]
    for _ in range(5):
        fields = sorted([n for n in nodes() if n.get("class", "").endswith(("EditText", "AutoCompleteTextView"))
                         and not (n.get("text") or "").strip()], key=lambda n: bounds(n)[1])
        for n in fields:
            if not todo:
                break
            x1, y1, x2, y2 = bounds(n)
            d.click((x1 + x2) // 2, (y1 + y2) // 2)
            time.sleep(1)
            psani.human_type(d, todo.pop(0))
            time.sleep(1)
        for n in nodes():  # souhlas s pravidly
            if n.get("checkable") == "true" and n.get("checked") == "false":
                x1, y1, x2, y2 = bounds(n)
                d.click((x1 + x2) // 2, (y1 + y2) // 2)
                time.sleep(1)
        for label in ("Odeslat", "Odeslat žádost", "Hotovo"):
            sel = d(description=label) if d(description=label).exists else d(text=label)
            if sel.exists and not todo:
                sel.click()
                time.sleep(6)
                return True
        d.swipe(540, 1800, 540, 800, 0.3)  # další otázky / tlačítko Odeslat bývají níž
        time.sleep(2)
    return False


def join_open_group(answerer=None):
    """Na otevřené skupině klepne Přidat se. Když skupina chce odpovědi na otázky, zeptá se answerer(texty, počet polí)
    → {"skip": bool, "answers": [...], "reason": "…"} a vyplní je. Vrací stav členství (případně 'skipped-questions')."""
    for sel in (d(description="Přidat se", className="android.widget.Button"), d(descriptionStartsWith="Přidat se do skupiny"), d(text="Přidat se")):
        if sel.exists:
            sel.click()
            time.sleep(6)
            break
    q = questions_screen()
    if q:
        screenshot("otazky")
        res = answerer(q["texts"], q["fields"]) if answerer else {"skip": True, "reason": "bez AI"}
        if res.get("skip") or not fill_questions(res.get("answers") or []):
            if d(description="Zavřít").exists:
                d(description="Zavřít").click()
            else:
                d.press("back")
            time.sleep(3)
            global LAST_SKIP_REASON
            LAST_SKIP_REASON = res.get("reason") or "formulář se nepodařilo odeslat"
            return "skipped-questions"
    return membership()


LAST_SKIP_REASON = ""


def join_or_post(group, members, photos, text, answerer=None):
    """Otevře skupinu; když nejsem člen, přidá se. Když jsem člen, zveřejní příspěvek.
    Vrací (stav, screenshot): 'posted' / 'posted-pending' (čeká na schválení adminem) / 'pending' / 'none'."""
    open_group(group, members)
    state = membership()
    if state == "none":
        state = join_open_group(answerer)
    if state != "member":
        return state, None
    if not d(description="Napište něco…").exists and (d(description="Co prodáváte?").exists or d(text="Co prodáváte?").exists):
        return "sell-group", None  # bazarová skupina – jen inzeráty s cenou, tam nepostujeme
    compose(text, photos)
    publish()
    shot = screenshot("zverejneno")
    descs = " | ".join((n.get("content-desc") or n.get("text") or "") for n in nodes())
    pending = re.search(r"ke schválení|správcům|odpovědí se správci|pending|admin", descs, re.I)
    return ("posted-pending" if pending else "posted"), shot


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

ME = "Martin Hejduk"
DEBUG = False
# „Václav Kadavý okomentoval váš příspěvek ve skupině Motorkáři CZ/SK.. 5 d. Nepřečteno“
# „Jaroslav Strakoš s 5 dalšími okomentovali váš příspěvek ve skupině …“, „X odpověděl(a) na váš komentář …: „text““
COMMENT_RE = re.compile(r"^(?P<name>.+?) (?:s \d+ dalšími |a \d+ dalších )?(?P<kind>okomentoval\w*(?:\(a\))?|odpověděl\w*(?:\(a\))?)"
                        r" (?:na )?(?P<what>váš|vaši|vás|vaše)\b.*?(?:ve skupině (?P<group>.+?))?(?:[.:]|$)")
# „Jaro Krutilek vás zmiňuje v komentáři ve skupině F1 CZ/SK Club: "Martin Hejduk jsem v Norsku…".. 1 d.“
MENTION_RE = re.compile(r"^(?P<name>.+?) vás (?:zmiňuje|zmínil\w*|označil\w*(?:\(a\))?) v komentáři(?: ve skupině (?P<group>.+?))?(?::|\.|$)")
TIME_TAIL = re.compile(r"\.*\s*\d+\s*(?:s|m|h|d|t|r)\.?(?:\s*(?:Nep|P)řečteno)?\s*$|\s*(?:Nep|P)řečteno\s*$")


def open_notifications():
    """Karta Upozornění ve Facebooku (ověří, že se opravdu otevřela)."""
    home()
    for _ in range(3):
        tab = d(descriptionStartsWith="Upozornění, karta")
        if not tab.wait(timeout=10):
            d.press("back")  # něco zakrývá spodní lištu
            time.sleep(2)
            continue
        tab.click()
        time.sleep(5)
        if d(description="Spravovat nastavení upozornění").exists or d(descriptionStartsWith="Spravovat nastavení").exists:
            return
    raise RuntimeError("Facebook: karta Upozornění se neotevřela")


def comment_notifications():
    """Upozornění na komentáře pod MÝMI příspěvky / odpovědi na mé komentáře: [{key, name, group, reply, bounds}].
    key je bez času („5 d.“), aby se neměnil."""
    out = []
    for n in nodes():
        t = re.sub(r"\s+", " ", (n.get("content-desc") or "").replace("\xa0", " ")).strip()
        if not n.get("class", "").endswith("Button") or t.startswith("Spravovat"):
            continue
        m = COMMENT_RE.search(t)
        mention = None if m else MENTION_RE.search(t)
        if (m or mention) and not any(o["key"] == TIME_TAIL.sub("", t)[:200] for o in out):
            g = (m or mention)["group"]
            out.append({"key": TIME_TAIL.sub("", t)[:200], "name": (m or mention)["name"].strip(), "group": (g or "").strip(" ."),
                        "reply": bool(mention) or m["kind"].startswith("odpověd"), "bounds": bounds(n)})
    return out


def all_comment_notifications(screens=12):
    """Projde seznam upozornění odshora (posouvá) a vrátí všechna upozornění na komentáře (bez bounds)."""
    out = []
    for _ in range(screens):
        for i in comment_notifications():
            if not any(o["key"] == i["key"] for o in out):
                out.append(i)
        _more_notifications()
        d.swipe(540, 1800, 540, 800, 0.3)
        time.sleep(2)
    return out


def _more_notifications():
    """Na konci seznamu je tlačítko „Předchozí upozornění“ – načte starší."""
    sel = d(description="Předchozí upozornění")
    if sel.exists:
        sel.click()
        time.sleep(4)


def open_notification(key):
    """Otevře kartu Upozornění, najde upozornění podle klíče (posouvá dolů) a otevře ho."""
    open_notifications()
    for _ in range(26):
        match = next((i for i in comment_notifications() if i["key"] == key), None)
        if match and 250 < match["bounds"][1] and match["bounds"][3] < 2050:  # celé vidět (ne pod spodní lištou)
            x1, y1, x2, y2 = match["bounds"]
            d.click((x1 + x2) // 2, (y1 + y2) // 2)
            # otevře se celý příspěvek, nebo jen vlákno „Odpovědi“ u komentáře
            if not (d(descriptionStartsWith="Příspěvek od").wait(timeout=20) or d(description="Komentář").exists
                    or d(text="Odpovědi").exists or d(textStartsWith="Odpovědi na komentář").exists
                    or d(descriptionStartsWith="Odpovědi na komentář").exists):
                raise RuntimeError("po klepnutí na upozornění se neotevřel příspěvek")
            time.sleep(3)
            return
        if match:
            d.swipe(540, 1500, 540, 1000, 0.3)  # upozornění je u kraje – kousek posunout
            time.sleep(2)
            continue
        _more_notifications()
        d.swipe(540, 1800, 540, 800, 0.3)
        time.sleep(2)
    raise RuntimeError("upozornění na komentář jsem nenašel")


BADGES = {"Moderátor", "Moderátorka", "Správce", "Správkyně", "Autor", "Autorka", "Top fanoušek", "Nejaktivnější fanoušek",
          "Expert skupiny", "Nový člen", "Nová členka", "Admin", "Sledovat", "·"}


def age_minutes(age):
    """„4 h“ → 240, „2 d“ → 2880, „1 T“ → 10080; neznámé → velké číslo (= staré)."""
    m = re.match(r"(\d+)\s*(\w+)", age or "")
    if not m:
        return 10 ** 7
    n, u = int(m.group(1)), m.group(2)
    return n * {"s": 0, "m": 1, "min": 1, "h": 60, "d": 1440, "T": 10080, "t": 10080, "r": 525600}.get(u, 10 ** 6)


def too_old(age):
    """Komentář starší než týden (nebo neznámé stáří) – na ten už bot nereaguje."""
    m = re.match(r"(\d+)\s*(\w+)", age or "")
    if not m:
        return False  # stáří nezjištěno – rozhodne „answered“
    n, unit = int(m.group(1)), m.group(2)
    return unit in ("T", "t", "r") or (unit == "d" and n > 7)


def read_post_comments():
    """Na otevřeném příspěvku přečte komentáře: [{name, text, indent, answered}].
    answered = pod komentářem už je moje odpověď (např. jsi odpověděl ručně)."""
    found, collapsed = [], set()
    d(descriptionStartsWith="Profilový obrázek").wait(timeout=15)  # komentáře se načítají se zpožděním
    time.sleep(2)
    sort_newest()
    idle = 0
    for _ in range(14):
        _expand_comments()  # rozbalit na téhle obrazovce a hned ji přečíst (po posunu FB odpovědi zase sbalí)
        time.sleep(1)
        all_nodes = nodes()
        for n in all_nodes:  # „Zobrazit 1 odpověď Filip Baldrych…“, co se nerozbalilo → komentář má odpověď
            m = re.match(r"Zobrazit .*?odpově\w* (.+?)…?$", n.get("content-desc") or "")
            if m:
                collapsed.add(m.group(1).strip())
        screen = []  # komentáře na téhle obrazovce, shora dolů
        for i, n in enumerate(all_nodes):
            desc = n.get("content-desc") or ""
            if not (n.get("class", "").endswith("ImageView") and desc.startswith("Profilový obrázek ")):
                continue
            name, x1 = desc[len("Profilový obrázek "):].split(",")[0].strip(), bounds(n)[0]  # „…, nezobrazený příběh“
            if bounds(n)[1] > 1900:
                continue  # u spodního okraje je komentář vidět jen napůl – přečte se na další obrazovce
            # za fotkou: tlačítko se jménem, pak tlačítko s textem komentáře
            rest = [m for m in all_nodes[i + 1:i + 12] if m.get("class", "").endswith("Button")]
            if not rest or (rest[0].get("content-desc") or rest[0].get("text")) != name or len(rest) < 2:
                continue
            # za jménem můžou být odznaky (Moderátor, Správce, Autor, Top fanoušek…) – ty přeskočit
            # text komentáře je vpravo od profilovky (x ≥ 100) – vlevo dole je lišta reakcí ❤️ 🙌 🔥, ta to není
            text = next(((m.get("content-desc") or m.get("text") or "").strip() for m in rest[1:]
                         if (m.get("content-desc") or m.get("text") or "").strip() not in BADGES and bounds(m)[0] >= 100), "")
            if not text or text.startswith(("Odpovědět", "Tlačítko")):
                continue
            # stáří komentáře („4 h“, „5 d“, „3 T“) je hned za jménem
            age = next(((m.get("content-desc") or m.get("text") or "").strip() for m in all_nodes[i + 1:i + 8]
                        if re.fullmatch(r"\d+\s*(s|m|min|h|d|T|t|r)\.?", (m.get("content-desc") or m.get("text") or "").strip())), "")
            screen.append({"name": name, "text": text, "indent": x1 > 60, "answered": False, "age": age, "y": bounds(n)[1]})
        screen.sort(key=lambda c: c["y"])
        if DEBUG:
            print("  obrazovka:", [(c["name"].split()[0], c["text"][:15], c["indent"]) for c in screen])
        new = _merge_screen(found, screen)
        idle = 0 if new else idle + 1
        if idle >= 2:
            break
        d.swipe(540, 1700, 540, 900, 0.3)
        time.sleep(2)
    # vlákna: komentář + odsazené odpovědi pod ním (při řazení „Nejnovější“ jsou i odpovědi podle data, ne popořadě)
    for i, c in enumerate(found):
        c.pop("y", None)
    for i, c in enumerate(found):
        start = i
        while start > 0 and found[start]["indent"]:
            start -= 1
        end = start + 1
        while end < len(found) and found[end]["indent"]:
            end += 1
        thread = found[start:end]
        mine = [x for x in thread if x["name"] == ME]
        if not c["indent"]:
            c["answered"] = bool(mine) or c["name"] in collapsed
        else:
            # odpověď je vyřízená, když je ve vlákně Martinova odpověď NOVĚJŠÍ než ona
            c["answered"] = any(age_minutes(m["age"]) < age_minutes(c["age"]) for m in mine)
            c["toMe"] = c["text"].startswith(ME) or (thread[0]["name"] == c["name"] and bool(mine))
    return found


def sort_newest():
    """Řazení komentářů „Nejnovější“ (= všechny komentáře od nejnovějších). Při „Nejrelevantnější“ FB část komentářů schová."""
    for _ in range(3):
        f = d(descriptionStartsWith="Zobrazuje se")
        if f.exists:
            if "Nejnovější" in (f.info.get("contentDescription") or ""):
                return True
            f.click()
            time.sleep(2)
            opt = d(descriptionStartsWith="Nejnovější, ")
            if opt.wait(timeout=5):
                opt.click()
                time.sleep(4)
                return True
            d.press("back")
            return False
        d.swipe(540, 1500, 540, 900, 0.3)
        time.sleep(2)
    return False  # příspěvek bez filtru (třeba vlákno „Odpovědi“) – nevadí


def _merge_screen(found, screen):
    """Připojí komentáře z nové obrazovky k už přečteným podle překryvu (stejné komentáře nahoře nové obrazovky
    = konec předchozí). Neslučuje podle textu, protože stejný text („Cena?“) může mít víc lidí. Vrací počet nových."""
    key = lambda c: (c["name"], c["text"], c["indent"])
    for k in range(min(len(found), len(screen)), 0, -1):
        if [key(c) for c in found[-k:]] == [key(c) for c in screen[:k]]:
            found.extend(screen[k:])
            return len(screen) - k
    # bez překryvu: přeskočit, co už je na konci (obrazovka se neposunula)
    fresh = [c for c in screen if key(c) not in {key(f) for f in found[-len(screen):]}] if found else screen
    found.extend(fresh)
    return len(fresh)


def _expand_comments():
    """Rozbalí skryté komentáře a odpovědi, které jsou právě na obrazovce."""
    for sel in (d(descriptionStartsWith="Zobrazit další komentáře"), d(descriptionStartsWith="Zobrazit předchozí komentáře"),
                d(descriptionMatches=r"(?i)^Zobrazit( všech)? \d* ?(odpověď|odpovědi|odpovědí).*"),
                d(textMatches=r"(?i)^Zobrazit( všech)? \d* ?(odpověď|odpovědi|odpovědí).*")):
        for _k in range(10):  # rozbalit všechny skryté komentáře a odpovědi (jinak by bot nepoznal, že už jsi odpověděl)
            if not sel.exists:
                break
            sel.click()
            time.sleep(3)


def _match_comment(name, text):
    snippet = re.sub(r"\W+", "", text[:25]).lower()
    buttons = [n for n in nodes() if n.get("class", "").endswith("Button")]
    for i, n in enumerate(buttons[:-1]):
        if (n.get("content-desc") or n.get("text")) == name and bounds(buttons[i + 1])[0] >= 100:
            t = buttons[i + 1].get("content-desc") or buttons[i + 1].get("text") or ""
            if (snippet and snippet in re.sub(r"\W+", "", t).lower()) or (not snippet and t.strip() == text.strip()):  # jen emoji
                return bounds(buttons[i + 1])
    return None


def _find_comment(name, text):
    """Na otevřeném příspěvku najde komentář (jméno + text) – nejdřív na obrazovce, pak nahoru, pak dolů.
    Bot zůstává na stejném příspěvku, nic znovu neotevírá. Vrací bounds textu komentáře."""
    for direction in ("tady", "nahoru", "dolů"):
        for _ in range(1 if direction == "tady" else 14):
            _expand_comments()  # komentář může být schovaný pod „Zobrazit předchozí komentáře“
            b = _match_comment(name, text)
            if b:
                return b
            if direction == "tady":
                break
            before = d.dump_hierarchy()
            if direction == "nahoru":
                d.swipe(540, 800, 540, 1600, 0.3)
            else:
                d.swipe(540, 1600, 540, 800, 0.3)
            time.sleep(1.5)
            if d.dump_hierarchy() == before:
                break  # začátek / konec stránky
    return None


def keyboard_shown():
    out = subprocess.run([ADB, "shell", "dumpsys", "input_method"], capture_output=True, text=True).stdout
    return "mInputShown=true" in out


def reply_to_comment(name, text, reply):
    """Na otevřeném příspěvku odpoví pod komentářem daného člověka."""
    b = _find_comment(name, text)
    if b and b[3] > 1700:  # komentář u spodního okraje – posunout, ať je vidět i tlačítko Odpovědět
        d.swipe(540, 1500, 540, 900, 0.3)
        time.sleep(2)
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
    _type_into_box(reply)
    in_thread = d(textStartsWith="Odpovědi na komentář").exists or d(descriptionStartsWith="Odpovědi na komentář").exists
    if not in_thread and not d(descriptionStartsWith="Odpovídáte uživateli").exists and not d(textStartsWith="Odpovídáte uživateli").exists:
        raise RuntimeError("Facebook: nejsem v režimu odpovědi na komentář – neodesílám")
    click_desc("Odeslat")
    time.sleep(4)
    if keyboard_shown():
        d.press("back")  # schovat klávesnici, ať jde stránkou dál posouvat
        time.sleep(1)


def _type_into_box(text):
    """Vloží text do pole pro komentář. Klávesnice ho umí celé zakrýt (uiautomator pak vidí jen ji) – proto ji
    nejdřív schová (pole zůstane aktivní) a pak teprve hledá pole."""
    time.sleep(2)  # klávesnice vyskakuje se zpožděním
    # po klepnutí na „Odpovědět“ je pole aktivní (kurzor za jménem) – stačí psát; vkládá se na pozici kurzoru,
    # takže zmínka „Jméno Příjmení“ na začátku zůstane jako u člověka
    if keyboard_shown() or d(focused=True).exists:
        psani.human_type(d, text)
        return
    for _ in range(2):
        box = d(className="android.widget.AutoCompleteTextView")
        if not box.exists:
            box = d(className="android.widget.EditText")
        if box.exists:
            box.click()
            time.sleep(1)
            psani.human_type(d, text)
            return
        d.press("back")  # schovat klávesnici
        time.sleep(2)
    raise RuntimeError("Facebook: nenašel jsem pole pro odpověď")


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
    for _ in range(4):  # profil se někdy otevře na šířku a tlačítko je až níž
        label = next((lb for lb in ("Zpráva", "Poslat zprávu", "Napsat zprávu") if d(description=lb).exists or d(text=lb).exists), None)
        if label:
            click_desc(label)
            break
        w, h = d.window_size()
        d.swipe(w // 2, int(h * 0.8), w // 2, int(h * 0.35), 0.3)
        time.sleep(2)
    else:
        raise RuntimeError(f"na profilu {name} není tlačítko Zpráva")
    time.sleep(8)
    # chat se otevře buď v Messengeru, nebo přímo ve Facebooku (okno chatu nad stránkou)
    texts = [(n.get("content-desc") or n.get("text") or "") for n in ET.fromstring(d.dump_hierarchy()).iter("node")]
    if any("poklepáním zobrazíte datum" in t or t.startswith(("Odesláno", "Zobrazeno")) for t in texts):
        d.press("back")
        return "existing"  # s člověkem už konverzace je – nic nového nepsat, ať to není divné
    for label in ("Pokračovat",):  # „V chatu nemůžete posílat zprávy. Otevřete chat…“ → Pokračovat
        sel = d(description=label) if d(description=label).exists else d(text=label)
        if sel.exists:
            sel.click()
            time.sleep(6)
    box = d(className="android.widget.EditText")
    if not box.wait(timeout=10):
        raise RuntimeError("v chatu chybí pole pro zprávu")
    box.click()
    psani.human_type(d, message)
    send = d(descriptionStartsWith="Poslat")
    if not send.exists:
        raise RuntimeError("v chatu chybí tlačítko Poslat")
    send.click()
    time.sleep(3)
    d.press("back")
    return "sent"


if __name__ == "__main__":
    if len(sys.argv) >= 6 and sys.argv[1] == "post":
        print(post(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], "--jen-pripravit" in sys.argv))
    else:
        print(__doc__)
