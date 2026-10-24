"""Lampičky – dashboard bota. Lokální stránka jen pro tebe: http://localhost:5005

  .venv/bin/python dashboard.py

Záložky:
  Přehled    – „Musím teď něco udělat?“ Karty Odpovědět (odpovídáš přímo na místě), objednávky k výrobě,
               možná zaseknuté konverzace, úkoly ve Facebooku a komu se připomenout.
  Zprávy     – všechny konverzace jako v Messengeru: seznam | chat | co o zákazníkovi víme.
  Objednávky – domluveno → vyrobeno → odesláno.
  Další      – skupiny a komentáře, učení bota, technika.

Odpověď napsaná tady jde do fronty (data.outbox_add) a bot ji v telefonu napíše po znacích – telefon ovládá jen bot.
Stránka běží jen na tomhle Macu (127.0.0.1) – data obsahují osobní údaje zákazníků.
Co sis označil(a) „Nepotřebuje odpověď“ a jak daleko je která objednávka, se pamatuje v dashboard_stav.json.
"""

import csv
import json
import re
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from urllib.parse import quote

from flask import Flask, abort, g, jsonify, redirect, request, send_file

import data

HERE = Path(__file__).resolve().parent
ADB = str(Path.home() / ".local" / "android" / "platform-tools" / "adb")
app = Flask(__name__)

STUCK_MIN = 20          # zákazník napsal a bot do 20 min neodpověděl → Odpovědět
QUIET_DAYS = 3          # zákazník psal poslední před víc než 3 dny → Možná zaseknuté; ticho po naší zprávě → Uzavřené
FOLLOWUP_DAYS = 2       # zeptali jsme se a zákazník mlčí déle → nabídka připomenout se
FOLLOWUP_MAX_DAYS = 21
TASK_DAYS = 3           # chyby u komentářů apod. starší než tohle už nestrašíme
OLD_DAYS = 7
SPAM = re.compile(r"live ?cams?|chat girl|room full of love", re.I)
ORDER = re.compile(r"objednávk\w* (je )?(kompletní|hotová|domluvená)|kompletní objednávk|"
                   r"zaplatil\w* (poštovné|celou|zálohu|lampičku)|poslal\w* platbu|platba (přišla|dorazila)", re.I)
INTEREST = re.compile(r"\bcen[auy]?\b|kolik|zájem|záujem|chci|chcem|chtěl|bral|beru|vzal|objedn|koupi|kúpi|\bpm\b|\bsz\b|napiš|ozv", re.I)
TECH = re.compile(r"Exception|-3200\d|RPC|Selector|Traceback|\{'code'|uiautomator|androidx|Command '|non-zero exit|/Users/", re.I)

# zpráva zákazníka, která celá jen děkuje / potvrzuje / slibuje, že se ozve → není třeba odpovídat
CLOSING_CORE = {
    "ok", "okay", "okej", "oki", "oukej", "dobře", "dobre", "dobrá", "dobra", "super", "paráda", "parada", "výborně", "vyborne",
    "skvělé", "skvele", "díky", "diky", "dík", "dik", "díkes", "dikes", "děkuji", "dekuji", "děkuju", "dekuju", "děkujem",
    "děkujeme", "dekujeme", "dakujem", "ďakujem", "dakujeme", "ďakujeme", "vďaka", "vdaka", "domluveno", "dohodnuto",
    "platí", "plati", "jasně", "jasne", "jj", "rozumím", "rozumim", "ozvu", "ozvem", "ozveme", "pohodě", "pohode",
    "pořádku", "poradku", "počkám", "pockam", "čekám", "cekam", "fajn", "pa", "papa", "nashle", "nashledanou",
}
CLOSING_FILL = {
    "moc", "mockrát", "mockrat", "pekne", "pěkně", "pekně", "vám", "vam", "ti", "vám", "za", "info", "informace", "informaci",
    "odpověď", "odpoved", "tak", "zatím", "zatim", "se", "sa", "budu", "čekat", "cekat", "v", "a", "i", "to", "je", "vše",
    "vse", "všechno", "vsechno", "hezký", "hezky", "pěkný", "pekny", "krásný", "krasny", "den", "večer", "vecer", "víkend",
    "vikend", "mějte", "mejte", "majte", "ještě", "jeste", "pak", "potom", "zítra", "zitra", "brzy", "určitě", "urcite",
    "já", "ja", "vy", "vám", "taky", "též", "tez", "aj", "rado", "rádo", "se", "stalo", "nemáte", "zač", "zac", "už", "uz",
    "teda", "tedy", "no", "jo", "hm", "hmm", "spolu", "dohodneme", "domluvíme", "domluvime", "pane", "paní",
}
CLOSING_STEMS = ("domluv", "dohodn", "děkuj", "dekuj", "dakuj", "ďakuj", "dík", "dik", "ozv", "rozum", "počk", "pock", "čeká", "ceka", "super", "parád", "parad")
PROMISE = re.compile(r"\b(ozvu|ozvem|ozveme|ozvu sa|dám (vám |ti )?vědět|dam (vam |ti )?vedet|dáme vědět|napíšu (vám |ti )?(pak|později|zítra))\b", re.I)
CLOSING_EMOJI = re.compile("[\U0001F44D\U0001F44C\U0001F642\U0001F60A\U0001F64F❤\U0001F600-\U0001F64F\U0001F90F-\U0001F9FF☺]")

_cache = {}
_status_cache = {"ts": 0, "val": None}
h = lambda s: escape(str(s if s is not None else ""))


def norm(t):
    return re.sub(r"\W+", "", (t or "").lower())


def memo(key, fn):
    """Jedna sada dat na jeden požadavek (hlavička i obsah stránky ji sdílí)."""
    try:
        d = g.setdefault("_memo", {})
    except RuntimeError:
        return fn()
    if key not in d:
        d[key] = fn()
    return d[key]


# ====================================================================== načítání dat (rozbitý soubor nesmí shodit stránku)

def _cached(path, loader, default):
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return default
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        val = loader(path)
    except Exception:  # noqa: BLE001
        return default
    _cache[path] = (mtime, val)
    return val


def read_json(path, default):
    return _cached(path, lambda p: json.loads(p.read_text(encoding="utf-8")), default)


def read_text(path):
    return _cached(path, lambda p: p.read_text(encoding="utf-8"), "")


def state():
    s = read_json(HERE / "state_telefon.json", {})
    return s if isinstance(s, dict) else {}


def threads():
    t = state().get("threads", {})
    return t if isinstance(t, dict) else {}


def config():
    c = read_json(HERE / "config.json", {})
    return c if isinstance(c, dict) else {}


def parse_when(s):
    try:
        return datetime.strptime(s.strip(), "%d. %m. %Y %H:%M:%S").timestamp()
    except ValueError:
        return 0.0


def _load_log(path):
    rows = []
    with path.open(encoding="utf-8", newline="", errors="replace") as f:
        for r in csv.reader(f, delimiter=";"):
            if len(r) >= 3:
                rows.append({"ts": parse_when(r[0]), "who": r[1].strip(), "action": r[2].strip().strip('"'),
                             "detail": (r[3] if len(r) > 3 else "").strip()})
    return rows


def log_rows():
    return _cached(HERE / "log.csv", _load_log, [])


def stav_path():
    return HERE / "dashboard_stav.json"


def stav():
    d = read_json(stav_path(), {})
    d = dict(d) if isinstance(d, dict) else {}
    d.setdefault("skryto", {})
    d.setdefault("objednavky", {})
    return d


def save_stav(d):
    p = stav_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    _cache.pop(p, None)


def jobs():
    j = read_json(HERE / "fronta.json", [])
    return [x for x in j if isinstance(x, dict)] if isinstance(j, list) else []


def job_ts(j):
    try:
        dt = datetime.strptime(f"{datetime.now().year} {j.get('last', '')}", "%Y %d. %m. %H:%M")
        return dt.timestamp() if dt <= datetime.now() else dt.replace(year=dt.year - 1).timestamp()
    except ValueError:
        return 0.0


def problems():
    p = read_json(HERE / "problemy" / "seznam.json", [])
    return [x for x in p if isinstance(x, dict)] if isinstance(p, list) else []


def bot_status():
    """Proces bota, telefon přes adb, internet v telefonu (ping z telefonu). 20 s cache."""
    if _status_cache["val"] and time.time() - _status_cache["ts"] < 20:
        return _status_cache["val"]
    running, phone, online = 0, False, False
    try:
        ps = subprocess.run(["ps", "-ax"], capture_output=True, text=True, timeout=8).stdout
        running = sum(1 for l in ps.splitlines() if "bot_telefon.py run" in l and "Python" in l)
    except Exception:  # noqa: BLE001
        pass
    try:
        devs = subprocess.run([ADB, "devices"], capture_output=True, text=True, timeout=8).stdout
        phone = any(l.strip().endswith("\tdevice") for l in devs.splitlines()[1:])
        if phone:
            out = subprocess.run([ADB, "shell", "ping", "-c", "1", "-W", "2", "8.8.8.8"],
                                 capture_output=True, text=True, timeout=8).stdout
            online = " 1 received" in out
    except Exception:  # noqa: BLE001  – adb chybí nebo neodpovídá
        pass
    val = {"running": running, "phone": phone, "online": online}
    _status_cache.update(ts=time.time(), val=val)
    return val


def in_quiet_hours():
    try:
        start, end = config().get("quietHours", [23, 7])
    except (TypeError, ValueError):
        start, end = 23, 7
    hr = datetime.now().hour
    return (hr >= start or hr < end) if start > end else start <= hr < end


def pause_hours():
    return config().get("pauseAfterManualReplyHours", 12)


# ====================================================================== čas a čeština

def plural(n, one, few, many):
    return one if n == 1 else few if 2 <= n <= 4 else many


