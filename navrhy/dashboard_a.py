"""Lampičky – akční schránka (varianta A). Lokální stránka jen pro tebe: http://localhost:5005

  .venv/bin/python dashboard_a.py

Domovská stránka odpoví na jedinou otázku: „Musím teď něco udělat?“
  – nahoře stav bota/telefonu/internetu (jen když je něco špatně, jinak jeden zelený řádek),
  – karty „Na tahu jsi ty“ seřazené podle toho, jak dlouho kdo čeká, každá s odpovědí přímo na místě,
  – objednávky k výrobě, nabídka připomenout se lidem, kteří zmlkli.
Ostatní (všechny konverzace, skupiny, učení, technika) je v záložkách nahoře.

Odpověď napsaná tady jde do fronty (data.outbox_add) a bot ji v telefonu napíše po znacích.
Telefon ovládá jen bot. Nic z toho není na internetu – data obsahují osobní údaje zákazníků.
Co sis skryl(a) a jak daleko je která objednávka, si stránka pamatuje v dashboard_stav.json.
"""

import csv
import json
import re
import subprocess
import time
from collections import defaultdict
from datetime import datetime
from html import escape
from pathlib import Path

from flask import Flask, abort, redirect, request, send_file, url_for

import data

HERE = Path(__file__).resolve().parent
ADB = str(Path.home() / ".local" / "android" / "platform-tools" / "adb")
app = Flask(__name__)

STUCK_MIN = 20          # zákazník napsal, bot neodpověděl déle než tohle → karta
FOLLOWUP_DAYS = 2       # zeptali jsme se a zákazník mlčí déle než tohle → nabídka připomenutí
FOLLOWUP_MAX_DAYS = 21
OLD_DAYS = 7            # starší úkoly se schovají do „Starší“
TASK_DAYS = 3           # chyby u komentářů apod. starší než tohle už nestrašíme
SPAM = re.compile(r"live ?cams?|chat girl|room full of love", re.I)
ORDER = re.compile(r"objednávk\w* (je )?(kompletní|hotová|domluvená)|kompletní objednávk|"
                   r"zaplatil\w* (poštovné|celou|zálohu|lampičku)|poslal\w* platbu|platba (přišla|dorazila)", re.I)
INTEREST = re.compile(r"\bcen[auy]?\b|kolik|zájem|záujem|chci|chcem|chtěl|bral|beru|vzal|objedn|koupi|kúpi|\bpm\b|\bsz\b|napiš|ozv", re.I)
TECH = re.compile(r"Exception|-3200\d|RPC|Selector|Traceback|\{'code'|uiautomator|androidx|Command '|non-zero exit|/Users/", re.I)

_cache = {}
_status_cache = {"ts": 0, "val": None}


# ====================================================================== načítání dat

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
    except Exception:  # noqa: BLE001  – rozbitý soubor nesmí shodit stránku
        return default
    _cache[path] = (mtime, val)
    return val


def read_json(path, default):
    return _cached(path, lambda p: json.loads(p.read_text(encoding="utf-8")), default)


def state():
    return read_json(HERE / "state_telefon.json", {"threads": {}})


def config():
    return read_json(HERE / "config.json", {})


def parse_when(s):
    try:
        return datetime.strptime(s.strip(), "%d. %m. %Y %H:%M:%S").timestamp()
    except ValueError:
        return 0.0


def _load_log(path):
    rows = []
    with path.open(encoding="utf-8", newline="") as f:
        for r in csv.reader(f, delimiter=";"):
            if len(r) >= 3:
                rows.append({"ts": parse_when(r[0]), "who": r[1].strip(), "action": r[2].strip(),
                             "detail": (r[3] if len(r) > 3 else "").strip()})
    return rows


def log_rows():
    return _cached(HERE / "log.csv", _load_log, [])


def stav_path():
    return HERE / "dashboard_stav.json"


def stav():
    d = read_json(stav_path(), {})
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
    return read_json(HERE / "fronta.json", [])


def job_ts(j):
    try:
        dt = datetime.strptime(f"{datetime.now().year} {j.get('last', '')}", "%Y %d. %m. %H:%M")
        return dt.timestamp() if dt <= datetime.now() else dt.replace(year=dt.year - 1).timestamp()
    except ValueError:
        return 0.0


def problems():
    return read_json(HERE / "problemy" / "seznam.json", [])


def norm(t):
    return re.sub(r"\W+", "", (t or "").lower())


