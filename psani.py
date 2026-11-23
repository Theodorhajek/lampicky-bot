"""Psaní jako člověk: text v aktivním poli postupně přibývá po znacích (občas po dvou) s náhodnými pauzami,
místo vložení celé zprávy naráz. Podporuje češtinu (háčky, čárky) i emoji.

Bez schránky (vkládání ze schránky Android hlásí a vypadá podezřele) – pole se jen postupně prodlužuje,
stejně jako když člověk píše. Normální klávesnice Gboard zůstává, nic se neinstaluje.
"""

import random
import time

def _delay(ch, prev):
    if ch in " \n":
        base = random.uniform(0.12, 0.35)
    elif ch in ".,!?;:":
        base = random.uniform(0.2, 0.5)
    else:
        base = random.uniform(0.06, 0.22)
    if prev and prev in ".!?" and ch == " ":
        base += random.uniform(0.3, 0.9)  # konec věty
    if random.random() < 0.04:
        base += random.uniform(0.5, 1.5)  # chvilka přemýšlení
    return base


def _chunks(text):
    """Většinou po jednom znaku, občas dva rychle za sebou (jako když člověk píše plynule). Emoji vcelku."""
    i = 0
    while i < len(text):
        n = 2 if random.random() < 0.25 and text[i:i + 2].isalpha() else 1
        if ord(text[i]) > 0xFFFF or (i + 1 < len(text) and text[i + 1] == "️"):
            n = 2 if i + 1 < len(text) and text[i + 1] == "️" else 1
        yield text[i:i + n]
        i += n


def tap(d, el):
    """Klepnout do textového pole jako člověk: ne přesně doprostřed (u2 .click() trefí pokaždé stejný bod),
    ale náhodně v levé části pole, kde začíná text, a pokaždé trochu jinde. el = selektor nebo bounds (x1, y1, x2, y2)."""
    if isinstance(el, (list, tuple)):
        x1, y1, x2, y2 = el
    else:
        b = el.info["bounds"]
        x1, y1, x2, y2 = b["left"], b["top"], b["right"], b["bottom"]
    w, h = x2 - x1, y2 - y1
    x = x1 + int(w * random.uniform(0.08, 0.4))
    y = y1 + int(h * random.uniform(0.3, 0.7)) if h < 200 else y1 + random.randint(40, 110)  # velké pole: horní řádek
    d.click(x, y)


EDIT_CLASSES = ("android.widget.EditText", "android.widget.AutoCompleteTextView", "android.widget.MultiAutoCompleteTextView")


def _field(d, timeout=5):
    """Aktivní textové pole. Hledá se podle třídy + fokusu: samotné d(focused=True) občas na chvíli ukáže na jiný prvek
    (FB hledání, překreslení) a uiautomator pak místo vložení textu „ťuká“ klávesy → v poli je nesmysl („gvggg…“)."""
    end = time.time() + timeout
    while True:
        for cls in EDIT_CLASSES:
            f = d(className=cls, focused=True)
            if f.exists:
                return f
        if time.time() > end:
            break
        time.sleep(0.4)
    for cls in EDIT_CLASSES:  # fokus se ztratil – když je na obrazovce jediné pole, klepnout do něj
        f = d(className=cls)
        if f.count == 1:
            f.click()
            time.sleep(0.8)
            return f
    f = d(focused=True)  # jiný typ pole (původní chování)
    if f.exists:
        return f
    raise RuntimeError("psaní: žádné aktivní pole")


def _text(d):
    try:
        return _field(d, 2).get_text() or ""
    except Exception:  # noqa: BLE001
        return ""


def _put(d, typed):
    """Nastaví text pole a hned ověří, že tam opravdu je; jednou to zkusí znovu. False = nepovedlo se."""
    for attempt in range(2):
        try:
            f = _field(d, 3)
            f.set_text(typed)
            if _same(f.get_text() or "", typed):
                return True
        except Exception:  # noqa: BLE001  – pole se zrovna překreslilo
            pass
        time.sleep(0.6)
        if _same(_text(d), typed):  # pole se jen opozdilo
            return True
    return False


def human_type(d, text):
    """Napíše text do právě aktivního (focused) pole. Pole musí být předem aktivní (klepnuto).
    Po každém přidaném znaku se kontroluje, že v poli je opravdu to, co má být – jinak se nic neodešle."""
    _field(d, 5)
    time.sleep(random.uniform(0.4, 0.9))
    typed, prev = "", ""
    for chunk in _chunks(text):
        typed += chunk
        if not _put(d, typed):
            break
        time.sleep(_delay(chunk[-1], prev))
        prev = chunk[-1]
    time.sleep(random.uniform(0.5, 1.2))  # chvilka, než „klepne“ na Odeslat
    if not _same(_text(d), text) and not _put(d, text):  # celé naráz
        try:
            _field(d, 2).clear_text()
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError("psaní: text se nenapsal správně – nic neodesílám")


def _same(a, b):
    import re  # noqa: PLC0415
    k = lambda s: re.sub(r"[^\w]", "", s or "")
    return k(a) == k(b)