def dur(sec):
    m = max(0, int(sec // 60))
    if m < 1:
        return "chvilku"
    if m < 60:
        return f"{m} min"
    if m < 24 * 60:
        hh, mm = divmod(m, 60)
        return f"{hh} h" + (f" {mm} min" if mm and hh < 3 else "")
    d = m // 1440
    return f"{d} {plural(d, 'den', 'dny', 'dní')}"


def ago(ts):
    if not ts:
        return ""
    s = time.time() - ts
    if s < 90:
        return "teď"
    if s < 20 * 3600:
        return "před " + dur(s)
    day = datetime.fromtimestamp(ts).date()
    if day == (datetime.now() - timedelta(days=1)).date():
        return "včera"
    if s < 7 * 86400:
        d = int(s // 86400) or 1
        return f"před {d} {plural(d, 'dnem', 'dny', 'dny')}"
    return f"{day.day}. {day.month}."


def clock(ts):
    if not ts:
        return ""
    d = datetime.fromtimestamp(ts)
    today = datetime.now().date()
    if d.date() == today:
        return f"{d:%H:%M}"
    if d.date() == today - timedelta(days=1):
        return f"včera {d:%H:%M}"
    return f"{d.day}. {d.month}. {d:%H:%M}"


def day_label(ts):
    d = datetime.fromtimestamp(ts).date()
    today = datetime.now().date()
    if d == today:
        return "Dnes"
    if d == today - timedelta(days=1):
        return "Včera"
    names = ["pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle"]
    return f"{names[d.weekday()].capitalize()} {d.day}. {d.month}."


def short(t, n=220):
    t = re.sub(r"\s+", " ", t or "").strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def human_error(e):
    if not e:
        return ""
    if TECH.search(e):
        return "Technická chyba v telefonu (aplikace se nenačetla nebo se změnila obrazovka)."
    return e


def is_closing(text):
    """„Dobře domluveno, zatím děkuji“, „ozvu se“, „ok“, „👍“ … – celá zpráva jen děkuje/potvrzuje."""
    t = (text or "").strip()
    if not t or "?" in t:
        return False
    if PROMISE.search(t) and len(t) < 200:  # „během zítřku se ozvu“ – zákazník se ozve sám
        return True
    if len(t) > 90:
        return False
    words = re.findall(r"[^\W\d_]+", t.lower())
    if not words:
        return bool(CLOSING_EMOJI.search(t))
    if len(words) > 9:
        return False
    core = lambda w: w in CLOSING_CORE or w.startswith(CLOSING_STEMS)
    if any(not core(w) and w not in CLOSING_FILL for w in words):
        return False
    return any(core(w) for w in words) or bool(CLOSING_EMOJI.search(t))


def formal(text):
    return not re.search(r"\b(bys|máš|chceš|napiš|dej|pošli)\b|\bahoj\b|\bčau\b", text or "", re.I)


# ====================================================================== model: lidé a konverzace (jeden model pro všechny stránky)

IN_ACT = ("nová zpráva",)
ME_ACT = ("odesláno z dashboardu", "odpověděl jsi ručně")
HANDOFF_ACT = ("PŘEDÁNO TOBĚ", "limit odpovědí bota", "převzato tebou")
SENT_ACT = ("odesláno", "komentář – poslal jsem zprávu")

STATUS = {  # klíč: (krátký popis, sekce v seznamu)
    "predano": ("Předáno tobě", "odpovedet"),
    "pises_odpovedel": ("Odpověděl ti", "odpovedet"),
    "neodpovedel": ("Bot neodpověděl", "odpovedet"),
    "stare": ("Možná zaseknuté", "zaseknute"),
    "bot_pise": ("Bot odpovídá", "bot"),
    "objednavka": ("Objednávka", "zakaznik"),
    "prevzato": ("Píšeš ty", "zakaznik"),
    "pises": ("Píšeš ty", "zakaznik"),
    "hotovo": ("Čeká na zákazníka", "zakaznik"),
    "dekuje": ("Zákazník poděkoval", "zakaznik"),
    "uzavreno": ("Uzavřené", "uzavrene"),
    "skryto": ("Nepotřebuje odpověď", "uzavrene"),
    "zablokovany": ("Zablokovaný", "uzavrene"),
    "spam": ("Spam", "uzavrene"),
    "prazdna": ("Bez zpráv", "uzavrene"),
}
REPLY = ("predano", "pises_odpovedel", "neodpovedel")
SECTIONS = [
    ("odpovedet", "Odpovědět", "Zákazník čeká na tebe – odpověz."),
    ("zaseknute", "Možná zaseknuté", "Zákazník psal poslední a nikdo neodpověděl. Odpověz, nebo dej Nepotřebuje odpověď."),
    ("bot", "Bot odpovídá", ""),
    ("zakaznik", "Čeká na zákazníka", ""),
    ("uzavrene", "Uzavřené", ""),
]


def handoff_reason(action, note):
    if action.startswith("limit odpovědí"):
        return "Bot už odpověděl několikrát za sebou a nechce působit jako automat – pokračuj ty."
    if action.startswith("PŘEDÁNO TOBĚ – opakovaná chyba"):
        return "Bot se v téhle konverzaci opakovaně zasekl – odpověz prosím sám."
    if action.startswith("převzato"):
        return "Převzal(a) jsi konverzaci sám – bot mlčí."
    return human_error(note) or "Bot si nevěděl rady."


def db_rows():
    def load():
        out = {"msgs": [], "outbox": [], "dec": [], "com": []}
        try:
            with data.db() as con:
                out["msgs"] = con.execute("SELECT id, ts, person, direction, text, images FROM messages ORDER BY id").fetchall()
                out["outbox"] = con.execute("SELECT id, ts, kind, person, text, status, error, done_ts FROM outbox ORDER BY id").fetchall()
                out["dec"] = con.execute("SELECT ts, kind, person, note, handoff FROM decisions ORDER BY id").fetchall()
                out["com"] = con.execute("SELECT ts, grp, person, text, indent, reply, dm, note FROM comments ORDER BY ts").fetchall()
        except Exception:  # noqa: BLE001
            pass
        return out
    return memo("db", load)


def fix_batch_times(ms, plog):
    """Zprávy uložené najednou (první otevření chatu, sběr dat) mají v databázi stejný čas – skutečný čas
    starších z nich neznáme (ts=0). Když tehdy bot nic nedělal, čas poslední odhadneme z deníku."""
    i = 0
    while i < len(ms):
        j = i
        while j + 1 < len(ms) and abs(ms[j + 1]["ts"] - ms[i]["ts"]) < 3:
            j += 1
        if j - i >= 2:
            bts = ms[i]["ts"]
            live = any(bts - 5 <= r["ts"] <= bts + 900 for r in plog)
            for m in ms[i:j]:
                m["ts"] = 0
            if not live:
                ms[j]["ts"] = max((r["ts"] for r in plog if r["ts"] and r["ts"] < bts), default=0)
        i = j + 1


def people():
    return memo("people", _people)


def _people():
    st = threads()
    now = time.time()
    bot_down = bot_status()["running"] == 0
    hidden = stav()["skryto"]
    by_who = defaultdict(list)
    for r in log_rows():
        by_who[r["who"]].append(r)
    rows = db_rows()
    msgs = defaultdict(list)
    for mid, ts, person, dirn, text, img in rows["msgs"]:
        msgs[person].append({"id": mid, "ts": ts or 0, "dir": dirn, "text": text or "", "img": img or 0})
    outbox = defaultdict(list)
    for oid, ts, kind, person, text, status_, error, done in rows["outbox"]:
        outbox[person].append({"id": oid, "ts": ts or 0, "kind": kind, "text": text or "", "status": status_,
                               "error": error or "", "done": done or 0})
    handdec, notes = defaultdict(list), {}
    for ts, kind, person, note, handoff in rows["dec"]:
        if handoff:
            handdec[person].append({"ts": ts or 0, "note": note or ""})
        if note:
            notes[person] = {"ts": ts or 0, "note": note}
    comments = defaultdict(list)
    for ts, grp, person, text, indent, reply, dm, note in rows["com"]:
        comments[person].append({"ts": ts or 0, "grp": grp or "", "text": text or "", "indent": indent,
                                 "reply": reply or "", "dm": dm or "", "note": note or ""})
    orders_by = {o["person"]: o for o in orders()}

    out = {}
    for name in set(st) | set(msgs):
        try:
            p = _person(name, st.get(name) if isinstance(st.get(name), dict) else {}, by_who.get(name, []),
                        [dict(x) for x in msgs.get(name, [])], outbox.get(name, []), handdec.get(name, []),
                        notes.get(name), comments.get(name, []), orders_by.get(name), name in st, now)
        except Exception:  # noqa: BLE001  – jeden podivný záznam nesmí shodit celou stránku
            continue
        classify(p, now, bot_down, hidden)
        out[name] = p
    return out


def _person(name, t, ev, m, ob, handdec, note, comments, order, watched, now):
    last_db = max((x["ts"] for x in m), default=0)
    fix_batch_times(m, ev)
    tm = [x for x in m if x["ts"]]

    hand = None
    for r in reversed(ev):
        if r["action"].startswith(HANDOFF_ACT):
            hand = {"ts": r["ts"], "action": r["action"], "note": r["detail"]}
            break
    for dct in handdec[-1:]:
        if not hand or dct["ts"] > hand["ts"] + 5:
            hand = {"ts": dct["ts"], "action": "PŘEDÁNO TOBĚ", "note": dct["note"]}
    if hand:
        hand["reason"] = handoff_reason(hand["action"], hand["note"])

    cands = [(r["ts"], r["detail"].replace(" / ", "\n")) for r in ev if r["action"].startswith(IN_ACT) and r["detail"]]
    cands += [(x["ts"], x["text"]) for x in tm if x["dir"] == "in"][-1:]
    if not cands and m and m[-1]["dir"] == "in":
        cands = [(0, m[-1]["text"])]
    in_ts, in_text = max(cands, default=(0, ""), key=lambda c: c[0])
    log_in_ts = max((r["ts"] for r in ev if r["action"].startswith(IN_ACT)), default=0)

    me_ts = max([r["ts"] for r in ev if r["action"].startswith(ME_ACT)] +
                [o["done"] or 0 for o in ob if o["kind"] == "zprava" and o["status"] == "hotovo"] +
                [x["ts"] for x in tm if x["dir"] == "manual"], default=0)
    bot_ts = max([r["ts"] for r in ev if r["action"].startswith("odesláno") and "dashboard" not in r["action"]] +
                 [x["ts"] for x in tm if x["dir"] == "bot"], default=0)
    out_ts = max(me_ts, bot_ts)

    preview = (t.get("preview") or "").strip()
    if preview and preview != "Nová zpráva":
        customer_last = not preview.startswith("Já:")
    else:
        customer_last = bool(m and m[-1]["dir"] == "in")
    if customer_last and in_ts and out_ts > in_ts + 1:
        customer_last = False
    if not customer_last and log_in_ts and log_in_ts > out_ts + 1:
        customer_last = True
    if customer_last and preview and not preview.startswith("Já:") and norm(preview.rstrip("…"))[:40] not in norm(in_text):
        in_text = preview

    if preview.startswith("Já:"):
        last_out = preview[3:].strip()
    else:
        last_out = next((x["text"] for x in reversed(m) if x["dir"] != "in"), "")
    last_out_by = "bot" if any(norm(last_out)[:40] and norm(last_out)[:40] in norm(s) for s in (t.get("botSent") or [])) else "ty"

    hnorm = norm(t.get("handled", ""))
    pn = norm(preview.rstrip("…"))[:60]
    inn = norm(in_text)[:60]
    handled = bool((pn and pn in hnorm) or (inn and inn in hnorm))
    if in_ts and any(r["action"] == "bez odpovědi" and r["ts"] >= in_ts - 5 for r in ev):
        handled = True
    try:
        paused_left = float(t.get("pausedUntil", 0) or 0) / 1000 - now
    except (TypeError, ValueError):
        paused_left = 0
    activity = max([r["ts"] for r in ev] + [x["ts"] for x in m] + [in_ts], default=0)
    pending = [o for o in ob if o["status"] == "ceka"]
    last_ok = max((o["done"] or 0 for o in ob if o["status"] == "hotovo" and o["kind"] == "zprava"), default=0)
    failed = [o for o in ob if o["status"] == "chyba" and o["kind"] == "zprava" and (o["done"] or o["ts"]) > last_ok]

    # co bot poslal / zákazník napsal podle deníku, ale v databázi to ještě není (bot ukládá až při dalším otevření)
    out_norms = {norm(x["text"]) for x in m if x["dir"] != "in"}
    in_join = norm(" ".join(x["text"] for x in m if x["dir"] == "in"))
    for r in ev:
        if not r["ts"] or r["ts"] < last_db - 60:
            continue
        a = r["action"]
        if a.startswith("odeslána fotka"):
            m.append({"id": None, "ts": r["ts"], "dir": "bot", "text": "", "img": 1, "virtual": True})
        elif a.startswith(SENT_ACT):
            d = "manual" if "dashboard" in a else "bot"
            for part in r["detail"].split(" / "):
                if part.strip() and norm(part) not in out_norms:
                    m.append({"id": None, "ts": r["ts"], "dir": d, "text": part.strip(), "img": 0, "virtual": True})
                    out_norms.add(norm(part))
        elif a.startswith(IN_ACT) and r["detail"]:
            for part in r["detail"].split(" / "):
                n_ = norm(part)
                if n_ and n_[:40] not in in_join:
                    m.append({"id": None, "ts": r["ts"], "dir": "in", "text": part.strip(), "img": 0, "virtual": True})
                    in_join += n_
    if not m and preview and preview != "Nová zpráva":
        mine = preview.startswith("Já:")
        m.append({"id": None, "ts": in_ts if not mine else 0, "dir": "manual" if mine else "in",
                  "text": preview[3:].strip() if mine else preview, "img": 0, "virtual": True})
    elif customer_last and in_text and norm(in_text.rstrip("…"))[:40] and norm(in_text.rstrip("…"))[:40] not in in_join:
        m.append({"id": None, "ts": in_ts, "dir": "in", "text": in_text, "img": 0, "virtual": True})
    m.sort(key=lambda x: x["ts"] or 0)

    empty = not m
    return {"name": name, "t": t, "msgs": m, "hand": hand, "in_ts": in_ts, "in_text": in_text,
            "customer_last": customer_last, "me_ts": me_ts, "out_ts": out_ts, "last_out": last_out,
            "last_out_by": last_out_by, "handled": handled, "paused_left": paused_left,
            "activity": activity, "spam": bool(SPAM.search(name)), "empty": empty, "watched": watched,
            "pending": pending, "failed": failed, "outbox": ob, "blocked": bool(t.get("blocked")),
            "handed": bool(t.get("handedOff")), "note": note, "comments": comments, "log": ev, "order": order,
            "closing": customer_last and is_closing(in_text),
            "from_comment": bool(t.get("dmFromComment")) or any(r["action"].startswith("komentář") for r in ev)}


def classify(p, now, bot_down, hidden):
    """Nastaví p['status'], p['reason'] (jedna věta česky), p['badge'], p['since'] a p['hide_key']."""
    cl, closing = p["customer_last"], p["closing"]
    status, reason, badge = None, "", ""
    hand_ts = p["hand"]["ts"] if p["hand"] else 0
    if p["blocked"]:
        status, reason = "zablokovany", "Zablokovaný – bot mu už nikdy neodpoví."
    elif p["spam"]:
        status, reason = "spam", "Vypadá to na spam."
    elif p["empty"]:
        status, reason = "prazdna", "V konverzaci zatím nejsou žádné zprávy."
    elif p["handed"]:
        owner_took = bool(p["hand"]) and p["hand"]["action"].startswith("převzato")
        hr = p["hand"]["reason"] if p["hand"] else "Bot ti konverzaci předal."
        if cl and not closing:
            status, badge, reason = "predano", "Zákazník čeká", hr
        elif not cl and not owner_took and p["me_ts"] <= hand_ts + 1:
            status, badge, reason = "predano", "Bot slíbil, že se ozveš", hr
        elif closing:
            status, reason = "dekuje", "Zákazník poděkoval nebo potvrdil – odpověď není potřeba."
        else:
            status, reason = "prevzato", "Konverzaci máš ty – bot mlčí, dokud ji nevrátíš. Čeká se na zákazníka."
    elif p["paused_left"] > 0:
        until = clock(now + p["paused_left"])
        if cl and not closing:
            status, badge = "pises_odpovedel", "Odpověděl ti"
            reason = f"Píšeš si s ním ty, proto bot ještě {dur(p['paused_left'])} mlčí (do {until})."
        elif closing:
            status, reason = "dekuje", "Zákazník poděkoval nebo potvrdil – odpověď není potřeba."
        else:
            status, reason = "pises", f"Píšeš ty – bot mlčí do {until}. Čeká se na zákazníka."
    elif cl and not p["handled"] and not closing:
        if not p["watched"] or not p["in_ts"] or now - p["in_ts"] > QUIET_DAYS * 86400:
            status = "stare"
            reason = "Zákazník psal poslední a nikdo neodpověděl. Odpověz, nebo dej Nepotřebuje odpověď."
        elif bot_down:
            status, badge = "neodpovedel", "Bot neběží"
            reason = "Bot neběží – nikdo mu neodpoví. Odpověz sám, nebo bota spusť."
        elif now - p["in_ts"] > STUCK_MIN * 60:
            status, badge = "neodpovedel", "Bot neodpověděl"
            reason = f"Zákazník napsal před {dur(now - p['in_ts'])} a bot zatím nereagoval. Odpověz sám, nebo počkej na další kontrolu."
        else:
            status, reason = "bot_pise", "Zákazník právě napsal – bot odpoví během pár minut."
    elif closing:
        if now - (p["in_ts"] or p["activity"]) < QUIET_DAYS * 86400:
            status, reason = "dekuje", "Zákazník poděkoval nebo potvrdil – odpověď není potřeba."
        else:
            status, reason = "uzavreno", "Zákazník poděkoval a dál nepsal."
    elif cl:
        status, reason = "uzavreno", "Bot usoudil, že není potřeba odpovídat."
    elif p["activity"] and now - p["activity"] < QUIET_DAYS * 86400:
        status = "hotovo"
        reason = "Bot odpověděl, čeká se na zákazníka." if p["last_out_by"] == "bot" else "Odpověděl(a) jsi, čeká se na zákazníka."
    else:
        status, reason = "uzavreno", f"Zákazník se víc než {QUIET_DAYS} dny neozval."

    o = p["order"]
    if status == "predano" and o and not (cl and p["in_ts"] > o["ts"] + 5):
        status, badge = "objednavka", ""
        reason = "Objednávka je domluvená – teď ji vyrob a pošli (záložka Objednávky)."

    if cl and p["in_ts"]:
        since = p["in_ts"]
    elif status == "predano" and p["hand"]:
        since = hand_ts
    else:
        since = 0 if status == "stare" else p["activity"]
    p["since"] = since
    p["hide_key"] = f"konv|{p['name']}|{int(since) if since else 'n%d' % len(p['msgs'])}"
    if (status in REPLY or status == "stare") and p["hide_key"] in hidden:
        status, badge, reason = "skryto", "", "Označil(a) jsi: nepotřebuje odpověď. Když zákazník napíše znovu, vrátí se to."
    p["status"], p["reason"], p["badge"] = status, reason, badge
    p["section"] = STATUS[status][1]


def orders():
    return memo("orders", _orders)


def _orders():
    """Kompletní objednávky – z poznámek, se kterými bot předal konverzaci (poslední na člověka)."""
    found = {}
    for r in log_rows():
        if r["action"].startswith("PŘEDÁNO") and ORDER.search(r["detail"]):
            found[r["who"]] = (r["ts"], r["detail"])
    for ts, kind, person, note, handoff in db_rows()["dec"]:
        if handoff and note and ORDER.search(note) and (person not in found or (ts or 0) > found[person][0] + 5):
            found[person] = (ts or 0, note)
    progress = stav()["objednavky"]
    out = []
    for person, (ts, note) in found.items():
        key = f"{person}|{int(ts)}"
        mm = re.match(r"\s*objednávk\w*\s+(?:je\s+)?\w+\s*[:–-]\s*(.*)", note, re.I)
        what, rest = None, note
        if mm:
            parts = [x.strip() for x in mm.group(1).split(",")]
            what, rest = parts[0], ", ".join(parts[1:])
        pr = progress.get(key, {}) if isinstance(progress.get(key), dict) else {}
        step = pr.get("krok", "domluveno")
        if step not in dict(ORDER_STEPS):
            step = "domluveno"
        idx = [k for k, _ in ORDER_STEPS].index(step)
        out.append({"key": key, "person": person, "ts": ts, "note": note, "what": what, "rest": rest,
                    "step": step, "idx": idx, "step_ts": pr.get("ts", ts),
                    "late": idx < 2 and time.time() - ts > 5 * 86400})
    out.sort(key=lambda o: o["ts"])
    return out


ORDER_STEPS = [("domluveno", "Domluveno"), ("vyrobeno", "Vyrobeno"), ("odeslano", "Odesláno")]


# ====================================================================== úkoly pro Přehled

def items():
    return memo("items", _items)


def _items():
    ppl = people()
    now = time.time()
    hidden = stav()["skryto"]
    reply, stuck, fb, follow = [], [], [], []
    for p in ppl.values():
        s, name = p["status"], p["name"]
        if s in REPLY:
            reply.append({"kind": "reply", "key": p["hide_key"], "p": p, "person": name, "since": p["since"],
                          "badge": p["badge"], "why": p["reason"], "urgent": True})
        elif s == "stare":
            stuck.append({"kind": "reply", "key": p["hide_key"], "p": p, "person": name, "since": p["since"],
                          "badge": "Bez odpovědi", "why": "", "urgent": False})
        if p["failed"] and s not in REPLY:
            f = p["failed"][-1]
            key = f"chyba|{name}|{f['id']}"
            if now - (f["done"] or f["ts"]) < TASK_DAYS * 86400 and key not in hidden:
                reply.append({"kind": "retry", "key": key, "p": p, "person": name, "since": f["ts"], "out": f,
                              "badge": "Zpráva neodešla", "urgent": True,
                              "why": "Bot tvoji zprávu v telefonu nedokázal napsat. " + human_error(f["error"])})
        if (s in ("hotovo", "prevzato", "pises", "uzavreno") and not p["customer_last"] and "?" in (p["last_out"] or "")
                and FOLLOWUP_DAYS * 86400 < now - p["activity"] < FOLLOWUP_MAX_DAYS * 86400 and not p["order"]):
            key = f"pripom|{name}|{int(p['activity'])}"
            if key not in hidden:
                text = "Dobrý den, máte o lampičku ještě zájem? 🙂" if formal(p["last_out"]) else "Ahoj, máš o lampičku ještě zájem? 🙂"
                follow.append({"key": key, "person": name, "since": p["activity"], "p": p, "suggest": text})

    # komentáře, na které bot nedokázal odpovědět / zájemci v komentářích
    seen = set()
    rows = db_rows()["com"]
    replied = {(r[2], norm(r[3])) for r in rows if r[5]}
    grp_of = {(r[2], norm(r[3])): r[1] for r in rows}
    fresh = [r for r in rows if not r[5] and not r[6] and not r[4] and (r[0] or 0) > now - TASK_DAYS * 86400]
    logs = log_rows()
    for i in range(len(logs) - 1, -1, -1):
        r = logs[i]
        if not r["action"].startswith("KOMENTÁŘ – CHYBA") or now - r["ts"] > TASK_DAYS * 86400:
            continue
        mm = re.match(r"„(.*?)“", r["detail"])
        text = mm.group(1) if mm else ""
        k = (r["who"], norm(text))
        later = any(x["who"] == r["who"] and x["action"].startswith("komentář –") for x in logs[i + 1:])
        talking = r["who"] in ppl and ppl[r["who"]]["msgs"]
        if k in seen or k in replied or later or talking or not (INTEREST.search(text) or "?" in text):
            seen.add(k)
            continue
        seen.add(k)
        grp = grp_of.get(k) or ""
        fb.append({"key": f"kom|{r['who']}|{norm(text)[:40]}", "person": r["who"], "since": r["ts"],
                   "badge": "Komentář bez odpovědi", "said": text, "p": ppl.get(r["who"]),
                   "why": ("Bot na komentář" + (f" ve skupině {grp}" if grp else "") + " nedokázal odpovědět. Odpověz ve Facebooku ručně.")})
    for ts, grp, person, text, *_ in fresh:
        k = (person, norm(text))
        if k in seen or not INTEREST.search(text or "") or (person in ppl and ppl[person]["msgs"]):
            continue
        seen.add(k)
        fb.append({"key": f"kom|{person}|{norm(text)[:40]}", "person": person, "since": ts, "p": None,
                   "badge": "Má zájem v komentáři", "said": text,
                   "why": f"Napsal pod příspěvek ve skupině {grp or '?'} a bot mu neodpověděl ani nenapsal zprávu. Ozvi se mu ve Facebooku."})
    for j in jobs():
        if j.get("status") == "otazky-vyres-rucne":
            fb.append({"key": f"skup|{j.get('group', '')}|{j.get('last', '')}", "person": j.get("group", "?"), "since": job_ts(j),
                       "badge": "Skupina chce odpovědi", "said": "", "p": None, "detail": j.get("error", ""),
                       "why": "Bot požádal o členství, ale na otázky ke vstupu musíš odpovědět ty (ve Facebooku → Skupiny)."})
    for r in logs:
        if r["action"].startswith("BLOKOVÁNÍ SELHALO") and now - r["ts"] < OLD_DAYS * 86400:
            fb.append({"key": f"blok|{r['who']}|{int(r['ts'])}", "person": r["who"], "since": r["ts"], "said": "",
                       "badge": "Zablokuj ručně", "p": ppl.get(r["who"]),
                       "why": "Bot chtěl tohohle člověka zablokovat, ale v telefonu se to nepovedlo. " + human_error(r["detail"])})
    fb = [c for c in fb if c["key"] not in hidden]
    reply.sort(key=lambda c: c["since"] or now)
    stuck.sort(key=lambda c: -(c["p"]["activity"] or 0))
    fb.sort(key=lambda c: -(c["since"] or 0))
    follow.sort(key=lambda f: f["since"])
    return {"reply": reply, "stuck": stuck, "fb": fb, "follow": follow}


def alerts():
    """Stav techniky v lidské řeči: (nadpis, co s tím)."""
    s = bot_status()
    out = []
    if s["running"] == 0:
        out.append(("Bot je vypnutý – nikomu se neodpovídá.",
                    "Spusť ho v Terminálu: <code>cd messenger-bot &amp;&amp; .venv/bin/python bot_telefon.py run</code>"))
    elif s["running"] > 1:
        out.append((f"Bot běží {s['running']}× najednou.", "Perou se o telefon. Jedno okno Terminálu s botem zavři (Ctrl+C)."))
    if not s["phone"]:
        out.append(("Telefon není připojený.", "Zkontroluj kabel USB a že je telefon odemčený s povoleným Laděním USB."))
    elif not s["online"]:
        since = 0
        for r in reversed(log_rows()):
            if r["who"] == "telefon" and r["action"] in ("BEZ INTERNETU", "internet zpátky"):
                since = r["ts"] if r["action"] == "BEZ INTERNETU" else 0
                break
        out.append(("Telefon nemá internet" + (f" ({ago(since)})." if since else "."),
                    "Zkontroluj Wi-Fi nebo data v telefonu. Bot počká a pak pokračuje sám."))
    now = time.time()
    pend = [o for p in people().values() for o in p["pending"]]
    if pend and s["running"]:
        oldest = min(o["ts"] for o in pend)
        if now - oldest > 180:
            out.append((f"Zpráva čeká na odeslání už {dur(now - oldest)}.",
                        "Bot je asi zrovna u příspěvků nebo komentářů. Napíše ji, jakmile skončí."))
    recent = sum(int(p.get("count", 1) or 1) for p in problems()
                 if p.get("status") == "novy" and now - float(p.get("lastTs", 0) or 0) < 3600)
    if recent >= 3:
        out.append((f"Bot se za poslední hodinu {recent}× zasekl.",
                    "Většinou si poradí sám (restartuje Messenger). Když to trvá, mrkni do <a href='/dalsi/technika'>Techniky</a>."))
    return out


def today_summary():
    today = datetime.now().date()
    rows = [r for r in log_rows() if r["ts"] and datetime.fromtimestamp(r["ts"]).date() == today]
    replied = {r["who"] for r in rows if r["action"].startswith("odesláno") and "dashboard" not in r["action"]}
    comments = sum(1 for r in rows if r["action"].startswith("komentář – odpověděl"))
    posts = (state().get("postsByDay") or {}).get(today.isoformat(), 0)
    maxp = config().get("maxPostsPerDay", 4)
    busy = sum(1 for p in people().values() if p["status"] == "bot_pise")
    n = len(replied)
    txt = (f"Dnes bot odpověděl {n} {plural(n, 'člověku', 'lidem', 'lidem')}, "
           f"na {comments} {plural(comments, 'komentář', 'komentáře', 'komentářů')} a přidal {posts} z {maxp} příspěvků.")
    if busy:
        txt += f" Právě vyřizuje {busy} {plural(busy, 'konverzaci', 'konverzace', 'konverzací')}."
    if in_quiet_hours():
        txt += " Teď je noční klid – bot jen reaguje na nové zprávy."
    return txt


def nav_counts():
    def calc():
        it = items()
        op = [o for o in orders() if o["step"] != "odeslano"]
        return {"prehled": len(it["reply"]), "objednavky": len(op), "late": any(o["late"] for o in op)}
    return memo("counts", calc)


def quick_texts():
    info = read_text(HERE / "info.md")
    acct = re.search(r"\b\d{4,12}/\d{4}\b", info)
    pre = re.search(r"Celé předem převodem:\s*(.+)", info)
    dob = re.search(r"Na dobírku:\s*(.+)", info)
    out = [("Cena", "Lampička stojí 690 Kč + doprava 🙂")]
    if pre and dob:
        out.append(("Platba", f"Jde to dvěma způsoby: 1) celé předem převodem: {pre.group(1).strip()} "
                              f"2) na dobírku: {dob.group(1).strip()}"))
    if acct:
        out.append(("Číslo účtu", f"Číslo účtu: {acct.group(0)}, do zprávy pro příjemce napište prosím své jméno."))
    out += [("Údaje k odeslání", "Pro odeslání potřebuju jméno, telefon, e-mail, dopravce (Zásilkovna nebo PPL) a adresu, případně adresu výdejního místa."),
            ("Výroba", "Výroba trvá zhruba 5–7 dní."),
            ("Děkuji", "Děkuji, ozvu se 🙂")]
    return out


def order_facts(p):
    """Telefon / e-mail / dopravce, pokud je zákazník napsal (jen nápověda)."""
    alltext = "\n".join(m["text"] for m in p["msgs"] if m["dir"] == "in")
    low = alltext.lower()
    phone = re.search(r"(\+?42[01])?\s?(\d{3}\s?\d{3}\s?\d{3})\b", alltext)
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", alltext)
    carrier = "Zásilkovna" if re.search(r"z[áa]silkovn|packeta|z-box|zbox", low) else "PPL" if re.search(r"\bppl\b", low) else ""
    return [(k, v) for k, v in (("Telefon", phone.group(0).strip() if phone else ""), ("E-mail", email.group(0) if email else ""),
                                ("Dopravce", carrier)) if v]


LOG_PLAIN = [
    ("odesláno z dashboardu", "Poslal(a) jsi zprávu z dashboardu"),
    ("odesláno", "Bot poslal zprávu"),
    ("odeslána fotka", "Bot poslal fotku"),
    ("PŘEDÁNO TOBĚ", "Bot předal konverzaci tobě"),
    ("nová zpráva, čeká na tebe", "Nová zpráva – čeká na tebe"),
    ("nová zpráva, bot pozastaven", "Nová zpráva – bot mlčí, píšeš ty"),
    ("nová zpráva", "Nová zpráva"),
    ("odpověděl jsi ručně", "Odpověděl(a) jsi v telefonu – bot chvíli mlčí"),
    ("bez odpovědi", "Bot usoudil, že není třeba odpovídat"),
    ("komentář – odpověděl", "Bot odpověděl na komentář"),
    ("komentář – poslal jsem zprávu", "Bot napsal do Messengeru (po komentáři)"),
    ("komentář – s člověkem už máme chat", "Komentář – chat už běží, zprávu neposlal"),
    ("komentář bez veřejné odpovědi", "Komentář bez veřejné odpovědi"),
    ("zpráva z komentáře nevyšla", "Zprávu po komentáři se nepovedlo poslat"),
    ("vráceno botovi", "Vráceno botovi"),
    ("převzato tebou", "Převzal(a) jsi konverzaci"),
    ("limit odpovědí bota", "Bot narazil na limit odpovědí – předáno tobě"),
    ("BLOKOVÁNÍ SELHALO", "Zablokování se nepovedlo"),
    ("ZABLOK", "Zablokován"),
    ("KOMENTÁŘ – CHYBA", "Na komentář se nepovedlo odpovědět"),
    ("KOMENTÁŘ – NÁVRH", "Bot jen navrhl odpověď na komentář"),
    ("DASHBOARD – CHYBA", "Zprávu z dashboardu se nepovedlo poslat"),
    ("NÁVRH", "Bot jen navrhl odpověď (nic neposlal)"),
    ("PŘÍSPĚVEK ZVEŘEJNĚN", "Příspěvek zveřejněn"),
    ("PŘÍSPĚVEK – CHYBA", "Příspěvek se nepovedl"),
    ("BEZ INTERNETU", "Telefon bez internetu"),
    ("internet zpátky", "Internet zpátky"),
    ("nepřečetl jsem zprávy", "Bot nepřečetl zprávy (zkusí znovu)"),
    ("žádost o členství", "Žádost o členství ve skupině"),
    ("otázky ke členství", "Skupina chce odpovědi na otázky ke členství"),
]


def plain_action(a):
    return next((v for k, v in LOG_PLAIN if a.startswith(k)), a)


# ====================================================================== vzhled

CSS = r"""
:root{
 --bg:#f4f3ef;--panel:#ffffff;--panel2:#faf9f6;--ink:#1c1b19;--mute:#6d6b66;--faint:#8d8a83;--line:#e5e2db;
 --acc:#2f6f55;--acc-ink:#fff;--accsoft:#e6f1ec;
 --ja:#c2410c;--jasoft:#fdeee4;--bot:#3559b7;--botsoft:#e9eefb;--zak:#8a6d1a;--zaksoft:#f8f1dc;--ok:#2f7a4f;--oksoft:#e4f3ea;
 --bad:#b42318;--badsoft:#fde8e6;
 --b-in:#ecebe6;--b-bot:#dcefe5;--b-man:#d9e6fb;--shadow:0 1px 2px rgba(0,0,0,.05),0 4px 16px rgba(0,0,0,.04);
 color-scheme:light;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
 --bg:#121211;--panel:#1b1b1a;--panel2:#202020;--ink:#ecebe6;--mute:#a3a09a;--faint:#85827b;--line:#2e2d2b;
 --acc:#5cc495;--acc-ink:#0d1f17;--accsoft:#1b2e25;
 --ja:#ff8a4c;--jasoft:#3a2216;--bot:#8aa8ff;--botsoft:#1c2440;--zak:#e2c26a;--zaksoft:#332b14;--ok:#6fd49a;--oksoft:#16301f;
 --bad:#ff7b72;--badsoft:#3b1a17;
 --b-in:#2a2a28;--b-bot:#1d3529;--b-man:#1e2b45;--shadow:none;color-scheme:dark;}}
:root[data-theme="dark"]{
 --bg:#121211;--panel:#1b1b1a;--panel2:#202020;--ink:#ecebe6;--mute:#a3a09a;--faint:#85827b;--line:#2e2d2b;
 --acc:#5cc495;--acc-ink:#0d1f17;--accsoft:#1b2e25;
 --ja:#ff8a4c;--jasoft:#3a2216;--bot:#8aa8ff;--botsoft:#1c2440;--zak:#e2c26a;--zaksoft:#332b14;--ok:#6fd49a;--oksoft:#16301f;
 --bad:#ff7b72;--badsoft:#3b1a17;
 --b-in:#2a2a28;--b-bot:#1d3529;--b-man:#1e2b45;--shadow:none;color-scheme:dark;}
*{box-sizing:border-box}
[hidden]{display:none!important}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,system-ui,sans-serif;
 -webkit-font-smoothing:antialiased;display:flex;flex-direction:column}
a{color:inherit}
button,textarea,input{font:inherit;color:inherit}
:focus-visible{outline:3px solid color-mix(in srgb,var(--acc) 55%,transparent);outline-offset:2px}
code{font:14px ui-monospace,Menlo,monospace;background:var(--panel2);padding:1px 6px;border-radius:6px;border:1px solid var(--line);user-select:all;word-break:break-all}

/* horní lišta */
.top{display:flex;align-items:center;gap:14px;padding:0 16px;height:58px;background:var(--panel);border-bottom:1px solid var(--line);flex:none}
.brand{font-weight:700;font-size:18px;display:flex;align-items:center;gap:8px;white-space:nowrap;text-decoration:none}
.brand i{width:12px;height:12px;border-radius:50%;background:radial-gradient(circle at 35% 35%,#ffe7a3,#f2a93b);box-shadow:0 0 10px #f2a93b99}
.tabs{display:flex;gap:2px;overflow-x:auto;scrollbar-width:none;flex:1;min-width:0}
.tabs::-webkit-scrollbar{display:none}
.tabs a{padding:8px 12px;border-radius:10px;text-decoration:none;color:var(--mute);font-weight:550;white-space:nowrap;display:flex;gap:6px;align-items:center}
.tabs a:hover{background:var(--panel2);color:var(--ink)}
.tabs a.on{background:var(--accsoft);color:var(--ink)}
.cnt{min-width:22px;height:22px;padding:0 6px;border-radius:11px;background:var(--ja);color:#fff;font-size:13px;font-weight:700;display:inline-flex;align-items:center;justify-content:center}
.cnt.soft{background:var(--line);color:var(--mute)}
.cnt.soft.late{background:var(--bad);color:#fff}
.hp{display:flex;align-items:center;gap:7px;font-size:14.5px;font-weight:600;padding:6px 12px;border-radius:999px;background:var(--oksoft);color:var(--ok);white-space:nowrap;text-decoration:none;flex:none}
.hp i{width:9px;height:9px;border-radius:50%;background:var(--ok);flex:none}
.hp.bad{color:#fff;background:var(--bad)}.hp.bad i{background:#fff}
.iconbtn{width:38px;height:38px;border-radius:10px;border:1px solid var(--line);background:var(--panel2);cursor:pointer;display:inline-flex;align-items:center;justify-content:center;flex:none}
.alert{background:var(--badsoft);color:var(--ink);border:1px solid color-mix(in srgb,var(--bad) 50%,var(--line));border-radius:12px;padding:12px 14px;margin-bottom:10px;font-size:16px}
.alert b{color:var(--bad);display:block}
.alert a{color:var(--bad)}

/* schránka: seznam | konverzace | info */
.inbox{flex:1;min-height:0;display:grid;grid-template-columns:minmax(300px,380px) minmax(0,1fr) 320px}
.col{min-height:0;min-width:0;display:flex;flex-direction:column;background:var(--panel)}
.list{border-right:1px solid var(--line)}
.side{border-left:1px solid var(--line);background:var(--panel2);overflow-y:auto}
.lhead{padding:12px;border-bottom:1px solid var(--line)}
.search{width:100%;padding:9px 12px;border-radius:10px;border:1px solid var(--line);background:var(--panel2)}
.litems{overflow-y:auto;flex:1}
.sec{position:sticky;top:0;z-index:1;background:var(--panel);padding:14px 14px 6px;font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--mute);display:flex;flex-wrap:wrap;justify-content:space-between;border-bottom:1px solid var(--line)}
.sec.s-odpovedet{color:var(--ja)}.sec.s-zaseknute{color:var(--zak)}
.sec small{flex-basis:100%;margin-top:2px;font-weight:500;text-transform:none;letter-spacing:0;font-size:13px;color:var(--faint)}
.lfold>summary{padding:10px 14px;font-size:14px;color:var(--mute);border-bottom:1px solid var(--line);cursor:pointer;list-style:none}
.lfold>summary::-webkit-details-marker{display:none}
.lfold>summary:before{content:"› ";font-weight:700}
.lfold[open]>summary:before{content:"⌄ "}
.ci{display:flex;gap:12px;padding:12px 14px;text-decoration:none;border-bottom:1px solid var(--line);position:relative}
.ci:hover{background:var(--panel2)}
.ci.sel{background:var(--accsoft)}
.ci.sel::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--acc)}
.av{width:42px;height:42px;border-radius:50%;flex:none;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:15px;background:var(--line);color:var(--mute)}
.st-odpovedet .av{background:var(--jasoft);color:var(--ja)}.st-zaseknute .av{background:var(--zaksoft);color:var(--zak)}
.st-bot .av{background:var(--botsoft);color:var(--bot)}.st-uzavrene .av{opacity:.65}
.cb{min-width:0;flex:1;display:flex;flex-direction:column;gap:2px}
.r1{display:flex;gap:8px;align-items:baseline}
.nm{font-weight:650;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tm{font-size:13px;color:var(--faint);white-space:nowrap}
.r2{font-size:15px;color:var(--mute);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.r2.fromthem{color:var(--ink);font-weight:500}
.who{color:var(--faint)}
.r3{font-size:13.5px;color:var(--mute);display:flex;gap:6px;align-items:center;min-width:0}
.r3 span.t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wait{flex:none;font-weight:700;font-size:12.5px;padding:1px 8px;border-radius:999px;background:var(--jasoft);color:var(--ja);white-space:nowrap}
.wait.hot{background:var(--ja);color:#fff}
.wait.b{background:var(--botsoft);color:var(--bot)}
.wait.z{background:var(--zaksoft);color:var(--zak)}
.tag{flex:none;font-size:12.5px;padding:1px 8px;border-radius:999px;background:var(--line);color:var(--mute);font-weight:600;white-space:nowrap}
.tag.bad{background:var(--bad);color:#fff}.tag.ok{background:var(--oksoft);color:var(--ok)}
.empty{padding:40px 24px;text-align:center;color:var(--mute)}
.empty b{display:block;color:var(--ink);font-size:19px;margin-bottom:6px}

/* konverzace */
.thread{min-width:0}
.thead{padding:10px 18px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:12px;flex:none;min-height:62px}
.thead h1{font-size:20px;margin:0;line-height:1.2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.thead .sub{font-size:14px;color:var(--mute)}
.back{display:none;text-decoration:none;font-size:16px;padding:6px 10px 6px 4px;color:var(--acc);font-weight:600}
.grow{flex:1;min-width:0}
.btn{padding:9px 16px;border-radius:10px;border:1px solid var(--line);background:var(--panel);cursor:pointer;font-weight:600;font-size:15px;white-space:nowrap;text-decoration:none;display:inline-flex;gap:6px;align-items:center;justify-content:center;min-height:40px}
.btn:hover{border-color:var(--mute)}
.btn.pri{background:var(--acc);border-color:var(--acc);color:var(--acc-ink)}
.btn.pri:hover{filter:brightness(1.07)}
.btn:disabled{opacity:.55;cursor:default}
.btn.sm{padding:5px 12px;font-size:14px;min-height:34px}
.btn.ghost{background:transparent}
.banner{margin:12px 18px 6px;padding:12px 14px;border-radius:12px;flex:none;font-size:16px}
.banner .brow{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.banner .bt{flex:1;min-width:220px}.banner .bt b{display:block}
.banner .bbtns{display:flex;gap:6px;flex-wrap:wrap}
.banner.k-odpovedet{background:var(--jasoft)}.banner.k-odpovedet .bt b{color:var(--ja)}
.banner.k-zaseknute{background:var(--zaksoft)}.banner.k-zaseknute .bt b{color:var(--zak)}
.banner.k-bot{background:var(--botsoft)}.banner.k-bot .bt b{color:var(--bot)}
.banner.k-zakaznik,.banner.k-uzavrene{background:var(--panel2);border:1px solid var(--line)}
.teach summary{cursor:pointer;color:var(--ink)}
.teach{margin-top:10px;padding-top:8px;border-top:1px solid color-mix(in srgb,var(--ja) 25%,transparent);font-size:15px}
.teach .tr{display:flex;gap:8px;margin-top:6px}
.teach input{flex:1;min-width:0;padding:7px 11px;border-radius:9px;border:1px solid var(--line);background:var(--panel)}
.teach .lst{font-size:14px;color:var(--ok);font-weight:600}
.msgs{flex:1;overflow-y:auto;-webkit-mask-image:linear-gradient(transparent 0,#000 16px);mask-image:linear-gradient(transparent 0,#000 16px);padding:14px 18px 20px;display:flex;flex-direction:column;gap:3px}
.day{align-self:center;font-size:13px;color:var(--mute);background:var(--panel2);border:1px solid var(--line);padding:2px 12px;border-radius:999px;margin:14px 0 8px}
.m{display:flex;flex-direction:column;max-width:min(560px,80%)}
.m.in{align-self:flex-start}.m.bot,.m.manual,.m.pend{align-self:flex-end;align-items:flex-end}
.bb{padding:9px 13px;border-radius:18px;white-space:pre-wrap;word-wrap:break-word;overflow-wrap:anywhere}
.m.in .bb{background:var(--b-in);border-bottom-left-radius:6px}
.m.bot .bb{background:var(--b-bot);border-bottom-right-radius:6px}
.m.manual .bb{background:var(--b-man);border-bottom-right-radius:6px}
.m.pend .bb{background:transparent;border:2px dashed var(--bot);border-bottom-right-radius:6px}
.m.err .bb{border-color:var(--bad)}
.meta{font-size:12.5px;color:var(--faint);margin:2px 6px 8px}
.meta b{font-weight:600;color:var(--mute)}
.photo{display:inline-flex;gap:6px;align-items:center;font-size:14px;color:var(--mute)}
.sys{align-self:center;text-align:center;font-size:14px;color:var(--mute);margin:8px 0;max-width:80%}
.sys.ok{color:var(--ok);font-weight:600}
.spin{display:inline-block;width:12px;height:12px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:sp 1s linear infinite;vertical-align:-1px}
@keyframes sp{to{transform:rotate(360deg)}}
.composer{border-top:1px solid var(--line);padding:10px 18px 14px;flex:none;background:var(--panel)}
.quick{display:flex;gap:6px;overflow-x:auto;padding-bottom:8px;scrollbar-width:thin}
.quick button{flex:none;font-size:13.5px;padding:4px 10px;border-radius:999px;border:1px solid var(--line);background:var(--panel2);cursor:pointer;color:var(--mute)}
.quick button:hover{color:var(--ink);border-color:var(--mute)}
.cbox{display:flex;gap:10px;align-items:flex-end}
.cbox textarea{flex:1;min-width:0;min-height:52px;max-height:40vh;resize:none;padding:12px 14px;border-radius:14px;border:1px solid var(--line);background:var(--panel2);line-height:1.45}
.cbox textarea:focus{border-color:var(--acc);outline:none;box-shadow:0 0 0 3px color-mix(in srgb,var(--acc) 22%,transparent)}
.send{height:52px;padding:0 20px;font-size:16px}
.chint{font-size:13.5px;color:var(--faint);margin-top:6px;display:flex;gap:10px;justify-content:space-between;flex-wrap:wrap}
.st{font-weight:600}
.st.sending{color:var(--bot)}.st.done{color:var(--ok)}.st.err{color:var(--bad)}.st.warn{color:var(--ja)}
kbd{font:12px ui-monospace,Menlo,monospace;border:1px solid var(--line);border-bottom-width:2px;border-radius:5px;padding:0 5px;background:var(--panel2)}

/* panel se zákazníkem */
.sbox{padding:16px 18px;border-bottom:1px solid var(--line)}
.sbox h3{margin:0 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--mute)}
.sbox p{margin:0;font-size:15.5px}
.facts{list-style:none;margin:6px 0 0;padding:0;font-size:15px}
.facts li{display:flex;gap:8px;padding:2px 0}
.facts .lbl{color:var(--mute);min-width:80px}
.facts .val{flex:1;min-width:0;overflow-wrap:anywhere;user-select:all}
.cmt{font-size:15px;padding:6px 0}
.cmt .g{font-size:13px;color:var(--faint)}
.cmt .rp{color:var(--mute);font-size:14px;margin-top:2px}
.hist{list-style:none;margin:0;padding:0;font-size:14px}
.hist li{display:flex;gap:8px;padding:3px 0}
.hist .w{color:var(--faint);min-width:78px;flex:none}
.sideclose{display:none}
.nothread{flex:1;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:14px;color:var(--mute);text-align:center;padding:30px}
.nothread>b{font-size:22px;color:var(--ink)}

/* ostatní stránky */
.page{flex:1;overflow-y:auto}
.wrap{max-width:860px;margin:0 auto;padding:22px 16px 60px;overflow-wrap:anywhere}
.wrap.wide{max-width:980px}
.wrap h1{font-size:26px;margin:4px 0 4px;line-height:1.2}
.lead{color:var(--mute);margin:0 0 18px;font-size:16px}
.wrap h2{font-size:15px;text-transform:uppercase;letter-spacing:.05em;color:var(--mute);margin:28px 0 10px;display:flex;gap:8px;align-items:center}
.wrap h2.ja{color:var(--ja)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin-bottom:10px;box-shadow:var(--shadow);transition:opacity .3s}
.card.ja{border-color:color-mix(in srgb,var(--ja) 45%,var(--line));border-left:5px solid var(--ja)}
.card.zak{border-left:5px solid var(--zak)}
.card.okl{border-left:5px solid var(--ok)}
.card.badl{border-left:5px solid var(--bad)}
.card.gone{opacity:0}
.ch{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:4px}
.ch .nml{font-weight:650;font-size:18.5px;text-decoration:none}
.ch a.nml:hover{text-decoration:underline}
.ch .right{margin-left:auto}
.why{color:var(--mute);font-size:15.5px;margin:2px 0 10px}
.mini{display:flex;flex-direction:column;gap:2px;margin:6px 0 10px;padding:10px 12px;background:var(--panel2);border-radius:12px}
.mini .m{max-width:88%}
.mini .meta{margin-bottom:4px}
.rc .composer{border:0;padding:0;background:none}
.acts{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px}
.acts .more{margin-left:auto;font-size:15px;color:var(--acc);text-decoration:none;font-weight:600}
.acts .more:hover{text-decoration:underline}
.said{margin:6px 0 10px;padding:9px 13px;background:var(--b-in);border-radius:6px 16px 16px 16px;white-space:pre-wrap}
.hero{display:flex;gap:16px;align-items:center;padding:20px 22px;margin-bottom:18px}
.hero .ic{flex:none;width:54px;height:54px;border-radius:50%;display:grid;place-items:center;font-size:28px;font-weight:800;background:var(--oksoft);color:var(--ok)}
.hero.ja .ic{background:var(--jasoft);color:var(--ja)}
.hero h1{margin:0!important}.hero p{margin:4px 0 0;color:var(--mute)}
.fold{margin:6px 0 10px}
.fold>summary{cursor:pointer;list-style:none;padding:10px 0;font-size:15px;text-transform:uppercase;letter-spacing:.05em;color:var(--mute);font-weight:700;display:flex;gap:8px;align-items:center}
.fold>summary::-webkit-details-marker{display:none}
.fold>summary:before{content:"›";display:inline-block;transition:transform .15s;font-size:20px;line-height:1}
.fold[open]>summary:before{transform:rotate(90deg)}
.fold>summary .cnt{text-transform:none;letter-spacing:0}
.fold .copy{color:var(--mute);font-size:15px;margin:-4px 0 10px}
.fold.z>summary{color:var(--zak)}
details.mini2 summary{cursor:pointer;color:var(--mute);font-size:14px;margin-top:6px}
.chip{display:inline-flex;align-items:center;font-size:13px;font-weight:650;padding:2px 10px;border-radius:999px;background:var(--line);color:var(--mute);white-space:nowrap}
.chip.ok{background:var(--oksoft);color:var(--ok)}.chip.ja{background:var(--jasoft);color:var(--ja)}
.chip.info{background:var(--botsoft);color:var(--bot)}.chip.zak{background:var(--zaksoft);color:var(--zak)}.chip.bad{background:var(--badsoft);color:var(--bad)}
.small{font-size:14px;color:var(--mute)}
.crow{display:flex;gap:14px;align-items:flex-start}
.thumb{width:92px;height:92px;object-fit:cover;border-radius:10px;border:1px solid var(--line);flex:none;background:var(--panel2)}
.shot{width:120px;max-height:220px;object-fit:cover;object-position:top;border-radius:10px;border:1px solid var(--line);flex:none}
.kpis{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:6px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px 16px;min-width:140px;flex:1}
.kpi b{font-size:26px;display:block;line-height:1.1}
.kpi span{color:var(--mute);font-size:14px}
.ta{width:100%;min-height:300px;padding:14px;border-radius:12px;border:1px solid var(--line);background:var(--panel2);font:15.5px/1.55 ui-monospace,Menlo,monospace;resize:vertical}
.inp{width:100%;padding:10px 12px;border-radius:10px;border:1px solid var(--line);background:var(--panel2)}
.pair{display:grid;gap:4px;padding:10px 0;border-bottom:1px solid var(--line)}
.pair:last-child{border:0}
.pair .c{background:var(--b-in);padding:6px 11px;border-radius:14px;justify-self:start;max-width:85%}
.pair .s{background:var(--b-man);padding:6px 11px;border-radius:14px;justify-self:end;max-width:85%}
.lesson{padding:8px 0;border-bottom:1px solid var(--line)}
.lesson:last-child{border-bottom:0}
.steps{display:flex;gap:6px;align-items:center;font-size:14px;color:var(--mute);margin:8px 0 4px;flex-wrap:wrap}
.steps span{padding:2px 10px;border-radius:999px;background:var(--panel2);border:1px solid var(--line)}
.steps span.done{background:var(--oksoft);color:var(--ok);border-color:transparent;font-weight:600}
.make{font-size:18px;margin:2px 0}
.rows .row{display:flex;gap:12px;align-items:flex-start;padding:12px 2px;border-bottom:1px solid var(--line)}
.rows .row:last-child{border-bottom:0}
.rows .main{flex:1;min-width:0}
.rows .sidec{text-align:right;flex:none;font-size:14px;color:var(--mute)}
.filt{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 12px}
.filt button{font-size:14px;padding:5px 12px;border-radius:999px;border:1px solid var(--line);background:var(--panel);cursor:pointer;color:var(--mute);font-weight:550}
.filt button.on{background:var(--ink);border-color:var(--ink);color:var(--panel)}
.subnav{display:flex;gap:4px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:4px;margin:0 0 18px;overflow-x:auto;scrollbar-width:none}
.subnav a{flex:1;text-align:center;padding:8px 12px;border-radius:9px;text-decoration:none;color:var(--mute);font-weight:600;white-space:nowrap}
.subnav a.on{background:var(--accsoft);color:var(--ink)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}
.tile b{display:flex;align-items:center;gap:8px;font-size:17px}
.tile div{color:var(--mute);font-size:15px;margin-top:4px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--ok);flex:none}
.dot.bad{background:var(--bad)}.dot.mute{background:var(--faint)}
.ev{display:flex;gap:12px;padding:7px 0;border-bottom:1px solid var(--line);font-size:15px}
.ev:last-child{border-bottom:0}
.ev .t{color:var(--faint);width:96px;flex:none}
.toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:12px 20px;border-radius:12px;font-size:16px;z-index:30;max-width:calc(100% - 32px);box-shadow:0 6px 24px rgba(0,0,0,.25);transition:opacity .4s}
.plink{text-decoration:none;font-weight:650}
.plink:hover{text-decoration:underline}

/* menší displeje */
@media (max-width:1240px){
 .inbox{grid-template-columns:minmax(290px,350px) minmax(0,1fr)}
 .side{position:fixed;right:0;top:0;bottom:0;width:min(380px,92vw);z-index:20;transform:translateX(105%);transition:transform .2s;box-shadow:-10px 0 30px rgba(0,0,0,.18)}
 body.sideopen .side{transform:none}
 .sideclose{display:flex;justify-content:flex-end;padding:10px 12px 0}
}
@media (min-width:1241px){.infobtn{display:none}.thread.wide{grid-column:span 2}}
@media (max-width:820px){
 .top{flex-wrap:wrap;height:auto;gap:6px 8px;padding:8px 12px 6px}
 .brand{flex:1}
 .tabs{order:3;flex-basis:100%;margin:0 -6px}
 .tabs a{padding:7px 10px}
 .inbox{grid-template-columns:minmax(0,1fr)}
 body.hassel .list{display:none}
 body:not(.hassel) .thread{display:none}
 .back{display:inline-block}
 .thead{padding:8px 12px;min-height:54px}
 .banner{margin:10px 12px 0;padding:10px 12px}.banner .bt .small,.teach summary .small{display:none}
 .msgs{padding:10px 12px}
 .m{max-width:88%}
 .composer{padding:8px 10px 12px}
 .send{padding:0 14px}
 .chint .keys{display:none}
 .crow{flex-wrap:wrap}
 .wrap{padding:16px 14px 50px}
 .wrap h1{font-size:23px}
 .hero{padding:16px}
 .hero .ic{width:44px;height:44px;font-size:22px}
 .acts .more{margin-left:0}
 .card{padding:12px 14px}
}
"""

ICON_INFO = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/></svg>'
ICON_THEME = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="8"/><path d="M12 4a8 8 0 0 0 0 16z" fill="currentColor"/></svg>'
ICON_PHOTO = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="11" r="2"/><path d="m21 17-5-5-8 7"/></svg>'

THEME_JS = r"""(function(){try{var t=localStorage.getItem('theme');if(t)document.documentElement.setAttribute('data-theme',t);}catch(e){}})();"""

COMMON_JS = r"""
const CFG=JSON.parse(document.getElementById('CFG').textContent);
const $=(s,r)=>(r||document).querySelector(s), $$=(s,r)=>[...(r||document).querySelectorAll(s)];
let BUSY=0;
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function cycleTheme(){var r=document.documentElement,c=r.getAttribute('data-theme'),
 n=c==='light'?'dark':c==='dark'?null:'light';if(n)r.setAttribute('data-theme',n);else r.removeAttribute('data-theme');
 try{n?localStorage.setItem('theme',n):localStorage.removeItem('theme')}catch(e){}
 var b=document.getElementById('themebtn');if(b)b.title='Vzhled: '+(n==='light'?'světlý':n==='dark'?'tmavý':'podle systému');}
function toast(msg){let t=$('.toast');if(!t){t=document.createElement('div');t.className='toast';t.setAttribute('role','status');document.body.appendChild(t)}
 t.textContent=msg;t.style.opacity=1;clearTimeout(t._h);t._h=setTimeout(()=>{t.style.opacity=0},4500)}
function applyTop(j){if(!j)return;if(j.health!=null){const hb=$('#health');if(hb)hb.outerHTML=j.health}
 if(j.counts)for(const k in j.counts){const el=document.querySelector('[data-cnt="'+k+'"]');if(el){el.textContent=j.counts[k]||'';el.hidden=!j.counts[k];
  if(k==='objednavky')el.classList.toggle('late',!!j.counts.late)}}
 if(j.counts){const t=document.title.replace(/^\(\d+\) /,'');document.title=(j.counts.prehled?'('+j.counts.prehled+') ':'')+t}}
async function refreshTop(){try{applyTop(await (await fetch('/api/stav')).json())}catch(e){}}
async function post(url,body){const fd=new FormData();for(const k in body)fd.append(k,body[k]);
 try{const r=await fetch(url,{method:'POST',body:fd,headers:{'X-Requested-With':'fetch'}});
  try{return await r.json()}catch(e){return {ok:false,error:'Chyba dashboardu ('+r.status+')'}}}
 catch(e){return {ok:false,error:'Dashboard neodpovídá – běží?'}}}
function autosize(t){if(!t)return;t.style.height='auto';t.style.height=Math.min(t.scrollHeight,innerHeight*.4)+'px'}
function dk(t){return 'draft:'+t.dataset.person}
function saveDraft(t){try{t.value.trim()&&t.value!==t.defaultValue?localStorage.setItem(dk(t),t.value):localStorage.removeItem(dk(t))}catch(e){}}
function restoreDrafts(root){$$('.cmp textarea[data-person]',root).forEach(t=>{try{const d=localStorage.getItem(dk(t));if(d)t.value=d}catch(e){}autosize(t)})}
function boxOf(el){return el.closest('.cmp')||$('#composer')}
function setSt(box,cls,html){const e=box&&$('.st',box);if(e){e.className='st '+cls;e.innerHTML=html}}
function insertText(t,s){if(!t)return;const a=t.selectionStart,b=t.selectionEnd,v=t.value;
 const pre=v.slice(0,a),sep=pre&&!/\s$/.test(pre)?' ':'';t.value=pre+sep+s+v.slice(b);t.focus();
 const p=(pre+sep+s).length;t.setSelectionRange(p,p);autosize(t);saveDraft(t)}
function emit(n){document.dispatchEvent(new CustomEvent(n))}
async function sendFrom(box,override){
 if(!box)return;const ta=$('textarea',box),person=box.dataset.person;
 const text=(override!=null?override:(ta?ta.value:'')).trim();if(!text){if(ta)ta.focus();return}
 const b=$('[data-send]',box);if(b)b.disabled=true;
 const j=await post('/api/poslat',{person,text});if(b)b.disabled=false;
 if(!j.ok){setSt(box,'err',esc(j.error||'Nepovedlo se'));return}
 if(override==null&&ta){ta.value='';ta.defaultValue='';autosize(ta);saveDraft(ta)}
 box.dataset.last=text;
 setSt(box,'sending','<span class="spin"></span> Odesílá se přes telefon…'+(CFG.botDown?' Bot ale neběží – odejde, až ho spustíš.':' Bot to napíše zhruba do 20 s.'));
 watch(box,j.id,'zprava');emit('queued')}
function watch(box,id,kind){BUSY++;const iv=setInterval(async()=>{let j;try{j=await (await fetch('/api/odeslani/'+id)).json()}catch(e){return}
 if(j.status==='ceka')return;clearInterval(iv);BUSY--;
 if(j.status==='hotovo'){const msg=kind==='zprava'?'Odesláno ✓ (bot teď '+CFG.pauseH+' h mlčí)':kind==='botovi'?'Hotovo ✓ Bot tu zase odpovídá.':'Hotovo ✓ Bot tu mlčí, píšeš ty.';
  setSt(box,'done',msg);if(box.matches('article'))toast((box.dataset.person||'')+': '+msg)}
 else setSt(box,'err','Neodešlo – '+esc(j.error||'neznámá chyba')+(kind==='zprava'?' <button type="button" class="btn sm" data-retry-last>Zkusit znovu</button>':''));
 emit('settled')},2500)}
async function act(box,kind){if(!box)return;const ta=$('textarea',box);const text=kind==='botovi'&&ta?ta.value.trim():'';
 const j=await post('/api/akce',{person:box.dataset.person,kind,text});
 if(!j.ok){setSt(box,'err',esc(j.error||'Nepovedlo se'));return}
 if(text&&ta){ta.value='';autosize(ta);saveDraft(ta)}
 setSt(box,'sending','<span class="spin"></span> '+(kind==='botovi'?(text?'Pošlu zprávu a pak vrátím konverzaci botovi…':'Vracím konverzaci botovi…'):'Říkám botovi, ať tu mlčí…')+' (do ~20 s)');
 watch(box,j.id,kind);emit('queued')}
async function hideIt(el){const card=el.closest('article.card');const j=await post('/skryt',{key:el.dataset.hide});
 if(!j.ok){toast(j.error||'Nepovedlo se');return}
 if(card){card.classList.add('gone');setTimeout(()=>{card.remove();emit('settled')},320)}else emit('settled');
 toast('Označeno. Když zákazník napíše znovu, vrátí se to.')}
async function lesson(form){const inp=$('input',form);const v=inp.value.trim();if(!v){inp.focus();return}
 const j=await post('/uceni/pridat',{lekce:v});const st=$('.lst',form);
 if(j.ok){inp.value='';if(st)st.textContent='Uloženo ✓ Bot to použije u další odpovědi.'}else if(st)st.textContent=j.error||'Nepovedlo se'}
document.addEventListener('click',e=>{
 const q=e.target.closest('[data-quick]');if(q){insertText($('textarea',boxOf(q)),q.dataset.quick);return}
 const s=e.target.closest('[data-send]');if(s){e.preventDefault();sendFrom(boxOf(s));return}
 const a=e.target.closest('[data-act]');if(a){e.preventDefault();act(a.closest('.cmp')||$('#composer'),a.dataset.act);return}
 const r=e.target.closest('[data-retry]');if(r){e.preventDefault();sendFrom(r.closest('.cmp')||$('#composer'),r.dataset.retry);return}
 const rl=e.target.closest('[data-retry-last]');if(rl){const b=boxOf(rl);sendFrom(b,b.dataset.last);return}
 const hd=e.target.closest('[data-hide]');if(hd){e.preventDefault();hideIt(hd);return}
 const f=e.target.closest('.filt button');if(f){const c=f.closest('.filt');$$('button',c).forEach(x=>x.classList.toggle('on',x===f));
  $$('[data-f]',$(c.dataset.target)).forEach(r=>{r.hidden=!!f.dataset.v&&(' '+r.dataset.f+' ').indexOf(' '+f.dataset.v+' ')<0});return}
});
document.addEventListener('submit',e=>{const f=e.target;
 if(f.matches('.lessonform')){e.preventDefault();lesson(f)}
 else if(f.matches('.cmp')){e.preventDefault();sendFrom(f)}
 else{const b=e.submitter;if(b&&b.dataset.busy)setTimeout(()=>{b.disabled=true;b.textContent=b.dataset.busy},0)}});
document.addEventListener('input',e=>{const t=e.target;if(t.matches('.cmp textarea')){autosize(t);saveDraft(t)}});
document.addEventListener('keydown',e=>{const t=e.target;
 if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)&&t.matches&&t.matches('.cmp textarea')){e.preventDefault();sendFrom(boxOf(t))}});
document.addEventListener('DOMContentLoaded',()=>{restoreDrafts(document);
 const t=$('.toast');if(t){setTimeout(()=>{t.style.opacity=0},4500);try{history.replaceState(null,'',location.pathname+location.hash)}catch(e){}}
 if(!window.OWN_REFRESH)setInterval(()=>{if(!document.hidden)refreshTop()},30000)});
"""

PREHLED_JS = r"""
window.OWN_REFRESH=true;
async function refreshMain(){
 if(BUSY||document.hidden)return;
 let j;try{j=await (await fetch('/api/prehled')).json()}catch(e){return}
 applyTop(j);if(BUSY)return;
 const main=$('#main'),tpl=document.createElement('div');tpl.innerHTML=j.html;
 const act=document.activeElement;
 const keep=$$('.cmp',main).filter(c=>c.contains(act)||$$('textarea,input',c).some(t=>t.value.trim()&&t.value!==t.defaultValue)||$('.st.sending,.st.err',c));
 for(const c of keep){const n=c.dataset.id&&tpl.querySelector('[data-id="'+CSS.escape(c.dataset.id)+'"]');if(!n)return;}
 const open={};$$('details[data-k]',main).forEach(d=>open[d.dataset.k]=d.open);
 $$('details[data-k]',tpl).forEach(d=>{if(d.dataset.k in open)d.open=open[d.dataset.k]});
 const sel=act&&act.selectionStart!=null?[act.selectionStart,act.selectionEnd]:null;
 for(const c of keep){tpl.querySelector('[data-id="'+CSS.escape(c.dataset.id)+'"]').replaceWith(c)}
 restoreDrafts(tpl);main.replaceChildren(...tpl.childNodes);
 if(act&&main.contains(act)){act.focus();if(sel)try{act.setSelectionRange(sel[0],sel[1])}catch(e){}}
}
setInterval(refreshMain,20000);
document.addEventListener('settled',()=>setTimeout(refreshMain,5000));
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshMain()});
"""

INBOX_JS = r"""
window.OWN_REFRESH=true;
const S=JSON.parse(document.getElementById('S').textContent);
const deacc=s=>(s||'').normalize('NFD').replace(/[̀-ͯ]/g,'').toLowerCase();
let pollTimer=null;
function nearBottom(el){return el.scrollHeight-el.scrollTop-el.clientHeight<120}
function toBottom(){const m=$('#msgs');if(m)m.scrollTop=m.scrollHeight}
function applySearch(){const q=deacc($('#search').value.trim());
 $$('#litems .ci').forEach(a=>{a.hidden=!!q&&!deacc(a.dataset.name).includes(q)});
 $$('#litems .sec').forEach(s=>{s.hidden=!$$('#litems .ci[data-sec="'+s.dataset.sec+'"]').some(a=>!a.hidden)});
 $$('#litems details').forEach(d=>{if(q)d.open=$$('.ci',d).some(a=>!a.hidden)});}
async function refreshList(){
 try{const r=await fetch('/api/zpravy?c='+encodeURIComponent(S.person||''));const j=await r.json();
  const li=$('#litems'),st=li.scrollTop,op={};$$('details[data-k]',li).forEach(d=>op[d.dataset.k]=d.open);
  li.innerHTML=j.list;$$('details[data-k]',li).forEach(d=>{if(d.dataset.k in op)d.open=op[d.dataset.k]});li.scrollTop=st;applySearch();
  applyTop(j);
  if(S.person&&j.sig!==S.sig)await loadThread();
 }catch(e){}
 schedule();
}
function schedule(){clearTimeout(pollTimer);pollTimer=setTimeout(refreshList,S.pending||BUSY?3000:10000)}
async function loadThread(){
 if(!S.person)return;
 let j;try{const r=await fetch('/api/vlakno/'+encodeURIComponent(S.person));if(!r.ok)return;j=await r.json()}catch(e){return}
 const m=$('#msgs'),stick=nearBottom(m);
 const bn=$('#banner'),li=bn&&$('input',bn),td=bn&&$('#teach');
 if(bn&&!(li&&(li.value.trim()||document.activeElement===li))){const op=td&&td.open;bn.outerHTML=j.banner;const nt=$('#teach');if(op&&nt)nt.open=true}
 m.innerHTML=j.msgs;$('#sidein').innerHTML=j.side;
 S.sig=j.sig;S.pending=j.pending;if(stick)toBottom();
}
document.addEventListener('queued',()=>{S.pending=true;loadThread().then(toBottom);schedule()});
document.addEventListener('settled',()=>{setTimeout(refreshList,800)});
document.addEventListener('DOMContentLoaded',()=>{
 toBottom();applySearch();
 $('#search').addEventListener('input',applySearch);
 const ta=$('#tx');if(ta&&window.innerWidth>820)ta.focus();
 document.addEventListener('keydown',e=>{const typing=/INPUT|TEXTAREA/.test(document.activeElement.tagName);
  if(e.key==='/'&&!typing){e.preventDefault();$('#search').focus()}
  if(e.key==='Escape'){if(document.body.classList.contains('sideopen'))document.body.classList.remove('sideopen');else if(typing)document.activeElement.blur()}});
 schedule();
});
"""

FLASH = {
    "odeslano": "Zpráva je ve frontě. Bot ji v telefonu napíše do ~20 s.",
    "skryto": "Označeno. Když zákazník napíše znovu, vrátí se to.",
    "obj": "Objednávka posunuta.",
    "problem": "Označeno jako vyřešené.",
    "lekce": "Uloženo – bot to použije u další odpovědi.",
    "info": "Uloženo – bot to použije u další odpovědi.",
    "prazdne": "Prázdné – nic jsem neuložil.",
}


def initials(name):
    parts = [x for x in re.split(r"\s+", name or "") if x and x[0].isalpha()]
    return ("".join(x[0] for x in parts[:2]) or "?").upper()


def conv_url(person):
    return "/zpravy/" + quote(person or "", safe="")


def health_html(s=None):
    s = s or bot_status()
    probs = []
    if s["running"] == 0:
        probs.append("Bot neběží")
    elif s["running"] > 1:
        probs.append(f"Bot běží {s['running']}×")
    if not s["phone"]:
        probs.append("Telefon odpojený")
    elif not s["online"]:
        probs.append("Telefon bez internetu")
    if not probs:
        tip = "Bot běží, telefon je připojený a má internet." + (" Teď je noční klid." if in_quiet_hours() else "")
        return f'<a class="hp" id="health" href="/dalsi/technika" title="{h(tip)}"><i></i><span>Vše běží</span></a>'
    return (f'<a class="hp bad" id="health" href="/dalsi/technika" title="Podrobnosti v Technice">'
            f'<i></i><span>{h(" · ".join(probs))}</span></a>')


def page(title, active, body, body_class="", scripts="", wrap="", raw=False):
    try:
        counts = nav_counts()
    except Exception:  # noqa: BLE001
        counts = {"prehled": 0, "objednavky": 0, "late": False}
    tabs = [("prehled", "Přehled", "/"), ("zpravy", "Zprávy", "/zpravy"), ("objednavky", "Objednávky", "/objednavky"), ("dalsi", "Další", "/dalsi")]
    nav = ""
    for k, t, u in tabs:
        cnt = ""
        if k == "prehled":
            cnt = f'<span class="cnt" data-cnt="prehled" {"" if counts["prehled"] else "hidden"}>{counts["prehled"] or ""}</span>'
        elif k == "objednavky":
            cnt = (f'<span class="cnt soft{" late" if counts["late"] else ""}" data-cnt="objednavky" '
                   f'title="{"Některá objednávka už by měla být hotová" if counts["late"] else "Rozpracované objednávky"}" '
                   f'{"" if counts["objednavky"] else "hidden"}>{counts["objednavky"] or ""}</span>')
        nav += f'<a class="{"on" if k == active else ""}" href="{u}">{t}{cnt}</a>'
    flash = FLASH.get(request.args.get("ok", ""))
    toast = f'<div class="toast" role="status">{h(flash)}</div>' if flash else ""
    cfg = json.dumps({"pauseH": pause_hours(), "botDown": bot_status()["running"] == 0}).replace("</", "<\\/")
    if not raw:
        body = f'<main class="page"><div class="wrap {wrap}" id="main">{body}</div></main>'
    t = (f"({counts['prehled']}) " if counts["prehled"] else "") + title
    return (
        '<!doctype html><html lang="cs"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<meta name="color-scheme" content="light dark">'
        f'<title>{h(t)} – Lampičky</title><script>{THEME_JS}</script><style>{CSS}</style></head>'
        f'<body class="{body_class}"><header class="top"><a class="brand" href="/"><i></i><span>Lampičky</span></a>'
        f'<nav class="tabs" aria-label="Hlavní menu">{nav}</nav>{health_html()}'
        f'<button class="iconbtn" id="themebtn" onclick="cycleTheme()" title="Přepnout světlý/tmavý vzhled" aria-label="Vzhled">{ICON_THEME}</button></header>'
        f'{body}{toast}<script type="application/json" id="CFG">{cfg}</script><script>{COMMON_JS}</script>{scripts}</body></html>')


# ---------- bubliny ----------

def bubble(m, show_meta=True):
    labels = {"in": "", "bot": "Bot", "manual": "Ty"}
    content = h(m["text"])
    if m.get("img"):
        content = (content + "<br>" if content else "") + f'<span class="photo">{ICON_PHOTO} fotka</span>'
    if not content:
        return ""
    meta = ""
    if show_meta and (m["ts"] or m["dir"] != "in"):
        lab = labels.get(m["dir"], "")
        bits = ([f"<b>{lab}</b>"] if lab else []) + ([h(clock(m["ts"]))] if m["ts"] else [])
        meta = f'<div class="meta">{" · ".join(bits)}</div>'
    d = m["dir"] if m["dir"] in labels else "in"
    return f'<div class="m {d}"><div class="bb">{content}</div>{meta}</div>'


def outbox_html(p, recent_ok=True):
    out = []
    now = time.time()
    ob = p["outbox"]
    for o in ob:
        if o["kind"] != "zprava":
            continue
        if o["status"] == "ceka":
            out.append(f'<div class="m pend"><div class="bb">{h(o["text"])}</div>'
                       f'<div class="meta"><span class="spin"></span> <b>Odesílá se přes telefon…</b> bot to napíše zhruba do 20 s</div></div>')
        elif o["status"] == "chyba" and now - (o["done"] or o["ts"]) < TASK_DAYS * 86400 and not any(
                x["ts"] > o["ts"] and x["status"] == "hotovo" and x["kind"] == "zprava" for x in ob):
            out.append(f'<div class="m pend err"><div class="bb">{h(o["text"])}</div>'
                       f'<div class="meta" style="color:var(--bad)"><b>Neodešlo.</b> {h(short(human_error(o["error"]), 160))} '
                       f'<button type="button" class="btn sm" data-retry="{h(o["text"])}">Zkusit znovu</button></div></div>')
        elif recent_ok and o["status"] == "hotovo" and o["done"] and now - o["done"] < 600:
            out.append(f'<div class="sys ok">Odesláno ✓ – bot zprávu napsal v telefonu ({h(clock(o["done"]))})</div>')
    return "".join(out)


def msgs_html(p):
    out, prev_day, old_shown = [], None, False
    ms = [m for m in p["msgs"] if m["text"] or m.get("img")]
    for i, m in enumerate(ms):
        if m["ts"]:
            d = datetime.fromtimestamp(m["ts"]).date()
            if d != prev_day:
                out.append(f'<div class="day">{day_label(m["ts"])}</div>')
                prev_day = d
        elif not old_shown:
            out.append('<div class="day">Starší zprávy</div>')
            old_shown = True
        nxt = ms[i + 1] if i + 1 < len(ms) else None
        end_group = not nxt or nxt["dir"] != m["dir"] or (nxt["ts"] - m["ts"] > 900)
        out.append(bubble(m, end_group))
    if not ms:
        out.append('<div class="sys">Zprávy z téhle konverzace bot ještě neuložil.</div>')
    out.append(outbox_html(p))
    return "".join(out)


def mini_msgs(p, n=3):
    ms = [m for m in p["msgs"] if m["text"] or m.get("img")][-n:]
    inner = "".join(bubble(m) for m in ms) + outbox_html(p, recent_ok=False)
    return f'<div class="mini">{inner}</div>' if inner else ""


def quick_html():
    return "".join(f'<button type="button" data-quick="{h(v)}" title="{h(v)}">{h(k)}</button>' for k, v in quick_texts())


def composer_warn():
    s = bot_status()
    if s["running"] == 0:
        return '<span class="st warn">Bot neběží – zpráva odejde, až ho spustíš.</span>'
    if not s["phone"]:
        return '<span class="st warn">Telefon je odpojený – zpráva počká.</span>'
    if not s["online"]:
        return '<span class="st warn">Telefon je bez internetu – zpráva počká.</span>'
    return '<span class="st">Bot zprávu napíše v telefonu za tebe.</span>'


def wait_badge(since, cls=""):
    if not since:
        return ""
    w = time.time() - since
    return f'<span class="wait {cls or ("hot" if w > 2 * 3600 else "")}">čeká {dur(w)}</span>'


# ---------- karty na Přehledu ----------

def reply_card(c, stuck=False):
    p = c["p"]
    person = c["person"]
    extra = '<span class="chip ok">objednávka</span>' if p and p["order"] else ""
    if c["kind"] != "retry" and p and p["failed"] and not stuck:
        extra += '<span class="chip bad">neodešlo</span>'
    if p and p["pending"]:
        extra += '<span class="chip info">odesílá se…</span>'
    right = wait_badge(c["since"], "z" if stuck else "") if c["since"] else f'<span class="small">{h(ago(p["activity"]))}</span>'
    head = (f'<div class="ch"><a class="nml" href="{conv_url(person)}">{h(person)}</a>'
            f'<span class="chip {"zak" if stuck else "ja"}">{h(c["badge"])}</span>{extra}<span class="right">{right}</span></div>'
            + (f'<div class="why">{h(c["why"])}</div>' if c["why"] else ""))
    acts_tail = f'<a class="more" href="{conv_url(person)}">Otevřít chat →</a>'
    if c["kind"] == "retry":
        o = c["out"]
        return (f'<article class="card ja rc cmp" data-person="{h(person)}" data-id="retry:{h(c["key"])}">{head}'
                f'<div class="mini"><div class="m pend err"><div class="bb">{h(o["text"])}</div></div></div>'
                f'<div class="chint"><span class="st"></span></div>'
                f'<div class="acts"><button type="button" class="btn pri" data-retry="{h(o["text"])}">Neodešlo – Zkusit znovu</button>'
                f'<button type="button" class="btn ghost sm" data-hide="{h(c["key"])}">Nepotřebuje odpověď</button>{acts_tail}</div></article>')
    ret = ""
    if p["handed"] or p["paused_left"] > 0:
        ret = ('<button type="button" class="btn sm" data-act="botovi" title="Bot bude v konverzaci zase odpovídat sám. '
               'Když něco napíšeš, nejdřív se to odešle.">Vrátit botovi</button>')
    return (f'<article class="card {"zak" if stuck else "ja"} rc cmp" data-person="{h(person)}" data-id="r:{h(person)}">{head}'
            f'{mini_msgs(p)}'
            f'<div class="quick">{quick_html()}</div>'
            f'<div class="cbox"><textarea rows="1" data-person="{h(person)}" placeholder="Napiš odpověď…" aria-label="Odpověď pro {h(person)}"></textarea>'
            f'<button type="button" class="btn pri send" data-send>Odeslat</button></div>'
            f'<div class="chint">{composer_warn()}</div>'
            f'<div class="acts">{ret}<button type="button" class="btn ghost sm" data-hide="{h(c["key"])}" '
            f'title="Vyřízeno jinak – zmizí odsud. Vrátí se, když zákazník napíše znovu.">Nepotřebuje odpověď</button>{acts_tail}</div></article>')


def fb_card(c):
    p = c.get("p")
    name = (f'<a class="nml" href="{conv_url(c["person"])}">{h(c["person"])}</a>' if p
            else f'<span class="nml">{h(c["person"])}</span>')
    body = (f'<article class="card" data-id="fb:{h(c["key"])}"><div class="ch">{name}<span class="chip ja">{h(c["badge"])}</span>'
            f'<span class="right small">{h(ago(c["since"]))}</span></div><div class="why">{h(c["why"])}</div>')
    if c.get("said"):
        body += f'<div class="said">{h(c["said"])}</div>'
    if c.get("detail"):
        body += f'<details class="mini2"><summary>Co bot zkoušel</summary><div class="why">{h(human_error(c["detail"]))}</div></details>'
    return body + (f'<div class="acts"><button type="button" class="btn sm" data-hide="{h(c["key"])}">Hotovo, skrýt</button>'
                   + (f'<a class="more" href="{conv_url(c["person"])}">Otevřít chat →</a>' if p else "") + '</div></article>')


def follow_card(f):
    p = f["p"]
    person = f["person"]
    return (f'<article class="card rc cmp" data-person="{h(person)}" data-id="f:{h(f["key"])}"><div class="ch">'
            f'<a class="nml" href="{conv_url(person)}">{h(person)}</a><span class="chip">mlčí {h(dur(time.time() - f["since"]))}</span></div>'
            f'<div class="why">Poslední zpráva ({"bot" if p["last_out_by"] == "bot" else "ty"}): „{h(short(p["last_out"], 240))}“</div>'
            f'<div class="cbox"><textarea rows="1" data-person="{h(person)}" aria-label="Připomenutí pro {h(person)}">{h(f["suggest"])}</textarea>'
            f'<button type="button" class="btn pri send" data-send>Připomenout se</button></div>'
            f'<div class="chint"><span class="st"></span></div>'
            f'<div class="acts"><button type="button" class="btn ghost sm" data-hide="{h(f["key"])}">Nepřipomínat</button>'
            f'<a class="more" href="{conv_url(person)}">Otevřít chat →</a></div></article>')


def order_form(o, nxt):
    if o["idx"] < 2:
        k = ORDER_STEPS[o["idx"] + 1][0]
        label = "Mám vyrobeno" if k == "vyrobeno" else "Balík odeslán"
        cls = "btn pri sm"
    else:
        k, label, cls = "vyrobeno", "Vrátit na „vyrobeno“", "btn ghost sm"
    return (f'<form method="post" action="/objednavka" style="display:inline"><input type="hidden" name="next" value="{h(nxt)}">'
            f'<input type="hidden" name="key" value="{h(o["key"])}"><input type="hidden" name="krok" value="{k}">'
            f'<button class="{cls}" data-busy="…">{label}</button></form>')


def order_title(o):
    what = h(o["what"]) if o["what"] else ""
    if o["idx"] == 0:
        return f'Vyrobit: <b>{what}</b>' if what else "Vyrobit"
    if o["idx"] == 1:
        return f'Odeslat balík: <b>{what}</b>' if what else "Odeslat balík"
    return f'Odesláno ✓ {("<b>" + what + "</b>") if what else ""}'


def steps_html(o):
    return "".join(f'<span class="{"done" if i <= o["idx"] else ""}">{"✓ " if i <= o["idx"] else ""}{v}</span>'
                   for i, (_, v) in enumerate(ORDER_STEPS))


def order_html(o, nxt, compact=False):
    when_txt = f'domluveno {ago(o["ts"])}' + (" · už by měla být hotová" if o["late"] else "")
    rest = ""
    if not compact:
        if o["what"] and o["rest"]:
            rest = f'<details class="mini2"><summary>Doprava, kontakt a platba</summary><div class="why">{h(o["rest"])}</div></details>'
        elif not o["what"]:
            rest = f'<div class="why" style="color:var(--ink)">{h(o["note"])}</div>'
    return (f'<article class="card {"badl" if o["late"] else "okl"}" data-id="o:{h(o["key"])}"><div class="ch">'
            f'<a class="nml" href="{conv_url(o["person"])}">{h(o["person"])}</a>'
            f'<span class="right small" style="{"color:var(--bad);font-weight:600" if o["late"] else ""}">{h(when_txt)}</span></div>'
            f'<div class="make">{order_title(o)}</div>{rest}'
            + ("" if compact else f'<div class="steps">{steps_html(o)}</div>')
            + f'<div class="acts">{order_form(o, nxt)}<a class="more" href="{conv_url(o["person"])}">Otevřít chat →</a></div></article>')


# ====================================================================== Přehled

def prehled_html():
    it = items()
    body = ""
    for title, how in alerts():
        body += f'<div class="alert" role="alert"><b>{h(title)}</b>{how}</div>'
    reply = it["reply"]
    if reply:
        first = reply[0]
        longest = (f'Nejdéle čeká {h(first["person"])} ({dur(time.time() - first["since"])}). ' if first["since"] else "")
        body += (f'<section class="card hero ja"><span class="ic">✉</span><div><h1>Odpovědět: {len(reply)}</h1>'
                 f'<p>{longest}Odpovídej rovnou tady – bot to napíše v telefonu.</p></div></section>')
        body += "".join(reply_card(c) for c in reply)
    else:
        body += (f'<section class="card hero"><span class="ic">✓</span><div><h1>Nic na tebe nečeká ✓</h1>'
                 f'<p>{h(today_summary())}</p></div></section>')

    open_orders = [o for o in orders() if o["step"] != "odeslano"]
    if open_orders:
        body += f'<h2>Vyrobit / odeslat <span class="cnt soft">{len(open_orders)}</span></h2>'
        body += "".join(order_html(o, "/", compact=True) for o in open_orders)
    if it["stuck"]:
        body += (f'<details class="fold z" open data-k="stuck"><summary>Možná zaseknuté <span class="cnt soft">{len(it["stuck"])}</span></summary>'
                 '<div class="copy">Zákazník psal poslední a nikdo neodpověděl. Odpověz, nebo dej Nepotřebuje odpověď.</div>'
                 + "".join(reply_card(c, stuck=True) for c in it["stuck"]) + "</details>")
    if it["fb"]:
        body += (f'<details class="fold" data-k="fb"><summary>Udělej ve Facebooku <span class="cnt soft">{len(it["fb"])}</span></summary>'
                 '<div class="copy">Tohle bot v telefonu nezvládne – udělej to ve Facebooku a pak dej Hotovo.</div>'
                 + "".join(fb_card(c) for c in it["fb"]) + "</details>")
    if it["follow"]:
        body += (f'<details class="fold" data-k="follow"><summary>Připomenout se <span class="cnt soft">{len(it["follow"])}</span></summary>'
                 f'<div class="copy">Zeptali jsme se a zákazník mlčí přes {FOLLOWUP_DAYS} dny. Text můžeš upravit.</div>'
                 + "".join(follow_card(f) for f in it["follow"]) + "</details>")
    if reply:
        body += f'<p class="small" style="margin-top:26px">{h(today_summary())}</p>'
    return body


@app.route("/")
def prehled():
    return page("Přehled", "prehled", prehled_html(), scripts=f"<script>{PREHLED_JS}</script>")


@app.route("/api/prehled")
def api_prehled():
    return jsonify(html=prehled_html(), health=health_html(), counts=nav_counts())


@app.route("/api/stav")
def api_stav():
    s = bot_status()
    return jsonify(health=health_html(s), counts=nav_counts(), running=s["running"], phone=s["phone"], online=s["online"])


# ====================================================================== Zprávy

def list_html(sel):
    ppl = sorted(people().values(), key=lambda p: p["name"])
    order_ = {k: i for i, (k, _, _) in enumerate(SECTIONS)}

    def rank(p):
        if p["section"] in ("odpovedet",):
            return (order_[p["section"]], p["since"] or 0)
        return (order_[p["section"]], -(p["activity"] or 0))
    ppl.sort(key=rank)
    if not ppl:
        return '<div class="empty"><b>Zatím žádné konverzace</b>Až někdo napíše do Messengeru, objeví se tady.</div>'
    out = []
    for sec, title, sub in SECTIONS:
        items_ = [p for p in ppl if p["section"] == sec]
        if not items_:
            continue
        out.append(f'<div class="sec s-{sec}" data-sec="{sec}">{title} · {len(items_)}'
                   + (f"<small>{h(sub)}</small>" if sub and sec != "odpovedet" else "") + '</div>')
        main_, folded = items_, []
        if sec == "uzavrene":
            main_ = [p for p in items_ if p["status"] not in ("spam", "prazdna")]
            folded = [p for p in items_ if p["status"] in ("spam", "prazdna")]
        out.extend(list_item(p, sel) for p in main_)
        if folded:
            out.append(f'<details class="lfold" data-k="spam"><summary>Spam a prázdné ({len(folded)})</summary>'
                       + "".join(list_item(p, sel) for p in folded) + '</details>')
    return "".join(out)


def list_item(p, sel):
    vis = [m for m in p["msgs"] if m["text"] or m.get("img")]
    last = vis[-1] if vis else {"dir": "?", "text": "", "img": 0}
    who = {"bot": '<span class="who">Bot: </span>', "manual": '<span class="who">Ty: </span>'}.get(last["dir"], "")
    prev = last["text"] or ("Fotka" if last.get("img") else "")
    badges = ""
    if p["section"] == "odpovedet":
        badges += wait_badge(p["since"]) if p["since"] else '<span class="wait">čeká</span>'
    elif p["section"] == "zaseknute":
        badges += '<span class="wait z">zaseknuté?</span>'
    elif p["status"] == "bot_pise":
        badges += '<span class="wait b">bot píše</span>'
    if p["pending"]:
        badges += '<span class="wait b">odesílá se…</span>'
    if p["failed"]:
        badges += '<span class="tag bad">neodešlo</span>'
    if p["status"] in ("pises", "prevzato") or (p["paused_left"] > 0 and p["section"] != "odpovedet"):
        badges += '<span class="tag">píšeš ty</span>'
    if p["order"]:
        badges += '<span class="tag ok">objednávka</span>'
    if p["status"] == "zablokovany":
        badges = '<span class="tag">zablokovaný</span>'
    sub = h(p["badge"] or "") if p["section"] == "odpovedet" else (h(p["reason"]) if p["section"] in ("uzavrene",) else "")
    return (f'<a class="ci st-{p["section"]} {"sel" if p["name"] == sel else ""}" href="{conv_url(p["name"])}" '
            f'data-name="{h(p["name"])}" data-sec="{p["section"]}">'
            f'<span class="av">{h(initials(p["name"]))}</span><span class="cb">'
            f'<span class="r1"><span class="nm">{h(p["name"])}</span><span class="tm">{h(ago(p["activity"]))}</span></span>'
            f'<span class="r2 {"fromthem" if last["dir"] == "in" else ""}">{who}{h(short(prev, 140))}</span>'
            + (f'<span class="r3">{badges}<span class="t">{sub}</span></span>' if badges or sub else "")
            + '</span></a>')


def conv_sig(p):
    return (f'{len(p["msgs"])}|{p["activity"]}|{p["status"]}|{p["handed"]}|{p["paused_left"] > 0}|'
            + ",".join(f'{o["id"]}{o["status"]}' for o in p["outbox"][-5:]))


def banner_html(p):
    sec = p["section"]
    title = STATUS[p["status"]][0]
    if sec == "odpovedet":
        title = "Odpovědět" + (f" – čeká {dur(time.time() - p['since'])}" if p["since"] else "")
    pend_act = [o for o in p["pending"] if o["kind"] in ("botovi", "prevzit")]
    btns = ""
    if p["blocked"]:
        btns = ""
    elif pend_act:
        btns = f'<span class="small"><span class="spin"></span> {"Vracím botovi…" if pend_act[-1]["kind"] == "botovi" else "Přebírám…"}</span>'
    elif p["handed"] or p["paused_left"] > 0:
        btns = '<button type="button" class="btn sm" data-act="botovi" title="Bot zase začne v téhle konverzaci odpovídat sám">Vrátit botovi</button>'
    else:
        btns = '<button type="button" class="btn sm" data-act="prevzit" title="Bot v téhle konverzaci přestane odpovídat, dokud ji nevrátíš">Převezmu sám</button>'
    if sec in ("odpovedet", "zaseknute"):
        btns += (f'<button type="button" class="btn sm ghost" data-hide="{h(p["hide_key"])}" '
                 f'title="Vyřízeno jinak. Vrátí se, když zákazník napíše znovu.">Nepotřebuje odpověď</button>')
    hint = ""
    if p["handed"] and p["status"] not in ("dekuje",):
        hint = "Bot tu nic nepíše, dokud konverzaci nevrátíš."
    teach = ""
    if p["handed"] and p["hand"] and not p["hand"]["action"].startswith("převzato"):
        teach = ('<details class="teach" id="teach"><summary><b>Naučit bota</b> '
                 '<span class="small">– napiš pravidlo, ať to příště zvládne sám</span></summary><form class="lessonform">'
                 '<div class="tr"><input type="text" placeholder="Příště odpověz… (např. Intenzita světla se měnit nedá.)" aria-label="Nová lekce">'
                 '<button class="btn sm">Přidat lekci</button></div><span class="lst"></span></form></details>')
    return (f'<div class="banner k-{sec}" id="banner"><div class="brow"><div class="bt"><b>{h(title)}</b>{h(p["reason"])}'
            + (f' <span class="small">{h(hint)}</span>' if hint else "")
            + f'</div><div class="bbtns">{btns}</div></div>{teach}</div>')


def side_html(p):
    parts = []
    note = p["note"]
    if not note:
        cn = next((x for x in reversed(p["comments"]) if x["note"] and x["note"] != "z auditu"), None)
        ho = next((r for r in reversed(p["log"]) if r["action"].startswith("PŘEDÁNO") and r["detail"]), None)
        note = {"note": cn["note"], "ts": cn["ts"]} if cn else {"note": ho["detail"], "ts": ho["ts"]} if ho else None
    parts.append('<div class="sbox"><h3>Co chce</h3>' + (
        f'<p>{h(human_error(note["note"]))}</p><div class="small" style="margin-top:6px">shrnutí od bota, {h(ago(note["ts"]))}</div>'
        if note else '<p class="small">Bot o tomhle zákazníkovi zatím nic nezapsal.</p>') + '</div>')
    o = p["order"]
    facts = order_facts(p)
    if o or facts:
        box = '<div class="sbox"><h3>Objednávka</h3>'
        if o:
            box += (f'<p class="make">{order_title(o)}</p><div class="steps">{steps_html(o)}</div>'
                    + (f'<p class="small" style="margin:4px 0 8px">{h(o["rest"])}</p>' if o["what"] and o["rest"] else "")
                    + f'<div class="acts" style="margin-top:6px">{order_form(o, conv_url(p["name"]))}</div>')
        if facts:
            box += ('<ul class="facts">' + "".join(f'<li><span class="lbl">{h(k)}</span><span class="val">{h(v)}</span></li>' for k, v in facts)
                    + '</ul><div class="small" style="margin-top:4px">nalezeno ve zprávách</div>')
        parts.append(box + '</div>')
    if p["comments"]:
        cm = "".join(f'<div class="cmt"><div class="g">{h(x["grp"] or "skupina?")} · {h(ago(x["ts"]))}</div>„{h(x["text"])}“'
                     + (f'<div class="rp">↳ bot odpověděl: {h(x["reply"])}</div>' if x["reply"] else "") + '</div>'
                     for x in p["comments"][-6:])
        parts.append(f'<div class="sbox"><h3>Psal pod příspěvky</h3>{cm}</div>')
    if p["log"]:
        hl = "".join(f'<li><span class="w">{h(clock(r["ts"]))}</span><span>{h(plain_action(r["action"]))}</span></li>'
                     for r in reversed(p["log"][-8:]))
        parts.append(f'<div class="sbox"><h3>Co se dělo</h3><ul class="hist">{hl}</ul></div>')
    line = ""
    if p["blocked"]:
        line = "Zablokovaný – bot mu nikdy neodpoví."
    elif p["handed"]:
        line = "Předáno tobě – bot tu nepíše, dokud konverzaci nevrátíš."
    elif p["paused_left"] > 0:
        line = f"Bot tu mlčí do {clock(time.time() + p['paused_left'])}, protože jsi psal(a) ty."
    if line:
        parts.append(f'<div class="sbox"><h3>Bot</h3><p>{h(line)}</p></div>')
    return "".join(parts)


def thread_html(p):
    if p["blocked"]:
        composer = '<div class="composer"><div class="small">Zablokovaný zákazník – odsud se mu psát nedá.</div></div>'
    else:
        composer = (
            f'<form class="composer cmp" id="composer" method="post" action="{conv_url(p["name"])}" data-person="{h(p["name"])}">'
            f'<input type="hidden" name="kind" value="zprava"><div class="quick">{quick_html()}</div>'
            f'<div class="cbox"><textarea id="tx" name="text" rows="1" data-person="{h(p["name"])}" placeholder="Napiš odpověď…" aria-label="Odpověď"></textarea>'
            '<button class="btn pri send" id="sendbtn" data-send>Odeslat</button></div>'
            f'<div class="chint">{composer_warn()}'
            '<span class="keys"><kbd>⌘</kbd>/<kbd>Ctrl</kbd>+<kbd>Enter</kbd> odešle · prázdný řádek = další zpráva</span></div></form>')
    return (
        f'<section class="col thread"><div class="thead"><a class="back" href="/zpravy">‹ Zpět</a>'
        f'<div class="grow"><h1>{h(p["name"])}</h1><div class="sub">{h(STATUS[p["status"]][0])} · poslední aktivita {h(ago(p["activity"]) or "–")}</div></div>'
        f'<button class="iconbtn infobtn" onclick="document.body.classList.toggle(\'sideopen\')" title="Co o zákazníkovi víme" aria-label="Info">{ICON_INFO}</button></div>'
        f'{banner_html(p)}<div class="msgs" id="msgs">{msgs_html(p)}</div>{composer}</section>'
        f'<aside class="col side"><div class="sideclose"><button class="btn sm" onclick="document.body.classList.remove(\'sideopen\')">Zavřít</button></div>'
        f'<div id="sidein">{side_html(p)}</div></aside>')


def nothread_html():
    reply = [p for p in people().values() if p["section"] == "odpovedet"]
    reply.sort(key=lambda p: p["since"] or time.time())
    if reply:
        n = len(reply)
        return (f'<section class="col thread wide"><div class="nothread"><b>Vyber konverzaci vlevo</b>'
                f'<span>{n} {plural(n, "konverzace čeká", "konverzace čekají", "konverzací čeká")} na odpověď.</span>'
                f'<a class="btn pri" href="{conv_url(reply[0]["name"])}">Otevřít nejdéle čekající →</a></div></section>')
    return '<section class="col thread wide"><div class="nothread"><b>Vyber konverzaci vlevo</b><span>Nic na tebe nečeká ✓</span></div></section>'


def same_origin():
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        return False
    o = request.headers.get("Origin")
    return not o or o.split("://", 1)[-1] == request.host


def is_fetch():
    return request.headers.get("X-Requested-With") == "fetch"


def deny():
    if is_fetch():
        return jsonify(ok=False, error="Požadavek z cizí stránky – zamítnuto."), 403
    abort(403)


@app.route("/zpravy")
@app.route("/zpravy/<path:person>", methods=["GET", "POST"])
def zpravy(person=None):
    if request.method == "POST":  # záloha bez JavaScriptu
        if not same_origin():
            return deny()
        kind = request.form.get("kind")
        text = (request.form.get("text") or "").replace("\r\n", "\n").strip()
        try:
            if kind == "zprava" and text:
                data.outbox_add("zprava", person, text)
            elif kind in ("botovi", "prevzit"):
                data.outbox_add(kind, person)
        except Exception:  # noqa: BLE001
            abort(500)
        return redirect(conv_url(person) + "?ok=odeslano")
    ppl = people()
    sel = ppl.get(person) if person else None
    if person and not sel:
        abort(404)
    left = ('<section class="col list"><div class="lhead"><input id="search" class="search" type="search" placeholder="Hledat jméno…  ( / )" aria-label="Hledat"></div>'
            f'<div class="litems" id="litems">{list_html(person)}</div></section>')
    right = thread_html(sel) if sel else nothread_html()
    st = {"person": person, "sig": conv_sig(sel) if sel else "", "pending": bool(sel and sel["pending"])}
    js = (f'<script type="application/json" id="S">{json.dumps(st, ensure_ascii=False).replace("</", "<" + chr(92) + "/")}</script>'
          f'<script>{INBOX_JS}</script>')
    return page(person or "Zprávy", "zpravy", f'<main class="inbox">{left}{right}</main>', raw=True,
                body_class="hassel" if sel else "", scripts=js)


@app.route("/api/zpravy")
def api_zpravy():
    person = request.args.get("c") or None
    p = people().get(person) if person else None
    return jsonify(list=list_html(person), counts=nav_counts(), health=health_html(), sig=conv_sig(p) if p else "")


@app.route("/api/vlakno/<path:person>")
def api_vlakno(person):
    p = people().get(person)
    if not p:
        return jsonify(error="nenalezeno"), 404
    return jsonify(banner=banner_html(p), msgs=msgs_html(p), side=side_html(p), sig=conv_sig(p), pending=bool(p["pending"]))


@app.route("/chat/<path:person>")
@app.route("/konverzace/<path:person>")
def old_chat(person):
    return redirect(conv_url(person))


@app.route("/konverzace")
def old_list():
    return redirect("/zpravy")


def _queue(kind, person, text=""):
    data.outbox_add(kind, person, text)
    with data.db() as con:
        return con.execute("SELECT max(id) FROM outbox WHERE person=? AND kind=?", (person, kind)).fetchone()[0]


@app.route("/api/poslat", methods=["POST"])
def api_poslat():
    if not same_origin():
        return deny()
    person = (request.form.get("person") or "").strip()
    text = (request.form.get("text") or "").replace("\r\n", "\n").strip()
    if not person or not text:
        return jsonify(ok=False, error="Chybí text zprávy."), 400
    if (threads().get(person) or {}).get("blocked"):
        return jsonify(ok=False, error="Zákazník je zablokovaný."), 400
    try:
        oid = _queue("zprava", person, text)
    except Exception:  # noqa: BLE001
        return jsonify(ok=False, error="Nepovedlo se uložit do fronty (databáze)."), 500
    return jsonify(ok=True, id=oid)


@app.route("/api/akce", methods=["POST"])
def api_akce():
    if not same_origin():
        return deny()
    person = (request.form.get("person") or "").strip()
    kind = request.form.get("kind")
    text = (request.form.get("text") or "").replace("\r\n", "\n").strip()
    if not person or kind not in ("botovi", "prevzit"):
        return jsonify(ok=False, error="Neznámá akce."), 400
    try:
        if text and kind == "botovi":  # napsal(a) jsi něco a pak klikl(a) na „Vrátit botovi“ → nejdřív to pošli
            _queue("zprava", person, text)
        oid = _queue(kind, person)
    except Exception:  # noqa: BLE001
        return jsonify(ok=False, error="Nepovedlo se uložit do fronty (databáze)."), 500
    return jsonify(ok=True, id=oid)


@app.route("/api/odeslani/<int:oid>")
def api_odeslani(oid):
    try:
        with data.db() as con:
            r = con.execute("SELECT status, error, done_ts FROM outbox WHERE id=?", (oid,)).fetchone()
    except Exception:  # noqa: BLE001
        r = None
    if not r:
        return jsonify(status="nenalezeno"), 404
    return jsonify(status=r[0], error=human_error(r[1] or ""), done=r[2])


# ====================================================================== Objednávky

@app.route("/objednavky")
def objednavky():
    allo = orders()
    open_ = [o for o in allo if o["step"] != "odeslano"]
    done = sorted([o for o in allo if o["step"] == "odeslano"], key=lambda o: -(o["step_ts"] or 0))
    body = ('<h1>Objednávky</h1><p class="lead">Konverzace, kde bot domluvil všechno potřebné (motiv, doprava, platba). '
            'Posouvej je tlačítkem: domluveno → vyrobeno → odesláno.</p>')
    body += "".join(order_html(o, "/objednavky") for o in open_) or \
        '<div class="card empty"><b>Žádná rozpracovaná objednávka</b>Až bot nějakou domluví, objeví se tady.</div>'
    if done:
        body += (f'<details class="fold" data-k="done"><summary>Odeslané <span class="cnt soft">{len(done)}</span></summary>'
                 + "".join(order_html(o, "/objednavky") for o in done) + "</details>")
    return page("Objednávky", "objednavky", body)


# ====================================================================== Další

JOB_STATUS = {
    "zverejneno": ("Zveřejněno", "ok"), "ceka": ("Čeká na zveřejnění", "info"),
    "ceka-na-clenstvi": ("Čeká na přijetí do skupiny", "info"), "ceka-na-schvaleni-prispevku": ("Čeká na schválení adminem", "info"),
    "otazky-vyres-rucne": ("Vyplň otázky ke členství", "ja"), "chyba": ("Nepovedlo se", "zak"),
    "nelze-postovat": ("Ve skupině nejde přidávat", ""), "preskoceno-bazar": ("Přeskočeno (bazar)", ""),
    "vzdano-nepřijali": ("Nepřijali tě", ""),
}


def subnav(active):
    tabs = [("skupiny", "Skupiny a komentáře", "/dalsi"), ("uceni", "Učení bota", "/dalsi/uceni"), ("technika", "Technika", "/dalsi/technika")]
    return '<nav class="subnav">' + "".join(f'<a class="{"on" if k == active else ""}" href="{u}">{t}</a>' for k, t, u in tabs) + '</nav>'


def post_photo(j):
    phs = j.get("photos") or ([j.get("photo")] if j.get("photo") else [])
    ph = phs[0] if phs else None
    try:
        return f'/foto?p={quote(ph)}' if ph and (HERE / ph).is_file() else ""
    except (TypeError, OSError):
        return ""


@app.route("/dalsi")
def dalsi():
    js = jobs()
    st = state()
    today = datetime.now().date().isoformat()
    posted = (st.get("postsByDay") or {}).get(today, 0)
    maxp = config().get("maxPostsPerDay", 4)
    need = [j for j in js if j.get("status") == "otazky-vyres-rucne"]
    waiting = [j for j in js if str(j.get("status", "")).startswith("ceka")]
    body = subnav("skupiny") + ('<h1>Skupiny a komentáře</h1><p class="lead">Bot sám přidává příspěvky z fronty (nejvýš pár denně, mimo noční klid), '
                                'čte komentáře pod nimi a zájemcům píše do Messengeru.</p>')
    body += (f'<div class="kpis"><div class="kpi"><b>{posted} / {maxp}</b><span>zveřejněno dnes</span></div>'
             f'<div class="kpi"><b>{len(waiting)}</b><span>čeká ve frontě</span></div>'
             f'<div class="kpi"><b style="color:{"var(--ja)" if need else "inherit"}">{len(need)}</b><span>potřebuje tebe</span></div></div>')
    if need:
        body += '<h2 class="ja">Potřebuje tebe</h2>'
        for j in need:
            img = f'<img class="thumb" src="{post_photo(j)}" alt="" loading="lazy">' if post_photo(j) else ""
            body += (f'<div class="card ja"><div class="crow">{img}<div class="grow"><b>{h(j.get("group"))}</b> '
                     f'<span class="chip ja">Vyplň otázky ke členství</span>'
                     f'<div class="small" style="margin-top:4px">Bot požádal o členství, ale na otázky ke vstupu musíš odpovědět ty (ve Facebooku → Skupiny).</div>'
                     + (f'<details class="mini2"><summary>Co bot zkoušel</summary><div class="why">{h(human_error(j.get("error", "")))}</div></details>' if j.get("error") else "")
                     + '</div></div></div>')
    order_ = {"otazky-vyres-rucne": 0, "chyba": 1, "ceka": 2, "ceka-na-clenstvi": 2, "ceka-na-schvaleni-prispevku": 2}
    body += f'<h2>Příspěvky <span class="cnt soft">{len(js)}</span></h2><div class="card rows" style="padding:4px 16px">'
    for j in sorted(js, key=lambda j: order_.get(j.get("status"), 3)):
        label, cls = JOB_STATUS.get(j.get("status"), (j.get("status", "?"), ""))
        err = human_error(j.get("error") or "")
        if j.get("status") == "chyba" and TECH.search(j.get("error") or ""):
            err = "Ani na třetí pokus se to v telefonu nepovedlo (technická chyba). Zkus příspěvek přidat ručně."
        body += (f'<div class="row"><div class="main"><b>{h(j.get("group", ""))}</b> <span class="small">{h(j.get("members", ""))}</span>'
                 f'<div class="small">{h(short(j.get("text", ""), 110))}</div>'
                 + (f'<details class="mini2"><summary>Co se stalo</summary><div class="why">{h(err)}</div></details>' if err else "")
                 + f'</div><div class="sidec"><span class="chip {cls}">{h(label)}</span><div>{h(j.get("last", ""))}</div></div></div>')
    if not js:
        body += '<div class="empty">Fronta je prázdná.</div>'
    body += "</div>"

    rows = sorted(db_rows()["com"], key=lambda r: -(r[0] or 0))[:150]
    ppl = people()
    no_reply = sum(1 for r in rows if not r[5] and not r[6] and not r[4])
    body += (f'<h2>Komentáře <span class="cnt soft">{len(rows)}</span></h2>'
             '<div class="filt" data-target="#coms"><button type="button" class="on" data-v="">Všechny</button>'
             f'<button type="button" data-v="bez">Bez odpovědi ({no_reply})</button><button type="button" data-v="zprava">Bot poslal zprávu</button></div><div id="coms">')
    for ts, grp, person, text, indent, reply, dm, note in rows:
        f = "bez" if not reply and not dm else ("zprava" if dm else "")
        who = (f'<a class="nml" href="{conv_url(person)}">{h(person)}</a>' if person in ppl else f'<span class="nml">{h(person)}</span>')
        body += (f'<article class="card" data-f="{f}" style="padding:12px 16px"><div class="ch">{who}'
                 f'<span class="small">{h(grp or "")}</span><span class="right small">{h(ago(ts))}</span></div>'
                 f'<div>{"↳ " if indent else ""}„{h(text)}“</div>')
        if reply:
            body += f'<div class="small">Bot odpověděl: {h(reply)}</div>'
        if dm:
            body += f'<div class="small"><span class="chip ok">poslal zprávu</span> {h(short(dm, 160))}</div>'
        if not reply and not dm:
            body += f'<div class="small">Bez odpovědi{(" – " + h(short(note, 200))) if note and note != "z auditu" else ""}</div>'
        body += "</article>"
    if not rows:
        body += '<div class="card empty">Zatím žádné komentáře.</div>'
    return page("Skupiny a komentáře", "dalsi", body + "</div>")


@app.route("/dalsi/uceni")
def dalsi_uceni():
    text = read_text(HERE / "lekce.md")
    lessons = [l[2:] for l in text.splitlines() if l.startswith("- ")]
    body = subnav("uceni") + ('<h1>Učení bota</h1><p class="lead">Lekce bot čte před každou odpovědí v Messengeru – stačí jedna věta, '
                              'jako bys to vysvětloval(a) brigádníkovi.</p>')
    body += ('<div class="card"><form method="post" action="/uceni/pridat"><input type="hidden" name="next" value="/dalsi/uceni">'
             '<div class="cbox" style="align-items:center"><input class="inp" type="text" name="lekce" required '
             'placeholder="Nová lekce, např. „Na dotaz na baterii odpověz: bohužel jen USB.“" aria-label="Nová lekce">'
             '<button class="btn pri" data-busy="Ukládám…">Přidat lekci</button></div></form></div>')
    body += f'<h2>Lekce <span class="cnt soft">{len(lessons)}</span></h2><div class="card">'
    body += "".join(f'<div class="lesson">{h(l)}</div>' for l in lessons) or '<div class="empty">Zatím žádné lekce.</div>'
    body += ('<details class="mini2"><summary>Upravit nebo smazat lekce</summary><form method="post" action="/uceni/lekce">'
             '<input type="hidden" name="next" value="/dalsi/uceni">'
             f'<textarea class="ta" name="lekce" spellcheck="true">{h(text)}</textarea>'
             '<div class="small">Každá lekce je řádek začínající „- “. Nejdůležitější nahoru. Ukládá se do lekce.md.</div>'
             '<div class="acts"><button class="btn pri" data-busy="Ukládám…">Uložit lekce</button></div></form></details></div>')

    rows = [r for r in reversed(db_rows()["dec"]) if r[4] and not ORDER.search(r[3] or "")][:25]
    if rows:
        body += (f'<details class="fold" data-k="nevedel"><summary>Co bot nevěděl <span class="cnt soft">{len(rows)}</span></summary>'
                 '<div class="copy">Tohle předal tobě. Co se opakuje, z toho udělej lekci.</div><div class="card">'
                 + "".join(f'<div class="lesson"><span class="small">{h(clock(ts))} · <a class="plink" href="{conv_url(pp)}">{h(pp)}</a></span>'
                           f'<div>{h(short(human_error(n or ""), 300))}</div></div>' for ts, _k, pp, n, _hh in rows) + '</div></details>')
    try:
        ex = data.manual_examples(30) or ""
    except Exception:  # noqa: BLE001
        ex = ""
    pairs = re.findall(r"- Zákazník: „(.*?)“ → Prodejce: „(.*?)“$", ex, re.M)
    body += ('<h2>Tvoje odpovědi, ze kterých se bot učí styl</h2>'
             '<p class="small" style="margin-top:-4px">Zprávy, které jsi psal(a) sám. Osobní údaje se botovi nepředávají.</p>')
    body += ('<div class="card">' + "".join(f'<div class="pair"><div class="c">{h(a)}</div><div class="s">{h(b)}</div></div>' for a, b in reversed(pairs))
             + '</div>') if pairs else '<div class="card small">Zatím žádné.</div>'
    info = HERE / "info.md"
    if info.exists():
        body += ('<details class="fold" data-k="info"><summary>Co bot ví o lampičkách (ceny, doprava, výroba)</summary><div class="card">'
                 '<form method="post" action="/uceni/info"><input type="hidden" name="next" value="/dalsi/uceni">'
                 f'<textarea class="ta" name="info" style="min-height:380px">{h(read_text(info))}</textarea>'
                 '<div class="small">Co je v [hranatých závorkách], bot nezná a předá to tobě.</div>'
                 '<div class="acts"><button class="btn pri" data-busy="Ukládám…">Uložit</button></div></form></div></details>')
    return page("Učení bota", "dalsi", body)


@app.route("/dalsi/technika")
def dalsi_technika():
    s = bot_status()
    show_all = request.args.get("vse") == "1"
    if not s["phone"]:
        net = (None, "Internet v telefonu", "Nevím", "Telefon je odpojený – nejde to zjistit.")
    else:
        net = (s["online"], "Internet v telefonu", "Funguje" if s["online"] else "Nefunguje",
               "Telefon je online." if s["online"] else "Wi-Fi nebo data v telefonu. Bot počká.")
    tiles = [
        (s["running"] == 1, "Bot", "Běží" if s["running"] == 1 else "Neběží" if not s["running"] else f"Běží {s['running']}×",
         "Hlídá Messenger, komentáře a frontu příspěvků." if s["running"] == 1 else
         "Spusť: <code>.venv/bin/python bot_telefon.py run</code>" if not s["running"] else "Jedno okno s botem zavři (Ctrl+C)."),
        (s["phone"], "Telefon", "Připojený" if s["phone"] else "Odpojený",
         "Přes USB kabel." if s["phone"] else "Zkontroluj kabel a Ladění USB."),
        net,
    ]
    body = subnav("technika") + '<h1>Technika</h1><p class="lead">Když je nahoře zelené „Vše běží“, nemusíš sem chodit.</p><div class="tiles">'
    for ok, name, val, how in tiles:
        dot = "mute" if ok is None else "" if ok else "bad"
        body += f'<div class="card tile"><b><span class="dot {dot}"></span>{name}: {val}</b><div>{how}</div></div>'
    body += "</div>"
    if in_quiet_hours():
        q = config().get("quietHours", [23, 7])
        body += f'<p class="small">Teď je noční klid ({h(q[0])}–{h(q[1])} h): bot reaguje jen na nové zprávy, nepřidává příspěvky.</p>'

    its = problems()
    groups = {}
    for p in its:
        if p.get("status") != "novy" and not show_all:
            continue
        k = p.get("kind", "?")
        gr = groups.setdefault(k, {"kind": k, "count": 0, "last": None, "resolved": True})
        gr["count"] += int(p.get("count", 1) or 1)
        gr["resolved"] = gr["resolved"] and p.get("status") != "novy"
        if not gr["last"] or float(p.get("lastTs", 0) or 0) >= float(gr["last"].get("lastTs", 0) or 0):
            gr["last"] = p
    total_new = sum(1 for p in its if p.get("status") == "novy")
    body += (f'<h2>Kde se bot zasekl <span class="cnt soft">{total_new}</span></h2>'
             '<p class="small" style="margin-top:-4px">Bot si většinou poradí sám (restartuje aplikaci). Tohle je pro ladění – projdi to, když se něco opakuje.</p>')
    for gr in sorted(groups.values(), key=lambda x: -float(x["last"].get("lastTs", 0) or 0)):
        p = gr["last"]
        folder = str(p.get("folder") or "")
        shot_ok = bool(folder) and (HERE / "problemy" / folder / "screenshot.png").is_file()
        img = (f'<a href="/shot/{quote(folder)}" target="_blank"><img class="shot" loading="lazy" alt="Snímek obrazovky" src="/shot/{quote(folder)}"></a>'
               if shot_ok else "")
        recent = time.time() - float(p.get("lastTs", 0) or 0) < 3600
        body += (f'<article class="card {"zak" if recent and not gr["resolved"] else ""}"><div class="crow"><div class="grow">'
                 f'<b>{h(gr["kind"])}</b> <span class="chip {"" if gr["resolved"] else "ja"}">{gr["count"]}×</span>'
                 f'<div class="small">naposledy {h(ago(float(p.get("lastTs", 0) or 0)) or p.get("time", ""))}{" · vyřešeno" if gr["resolved"] else ""}</div>'
                 f'<div class="why">{h(short(human_error(p.get("detail", "")), 200))}</div>')
        if not gr["resolved"]:
            body += (f'<form method="post" action="/problem"><input type="hidden" name="next" value="/dalsi/technika">'
                     f'<input type="hidden" name="kind" value="{h(gr["kind"])}"><button class="btn sm" data-busy="…">Vyřešeno</button></form>')
        body += f'</div>{img}</div></article>'
    if not groups:
        body += '<div class="card empty"><b>Žádné nevyřešené problémy</b>Bot jede hladce.</div>'
    body += f'<p><a class="plink" href="{"/dalsi/technika" if show_all else "/dalsi/technika?vse=1"}">{"Jen nevyřešené" if show_all else "Ukázat i vyřešené"}</a></p>'

    rows = list(reversed(log_rows()[-60:]))
    body += '<details class="fold" data-k="log"><summary>Co bot dělal (posledních 60 událostí)</summary><div class="card">'
    for r in rows:
        det = "" if TECH.search(r["detail"]) else short(r["detail"], 140)
        body += (f'<div class="ev"><span class="t">{h(clock(r["ts"]))}</span><span><b>{h(plain_action(r["action"]))}</b> · {h(r["who"])}'
                 + (f'<div class="small">{h(det)}</div>' if det else "") + '</span></div>')
    if not rows:
        body += '<div class="empty">Zatím nic.</div>'
    body += "</div></details>"
    return page("Technika", "dalsi", body)


@app.route("/skupiny")
@app.route("/komentare")
@app.route("/prispevky")
def old_groups():
    return redirect("/dalsi")


@app.route("/uceni")
def old_uceni():
    return redirect("/dalsi/uceni")


@app.route("/stav")
@app.route("/problemy")
def old_stav():
    return redirect("/dalsi/technika")


@app.route("/shot/<folder>")
def shot(folder):
    base = (HERE / "problemy").resolve()
    f = (base / folder / "screenshot.png").resolve()
    if base not in f.parents or not f.is_file():
        abort(404)
    return send_file(f)


@app.route("/foto")
def foto():
    p = request.args.get("p", "")
    f = (HERE / p).resolve()
    if not any((HERE / d).resolve() in f.parents for d in ("posty", "fotky")) or not f.is_file():
        abort(404)
    return send_file(f)


# ====================================================================== akce (POST)

def back(flash):
    nxt = request.form.get("next") or "/"
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    nxt = nxt.split("?")[0]
    return redirect(f"{nxt}?ok={flash}")


def done(flash, ok=True, error=""):
    if is_fetch():
        return jsonify(ok=ok, error=error)
    return back(flash)


@app.route("/skryt", methods=["POST"])
def skryt():
    if not same_origin():
        return deny()
    key = (request.form.get("key") or "").strip()
    if key:
        try:
            d = stav()
            d["skryto"] = dict(d["skryto"])
            d["skryto"][key] = time.time()
            save_stav(d)
        except Exception:  # noqa: BLE001
            return done("skryto", False, "Nepovedlo se uložit.")
    return done("skryto")


@app.route("/objednavka", methods=["POST"])
def objednavka():
    if not same_origin():
        return deny()
    key = (request.form.get("key") or "").strip()
    krok = request.form.get("krok")
    if key and krok in dict(ORDER_STEPS):
        try:
            d = stav()
            d["objednavky"] = dict(d["objednavky"])
            d["objednavky"][key] = {"krok": krok, "ts": time.time()}
            save_stav(d)
        except Exception:  # noqa: BLE001
            return done("obj", False, "Nepovedlo se uložit.")
    return done("obj")


@app.route("/problem", methods=["POST"])
def problem():
    if not same_origin():
        return deny()
    kind = request.form.get("kind") or ""
    f = HERE / "problemy" / "seznam.json"
    if kind and f.exists():
        try:
            its = json.loads(f.read_text(encoding="utf-8"))  # čerstvě, ať nepřepíšeme, co mezitím zapsal bot
            for p in its:
                if isinstance(p, dict) and p.get("kind") == kind and p.get("status") == "novy":
                    p["status"] = "vyreseno"
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(its, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(f)
            _cache.pop(f, None)
        except Exception:  # noqa: BLE001
            return done("problem", False, "Soubor s problémy se nepovedlo přečíst.")
    return done("problem")


@app.route("/uceni/pridat", methods=["POST"])
def lekce_pridat():
    if not same_origin():
        return deny()
    new = re.sub(r"\s+", " ", request.form.get("lekce") or "").strip().lstrip("- ").strip()
    if not new:
        return done("prazdne", False, "Lekce je prázdná.")
    f = HERE / "lekce.md"
    try:
        lines = f.read_text(encoding="utf-8").splitlines() if f.exists() else ["# Lekce", ""]
        at = next((i for i, l in enumerate(lines) if l.startswith("- ")), len(lines))
        lines.insert(at, f"- {new}")
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        return done("lekce", False, "Nepovedlo se uložit lekce.md.")
    return done("lekce")


@app.route("/uceni/lekce", methods=["POST"])
def lekce_ulozit():
    if not same_origin():
        return deny()
    text = (request.form.get("lekce") or "").replace("\r\n", "\n")
    try:
        (HERE / "lekce.md").write_text(text, encoding="utf-8")
    except Exception:  # noqa: BLE001
        return done("lekce", False, "Nepovedlo se uložit lekce.md.")
    return done("lekce")


@app.route("/uceni/info", methods=["POST"])
def info_ulozit():
    if not same_origin():
        return deny()
    text = (request.form.get("info") or "").replace("\r\n", "\n")
    if not text.strip():
        return done("prazdne", False, "Text je prázdný – nic jsem neuložil.")
    try:
        (HERE / "info.md").write_text(text, encoding="utf-8")
    except Exception:  # noqa: BLE001
        return done("info", False, "Nepovedlo se uložit info.md.")
    return done("info")


@app.errorhandler(404)
def not_found(_e):
    return page("Nenalezeno", "", '<h1>Tohle tu není</h1><p class="lead">Konverzace nebo stránka neexistuje. '
                '<a class="plink" href="/">Zpět na přehled</a></p>'), 404


@app.errorhandler(500)
def server_error(_e):
    return ('<!doctype html><meta charset="utf-8"><title>Chyba – Lampičky</title>'
            '<p style="font:17px system-ui;padding:30px">Něco se pokazilo při načítání dat. Zkus stránku obnovit. '
            '<a href="/">Přehled</a></p>'), 500


if __name__ == "__main__":
    print("Dashboard: http://localhost:5005")
    app.run(host="127.0.0.1", port=5005, debug=False)