def bot_status():
    """Stejná logika jako v původním dashboardu (proces bota, adb devices, ping z telefonu), 20 s cache."""
    if time.time() - _status_cache["ts"] < 20 and _status_cache["val"]:
        return _status_cache["val"]
    try:
        ps = subprocess.run(["ps", "-ax"], capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        ps = ""
    running = sum(1 for l in ps.splitlines() if "bot_telefon.py run" in l and "Python" in l)
    phone = online = False
    try:
        devs = subprocess.run([ADB, "devices"], capture_output=True, text=True, timeout=10).stdout
        phone = any(l.strip().endswith("\tdevice") for l in devs.splitlines()[1:])
        if phone:
            out = subprocess.run([ADB, "shell", "ping", "-c", "1", "-W", "2", "8.8.8.8"],
                                 capture_output=True, text=True, timeout=10).stdout
            online = " 1 received" in out
    except Exception:  # noqa: BLE001
        pass
    val = {"running": running, "phone": phone, "online": online}
    _status_cache.update(ts=time.time(), val=val)
    return val


def in_quiet_hours():
    start, end = config().get("quietHours", [23, 7])
    h = datetime.now().hour
    return (h >= start or h < end) if start > end else start <= h < end


# ====================================================================== čas a čeština

def plural(n, one, few, many):
    return one if n == 1 else few if 2 <= n <= 4 else many


def dur(sec):
    m = int(max(sec, 0) // 60)
    if m < 1:
        return "chvilku"
    if m < 60:
        return f"{m} min"
    h = m // 60
    if h < 24:
        return f"{h} h"
    d = h // 24
    return f"{d} {plural(d, 'den', 'dny', 'dní')}"


def ago(ts):
    if not ts:
        return ""
    s = time.time() - ts
    if s < 60:
        return "právě teď"
    if s < 86400:
        return "před " + dur(s)
    d = int(s // 86400)
    return "včera" if d == 1 else f"před {d} dny"


def when(ts):
    if not ts:
        return ""
    dt = datetime.fromtimestamp(ts)
    today = datetime.now().date()
    if dt.date() == today:
        return f"dnes {dt:%H:%M}"
    if (today - dt.date()).days == 1:
        return f"včera {dt:%H:%M}"
    return f"{dt.day}. {dt.month}. {dt:%H:%M}"


def short(t, n=220):
    t = re.sub(r"\s+", " ", t or "").strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def human_error(e):
    if not e:
        return ""
    if TECH.search(e):
        return "Technická chyba v telefonu (aplikace se nenačetla nebo se změnila obrazovka)."
    return e


# ====================================================================== model: lidé a konverzace

IN_ACT = ("nová zpráva",)
ME_ACT = ("odesláno z dashboardu", "odpověděl jsi ručně")
HANDOFF_ACT = ("PŘEDÁNO TOBĚ", "limit odpovědí bota", "převzato tebou")


def handoff_reason(action, note):
    if action.startswith("limit odpovědí"):
        return "Bot už odpověděl několikrát za sebou a nechce působit jako automat – pokračuj ty."
    if action.startswith("PŘEDÁNO TOBĚ – opakovaná chyba"):
        return "Bot se v téhle konverzaci opakovaně zasekl – odpověz prosím sám."
    if action.startswith("převzato tebou"):
        return "Převzal(a) jsi konverzaci sám – bot mlčí."
    return note or "Bot si nevěděl rady."


def people():
    """Všechny konverzace s tím, co o nich víme, a hlavně: kdo je na tahu."""
    st = state().get("threads", {})
    now = time.time()
    by_who = defaultdict(list)
    for r in log_rows():
        by_who[r["who"]].append(r)
    msgs, outbox, decisions = defaultdict(list), defaultdict(list), defaultdict(list)
    try:
        with data.db() as con:
            for p, dirn, text, ts, img in con.execute("SELECT person, direction, text, ts, images FROM messages ORDER BY id"):
                msgs[p].append({"dir": dirn, "text": text or "", "ts": ts or 0, "img": img})
            for row in con.execute("SELECT id, ts, kind, person, text, status, error, done_ts FROM outbox ORDER BY id"):
                outbox[row[3]].append(dict(zip(("id", "ts", "kind", "person", "text", "status", "error", "done"), row)))
            for ts, p, note in con.execute("SELECT ts, person, note FROM decisions WHERE handoff=1 ORDER BY id"):
                decisions[p].append({"ts": ts, "note": note or ""})
    except Exception:  # noqa: BLE001
        pass

    out = {}
    for name in set(st) | set(msgs):
        t = st.get(name, {})
        m, ev, ob = msgs.get(name, []), by_who.get(name, []), outbox.get(name, [])
        # historie importovaná naráz (≥3 zprávy se stejným časem) – skutečný čas těch zpráv neznáme
        if m:
            batch = [x for x in m if x["ts"] - m[0]["ts"] < 5]
            if len(batch) >= 3:
                for x in batch:
                    x["known"] = False
        tm = [x for x in m if x.get("known", True)]

        # předání
        hand = None
        for r in reversed(ev):
            if r["action"].startswith(HANDOFF_ACT):
                hand = {"ts": r["ts"], "action": r["action"], "note": r["detail"]}
                break
        for dct in decisions.get(name, [])[-1:]:
            if not hand or dct["ts"] > hand["ts"] + 5:
                hand = {"ts": dct["ts"], "action": "PŘEDÁNO TOBĚ", "note": dct["note"]}
        if hand:
            hand["reason"] = handoff_reason(hand["action"], hand["note"])

        # poslední zpráva zákazníka
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

        last_out = ""
        if preview.startswith("Já:"):
            last_out = preview[3:].strip()
        else:
            last_out = next((x["text"] for x in reversed(m) if x["dir"] != "in"), "")
        last_out_by = "bot" if any(norm(last_out)[:40] and norm(last_out)[:40] in norm(s) for s in t.get("botSent", [])) else "ty"

        handled = bool(preview) and norm(preview.rstrip("…"))[:60] in norm(t.get("handled", ""))
        paused_left = (t.get("pausedUntil", 0) or 0) / 1000 - now
        activity = max([r["ts"] for r in ev] + [x["ts"] for x in m] + [in_ts], default=0)
        spam = bool(SPAM.search(name))
        empty = not m and preview in ("", "Nová zpráva")
        pending = [o for o in ob if o["status"] == "ceka"]
        last_ok = max((o["done"] or 0 for o in ob if o["status"] == "hotovo" and o["kind"] == "zprava"), default=0)
        failed = [o for o in ob if o["status"] == "chyba" and (o["done"] or o["ts"]) > last_ok]

        p = {"name": name, "t": t, "msgs": m, "hand": hand, "in_ts": in_ts, "in_text": in_text,
             "customer_last": customer_last, "me_ts": me_ts, "out_ts": out_ts, "last_out": last_out,
             "last_out_by": last_out_by, "handled": handled, "paused_left": paused_left,
             "activity": activity, "spam": spam, "empty": empty, "watched": name in st, "pending": pending, "failed": failed, "outbox": ob,
             "blocked": bool(t.get("blocked")), "handed": bool(t.get("handedOff"))}
        p["status"] = classify(p, now)
        out[name] = p
    return out


def classify(p, now):
    if p["blocked"]:
        return "zablokovany"
    if p["spam"]:
        return "spam"
    if p["empty"]:
        return "prazdna"
    if p["handed"]:
        hand_ts = p["hand"]["ts"] if p["hand"] else 0
        owner_took = p["hand"] and p["hand"]["action"].startswith("převzato")
        if p["customer_last"] or (not owner_took and p["me_ts"] <= hand_ts + 1):
            return "predano"
        return "prevzato"
    if p["paused_left"] > 0:
        return "pises_odpovedel" if p["customer_last"] else "pises"
    if p["customer_last"] and not p["handled"]:
        if not p["watched"] or not p["in_ts"]:
            return "stare"  # bot ji nehlídá / neznáme čas – nejspíš starší, vyřízené jinde
        return "neodpovedel" if now - (p["in_ts"] or now) > STUCK_MIN * 60 else "bot_pise"
    return "hotovo"


STATUS = {  # (krátký popis, barva, seřazení)
    "predano": ("Na tahu jsi ty", "bad", 0),
    "pises_odpovedel": ("Odpověděl ti – na tahu jsi ty", "bad", 0),
    "neodpovedel": ("Bot neodpověděl", "warn", 1),
    "bot_pise": ("Bot právě odpovídá", "info", 2),
    "prevzato": ("Píšeš ty · čeká se na zákazníka", "mute", 3),
    "pises": ("Píšeš ty · bot mlčí", "mute", 3),
    "hotovo": ("Vyřízeno · čeká se na zákazníka", "ok", 4),
    "stare": ("Starší – možná bez odpovědi", "warn", 2),
    "zablokovany": ("Zablokován", "mute", 5),
    "spam": ("Spam", "mute", 6),
    "prazdna": ("Bez zpráv", "mute", 6),
}


def orders(ppl=None):
    """Kompletní objednávky – z poznámek, se kterými bot předal konverzaci (poslední na člověka)."""
    found = {}
    for r in log_rows():
        if r["action"].startswith("PŘEDÁNO") and ORDER.search(r["detail"]):
            found[r["who"]] = (r["ts"], r["detail"])
    try:
        with data.db() as con:
            for ts, person, note in con.execute("SELECT ts, person, note FROM decisions WHERE handoff=1 ORDER BY id"):
                if note and ORDER.search(note) and (person not in found or ts > found[person][0] + 5):
                    found[person] = (ts, note)
    except Exception:  # noqa: BLE001
        pass
    progress = stav()["objednavky"]
    out = []
    for person, (ts, note) in found.items():
        key = f"{person}|{int(ts)}"
        m = re.match(r"\s*objednávk\w*\s+(?:je\s+)?\w+\s*[:–-]\s*(.*)", note, re.I)
        what, rest = (None, note)
        if m:
            parts = [x.strip() for x in m.group(1).split(",")]
            what, rest = parts[0], ", ".join(parts[1:])
        step = progress.get(key, {}).get("krok", "domluveno")
        out.append({"key": key, "person": person, "ts": ts, "note": note, "what": what, "rest": rest,
                    "step": step, "step_ts": progress.get(key, {}).get("ts", ts)})
    out.sort(key=lambda o: o["ts"])
    return out


def formal(text):
    if re.search(r"\b(bys|máš|chceš|napiš|dej|pošli)\b|\bahoj\b|\bčau\b", text or "", re.I):
        return False
    return True


# ====================================================================== úkoly pro domovskou stránku

def items(ppl=None):
    """Všechno, kde jsi na tahu ty – jako karty. Vrací (aktivní, skryté-starší, připomenutí)."""
    ppl = ppl if ppl is not None else people()
    now = time.time()
    hidden = stav()["skryto"]
    order_people = {o["person"]: o for o in orders()}
    cards, follow = [], []

    for p in ppl.values():
        s = p["status"]
        name = p["name"]
        if s == "predano":
            only_order = name in order_people and not p["customer_last"]
            if only_order:
                continue  # řeší se v Objednávkách
            since = p["in_ts"] if p["customer_last"] and p["in_ts"] else (p["hand"]["ts"] if p["hand"] else p["activity"])
            cards.append({
                "key": f"predano|{name}|{int(since or 0)}", "kind": "reply", "person": name, "since": since,
                "badge": "Zákazník čeká na odpověď" if p["customer_last"] else "Bot slíbil, že se ozveš",
                "why": p["hand"]["reason"] if p["hand"] else "Bot konverzaci předal tobě.",
                "said": p["in_text"] if p["customer_last"] else "", "urgent": p["customer_last"],
                "p": p, "order": name in order_people})
        elif s == "pises_odpovedel":
            cards.append({
                "key": f"pises|{name}|{int(p['in_ts'] or 0)}", "kind": "reply", "person": name, "since": p["in_ts"],
                "badge": "Odpověděl ti", "urgent": True, "p": p, "order": name in order_people,
                "why": f"Píšeš si s ním sám, proto bot ještě {dur(p['paused_left'])} mlčí.",
                "said": p["in_text"]})
        elif s == "neodpovedel" and now - (p["in_ts"] or 0) < OLD_DAYS * 86400:
            cards.append({
                "key": f"stuck|{name}|{int(p['in_ts'] or 0)}", "kind": "reply", "person": name, "since": p["in_ts"],
                "badge": "Bot neodpověděl", "urgent": True, "p": p, "order": False,
                "why": "Zákazník napsal a bot zatím nereagoval. Buď odpověz sám, nebo to nech botovi – zkusí to při další kontrole.",
                "said": p["in_text"]})
        elif s == "stare":
            cards.append({
                "key": f"stare|{name}|{len(p['msgs'])}", "kind": "reply", "person": name, "since": p["in_ts"] or p["activity"],
                "badge": "Možná zapomenutá", "urgent": False, "p": p, "order": False, "force_old": True,
                "why": "Zákazník napsal poslední, ale je to starší konverzace, kterou bot nehlídá. Když je vyřízená jinde, dej Skrýt.",
                "said": p["in_text"]})
        if p["failed"]:
            f = p["failed"][-1]
            if now - (f["done"] or f["ts"]) < TASK_DAYS * 86400:
                cards.append({
                    "key": f"chyba|{name}|{f['id']}", "kind": "retry", "person": name, "since": f["ts"], "urgent": True,
                    "badge": "Zpráva neodešla", "p": p, "order": False, "out": f,
                    "why": "Bot tvoji zprávu v telefonu nedokázal napsat. " + human_error(f["error"] or ""),
                    "said": ""})
        # připomenout se
        if (s in ("hotovo", "prevzato") and not p["customer_last"] and "?" in (p["last_out"] or "")
                and FOLLOWUP_DAYS * 86400 < now - p["activity"] < FOLLOWUP_MAX_DAYS * 86400 and name not in order_people):
            key = f"pripom|{name}|{int(p['activity'])}"
            if key not in hidden:
                text = "Dobrý den, máte o lampičku ještě zájem? 🙂" if formal(p["last_out"]) else "Ahoj, máš o lampičku ještě zájem? 🙂"
                follow.append({"key": key, "person": name, "since": p["activity"], "p": p, "suggest": text})

    # komentáře, na které bot nedokázal odpovědět
    seen_comments = set()
    try:
        with data.db() as con:
            replied = {(p_, norm(t_)) for p_, t_ in con.execute("SELECT person, text FROM comments WHERE reply != ''")}
            grp_of = {(p_, norm(t_)): g for g, p_, t_ in con.execute("SELECT grp, person, text FROM comments")}
            fresh = con.execute("SELECT ts, grp, person, text FROM comments WHERE (reply IS NULL OR reply='') "
                                "AND (dm IS NULL OR dm='') AND indent=0 AND ts > ?", (now - TASK_DAYS * 86400,)).fetchall()
    except Exception:  # noqa: BLE001
        replied, grp_of, fresh = set(), {}, []
    for r in reversed(log_rows()):
        if not r["action"].startswith("KOMENTÁŘ – CHYBA") or now - r["ts"] > TASK_DAYS * 86400:
            continue
        m = re.match(r"„(.*?)“", r["detail"])
        text = m.group(1) if m else ""
        k = (r["who"], norm(text))
        later = any(x["who"] == r["who"] and x["ts"] > r["ts"] and x["action"].startswith("komentář –") for x in log_rows())
        talking = r["who"] in ppl and ppl[r["who"]]["msgs"]
        if k in seen_comments or k in replied or later or talking or not (INTEREST.search(text) or "?" in text):
            seen_comments.add(k)
            continue
        seen_comments.add(k)
        cards.append({"key": f"kom|{r['who']}|{norm(text)[:40]}", "kind": "fb", "person": r["who"], "since": r["ts"],
                      "badge": "Komentář bez odpovědi", "urgent": False, "p": ppl.get(r["who"]), "order": False,
                      "why": f"Bot na komentář ve skupině {grp_of.get(k) or ''} nedokázal odpovědět. Odpověz ve Facebooku ručně.".replace("  ", " "),
                      "said": text})
    for ts, grp, person, text in fresh:
        k = (person, norm(text))
        if k in seen_comments or not INTEREST.search(text or "") or (person in ppl and ppl[person]["msgs"]):
            continue
        seen_comments.add(k)
        cards.append({"key": f"kom|{person}|{norm(text)[:40]}", "kind": "fb", "person": person, "since": ts,
                      "badge": "Má zájem v komentáři", "urgent": False, "p": None, "order": False,
                      "why": f"Napsal pod příspěvek ve skupině {grp or '?'} a bot mu neodpověděl ani nenapsal zprávu. Ozvi se mu ve Facebooku.",
                      "said": text})

    # skupiny, kde bot neumí vyplnit otázky ke členství
    for j in jobs():
        if j.get("status") == "otazky-vyres-rucne":
            cards.append({"key": f"skup|{j['group']}|{j.get('last', '')}", "kind": "group", "person": j["group"],
                          "since": job_ts(j), "badge": "Skupina chce odpovědi", "urgent": False, "p": None,
                          "order": False, "said": "", "detail": j.get("error", ""),
                          "why": "Bot požádal o členství, ale na otázky ke vstupu musíš odpovědět ty (ve Facebooku → Skupiny)."})
    for r in log_rows():
        if r["action"].startswith("BLOKOVÁNÍ SELHALO") and now - r["ts"] < OLD_DAYS * 86400:
            cards.append({"key": f"blok|{r['who']}|{int(r['ts'])}", "kind": "fb", "person": r["who"], "since": r["ts"],
                          "badge": "Zablokuj ručně", "urgent": True, "p": ppl.get(r["who"]), "order": False, "said": "",
                          "why": "Bot chtěl tohohle člověka zablokovat, ale v telefonu se to nepovedlo. " + (r["detail"] or "")})

    cards = [c for c in cards if c["key"] not in hidden]
    for c in cards:
        c["old"] = c.get("force_old") or (bool(c["since"]) and now - c["since"] > OLD_DAYS * 86400)
    # nejdřív ti, kdo čekají na odpověď, pak ostatní; uvnitř nejdéle čekající nahoru
    cards.sort(key=lambda c: (c["old"], 0 if c["urgent"] else 1, c["since"] or now))
    follow.sort(key=lambda f: f["since"])
    return [c for c in cards if not c["old"]], [c for c in cards if c["old"]], follow


def alerts(ppl):
    """Stav techniky v lidské řeči. Vrací seznam (závažnost, nadpis, co s tím)."""
    s = bot_status()
    out = []
    if s["running"] == 0:
        out.append(("bad", "Bot je vypnutý – nikomu se neodpovídá",
                    "Spusť ho v Terminálu: <code>cd messenger-bot && .venv/bin/python bot_telefon.py run</code>"))
    elif s["running"] > 1:
        out.append(("bad", f"Bot běží {s['running']}× najednou",
                    "Perou se o telefon. Jedno okno Terminálu s botem zavři (Ctrl+C)."))
    if not s["phone"]:
        out.append(("bad", "Telefon není připojený",
                    "Zkontroluj kabel USB a že je telefon odemčený s povoleným Laděním USB."))
    elif not s["online"]:
        since = 0
        for r in reversed(log_rows()):
            if r["who"] == "telefon" and r["action"] in ("BEZ INTERNETU", "internet zpátky"):
                since = r["ts"] if r["action"] == "BEZ INTERNETU" else 0
                break
        out.append(("bad", "Telefon nemá internet" + (f" ({ago(since)})" if since else ""),
                    "Zkontroluj Wi-Fi nebo data v telefonu. Bot počká a pak pokračuje sám."))
    now = time.time()
    pend = [o for p in ppl.values() for o in p["pending"]]
    if pend and s["running"]:
        oldest = min(o["ts"] for o in pend)
        if now - oldest > 180:
            out.append(("warn", f"Zpráva čeká na odeslání už {dur(now - oldest)}",
                        "Bot je asi zrovna u příspěvků nebo komentářů. Napíše ji, jakmile skončí."))
    recent = sum(p.get("count", 1) for p in problems() if p.get("status") == "novy" and now - p.get("lastTs", 0) < 3600)
    if recent >= 3:
        out.append(("warn", f"Bot se za poslední hodinu {recent}× zasekl",
                    f"Většinou si poradí sám (restartuje Messenger). Když to trvá, mrkni do <a href='{url_for('stav_bota')}'>Stavu bota</a>."))
    return out


def today_summary(ppl):
    today = datetime.now().date()
    rows = [r for r in log_rows() if r["ts"] and datetime.fromtimestamp(r["ts"]).date() == today]
    replied = {r["who"] for r in rows if r["action"].startswith("odesláno") and "dashboard" not in r["action"]}
    comments = sum(1 for r in rows if r["action"].startswith("komentář – odpověděl"))
    posts = state().get("postsByDay", {}).get(today.isoformat(), 0)
    maxp = config().get("maxPostsPerDay", 4)
    busy = sum(1 for p in ppl.values() if p["status"] == "bot_pise")
    parts = [f"bot odpověděl {len(replied)} {plural(len(replied), 'člověku', 'lidem', 'lidem')}",
             f"na {comments} {plural(comments, 'komentář', 'komentáře', 'komentářů')}",
             f"přidal {posts} z {maxp} příspěvků"]
    txt = "Dnes " + ", ".join(parts[:-1]) + " a " + parts[-1] + "."
    if busy:
        txt += f" Právě vyřizuje {busy} {plural(busy, 'konverzaci', 'konverzace', 'konverzací')}."
    return txt


# ====================================================================== vzhled

CSS = """
:root{--bg:#f5f3ee;--card:#fff;--ink:#1c1b19;--mute:#6a6862;--line:#e3e0d8;--soft:#efece5;
--acc:#2d6a4f;--acc-ink:#fff;--accbg:#e6f2ec;--bad:#b42f2f;--badbg:#fbeaea;--warn:#a35a12;--warnbg:#fdf1e2;
--info:#2f5fa8;--infobg:#e8effa;--in:#eef1f6;--bot:#e9f4ee;--me:#fdf3e1;--shadow:0 1px 2px rgba(0,0,0,.05),0 2px 10px rgba(0,0,0,.04)}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#141413;--card:#1e1e1c;--ink:#eceae4;--mute:#a09d95;--line:#33322e;--soft:#262623;
--acc:#6cc39b;--acc-ink:#0f1f17;--accbg:#1a2d24;--bad:#ef8a8a;--badbg:#3a1f1f;--warn:#e9ad66;--warnbg:#36291a;--info:#8db4ef;--infobg:#1c2638;
--in:#252a33;--bot:#1c2c24;--me:#33291b;--shadow:none}}
:root[data-theme=dark]{--bg:#141413;--card:#1e1e1c;--ink:#eceae4;--mute:#a09d95;--line:#33322e;--soft:#262623;
--acc:#6cc39b;--acc-ink:#0f1f17;--accbg:#1a2d24;--bad:#ef8a8a;--badbg:#3a1f1f;--warn:#e9ad66;--warnbg:#36291a;--info:#8db4ef;--infobg:#1c2638;
--in:#252a33;--bot:#1c2c24;--me:#33291b;--shadow:none}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;font:17px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;background:var(--bg);color:var(--ink)}
a{color:var(--acc)}
code{font:14px ui-monospace,Menlo,monospace;background:var(--soft);padding:2px 6px;border-radius:6px;word-break:break-all}
header.top{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--line)}
.topin{max-width:980px;margin:0 auto;display:flex;align-items:center;gap:8px;padding:0 16px}
.brand{font-weight:700;font-size:18px;margin-right:6px;white-space:nowrap;text-decoration:none;color:var(--ink)}
nav{display:flex;gap:2px;overflow-x:auto;scrollbar-width:none;flex:1}
nav::-webkit-scrollbar{display:none}
nav a{padding:14px 12px;color:var(--mute);text-decoration:none;white-space:nowrap;border-bottom:3px solid transparent;font-size:16px}
nav a.on{color:var(--ink);border-bottom-color:var(--acc);font-weight:600}
nav .n{display:inline-block;min-width:22px;padding:0 6px;margin-left:4px;border-radius:999px;background:var(--bad);color:#fff;font-size:13px;text-align:center;line-height:20px}
.theme{background:none;border:0;color:var(--mute);font-size:20px;cursor:pointer;padding:8px;min-height:auto}
main{max-width:760px;margin:0 auto;padding:20px 16px 60px;overflow-wrap:anywhere}
main.wide{max-width:980px}
h1{font-size:28px;line-height:1.2;margin:8px 0 6px}
h2{font-size:20px;margin:34px 0 10px;display:flex;align-items:baseline;gap:8px}
h2 .cnt{color:var(--mute);font-weight:400;font-size:16px}
.lead{color:var(--mute);margin:0 0 18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin:0 0 12px;box-shadow:var(--shadow)}
.card.u-bad{border-left:5px solid var(--bad)}.card.u-warn{border-left:5px solid var(--warn)}.card.u-ok{border-left:5px solid var(--acc)}
.ch{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:6px}
.ch .name{font-weight:650;font-size:19px;text-decoration:none;color:var(--ink)}
.ch .name:hover{text-decoration:underline}
.ch .wait{margin-left:auto;font-size:15px;color:var(--mute)}
.ch .wait.late{color:var(--bad);font-weight:600}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:13.5px;font-weight:600;background:var(--soft);color:var(--mute);white-space:nowrap}
.badge.bad{background:var(--badbg);color:var(--bad)}.badge.warn{background:var(--warnbg);color:var(--warn)}
.badge.ok{background:var(--accbg);color:var(--acc)}.badge.info{background:var(--infobg);color:var(--info)}
.why{color:var(--mute);font-size:15.5px;margin:2px 0 10px}
.said{margin:8px 0 12px;padding:10px 14px;background:var(--in);border-radius:4px 14px 14px 14px;white-space:pre-wrap;font-size:17px}
.said .who{display:block;font-size:13px;color:var(--mute);margin-bottom:2px}
textarea,input[type=text]{width:100%;font:inherit;font-size:17px;padding:10px 12px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--ink)}
textarea{min-height:76px;resize:vertical}
textarea:focus,input[type=text]:focus{outline:2px solid var(--acc);outline-offset:1px;border-color:transparent}
.actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px}
button,.btn{font:inherit;font-size:16px;font-weight:600;min-height:44px;padding:9px 18px;border-radius:10px;border:1px solid var(--acc);background:var(--acc);color:var(--acc-ink);cursor:pointer;text-decoration:none;display:inline-flex;align-items:center}
button.sec,.btn.sec{background:transparent;color:var(--acc)}
button.link{background:none;border:0;color:var(--mute);font-weight:400;padding:9px 6px;text-decoration:underline;text-underline-offset:3px}
button:disabled{opacity:.6;cursor:wait}
.actions .more{margin-left:auto;font-size:15px;color:var(--mute)}
.hint{font-size:14px;color:var(--mute);margin-top:6px}
.hero{display:flex;gap:16px;align-items:center;padding:26px 22px}
.hero .ic{flex:none;width:56px;height:56px;border-radius:50%;background:var(--accbg);color:var(--acc);display:grid;place-items:center;font-size:30px;font-weight:700}
.hero h1{margin:0}.hero p{margin:4px 0 0;color:var(--mute)}
.okline{display:flex;align-items:center;gap:8px;color:var(--mute);font-size:15px;margin:0 0 18px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--acc);flex:none}
.dot.bad{background:var(--bad)}.dot.warn{background:var(--warn)}.dot.mute{background:var(--mute)}
.alert{display:flex;gap:14px;align-items:flex-start}
.alert .ic{flex:none;width:34px;height:34px;border-radius:50%;display:grid;place-items:center;font-weight:800;color:#fff;background:var(--bad)}
.alert.warn .ic{background:var(--warn)}
.alert b{display:block;font-size:18px}.alert div{color:var(--mute);font-size:15.5px}
details.fold{margin:0 0 12px}
details.fold>summary{cursor:pointer;list-style:none;padding:12px 0;color:var(--mute);font-weight:600;display:flex;gap:8px;align-items:center}
details.fold>summary::-webkit-details-marker{display:none}
details.fold>summary:before{content:"›";display:inline-block;transition:transform .15s;font-size:20px;line-height:1}
details.fold[open]>summary:before{transform:rotate(90deg)}
details.mini>summary{cursor:pointer;color:var(--mute);font-size:15px;margin:6px 0}
.empty{color:var(--mute);text-align:center;padding:26px 16px}
.summary{color:var(--mute);font-size:15.5px;border-top:1px solid var(--line);margin-top:30px;padding-top:16px}
.links{display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:8px}
.steps{display:flex;gap:6px;align-items:center;font-size:14px;color:var(--mute);margin:8px 0 4px;flex-wrap:wrap}
.steps span{padding:3px 10px;border-radius:999px;background:var(--soft)}
.steps span.done{background:var(--accbg);color:var(--acc);font-weight:600}
.make{font-size:19px;margin:4px 0}
.list .row{display:flex;gap:12px;align-items:center;padding:12px 4px;border-bottom:1px solid var(--line);text-decoration:none;color:var(--ink)}
.list .row:last-child{border-bottom:0}
.list .row:hover{background:var(--soft)}
.list .main{flex:1;min-width:0}
.list .main b{display:block}
.list .main .sub{color:var(--mute);font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.list .side{text-align:right;flex:none;font-size:14px;color:var(--mute)}
.list .side .badge{margin-bottom:4px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 12px}
.chips button{min-height:36px;padding:5px 14px;font-weight:500;background:var(--card);color:var(--ink);border:1px solid var(--line)}
.chips button.on{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.bubbles{display:flex;flex-direction:column;gap:6px}
.msg{max-width:82%;padding:9px 13px;border-radius:16px;white-space:pre-wrap;word-wrap:break-word}
.msg .who{display:block;font-size:12.5px;color:var(--mute)}
.msg.in{background:var(--in);align-self:flex-start;border-top-left-radius:4px}
.msg.bot{background:var(--bot);align-self:flex-end;border-top-right-radius:4px}
.msg.manual{background:var(--me);align-self:flex-end;border-top-right-radius:4px}
.toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:12px 20px;border-radius:12px;font-size:16px;z-index:9;max-width:calc(100% - 32px);box-shadow:0 6px 24px rgba(0,0,0,.25);transition:opacity .4s}
.shot{width:120px;height:auto;border-radius:8px;border:1px solid var(--line);flex:none}
.grid3{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.tile b{display:flex;align-items:center;gap:8px;font-size:18px}.tile div{color:var(--mute);font-size:15px;margin-top:4px}
.ev{display:flex;gap:12px;padding:8px 0;border-bottom:1px solid var(--line);font-size:15.5px}
.ev .t{color:var(--mute);width:92px;flex:none}
.lesson{padding:8px 0;border-bottom:1px solid var(--line)}
.lesson:last-child{border-bottom:0}
.kv{color:var(--mute);font-size:15px}
@media (max-width:560px){body{font-size:16.5px}h1{font-size:24px}.card{padding:14px}.ch .wait{margin-left:0;width:100%}
.msg{max-width:92%}.brand{display:none}.hero{padding:20px 16px}.actions .more{margin-left:0;width:100%}}
"""

JS = """
(function(){
  var root=document.documentElement;
  function setTheme(t){ if(t){root.setAttribute('data-theme',t)}else{root.removeAttribute('data-theme')} }
  try{ setTheme(localStorage.getItem('theme')); }catch(e){}
  var tb=document.getElementById('theme');
  if(tb) tb.addEventListener('click',function(){
    var dark=root.getAttribute('data-theme')==='dark'||(!root.getAttribute('data-theme')&&matchMedia('(prefers-color-scheme: dark)').matches);
    var t=dark?'light':'dark'; setTheme(t); try{localStorage.setItem('theme',t)}catch(e){}
  });
  document.querySelectorAll('form').forEach(function(f){
    f.addEventListener('submit',function(ev){
      var b=ev.submitter; if(b&&b.dataset.busy){setTimeout(function(){b.disabled=true;b.textContent=b.dataset.busy},0)}
    });
  });
  document.querySelectorAll('textarea').forEach(function(t){
    t.addEventListener('keydown',function(e){ if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){ var b=t.form.querySelector('button[data-main]'); if(b){b.click()} } });
  });
  var toast=document.querySelector('.toast');
  if(toast){ setTimeout(function(){toast.style.opacity=0},4500); setTimeout(function(){toast.remove()},5000);
    try{history.replaceState(null,'',location.pathname+location.hash)}catch(e){} }
  // filtry v seznamech
  document.querySelectorAll('.chips').forEach(function(c){
    var target=document.querySelector(c.dataset.target), q=document.querySelector(c.dataset.search);
    function apply(){
      var on=c.querySelector('.on'), f=on?on.dataset.f:'', s=q?q.value.trim().toLowerCase():'';
      target.querySelectorAll('[data-f]').forEach(function(r){
        var ok=(!f||(' '+r.dataset.f+' ').indexOf(' '+f+' ')>=0)&&(!s||r.textContent.toLowerCase().indexOf(s)>=0);
        r.style.display=ok?'':'none';
      });
    }
    c.querySelectorAll('button').forEach(function(b){ b.addEventListener('click',function(){
      c.querySelectorAll('button').forEach(function(x){x.classList.remove('on')}); b.classList.add('on'); apply(); }); });
    if(q) q.addEventListener('input',apply);
    apply();
  });
  // obnova stránky každých 30 s – jen když nic nepíšeš a nic sis nerozbalil
  var opened=false;
  document.querySelectorAll('details').forEach(function(d){ d.addEventListener('toggle',function(){ opened=true; }); });
  if(document.body.dataset.refresh){
    setInterval(function(){
      if(document.hidden||opened) return;
      var a=document.activeElement;
      if(a&&(a.tagName==='TEXTAREA'||a.tagName==='INPUT')) return;
      var dirty=[].some.call(document.querySelectorAll('textarea,input[type=text]'),function(x){return x.value!==x.defaultValue});
      if(!dirty) location.reload();
    },30000);
  }
})();
"""

FLASH = {
    "odeslano": "Zpráva je ve frontě. Bot ji v telefonu napíše do ~20 s a pak tam 12 h mlčí.",
    "botovi": "Hotovo – konverzace je zase na botovi.",
    "odeslano_botovi": "Zpráva je ve frontě a potom konverzaci převezme zase bot.",
    "prevzato": "Převzato – bot v té konverzaci mlčí, dokud ji nevrátíš.",
    "skryto": "Skryto. Kdyby zákazník napsal znovu, karta se vrátí.",
    "obj": "Objednávka posunuta.",
    "problem": "Označeno jako vyřešené.",
    "lekce": "Uloženo – bot to použije u další odpovědi.",
    "info": "Uloženo – bot to použije u další odpovědi.",
    "prazdne": "Zpráva je prázdná – nic jsem neposlal.",
}


def page(title, body, active="", wide=False, refresh=False, count=None):
    if count is None:
        try:
            a, _, _ = items()
            count = len(a)
        except Exception:  # noqa: BLE001
            count = 0
    tabs = [("domu", "Domů"), ("objednavky", "Objednávky"), ("lide", "Konverzace"), ("skupiny", "Skupiny"),
            ("uceni", "Učení"), ("stav_bota", "Stav bota")]
    nav = "".join(
        f'<a class="{"on" if k == active else ""}" href="{url_for(k)}">{v}'
        f'{f"<span class=n>{count}</span>" if k == "domu" and count else ""}</a>' for k, v in tabs)
    flash = FLASH.get(request.args.get("ok", ""))
    toast = f'<div class="toast" role="status">{escape(flash)}</div>' if flash else ""
    t = f"({count}) " if count else ""
    return (f'<!doctype html><html lang="cs"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{t}{escape(title)} · Lampičky</title><style>{CSS}</style></head>'
            f'<body{" data-refresh=1" if refresh else ""}><header class="top"><div class="topin">'
            f'<a class="brand" href="{url_for("domu")}">Lampičky</a><nav>{nav}</nav>'
            f'<button class="theme" id="theme" title="Světlý / tmavý režim" aria-label="Přepnout světlý a tmavý režim">◐</button>'
            f'</div></header><main class="{"wide" if wide else ""}">{body}</main>{toast}<script>{JS}</script></body></html>')


def here():
    return escape(request.full_path.rstrip("?") if request.query_string else request.path)


def hidden_next():
    path = request.path
    return f'<input type="hidden" name="next" value="{escape(path)}">'


def conv_link(name):
    return url_for("konverzace", person=name)


def wait_html(since, urgent):
    if not since:
        return ""
    s = time.time() - since
    late = urgent and s > 2 * 3600
    return f'<span class="wait{" late" if late else ""}">{"čeká " if urgent else ""}{dur(s) if urgent else ago(since)}</span>'


def reply_form(person, placeholder="Napiš odpověď…", value="", main_label="Odeslat", show_return=True, key=None):
    ret = (f'<button class="sec" formaction="{url_for("botovi")}" formnovalidate data-busy="Vracím…" '
           f'title="Bot bude v konverzaci zase odpovídat sám. Když něco napíšeš, nejdřív se to odešle.">Vrátit botovi</button>'
           if show_return else "")
    hide = (f'<button class="link" formaction="{url_for("skryt")}" formnovalidate name="key" value="{escape(key)}" '
            f'title="Vyřízeno jinak – karta zmizí. Vrátí se, když zákazník napíše znovu.">Skrýt</button>' if key else "")
    return (f'<form method="post" action="{url_for("odpovedet")}">'
            f'<input type="hidden" name="person" value="{escape(person)}">{hidden_next()}'
            f'<textarea name="text" required placeholder="{escape(placeholder)}" aria-label="Odpověď pro {escape(person)}">{escape(value)}</textarea>'
            f'<div class="actions"><button data-main data-busy="Posílám…">{main_label}</button>{ret}{hide}'
            f'<a class="more" href="{conv_link(person)}">Celá konverzace →</a></div></form>')


def card_html(c):
    p = c["p"]
    urgent = c["urgent"]
    badge_cls = "bad" if urgent else "warn"
    name = escape(c["person"])
    title = f'<a class="name" href="{conv_link(c["person"])}">{name}</a>' if p else f'<span class="name">{name}</span>'
    extra = ' <span class="badge ok">objednávka</span>' if c.get("order") else ""
    pend = ""
    if p and p["pending"]:
        pend = '<div class="hint"><span class="badge info">Odesílá se</span> Bot zprávu právě píše v telefonu…</div>'
    h = (f'<article class="card u-{"bad" if urgent else "warn"}"><div class="ch">{title}'
         f'<span class="badge {badge_cls}">{escape(c["badge"])}</span>{extra}{wait_html(c["since"], urgent)}</div>'
         f'<div class="why">{escape(c["why"])}</div>')
    if c.get("said"):
        h += f'<div class="said"><span class="who">{"Komentář" if c["kind"] == "fb" else "Zákazník napsal"}</span>{escape(c["said"])}</div>'
    if c.get("detail"):
        h += f'<details class="mini"><summary>Co bot zkoušel</summary><div class="why">{escape(human_error(c["detail"]))}</div></details>'
    if c["kind"] == "reply":
        h += pend + reply_form(c["person"], key=c["key"])
    elif c["kind"] == "retry":
        o = c["out"]
        h += (f'<div class="said" style="background:var(--me)"><span class="who">Tvoje zpráva, která neodešla</span>{escape(o["text"] or "")}</div>'
              + reply_form(c["person"], value=o["text"] or "", main_label="Zkusit znovu", show_return=False, key=c["key"]))
    else:
        h += (f'<form method="post" action="{url_for("skryt")}" class="actions">{hidden_next()}'
              f'<input type="hidden" name="key" value="{escape(c["key"])}">'
              f'<button class="sec" data-busy="…">Hotovo, skrýt</button>'
              + (f'<a class="more" href="{conv_link(c["person"])}">Konverzace →</a>' if p else "") + '</form>')
    return h + "</article>"


ORDER_STEPS = [("domluveno", "Domluveno"), ("vyrobeno", "Vyrobeno"), ("odeslano", "Odesláno")]


def order_html(o, compact=False):
    idx = [k for k, _ in ORDER_STEPS].index(o["step"]) if o["step"] in dict(ORDER_STEPS) else 0
    steps = "".join(f'<span class="{"done" if i <= idx else ""}">{"✓ " if i <= idx else ""}{v}</span>'
                    for i, (_, v) in enumerate(ORDER_STEPS))
    age = time.time() - o["ts"]
    late = idx < 2 and age > 5 * 86400
    what = (f'<div class="make">Vyrobit: <b>{escape(o["what"])}</b></div>' if o["what"]
            else f'<div class="why" style="color:var(--ink)">{escape(o["note"])}</div>')
    rest = (f'<details class="mini"><summary>Doprava, kontakt a platba</summary><div class="why">{escape(o["rest"])}</div></details>'
            if o["what"] and o["rest"] else "")
    nxt = ""
    if idx < 2:
        k, label = ORDER_STEPS[idx + 1]
        nxt = (f'<form method="post" action="{url_for("objednavka")}" class="actions">{hidden_next()}'
               f'<input type="hidden" name="key" value="{escape(o["key"])}"><input type="hidden" name="krok" value="{k}">'
               f'<button data-busy="…">{"Mám vyrobeno" if k == "vyrobeno" else "Odesláno"}</button>'
               f'<a class="more" href="{conv_link(o["person"])}">Konverzace →</a></form>')
    else:
        nxt = (f'<form method="post" action="{url_for("objednavka")}" class="actions">{hidden_next()}'
               f'<input type="hidden" name="key" value="{escape(o["key"])}"><input type="hidden" name="krok" value="vyrobeno">'
               f'<button class="link" data-busy="…">Vrátit na „vyrobeno“</button>'
               f'<a class="more" href="{conv_link(o["person"])}">Konverzace →</a></form>')
    when_txt = f'domluveno {ago(o["ts"])}' + (" · už by měla být hotová" if late else "")
    return (f'<article class="card u-{"bad" if late else "ok"}"><div class="ch"><a class="name" href="{conv_link(o["person"])}">{escape(o["person"])}</a>'
            f'<span class="wait{" late" if late else ""}">{escape(when_txt)}</span></div>'
            f'{what}{rest}<div class="steps">{steps}</div>{nxt}</article>')


# ====================================================================== stránky

@app.route("/")
def domu():
    ppl = people()
    active, old, follow = items(ppl)
    al = alerts(ppl)
    open_orders = [o for o in orders() if o["step"] != "odeslano"]
    body = ""
    for sev, title, how in al:
        body += (f'<div class="card alert {sev} u-{sev}" role="alert"><span class="ic">!</span>'
                 f'<span><b>{escape(title)}</b><div>{how}</div></span></div>')
    last = max((r["ts"] for r in log_rows()), default=0)
    quiet = " · noční klid (bot jen reaguje na nové zprávy)" if in_quiet_hours() else ""
    if not active and not al:
        body += (f'<section class="card hero u-ok"><span class="ic">✓</span><div><h1>Vše běží, nic nečeká</h1>'
                 f'<p>Bot hlídá zprávy i komentáře. Naposledy něco dělal {escape(ago(last) or "–")}{quiet}.</p></div></section>')
    else:
        if not al:
            body += (f'<div class="okline"><span class="dot"></span>Bot běží, telefon i internet v pořádku · '
                     f'naposledy něco dělal {escape(ago(last) or "–")}{quiet}</div>')
        if active:
            first = active[0]
            longest = f'Nejdéle čeká {escape(first["person"])} ({dur(time.time() - first["since"])}).' if first["since"] and first["urgent"] else ""
            body += f'<h1>Na tahu jsi ty: {len(active)}</h1><p class="lead">{longest} Odpovídej rovnou tady – bot to napíše v telefonu.</p>'
        else:
            body += '<h1>Nic nečeká</h1><p class="lead">Žádná konverzace teď nepotřebuje tebe.</p>'
    body += "".join(card_html(c) for c in active)
    if old:
        body += (f'<details class="fold"><summary>Starší a nejasné ({len(old)}) – nejspíš vyřízené jinde</summary>'
                 + "".join(card_html(c) for c in old) + "</details>")

    if open_orders:
        body += f'<h2>Objednávky k vyřízení <span class="cnt">{len(open_orders)}</span></h2>'
        body += "".join(order_html(o) for o in open_orders)

    if follow:
        body += (f'<details class="fold"><summary>Možná připomenout ({len(follow)}) – zeptali jsme se a mlčí přes {FOLLOWUP_DAYS} dny</summary>')
        for f in follow:
            p = f["p"]
            body += (f'<article class="card"><div class="ch"><a class="name" href="{conv_link(f["person"])}">{escape(f["person"])}</a>'
                     f'<span class="wait">{escape(ago(f["since"]))}</span></div>'
                     f'<div class="said" style="background:var(--bot)"><span class="who">Poslední zpráva ({"bot" if p["last_out_by"] == "bot" else "ty"})</span>{escape(short(p["last_out"], 300))}</div>'
                     + reply_form(f["person"], value=f["suggest"], main_label="Připomenout se", show_return=False, key=f["key"])
                     + "</article>")
        body += "</details>"

    body += (f'<div class="summary">{escape(today_summary(ppl))}<div class="links">'
             f'<a href="{url_for("lide")}">Všechny konverzace</a><a href="{url_for("skupiny")}">Příspěvky a komentáře</a>'
             f'<a href="{url_for("uceni")}">Naučit bota něco nového</a><a href="{url_for("stav_bota")}">Stav bota</a></div></div>')
    return page("Domů", body, "domu", refresh=True, count=len(active))


@app.route("/objednavky")
def objednavky():
    allo = orders()
    open_ = [o for o in allo if o["step"] != "odeslano"]
    done = sorted([o for o in allo if o["step"] == "odeslano"], key=lambda o: -o["step_ts"])
    body = ('<h1>Objednávky</h1><p class="lead">Konverzace, kde bot domluvil všechno potřebné (motiv, doprava, platba). '
            'Posouvej je tlačítkem: domluveno → vyrobeno → odesláno.</p>')
    body += "".join(order_html(o) for o in open_) or '<div class="card empty">Žádná rozpracovaná objednávka. Až bot nějakou domluví, objeví se tady.</div>'
    if done:
        body += f'<details class="fold"><summary>Odeslané ({len(done)})</summary>' + "".join(order_html(o) for o in done) + "</details>"
    return page("Objednávky", body, "objednavky")


@app.route("/konverzace")
def lide():
    ppl = people()
    rows = sorted(ppl.values(), key=lambda p: (STATUS[p["status"]][2], -(p["activity"] or 0)))
    groups = {"ty": ("predano", "pises_odpovedel", "neodpovedel"), "bot": ("bot_pise", "hotovo", "stare"),
              "pises": ("prevzato", "pises"), "ostatni": ("zablokovany", "spam", "prazdna")}
    counts = {g: sum(1 for p in rows if p["status"] in st) for g, st in groups.items()}
    chips = [("", "Všechny"), ("ty", f"Na tahu jsi ty ({counts['ty']})"), ("bot", f"Vyřizuje bot ({counts['bot']})"),
             ("pises", f"Píšeš ty ({counts['pises']})"), ("ostatni", f"Blokovaní a spam ({counts['ostatni']})")]
    body = ('<h1>Konverzace</h1><p class="lead">Všichni, kdo psali do Messengeru. Nahoře ti, kde jsi na tahu ty.</p>'
            '<input type="text" id="q" placeholder="Hledat jméno nebo text…" aria-label="Hledat" style="margin-bottom:10px">'
            '<div class="chips" data-target="#convs" data-search="#q">'
            + "".join(f'<button type="button" class="{"on" if not k else ""}" data-f="{k}">{escape(v)}</button>' for k, v in chips)
            + '</div><div class="card list" id="convs" style="padding:4px 14px">')
    for p in rows:
        label, cls, _ = STATUS[p["status"]]
        g = next(k for k, st in groups.items() if p["status"] in st)
        if p["customer_last"]:
            sub = "Zákazník: " + short(p["in_text"] or p["t"].get("preview", ""), 120)
        else:
            sub = ("Bot: " if p["last_out_by"] == "bot" else "Ty: ") + short(p["last_out"], 120) if p["last_out"] else ""
        hide = ' style="display:none"' if g == "ostatni" else ""
        body += (f'<a class="row" href="{conv_link(p["name"])}" data-f="{g}"{hide}><div class="main"><b>{escape(p["name"])}</b>'
                 f'<div class="sub">{escape(sub)}</div></div><div class="side"><span class="badge {cls}">{escape(label)}</span>'
                 f'<div>{escape(ago(p["activity"]))}</div></div></a>')
    if not rows:
        body += '<div class="empty">Zatím žádné konverzace.</div>'
    return page("Konverzace", body + "</div>", "lide", wide=True, refresh=True)


@app.route("/konverzace/<path:person>")
def konverzace(person):
    ppl = people()
    p = ppl.get(person)
    with data.db() as con:
        comments = con.execute("SELECT grp, text, reply, dm FROM comments WHERE person=? ORDER BY ts", (person,)).fetchall()
    if not p and not comments:
        abort(404)
    body = f'<p style="margin:0"><a href="{url_for("lide")}">← Všechny konverzace</a></p><h1>{escape(person)}</h1>'
    if p:
        label, cls, _ = STATUS[p["status"]]
        explain = {
            "predano": "Bot ti konverzaci předal a sám už neodpovídá. Odpověz, nebo ji vrať botovi.",
            "pises_odpovedel": f"Píšeš si s ním ty, takže bot ještě {dur(p['paused_left'])} mlčí. Zákazník ti odpověděl.",
            "neodpovedel": "Zákazník napsal a bot zatím neodpověděl.",
            "bot_pise": "Zákazník právě napsal – bot odpoví během pár minut.",
            "prevzato": "Konverzaci máš ty. Bot mlčí, dokud ji nevrátíš.",
            "pises": f"Píšeš si s ním ty, bot mlčí ještě {dur(p['paused_left'])}.",
            "hotovo": "Vše zodpovězeno, čeká se na zákazníka. Když napíše, bot odpoví sám.",
            "zablokovany": "Bot tomuhle člověku už nikdy neodpoví.",
            "stare": "Zákazník napsal poslední, ale je to starší konverzace, kterou bot nehlídá. Mrkni, jestli není vyřízená jinde.",
            "prazdna": "V konverzaci zatím nejsou žádné uložené zprávy.",
            "spam": "Vypadá to na spam nebo prázdnou konverzaci.",
        }[p["status"]]
        body += f'<div class="card u-{"bad" if cls == "bad" else "warn" if cls == "warn" else "ok"}"><span class="badge {cls}">{escape(label)}</span><div style="margin-top:6px">{escape(explain)}</div>'
        if p["hand"] and p["status"] in ("predano", "prevzato"):
            body += f'<div class="why" style="margin-top:8px"><b>Proč to bot předal ({escape(when(p["hand"]["ts"]))}):</b> {escape(p["hand"]["reason"])}</div>'
        if p["status"] != "zablokovany" and len(p["msgs"]) > 4:
            body += '<div class="actions"><a class="btn sec" href="#odpovedet">Odpovědět ↓</a></div>'
        body += "</div>"
    if comments:
        body += '<h2>Psal pod příspěvky</h2><div class="card">' + "".join(
            f'<div class="lesson"><span class="kv">{escape(g or "")}</span><div>„{escape(t)}“</div>'
            + (f'<div class="kv">Bot odpověděl: {escape(r)}</div>' if r else "")
            + (f'<div class="kv">Bot mu napsal zprávu: {escape(short(dm, 200))}</div>' if dm else "") + "</div>"
            for g, t, r, dm in comments) + "</div>"
    if p:
        body += '<h2>Zprávy</h2><div class="card"><div class="bubbles">'
        lbl = {"in": "Zákazník", "bot": "Bot", "manual": "Ty"}
        for m in p["msgs"]:
            body += (f'<div class="msg {m["dir"]}"><span class="who">{lbl.get(m["dir"], "")}</span>'
                     f'{escape(m["text"])}{" [fotka]" if m["img"] else ""}</div>')
        if not p["msgs"]:
            pv = p["t"].get("preview", "")
            body += f'<div class="empty">Zprávy se zatím neuložily.{" Poslední v seznamu: „" + escape(pv) + "“" if pv else ""}</div>'
        body += "</div></div>"
        for o in p["pending"]:
            body += f'<div class="card"><span class="badge info">Odesílá se</span> {escape(short(o["text"] or ({"botovi": "Vrátit botovi", "prevzit": "Převzít"}).get(o["kind"], ""), 200))}<div class="hint">Bot to v telefonu udělá do ~20 s.</div></div>'
        for o in p["failed"]:
            body += f'<div class="card u-bad"><span class="badge bad">Neodešlo</span> {escape(short(o["text"] or o["kind"], 200))}<div class="hint">{escape(human_error(o["error"] or ""))}</div></div>'
        if p["status"] not in ("zablokovany",):
            body += '<h2 id="odpovedet">Odpovědět</h2><div class="card">' + reply_form(person, show_return=p["status"] in ("predano", "prevzato", "pises", "pises_odpovedel"))
            body += ('<div class="hint">Prázdný řádek = další samostatná zpráva. Ctrl/⌘ + Enter odešle. '
                     'Po tvé zprávě bot v téhle konverzaci 12 h mlčí.</div></div>')
            if p["status"] not in ("predano", "prevzato"):
                body += (f'<form method="post" action="{url_for("prevzit")}" class="actions"><input type="hidden" name="person" value="{escape(person)}">'
                         f'{hidden_next()}<button class="sec" data-busy="…">Převezmu sám – ať bot mlčí</button></form>')
        if p["status"] in ("predano", "prevzato") and p["hand"]:
            body += (f'<h2>Naučit bota</h2><div class="card"><div class="why">Bot nevěděl: {escape(p["hand"]["reason"])}<br>'
                     f'Napiš pravidlo, ať to příště zvládne sám (např. „Intenzita světla se měnit nedá – odpověz bohužel ne.“).</div>'
                     f'<form method="post" action="{url_for("lekce_pridat")}">{hidden_next()}'
                     f'<input type="text" name="lekce" required placeholder="Příště odpověz…">'
                     f'<div class="actions"><button data-busy="Ukládám…">Přidat lekci</button></div></form></div>')
    return page(person, body, "lide")


@app.route("/chat/<path:person>")
def chat_alias(person):
    return redirect(url_for("konverzace", person=person))


JOB_STATUS = {
    "zverejneno": ("Zveřejněno", "ok"), "ceka": ("Čeká na zveřejnění", "info"),
    "ceka-na-clenstvi": ("Čeká na přijetí do skupiny", "info"), "ceka-na-schvaleni-prispevku": ("Čeká na schválení adminem", "info"),
    "otazky-vyres-rucne": ("Vyplň otázky ke členství", "bad"), "chyba": ("Nepovedlo se", "warn"),
    "nelze-postovat": ("Ve skupině nejde přidávat", "mute"), "preskoceno-bazar": ("Přeskočeno (bazar)", "mute"),
    "vzdano-nepřijali": ("Nepřijali tě", "mute"),
}


@app.route("/skupiny")
def skupiny():
    st = state()
    today = datetime.now().date().isoformat()
    posts = st.get("postsByDay", {}).get(today, 0)
    maxp = config().get("maxPostsPerDay", 4)
    js = jobs()
    order = {"otazky-vyres-rucne": 0, "chyba": 1, "ceka": 2, "ceka-na-clenstvi": 2, "ceka-na-schvaleni-prispevku": 2}
    waiting = sum(1 for j in js if j.get("status") in ("ceka", "ceka-na-clenstvi"))
    body = (f'<h1>Skupiny</h1><p class="lead">Dnes přidáno {posts} z {maxp} příspěvků · ve frontě čeká {waiting}. '
            'Bot přidává příspěvky sám, nejvýš 4 denně, mimo noční klid.</p>')
    body += f'<h2>Příspěvky <span class="cnt">{len(js)}</span></h2><div class="card list" style="padding:4px 14px">'
    for j in sorted(js, key=lambda j: order.get(j.get("status"), 3)):
        label, cls = JOB_STATUS.get(j.get("status"), (j.get("status", "?"), "mute"))
        err = human_error(j.get("error") or "")
        if j.get("status") == "chyba" and TECH.search(j.get("error") or ""):
            err = "Ani na třetí pokus se to v telefonu nepovedlo (technická chyba). Zkus příspěvek přidat ručně."
        body += (f'<div class="row"><div class="main"><b>{escape(j.get("group", ""))} <span class="kv">{escape(j.get("members", ""))}</span></b>'
                 f'<div class="kv">{escape(short(j.get("text", ""), 110))}</div>'
                 + (f'<details class="mini"><summary>Co se stalo</summary><div class="why">{escape(err)}</div></details>' if err else "")
                 + f'</div><div class="side"><span class="badge {cls}">{escape(label)}</span><div>{escape(j.get("last", ""))}</div></div></div>')
    if not js:
        body += '<div class="empty">Fronta je prázdná.</div>'
    body += "</div>"

    with data.db() as con:
        rows = con.execute("SELECT ts, grp, person, text, indent, reply, dm, note FROM comments ORDER BY ts DESC LIMIT 150").fetchall()
    threads = st.get("threads", {})
    no_reply = sum(1 for r in rows if not r[5] and not r[6] and not r[4])
    body += (f'<h2>Komentáře <span class="cnt">{len(rows)}</span></h2>'
             '<div class="chips" data-target="#coms">'
             f'<button type="button" class="on" data-f="">Všechny</button><button type="button" data-f="bez">Bez odpovědi ({no_reply})</button>'
             '<button type="button" data-f="zprava">Bot poslal zprávu</button></div><div id="coms">')
    for ts, grp, person, text, indent, reply, dm, note in rows:
        f = "bez" if not reply and not dm else ("zprava" if dm else "")
        who = (f'<a class="name" href="{conv_link(person)}">{escape(person)}</a>' if person in threads
               else f'<span class="name">{escape(person)}</span>')
        body += (f'<article class="card" data-f="{f}" style="padding:12px 16px"><div class="ch">{who}'
                 f'<span class="kv">{escape(grp or "")}</span><span class="wait">{escape(ago(ts))}</span></div>'
                 f'<div>{"↳ " if indent else ""}„{escape(text)}“</div>')
        if reply:
            body += f'<div class="kv">Bot odpověděl: {escape(reply)}</div>'
        if dm:
            body += f'<div class="kv"><span class="badge ok">poslal zprávu</span> {escape(short(dm, 160))}</div>'
        if not reply and not dm:
            body += f'<div class="kv">Bez odpovědi{(" – " + escape(short(note, 200))) if note and note != "z auditu" else ""}</div>'
        body += "</article>"
    if not rows:
        body += '<div class="card empty">Zatím žádné komentáře.</div>'
    return page("Skupiny", body + "</div>", "skupiny", wide=True)


@app.route("/uceni", methods=["GET", "POST"])
def uceni():
    lekce = HERE / "lekce.md"
    if request.method == "POST":
        lekce.write_text((request.form.get("lekce") or "").replace("\r\n", "\n"), encoding="utf-8")
        return back("lekce")
    text = lekce.read_text(encoding="utf-8") if lekce.exists() else ""
    lessons = [l[2:] for l in text.splitlines() if l.startswith("- ")]
    body = ('<h1>Učení</h1><p class="lead">Co bot ví a jak se zlepšuje. Lekce čte při každé odpovědi – stačí jedna věta.</p>'
            f'<div class="card"><form method="post" action="{url_for("lekce_pridat")}">{hidden_next()}'
            '<input type="text" name="lekce" required placeholder="Nová lekce, např. „Na dotaz na baterii odpověz: bohužel jen USB.“" aria-label="Nová lekce">'
            '<div class="actions"><button data-busy="Ukládám…">Přidat lekci</button></div></form></div>')
    body += f'<h2>Lekce <span class="cnt">{len(lessons)}</span></h2><div class="card">'
    body += "".join(f'<div class="lesson">{escape(l)}</div>' for l in lessons) or '<div class="empty">Zatím žádné lekce.</div>'
    body += (f'<details class="mini"><summary>Upravit nebo smazat lekce</summary><form method="post">{hidden_next()}'
             f'<textarea name="lekce" style="min-height:260px">{escape(text)}</textarea>'
             '<div class="hint">Každá lekce je řádek začínající „- “. Nejdůležitější nahoru.</div>'
             '<div class="actions"><button data-busy="Ukládám…">Uložit lekce</button></div></form></details></div>')

    try:
        with data.db() as con:
            rows = con.execute("SELECT ts, person, note FROM decisions WHERE handoff=1 ORDER BY id DESC LIMIT 25").fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    body += f'<h2>Co bot nevěděl <span class="cnt">{len(rows)}</span></h2><p class="lead">Tohle předal tobě. Co se opakuje, z toho udělej lekci.</p><div class="card">'
    body += "".join(f'<div class="lesson"><span class="kv">{escape(when(ts))} · <a href="{conv_link(p)}">{escape(p)}</a></span>'
                    f'<div>{escape(short(n or "", 300))}</div></div>' for ts, p, n in rows if not ORDER.search(n or ""))
    body += "</div>" if rows else '<div class="empty">Zatím nic.</div></div>'

    ex = data.manual_examples(20)
    body += ('<details class="fold"><summary>Tvoje ruční odpovědi – bot z nich bere styl</summary><div class="card">'
             + ("".join(f'<div class="lesson">{escape(l[2:])}</div>' for l in ex.splitlines()) if ex else '<div class="empty">Zatím žádné.</div>')
             + "</div></details>")
    info = HERE / "info.md"
    if info.exists():
        body += (f'<details class="fold"><summary>Co bot ví o lampičkách (ceny, doprava, výroba)</summary><div class="card">'
                 f'<form method="post" action="{url_for("info_ulozit")}">{hidden_next()}'
                 f'<textarea name="info" style="min-height:380px">{escape(info.read_text(encoding="utf-8"))}</textarea>'
                 '<div class="hint">Co je v [hranatých závorkách], bot nezná a předá to tobě.</div>'
                 '<div class="actions"><button data-busy="Ukládám…">Uložit</button></div></form></div></details>')
    return page("Učení", body, "uceni")


@app.route("/uceni/pridat", methods=["POST"])
def lekce_pridat():
    new = re.sub(r"\s+", " ", request.form.get("lekce") or "").strip().lstrip("- ")
    if new:
        f = HERE / "lekce.md"
        lines = f.read_text(encoding="utf-8").splitlines() if f.exists() else ["# Lekce", ""]
        at = next((i for i, l in enumerate(lines) if l.startswith("- ")), len(lines))
        lines.insert(at, f"- {new}")
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return back("lekce")


@app.route("/uceni/info", methods=["POST"])
def info_ulozit():
    text = (request.form.get("info") or "").replace("\r\n", "\n")
    if text.strip():
        (HERE / "info.md").write_text(text, encoding="utf-8")
    return back("info")


EVENT = [  # (začátek akce, srozumitelně)
    ("odesláno z dashboardu", "Tvoje zpráva odeslána"), ("odesláno", "Bot odpověděl"), ("PŘEDÁNO", "Předáno tobě"),
    ("nová zpráva, čeká", "Nová zpráva pro tebe"), ("nová zpráva, bot", "Nová zpráva (píšeš ty)"),
    ("odpověděl jsi ručně", "Odpověděl(a) jsi v telefonu"), ("komentář – odpověděl", "Odpověď na komentář"),
    ("komentář – poslal", "Zpráva z komentáře"), ("KOMENTÁŘ – CHYBA", "Komentář se nepovedl"),
    ("PŘÍSPĚVEK ZVEŘEJNĚN", "Příspěvek zveřejněn"), ("PŘÍSPĚVEK – CHYBA", "Příspěvek se nepovedl"),
    ("BEZ INTERNETU", "Telefon bez internetu"), ("internet zpátky", "Internet zpátky"), ("bez odpovědi", "Bez odpovědi (není potřeba)"),
    ("vráceno botovi", "Vráceno botovi"), ("převzato", "Převzato tebou"), ("ZABLOK", "Zablokováno"),
]


@app.route("/stav")
def stav_bota():
    s = bot_status()
    show_all = request.args.get("vse") == "1"
    tiles = [
        (s["running"] == 1, "Bot", "Běží" if s["running"] == 1 else "Neběží" if not s["running"] else f"Běží {s['running']}×",
         "Hlídá Messenger, komentáře a frontu příspěvků." if s["running"] == 1 else
         "Spusť: <code>.venv/bin/python bot_telefon.py run</code>" if not s["running"] else "Jeden z nich zavři."),
        (s["phone"], "Telefon", "Připojený" if s["phone"] else "Odpojený",
         "Přes USB kabel." if s["phone"] else "Zkontroluj kabel a Ladění USB."),
        (s["online"], "Internet v telefonu", "Funguje" if s["online"] else "Nefunguje",
         "Telefon je online." if s["online"] else "Wi-Fi nebo data v telefonu. Bot počká."),
    ]
    body = '<h1>Stav bota</h1><p class="lead">Technika na jednom místě. Když je všechno zelené, nemusíš sem chodit.</p><div class="grid3">'
    for ok, name, val, how in tiles:
        body += f'<div class="card tile"><b><span class="dot {"" if ok else "bad"}"></span>{name}: {val}</b><div>{how}</div></div>'
    body += "</div>"
    if in_quiet_hours():
        q = config().get("quietHours", [23, 7])
        body += f'<p class="kv">Teď je noční klid ({q[0]}–{q[1]} h): bot reaguje jen na nové zprávy, nepřidává příspěvky.</p>'

    items_ = problems()
    groups = {}
    for i, p in enumerate(items_):
        if p.get("status") != "novy" and not show_all:
            continue
        g = groups.setdefault(p.get("kind", "?"), {"kind": p.get("kind", "?"), "count": 0, "last": None, "n": 0, "resolved": True})
        g["count"] += p.get("count", 1)
        g["n"] += 1
        g["resolved"] &= p.get("status") != "novy"
        if not g["last"] or p.get("lastTs", 0) >= g["last"].get("lastTs", 0):
            g["last"] = p
    total_new = sum(1 for p in items_ if p.get("status") == "novy")
    body += (f'<h2>Kde se bot zasekl <span class="cnt">{total_new} nevyřešených</span></h2>'
             '<p class="lead">Bot si většinou poradí sám (restartuje aplikaci). Tohle je pro ladění – projdi to, když se něco opakuje.</p>')
    for g in sorted(groups.values(), key=lambda g: -(g["last"].get("lastTs", 0))):
        p = g["last"]
        shot = HERE / "problemy" / p.get("folder", "") / "screenshot.png"
        img = (f'<a href="{url_for("shot", folder=p["folder"])}" target="_blank"><img class="shot" loading="lazy" alt="Snímek obrazovky" '
               f'src="{url_for("shot", folder=p["folder"])}"></a>' if p.get("folder") and shot.exists() else "")
        recent = time.time() - p.get("lastTs", 0) < 3600
        body += (f'<article class="card u-{"warn" if recent and not g["resolved"] else "ok"}"><div style="display:flex;gap:14px;align-items:flex-start">'
                 f'<div style="flex:1;min-width:0"><b>{escape(g["kind"])}</b><div class="kv">{g["count"]}× · naposledy {escape(ago(p.get("lastTs")) or p.get("time", ""))}'
                 f'{" · vyřešeno" if g["resolved"] else ""}</div>'
                 f'<div class="why">{escape(short(human_error(p.get("detail", "")), 200))}</div>')
        if not g["resolved"]:
            body += (f'<form method="post" action="{url_for("problem")}" class="actions">{hidden_next()}'
                     f'<input type="hidden" name="kind" value="{escape(g["kind"])}">'
                     f'<button class="sec" data-busy="…">Vyřešeno – skrýt</button></form>')
        body += f'</div>{img}</div></article>'
    if not groups:
        body += '<div class="card empty">Žádné nevyřešené problémy. Bot jede hladce.</div>'
    body += f'<p><a href="{url_for("stav_bota", vse=1) if not show_all else url_for("stav_bota")}">{"Ukázat i vyřešené" if not show_all else "Jen nevyřešené"}</a></p>'

    rows = list(reversed(log_rows()[-60:]))
    body += '<details class="fold"><summary>Co bot dělal (posledních 60 událostí)</summary><div class="card">'
    for r in rows:
        label = next((v for k, v in EVENT if r["action"].startswith(k)), r["action"])
        det = "" if TECH.search(r["detail"]) else short(r["detail"], 140)
        body += (f'<div class="ev"><span class="t">{escape(when(r["ts"]))}</span><span><b>{escape(label)}</b> · {escape(r["who"])}'
                 f'{("<div class=kv>" + escape(det) + "</div>") if det else ""}</span></div>')
    body += "</div></details>"
    return page("Stav bota", body, "stav_bota", refresh=True)


@app.route("/shot/<folder>")
def shot(folder):
    base = (HERE / "problemy").resolve()
    f = (base / folder / "screenshot.png").resolve()
    if base not in f.parents or not f.exists():
        abort(404)
    return send_file(f)


# ====================================================================== akce (POST)

def back(flash, anchor=""):
    nxt = request.form.get("next") or "/"
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    nxt = nxt.split("?")[0]
    return redirect(f"{nxt}?ok={flash}{anchor}")


def _person():
    person = (request.form.get("person") or "").strip()
    if not person:
        abort(400)
    return person


@app.route("/odpovedet", methods=["POST"])
def odpovedet():
    person = _person()
    text = (request.form.get("text") or "").replace("\r\n", "\n").strip()
    if not text:
        return back("prazdne")
    data.outbox_add("zprava", person, text)
    return back("odeslano")


@app.route("/botovi", methods=["POST"])
def botovi():
    person = _person()
    text = (request.form.get("text") or "").replace("\r\n", "\n").strip()
    if text:  # napsal(a) jsi něco a pak klikl(a) na „Vrátit botovi“ → nejdřív to pošli
        data.outbox_add("zprava", person, text)
    data.outbox_add("botovi", person)
    return back("odeslano_botovi" if text else "botovi")


@app.route("/prevzit", methods=["POST"])
def prevzit():
    data.outbox_add("prevzit", _person())
    return back("prevzato")


@app.route("/skryt", methods=["POST"])
def skryt():
    key = (request.form.get("key") or "").strip()
    if key:
        d = stav()
        d["skryto"][key] = time.time()
        save_stav(d)
    return back("skryto")


@app.route("/objednavka", methods=["POST"])
def objednavka():
    key = (request.form.get("key") or "").strip()
    krok = request.form.get("krok")
    if key and krok in dict(ORDER_STEPS):
        d = stav()
        d["objednavky"][key] = {"krok": krok, "ts": time.time()}
        save_stav(d)
    return back("obj")


@app.route("/problem", methods=["POST"])
def problem():
    kind = request.form.get("kind") or ""
    f = HERE / "problemy" / "seznam.json"
    if kind and f.exists():
        items_ = json.loads(f.read_text(encoding="utf-8"))  # čerstvě, ať nepřepíšeme, co mezitím zapsal bot
        for p in items_:
            if p.get("kind") == kind and p.get("status") == "novy":
                p["status"] = "vyreseno"
        f.write_text(json.dumps(items_, ensure_ascii=False, indent=1), encoding="utf-8")
        _cache.pop(f, None)
    return back("problem")


if __name__ == "__main__":
    print("Dashboard: http://localhost:5005")
    app.run(host="127.0.0.1", port=5005, debug=False)
