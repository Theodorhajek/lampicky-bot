"""Dashboard bota na lampičky (varianta B: zprávy jako v Messengeru) – http://localhost:5005

  .venv/bin/python dashboard_b.py

Vlevo seznam konverzací seřazený podle toho, kdo má být na tahu, uprostřed konverzace s polem pro odpověď,
vpravo co o zákazníkovi víme. Odpověď napsaná tady jde do fronty (data.outbox_add) a bot ji v telefonu
napíše po znacích – telefon ovládá jen bot. Stránka běží jen na tomhle Macu, data obsahují osobní údaje.
"""

import csv
import json
import re
import subprocess
import time
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from urllib.parse import quote

from flask import Flask, abort, jsonify, redirect, request, send_file, url_for

import data

HERE = Path(__file__).resolve().parent
ADB = str(Path.home() / ".local" / "android" / "platform-tools" / "adb")
app = Flask(__name__)
_status_cache = {"ts": 0, "val": None}
_log_cache = {"mtime": None, "rows": []}

STUCK_MIN = 20          # zákazník napsal a bot do 20 min neodpověděl → asi se zasekl
QUIET_DAYS = 3          # po 3 dnech ticha od zákazníka je konverzace „hotová“
h = lambda s: escape(str(s if s is not None else ""))
norm = lambda t: re.sub(r"\W+", "", (t or "").lower())


# ======================================================================
#  DATA
# ======================================================================

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def config():
    return load_json(HERE / "config.json", {})


def state():
    s = load_json(HERE / "state_telefon.json", {})
    s.setdefault("threads", {})
    return s


def log_rows():
    """log.csv jako seznam slovníků {ts, who, action, detail}; čte se znovu jen když se soubor změní."""
    f = HERE / "log.csv"
    if not f.exists():
        return []
    mtime = f.stat().st_mtime
    if _log_cache["mtime"] != mtime:
        rows = []
        for r in csv.reader(f.read_text(encoding="utf-8").splitlines(), delimiter=";"):
            if len(r) < 3:
                continue
            try:
                ts = datetime.strptime(r[0].strip(), "%d. %m. %Y %H:%M:%S").timestamp()
            except ValueError:
                continue
            rows.append({"ts": ts, "who": r[1], "action": r[2].strip().strip('"'), "detail": r[3] if len(r) > 3 else ""})
        _log_cache.update(mtime=mtime, rows=rows)
    return _log_cache["rows"]


def bot_status():
    """Stejná logika jako v původním dashboardu: proces bota, telefon přes adb, internet v telefonu."""
    if _status_cache["val"] and time.time() - _status_cache["ts"] < 20:
        return _status_cache["val"]
    running, phone, online = 0, False, False
    try:
        ps = subprocess.run(["ps", "-ax"], capture_output=True, text=True, timeout=5).stdout
        running = sum(1 for l in ps.splitlines() if "bot_telefon.py run" in l and "Python" in l)
        devs = subprocess.run([ADB, "devices"], capture_output=True, text=True, timeout=5).stdout
        phone = any(l.strip().endswith("\tdevice") for l in devs.splitlines()[1:])
        if phone:
            out = subprocess.run([ADB, "shell", "ping", "-c", "1", "-W", "2", "8.8.8.8"],
                                 capture_output=True, text=True, timeout=8).stdout
            online = " 1 received" in out
    except Exception:  # noqa: BLE001  – adb chybí nebo neodpovídá
        pass
    rows = log_rows()
    val = {"running": running, "phone": phone, "online": online, "last": rows[-1] if rows else None}
    _status_cache.update(ts=time.time(), val=val)
    return val


def quiet_now():
    start, end = config().get("quietHours", [23, 7])
    hr = datetime.now().hour
    return (hr >= start or hr < end) if start > end else (start <= hr < end)


# ---------- čas lidsky ----------

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


# ---------- konverzace ----------

STATUS = {
    "ja": ("Čeká na tebe", "Bot si neví rady nebo píšeš ty – odpověz."),
    "bot": ("Bot odpovídá", "Zákazník napsal před chvílí, bot to vyřídí sám."),
    "zakaznik": ("Čeká na zákazníka", "Odpověděli jsme, teď je na tahu zákazník."),
    "hotovo": ("Hotovo", "Nic se neděje – zablokovaní, vyřízené nebo staré konverzace."),
}
ORDER = ["ja", "bot", "zakaznik", "hotovo"]
SENT_ACTIONS = ("odesláno", "komentář – poslal jsem zprávu")
HANDOFF_ACTIONS = ("PŘEDÁNO TOBĚ", "limit odpovědí bota", "převzato tebou")


def conversations():
    """Všechny konverzace i se zprávami a stavem. Zprávy z databáze + to, co bot podle deníku poslal a ještě
    se neuložilo (bot ukládá konverzaci až při dalším otevření)."""
    st = state()["threads"]
    logs = log_rows()
    by_person_log = {}
    for r in logs:
        by_person_log.setdefault(r["who"], []).append(r)
    with data.db() as con:
        msg_rows = con.execute("SELECT id, ts, person, direction, text, images FROM messages ORDER BY id").fetchall()
        dec_rows = con.execute("SELECT ts, kind, person, note, handoff FROM decisions ORDER BY id").fetchall()
        out_rows = con.execute("SELECT id, ts, kind, person, text, status, error, done_ts FROM outbox ORDER BY id").fetchall()
        com_rows = con.execute("SELECT ts, grp, person, text, reply, dm, note FROM comments ORDER BY ts").fetchall()
    msgs = {}
    for mid, ts, person, direction, text, images in msg_rows:
        msgs.setdefault(person, []).append({"id": mid, "ts": ts or 0, "dir": direction, "text": text or "", "images": images or 0})
    notes = {}
    for ts, kind, person, note, handoff in dec_rows:
        if note:
            notes[person] = {"ts": ts, "kind": kind, "note": note}
    outbox = {}
    for oid, ts, kind, person, text, status, error, done_ts in out_rows:
        outbox.setdefault(person, []).append({"id": oid, "ts": ts, "kind": kind, "text": text or "", "status": status,
                                              "error": error or "", "done_ts": done_ts})
    comments = {}
    for ts, grp, person, text, reply, dm, note in com_rows:
        comments.setdefault(person, []).append({"ts": ts, "grp": grp, "text": text, "reply": reply, "dm": dm, "note": note})

    people = set(msgs) | set(st)
    people |= {r["who"] for r in logs if r["action"].startswith("komentář – poslal jsem zprávu")}
    now = time.time()
    status = bot_status()
    quiet = quiet_now()
    cfg = config()
    out = []
    for person in people:
        t = st.get(person, {})
        plog = by_person_log.get(person, [])
        ms = list(msgs.get(person, []))
        last_db = max((m["ts"] for m in ms), default=0)
        out_norms = {norm(m["text"]) for m in ms if m["dir"] != "in"}
        fix_batch_times(ms, plog)
        for r in plog:  # co bot poslal, ale v databázi ještě není
            if r["ts"] < last_db - 60:
                continue
            if r["action"].startswith("odeslána fotka"):
                ms.append({"id": None, "ts": r["ts"], "dir": "bot", "text": "", "images": 1, "virtual": True})
            elif r["action"].startswith(SENT_ACTIONS):
                d = "manual" if "dashboard" in r["action"] else "bot"
                for part in r["detail"].split(" / "):
                    if part.strip() and norm(part) not in out_norms:
                        ms.append({"id": None, "ts": r["ts"], "dir": d, "text": part.strip(), "images": 0, "virtual": True})
                        out_norms.add(norm(part))
        if not ms and t.get("preview"):  # konverzace jen ze stavu bota
            p = t["preview"]
            mine = p.startswith("Já:")
            ms.append({"id": None, "ts": plog[-1]["ts"] if plog else 0, "dir": "manual" if mine else "in",
                       "text": p[3:].strip() if mine else p, "images": 0, "virtual": True, "preview": True})
        if not ms:  # jen komentář, chat v Messengeru není – to je na stránce Komentáře
            continue
        last = ms[-1] if ms else {"ts": 0, "dir": "?", "text": "", "images": 0}
        last_in = next((m for m in reversed(ms) if m["dir"] == "in"), None)
        last_ts = max([last["ts"]] + [r["ts"] for r in plog])
        blocked = bool(t.get("blocked"))
        handed = bool(t.get("handedOff"))
        paused_until = (t.get("pausedUntil") or 0) / 1000
        paused = paused_until > now
        pend = [o for o in outbox.get(person, []) if o["status"] == "ceka"]
        handoff = next((r for r in reversed(plog) if r["action"].startswith(HANDOFF_ACTIONS)), None)

        # --- kdo je na tahu ---
        key, reason, since = "hotovo", "", None
        if blocked:
            reason = "Zablokovaný – bot mu už nikdy neodpoví."
        elif handed:
            key = "ja"
            if handoff and handoff["action"].startswith("PŘEDÁNO"):
                reason = handoff["detail"] or "Bot ti konverzaci předal."
            elif handoff and handoff["action"].startswith("limit"):
                reason = f"Bot už odpověděl {cfg.get('maxBotRepliesInRow', 6)}× za sebou – převezmi to."
            elif handoff and handoff["action"].startswith("převzato"):
                reason = "Převzal jsi konverzaci – bot mlčí, dokud ji nevrátíš."
            else:
                reason = "Bot ti konverzaci předal."
            since = last_in["ts"] if last["dir"] == "in" else (handoff["ts"] if handoff else last_ts)
        elif last["dir"] == "in":
            wait = now - last["ts"] if last["ts"] else 0
            said_none = any(r["action"] == "bez odpovědi" and r["ts"] >= last["ts"] - 5 for r in plog)
            said_none = said_none or bool(norm(last["text"]) and norm(last["text"]) in (t.get("handled") or ""))
            since = last["ts"] or None
            if paused:
                key, reason = "ja", f"Zákazník odpověděl. Bot mlčí, protože píšeš ty (do {clock(paused_until)})."
            elif said_none:
                key, reason = "hotovo", "Bot usoudil, že není potřeba odpovídat."
            elif not last["ts"] or wait > QUIET_DAYS * 86400:
                key, reason = "hotovo", "Zákazník psal poslední a nikdo neodpověděl – starší konverzace."
            elif status["running"] == 0:
                key, reason = "ja", "Bot neběží – nikdo mu neodpoví. Odpověz sám nebo bota spusť."
            elif quiet:
                key, reason = "bot", f"Noční klid – bot odpoví ráno po {cfg.get('quietHours', [23, 7])[1]}:00."
            elif wait < STUCK_MIN * 60:
                key, reason = "bot", "Bot odpoví během pár minut."
            else:
                key, reason = "ja", f"Bot neodpověděl už {dur(wait)} – asi se zasekl. Odpověz sám."
        else:
            if paused:
                reason = f"Píšeš ty – bot mlčí do {clock(paused_until)}."
            if last["ts"] and now - last["ts"] < QUIET_DAYS * 86400:
                key = "zakaznik"
                reason = reason or ("Bot odpověděl, čeká se na zákazníka." if last["dir"] == "bot" else "Odpověděl jsi, čeká se na zákazníka.")
            else:
                reason = reason or "Zákazník už se neozval."
        out.append({
            "person": person, "msgs": ms, "last": last, "last_in": last_in, "last_ts": last_ts, "key": key,
            "reason": reason, "since": since, "blocked": blocked, "handed": handed, "paused": paused,
            "paused_until": paused_until, "pending": pend, "outbox": outbox.get(person, []), "note": notes.get(person),
            "comments": comments.get(person, []), "log": plog, "t": t,
            "from_comment": bool(t.get("dmFromComment")) or any(r["action"].startswith("komentář") for r in plog),
        })

    def rank(c):
        k = ORDER.index(c["key"])
        if c["key"] == "ja":
            return (k, c["since"] or c["last_ts"] or 0)  # nejdéle čekající nahoře
        return (k, -(c["last_ts"] or 0))
    return sorted(out, key=rank)


def fix_batch_times(ms, plog):
    """Zprávy uložené najednou (první otevření chatu, sběr dat) mají v databázi stejný čas – skutečný čas
    starších z nich neznáme (ts=0). Když tehdy bot nic nedělal (sběr dat), čas poslední zprávy odhadneme z deníku."""
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
                ms[j]["ts"] = max((r["ts"] for r in plog if r["ts"] < bts), default=0)
        i = j + 1


def conv_sig(c):
    return f'{len(c["msgs"])}|{c["last_ts"]}|{c["key"]}|{c["handed"]}|{c["paused"]}|' + \
        ",".join(f'{o["id"]}{o["status"]}' for o in c["outbox"][-5:])


def order_facts(c):
    """Co už zákazník napsal k objednávce (telefon, e-mail, dopravce, platba, adresa, fotka)."""
    ins = [m for m in c["msgs"] if m["dir"] == "in"]
    alltext = "\n".join(m["text"] for m in ins)
    low = alltext.lower()
    phone = re.search(r"(\+?420|\+?421)?[\s]?(\d{3}\s?\d{3}\s?\d{3})\b", alltext)
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", alltext)
    carrier = "Zásilkovna" if re.search(r"z[áa]silkovn|packeta|z-box|zbox", low) else "PPL" if re.search(r"\bppl\b", low) else ""
    pay = "dobírka" if "dobírk" in low or "dobirk" in low else "převodem" if re.search(r"převod|prevod|předem|predem|zaplac", low) else ""
    addr_m = next((m["text"] for m in reversed(ins) if re.search(r"\b\d{3}\s?\d{2}\b", m["text"]) and re.search(r"[A-Za-zÁ-ž]{3,}", m["text"])
                   and not (phone and phone.group(2) in m["text"] and len(m["text"]) < 16)), "")
    photo = any(m["images"] for m in ins)
    pieces = re.search(r"\b([2-9])\s?(ks|kus|kusy|lampičk|lampick)", low)
    return [
        ("Motiv / fotka", "poslal fotku" if photo else "", photo),
        ("Telefon", phone.group(0).strip() if phone else "", bool(phone)),
        ("E-mail", email.group(0) if email else "", bool(email)),
        ("Dopravce", carrier, bool(carrier)),
        ("Platba", pay, bool(pay)),
        ("Adresa", addr_m[:120], bool(addr_m)),
    ], (pieces.group(1) + " ks") if pieces else ""


LOG_PLAIN = [
    ("odesláno z dashboardu", "Poslal jsi zprávu z dashboardu"),
    ("odesláno", "Bot poslal zprávu"),
    ("odeslána fotka", "Bot poslal fotku"),
    ("PŘEDÁNO TOBĚ", "Bot předal konverzaci tobě"),
    ("nová zpráva, čeká na tebe", "Nová zpráva – čeká na tebe"),
    ("nová zpráva, bot pozastaven", "Nová zpráva – bot mlčí, píšeš ty"),
    ("odpověděl jsi ručně", "Odpověděl jsi v telefonu – bot 12 h mlčí"),
    ("bez odpovědi", "Bot usoudil, že není třeba odpovídat"),
    ("komentář – odpověděl jsem", "Bot odpověděl na komentář"),
    ("komentář – poslal jsem zprávu", "Bot napsal do Messengeru (po komentáři)"),
    ("vráceno botovi", "Vráceno botovi"),
    ("převzato tebou", "Převzal jsi konverzaci"),
    ("limit odpovědí bota", "Bot narazil na limit odpovědí – předáno tobě"),
    ("ZABLOKOVÁN", "Zablokován"),
    ("KOMENTÁŘ – CHYBA", "Na komentář se nepovedlo odpovědět"),
    ("DASHBOARD – CHYBA", "Zprávu z dashboardu se nepovedlo poslat"),
    ("NÁVRH", "Bot jen navrhl odpověď (nic neposlal)"),
]


def plain_action(a):
    return next((v for k, v in LOG_PLAIN if a.startswith(k)), a)


def problems():
    return load_json(HERE / "problemy" / "seznam.json", [])


def queue():
    return load_json(HERE / "fronta.json", [])


def comment_todo():
    """Komentáře, které bot nezvládl a v deníku je „vyřiď ručně“ (a pak už se to nepovedlo)."""
    rows = log_rows()
    out, seen = [], set()
    for i, r in enumerate(rows):
        if not (r["action"].startswith("KOMENTÁŘ – CHYBA") or r["action"].startswith("KOMENTÁŘ – NÁVRH")):
            continue
        if time.time() - r["ts"] > 4 * 86400:
            continue
        if any(x["who"] == r["who"] and x["action"].startswith("komentář – odpověděl") for x in rows[i + 1:]):
            continue
        m = re.match(r"„(.*?)“", r["detail"])
        key = (r["who"], m.group(1) if m else r["detail"])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out[::-1]


POST_STATUS = {  # stav: (text, třída, potřebuje tebe)
    "zverejneno": ("Zveřejněno", "ok", False),
    "ceka": ("Čeká ve frontě", "info", False),
    "ceka-na-clenstvi": ("Čeká na přijetí do skupiny", "info", False),
    "ceka-na-schvaleni-prispevku": ("Čeká na schválení adminem", "info", False),
    "otazky-vyres-rucne": ("Vyplň otázky ke členství ručně", "ja", True),
    "chyba": ("Nepovedlo se – zkus ručně", "ja", True),
    "nelze-postovat": ("Do skupiny nejde psát", "ja", True),
    "vzdano-nepřijali": ("Nepřijali nás – vzdáno", "mute", False),
    "preskoceno-bazar": ("Přeskočeno (bazar)", "mute", False),
}


def nav_counts(convs=None):
    convs = convs if convs is not None else conversations()
    return {
        "zpravy": sum(1 for c in convs if c["key"] == "ja"),
        "komentare": len(comment_todo()),
        "prispevky": sum(1 for j in queue() if POST_STATUS.get(j.get("status"), ("", "", False))[2]),
        "problemy": sum(1 for p in problems() if p.get("status") == "novy"),
    }


def quick_texts():
    info = (HERE / "info.md").read_text(encoding="utf-8") if (HERE / "info.md").exists() else ""
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
            ("OK", "OK")]
    return out


# ======================================================================
#  VZHLED
# ======================================================================

CSS = r"""
:root{
 --bg:#f4f3ef;--panel:#ffffff;--panel2:#faf9f6;--ink:#1c1b19;--mute:#6d6b66;--faint:#9a978f;--line:#e5e2db;
 --acc:#2f6f55;--acc-ink:#fff;--accsoft:#e6f1ec;
 --ja:#c2410c;--jasoft:#fdeee4;--bot:#3559b7;--botsoft:#e9eefb;--zak:#8a6d1a;--zaksoft:#f8f1dc;--ok:#2f7a4f;--oksoft:#e4f3ea;
 --bad:#b42318;--badsoft:#fde8e6;
 --b-in:#ecebe6;--b-bot:#dcefe5;--b-man:#d9e6fb;--shadow:0 1px 2px rgba(0,0,0,.05),0 4px 16px rgba(0,0,0,.04);
 color-scheme:light;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
 --bg:#121211;--panel:#1b1b1a;--panel2:#202020;--ink:#ecebe6;--mute:#a3a09a;--faint:#77746e;--line:#2e2d2b;
 --acc:#5cc495;--acc-ink:#0d1f17;--accsoft:#1b2e25;
 --ja:#ff8a4c;--jasoft:#3a2216;--bot:#8aa8ff;--botsoft:#1c2440;--zak:#e2c26a;--zaksoft:#332b14;--ok:#6fd49a;--oksoft:#16301f;
 --bad:#ff7b72;--badsoft:#3b1a17;
 --b-in:#2a2a28;--b-bot:#1d3529;--b-man:#1e2b45;--shadow:none;color-scheme:dark;}}
:root[data-theme="dark"]{
 --bg:#121211;--panel:#1b1b1a;--panel2:#202020;--ink:#ecebe6;--mute:#a3a09a;--faint:#77746e;--line:#2e2d2b;
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
.health{display:flex;gap:6px;align-items:center;flex:none}
.hp{display:flex;align-items:center;gap:6px;font-size:14px;padding:5px 10px;border-radius:999px;background:var(--panel2);border:1px solid var(--line);white-space:nowrap}
.hp i{width:9px;height:9px;border-radius:50%;background:var(--ok)}
.hp.bad{border-color:var(--bad);color:var(--bad);background:var(--badsoft)}.hp.bad i{background:var(--bad)}
.hp.night i{background:var(--faint)}
.iconbtn{width:38px;height:38px;border-radius:10px;border:1px solid var(--line);background:var(--panel2);cursor:pointer;display:inline-flex;align-items:center;justify-content:center;flex:none}
.alert{background:var(--badsoft);color:var(--ink);border-bottom:1px solid var(--bad);padding:10px 16px;display:flex;gap:12px;align-items:flex-start;flex:none;font-size:16px}
.alert b{color:var(--bad)}
.alert code{background:var(--panel);padding:1px 6px;border-radius:6px;font-size:14px;border:1px solid var(--line);user-select:all}

/* schránka: seznam | konverzace | info */
.inbox{flex:1;min-height:0;display:grid;grid-template-columns:minmax(300px,380px) minmax(0,1fr) 320px}
.col{min-height:0;min-width:0;display:flex;flex-direction:column;background:var(--panel)}
.list{border-right:1px solid var(--line)}
.side{border-left:1px solid var(--line);background:var(--panel2);overflow-y:auto}
.lhead{padding:12px 12px 8px;border-bottom:1px solid var(--line);display:flex;flex-direction:column;gap:10px}
.search{width:100%;padding:9px 12px;border-radius:10px;border:1px solid var(--line);background:var(--panel2)}
.filters{display:flex;gap:6px;flex-wrap:wrap}
.filters a{font-size:14px;padding:5px 10px;border-radius:999px;border:1px solid var(--line);text-decoration:none;color:var(--mute);display:flex;gap:6px;align-items:center;font-weight:550}
.filters a b{font-weight:700;color:var(--ink)}
.filters a.on{background:var(--ink);border-color:var(--ink);color:var(--panel)}.filters a.on b{color:var(--panel)}
.filters a.f-ja b{color:var(--ja)}.filters a.f-ja.on{background:var(--ja);border-color:var(--ja)}.filters a.f-ja.on b{color:#fff}
.litems{overflow-y:auto;flex:1}
.sec{position:sticky;top:0;z-index:1;background:var(--panel);padding:14px 14px 6px;font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--mute);display:flex;justify-content:space-between;border-bottom:1px solid var(--line)}
.sec.s-ja{color:var(--ja)}
.sec{flex-wrap:wrap}.sec small{flex-basis:100%;margin-top:2px;font-weight:500;text-transform:none;letter-spacing:0;font-size:13px;color:var(--faint)}
.ci{display:flex;gap:12px;padding:12px 14px;text-decoration:none;border-bottom:1px solid var(--line);position:relative}
.ci:hover{background:var(--panel2)}
.ci.sel{background:var(--accsoft)}
.ci.sel::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--acc)}
.av{width:42px;height:42px;border-radius:50%;flex:none;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:15px;background:var(--line);color:var(--mute)}
.st-ja .av{background:var(--jasoft);color:var(--ja)}.st-bot .av{background:var(--botsoft);color:var(--bot)}
.st-zakaznik .av{background:var(--zaksoft);color:var(--zak)}.st-hotovo .av{opacity:.7}
.cb{min-width:0;flex:1;display:flex;flex-direction:column;gap:2px}
.r1{display:flex;gap:8px;align-items:baseline}
.nm{font-weight:650;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tm{font-size:13px;color:var(--faint);white-space:nowrap}
.r2{font-size:15px;color:var(--mute);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.r2.fromthem{color:var(--ink);font-weight:500}
.who{color:var(--faint)}
.r3{font-size:13.5px;color:var(--mute);display:flex;gap:6px;align-items:center;min-width:0}
.r3 span.t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wait{flex:none;font-weight:700;font-size:12.5px;padding:1px 8px;border-radius:999px;background:var(--jasoft);color:var(--ja)}
.wait.hot{background:var(--ja);color:#fff}
.wait.b{background:var(--botsoft);color:var(--bot)}
.tag{flex:none;font-size:12.5px;padding:1px 8px;border-radius:999px;background:var(--line);color:var(--mute);font-weight:600}
.empty{padding:40px 24px;text-align:center;color:var(--mute)}
.empty b{display:block;color:var(--ink);font-size:19px;margin-bottom:6px}

/* konverzace */
.thread{min-width:0}
.thead{padding:10px 18px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:12px;flex:none;min-height:62px}
.thead h1{font-size:20px;margin:0;line-height:1.2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.thead .sub{font-size:14px;color:var(--mute)}
.back{display:none;text-decoration:none;font-size:16px;padding:6px 10px 6px 4px;color:var(--acc);font-weight:600}
.grow{flex:1;min-width:0}
.btn{padding:9px 16px;border-radius:10px;border:1px solid var(--line);background:var(--panel);cursor:pointer;font-weight:600;font-size:15px;white-space:nowrap;text-decoration:none;display:inline-flex;gap:6px;align-items:center}
.btn:hover{border-color:var(--mute)}
.btn.pri{background:var(--acc);border-color:var(--acc);color:var(--acc-ink)}
.btn.pri:hover{filter:brightness(1.07)}
.btn:disabled{opacity:.55;cursor:default}
.btn.sm{padding:6px 12px;font-size:14px}
.banner{margin:12px 18px 0;padding:12px 14px;border-radius:12px;display:flex;gap:12px;align-items:center;flex:none;font-size:16px}
.banner .bt{flex:1;min-width:0}.banner .bt b{display:block}
.banner .bt span{color:var(--mute);font-size:15px}
.banner.k-ja{background:var(--jasoft)}.banner.k-ja b{color:var(--ja)}
.banner.k-bot{background:var(--botsoft)}.banner.k-bot b{color:var(--bot)}
.banner.k-zakaznik{background:var(--zaksoft)}.banner.k-zakaznik b{color:var(--zak)}
.banner.k-hotovo{background:var(--panel2);border:1px solid var(--line)}
.msgs{flex:1;overflow-y:auto;padding:14px 18px 20px;display:flex;flex-direction:column;gap:3px}
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
.cbox textarea{flex:1;min-height:52px;max-height:40vh;resize:none;padding:12px 14px;border-radius:14px;border:1px solid var(--line);background:var(--panel2);line-height:1.45}
.cbox textarea:focus{border-color:var(--acc);outline:none;box-shadow:0 0 0 3px color-mix(in srgb,var(--acc) 22%,transparent)}
.send{height:52px;padding:0 20px;font-size:16px}
.chint{font-size:13.5px;color:var(--faint);margin-top:6px;display:flex;gap:10px;justify-content:space-between;flex-wrap:wrap}
.chint .st{font-weight:600}
.chint .st.sending{color:var(--bot)}.chint .st.done{color:var(--ok)}.chint .st.err{color:var(--bad)}.chint .st.warn{color:var(--ja)}
kbd{font:12px ui-monospace,Menlo,monospace;border:1px solid var(--line);border-bottom-width:2px;border-radius:5px;padding:0 5px;background:var(--panel2)}

/* panel se zákazníkem */
.sbox{padding:16px 18px;border-bottom:1px solid var(--line)}
.sbox h3{margin:0 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--mute)}
.sbox p{margin:0;font-size:15.5px}
.facts{list-style:none;margin:0;padding:0;font-size:15px}
.facts li{display:flex;gap:8px;padding:4px 0;align-items:flex-start}
.facts .ck{flex:none;width:20px;height:20px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;margin-top:2px;background:var(--line);color:var(--faint)}
.facts .y .ck{background:var(--oksoft);color:var(--ok)}
.facts .lbl{color:var(--mute);min-width:96px}
.facts .val{flex:1;min-width:0;overflow-wrap:anywhere;user-select:all}
.facts .n .val{color:var(--faint)}
.cmt{font-size:15px;padding:6px 0}
.cmt .g{font-size:13px;color:var(--faint)}
.cmt .rp{color:var(--mute);font-size:14px;margin-top:2px}
.hist{list-style:none;margin:0;padding:0;font-size:14px}
.hist li{display:flex;gap:8px;padding:3px 0}
.hist .w{color:var(--faint);min-width:78px;flex:none}
.sideclose{display:none}
.nothread{flex:1;display:flex;align-items:center;justify-content:center;flex-direction:column;color:var(--mute);text-align:center;padding:30px}
.nothread>b{font-size:22px;color:var(--ink);margin-bottom:6px}
.legend{margin-top:22px;text-align:left;font-size:15px;display:grid;gap:8px;max-width:440px}
.legend div{display:flex;gap:10px;align-items:flex-start}
.dot{flex:none;width:12px;height:12px;border-radius:50%;margin-top:6px}

/* ostatní stránky */
.page{flex:1;overflow-y:auto}
.wrap{max-width:980px;margin:0 auto;padding:22px 16px 60px}
.wrap h1{font-size:26px;margin:4px 0 4px}
.lead{color:var(--mute);margin:0 0 18px;font-size:16px}
.wrap h2{font-size:15px;text-transform:uppercase;letter-spacing:.05em;color:var(--mute);margin:28px 0 10px;display:flex;gap:8px;align-items:center}
.wrap h2.ja{color:var(--ja)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin-bottom:10px;box-shadow:var(--shadow)}
.card.ja{border-color:color-mix(in srgb,var(--ja) 45%,var(--line))}
.crow{display:flex;gap:14px;align-items:flex-start}
.chip{display:inline-flex;align-items:center;font-size:13px;font-weight:650;padding:2px 10px;border-radius:999px;background:var(--line);color:var(--mute);white-space:nowrap}
.chip.ok{background:var(--oksoft);color:var(--ok)}.chip.ja{background:var(--jasoft);color:var(--ja)}
.chip.info{background:var(--botsoft);color:var(--bot)}.chip.mute{opacity:.8}
.small{font-size:14px;color:var(--mute)}
.q{font-size:16.5px}
.arrow{color:var(--mute);font-size:15px;margin-top:4px}
details summary{cursor:pointer;color:var(--mute);font-size:14px;margin-top:6px}
details pre{white-space:pre-wrap;font-size:13px;background:var(--panel2);padding:8px;border-radius:8px;overflow-x:auto}
.thumb{width:92px;height:92px;object-fit:cover;border-radius:10px;border:1px solid var(--line);flex:none;background:var(--panel2)}
.shot{width:120px;max-height:220px;object-fit:cover;object-position:top;border-radius:10px;border:1px solid var(--line);flex:none}
.kpis{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:6px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px 16px;min-width:150px;flex:1}
.kpi b{font-size:26px;display:block;line-height:1.1}
.kpi span{color:var(--mute);font-size:14px}
.lekce{width:100%;min-height:300px;padding:14px;border-radius:12px;border:1px solid var(--line);background:var(--panel2);font:15.5px/1.55 ui-monospace,Menlo,monospace;resize:vertical}
.pair{display:grid;gap:4px;padding:10px 0;border-bottom:1px solid var(--line)}
.pair:last-child{border:0}
.pair .c{background:var(--b-in);padding:6px 11px;border-radius:14px;justify-self:start;max-width:85%}
.pair .s{background:var(--b-man);padding:6px 11px;border-radius:14px;justify-self:end;max-width:85%}
.saved{background:var(--oksoft);color:var(--ok);padding:10px 14px;border-radius:10px;font-weight:600;margin-bottom:10px}
.plink{text-decoration:none;font-weight:650}
.plink:hover{text-decoration:underline}

/* telefon a menší displeje */
@media (max-width:1240px){
 .inbox{grid-template-columns:minmax(290px,350px) minmax(0,1fr)}
 .side{position:fixed;right:0;top:0;bottom:0;width:min(380px,92vw);z-index:20;transform:translateX(105%);transition:transform .2s;box-shadow:-10px 0 30px rgba(0,0,0,.18)}
 body.sideopen .side{transform:none}
 .sideclose{display:flex;justify-content:flex-end;padding:10px 12px 0}
}
@media (min-width:1241px){.infobtn{display:none}.thread.wide{grid-column:span 2}}
@media (max-width:820px){
 body{font-size:17px}
 .top{gap:8px;padding:0 10px;height:54px}
 .brand span{display:none}
 .hp span{display:none}.hp{padding:6px}
 .inbox{grid-template-columns:minmax(0,1fr)}
 body.hassel .list{display:none}
 body:not(.hassel) .thread{display:none}
 .back{display:inline-block}
 .thead{padding:8px 12px}
 .banner{margin:10px 12px 0;flex-wrap:wrap}.banner .bt br,.banner .bt span{display:none}
 .msgs{padding:10px 12px}
 .m{max-width:88%}
 .composer{padding:8px 10px 12px}
 .send{padding:0 14px}
 .chint .keys{display:none}
 .crow{flex-wrap:wrap}
}
"""

ICON_INFO = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/></svg>'
ICON_THEME = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="8"/><path d="M12 4a8 8 0 0 0 0 16z" fill="currentColor"/></svg>'
ICON_PHOTO = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="11" r="2"/><path d="m21 17-5-5-8 7"/></svg>'

THEME_JS = r"""(function(){try{var t=localStorage.getItem('theme');if(t)document.documentElement.setAttribute('data-theme',t);}catch(e){}})();"""

COMMON_JS = r"""
function cycleTheme(){var r=document.documentElement,c=r.getAttribute('data-theme'),
 n=c==='light'?'dark':c==='dark'?null:'light';if(n)r.setAttribute('data-theme',n);else r.removeAttribute('data-theme');
 try{n?localStorage.setItem('theme',n):localStorage.removeItem('theme')}catch(e){}
 var b=document.getElementById('themebtn');if(b)b.title='Vzhled: '+(n==='light'?'světlý':n==='dark'?'tmavý':'podle systému');}
async function refreshTop(){try{const r=await fetch('/api/stav');const j=await r.json();
 document.getElementById('health').innerHTML=j.health;document.getElementById('alertbox').innerHTML=j.alert;
 for(const k in j.counts){const el=document.querySelector('[data-cnt="'+k+'"]');if(el){el.textContent=j.counts[k]||'';el.hidden=!j.counts[k];}}}catch(e){}}
"""

INBOX_JS = r"""
const S=JSON.parse(document.getElementById('S').textContent);
const $=s=>document.querySelector(s);
const deacc=s=>(s||'').normalize('NFD').replace(/[̀-ͯ]/g,'').toLowerCase();
let pollTimer=null,sendPoll=null;

function nearBottom(el){return el.scrollHeight-el.scrollTop-el.clientHeight<120}
function toBottom(){const m=$('#msgs');if(m)m.scrollTop=m.scrollHeight}
function applySearch(){const q=deacc($('#search').value.trim());
 document.querySelectorAll('#litems .ci').forEach(a=>{a.hidden=q&&!deacc(a.dataset.name).includes(q)});
 document.querySelectorAll('#litems .sec').forEach(s=>{let n=s.nextElementSibling,any=false;
  while(n&&!n.classList.contains('sec')){if(!n.hidden)any=true;n=n.nextElementSibling}s.hidden=!any});}

async function refreshList(){
 try{const r=await fetch('/api/prehled?f='+encodeURIComponent(S.f)+'&c='+encodeURIComponent(S.person||''));const j=await r.json();
  const li=$('#litems'),st=li.scrollTop;li.innerHTML=j.list;li.scrollTop=st;applySearch();
  $('#filters').innerHTML=j.filters;$('#health').innerHTML=j.health;$('#alertbox').innerHTML=j.alert;
  for(const k in j.counts){const el=document.querySelector('[data-cnt="'+k+'"]');if(el){el.textContent=j.counts[k]||'';el.hidden=!j.counts[k];}}
  document.title=(j.counts.zpravy?'('+j.counts.zpravy+') ':'')+'Zprávy – Lampičky';
  if(S.person&&j.sig!==S.sig)await loadThread();
 }catch(e){}
 schedule();
}
function schedule(){clearTimeout(pollTimer);pollTimer=setTimeout(refreshList,S.pending?3000:10000)}

async function loadThread(){
 if(!S.person)return;
 const r=await fetch('/api/vlakno/'+encodeURIComponent(S.person));if(!r.ok)return;const j=await r.json();
 const m=$('#msgs'),stick=nearBottom(m);
 $('#banner').outerHTML=j.banner;m.innerHTML=j.msgs;$('#sidein').innerHTML=j.side;
 S.sig=j.sig;S.pending=j.pending;if(stick)toBottom();bindBanner();
}

function setState(cls,html){const e=$('#sendstate');if(!e)return;e.className='st '+cls;e.innerHTML=html}

async function post(url,body){const fd=new FormData();for(const k in body)fd.append(k,body[k]);
 const r=await fetch(url,{method:'POST',body:fd,headers:{'X-Requested-With':'fetch'}});return r.json()}

async function send(textOverride){
 const ta=$('#tx');const text=(textOverride!==undefined?textOverride:ta.value).trim();if(!text){ta&&ta.focus();return}
 const b=$('#sendbtn');b.disabled=true;
 try{const j=await post('/api/poslat',{person:S.person,text:text});
  if(!j.ok){setState('err',j.error||'Nepovedlo se');return}
  if(textOverride===undefined){ta.value='';autosize();saveDraft()}
  setState('sending','<span class="spin"></span> Odesílá se přes telefon… bot to napíše zhruba do 20 s'+(S.botDown?' (bot ale teď neběží – odejde, až ho spustíš)':''));
  S.pending=true;await loadThread();toBottom();watch(j.id);
 }catch(e){setState('err','Nepovedlo se uložit – běží dashboard?')}
 finally{b.disabled=false}
}
function watch(id){clearInterval(sendPoll);sendPoll=setInterval(async()=>{
 try{const r=await fetch('/api/odeslani/'+id);const j=await r.json();
  if(j.status==='hotovo'){clearInterval(sendPoll);setState('done','Odesláno ✓ Bot teď v tomhle chatu '+S.pauseH+' h mlčí.');loadThread()}
  else if(j.status==='chyba'){clearInterval(sendPoll);setState('err','Nepodařilo se odeslat: '+(j.error||'neznámá chyba'));loadThread()}
 }catch(e){}},2000)}

async function action(kind){
 const j=await post('/api/akce',{person:S.person,kind:kind});
 if(j.ok){setState('sending','<span class="spin"></span> '+(kind==='botovi'?'Vracím konverzaci botovi…':'Říkám botovi, ať tu mlčí…')+' (provede se do ~20 s)');
  S.pending=true;await loadThread();watchAction(j.id,kind)}}
function watchAction(id,kind){clearInterval(sendPoll);sendPoll=setInterval(async()=>{
 const r=await fetch('/api/odeslani/'+id);const j=await r.json();
 if(j.status!=='ceka'){clearInterval(sendPoll);setState(j.status==='hotovo'?'done':'err',j.status==='hotovo'?(kind==='botovi'?'Hotovo ✓ Bot tu zase odpovídá.':'Hotovo ✓ Bot tu mlčí, píšeš ty.'):'Nepovedlo se: '+j.error);refreshList()}},2000)}
function bindBanner(){document.querySelectorAll('[data-act]').forEach(b=>b.onclick=()=>action(b.dataset.act));
 document.querySelectorAll('[data-retry]').forEach(b=>b.onclick=()=>send(b.dataset.retry))}

function autosize(){const t=$('#tx');if(!t)return;t.style.height='auto';t.style.height=Math.min(t.scrollHeight,window.innerHeight*0.4)+'px'}
function saveDraft(){try{const t=$('#tx');if(t.value)localStorage.setItem('draft:'+S.person,t.value);else localStorage.removeItem('draft:'+S.person)}catch(e){}}
function insertText(s){const t=$('#tx');const a=t.selectionStart,b=t.selectionEnd,v=t.value;
 const pre=v.slice(0,a),sep=pre&&!/\s$/.test(pre)?' ':'';t.value=pre+sep+s+v.slice(b);t.focus();
 const p=(pre+sep+s).length;t.setSelectionRange(p,p);autosize();saveDraft()}

document.addEventListener('DOMContentLoaded',()=>{
 toBottom();bindBanner();applySearch();
 $('#search').addEventListener('input',applySearch);
 const ta=$('#tx');
 if(ta){try{const d=localStorage.getItem('draft:'+S.person);if(d)ta.value=d}catch(e){}autosize();
  ta.addEventListener('input',()=>{autosize();saveDraft()});
  ta.addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){e.preventDefault();send()}});
  $('#composer').addEventListener('submit',e=>{e.preventDefault();send()});
  document.querySelectorAll('[data-quick]').forEach(b=>b.onclick=()=>insertText(b.dataset.quick));
  if(window.innerWidth>820)ta.focus();}
 document.addEventListener('keydown',e=>{const typing=/INPUT|TEXTAREA/.test(document.activeElement.tagName);
  if(e.key==='/'&&!typing){e.preventDefault();$('#search').focus()}
  if(e.key==='Escape'){if(document.body.classList.contains('sideopen'))document.body.classList.remove('sideopen');else if(typing)document.activeElement.blur()}
  if(!typing&&(e.key==='j'||e.key==='k')){const items=[...document.querySelectorAll('#litems .ci:not([hidden])')];
   const i=items.findIndex(a=>a.classList.contains('sel'));const n=items[e.key==='j'?i+1:Math.max(0,i-1)];if(n)location.href=n.href}});
 schedule();
});
"""


def initials(name):
    parts = [p for p in re.split(r"\s+", name or "") if p and p[0].isalpha()]
    return ("".join(p[0] for p in parts[:2]) or "?").upper()


def health_html(s=None):
    s = s or bot_status()
    def pill(ok, text, cls=""):
        return f'<span class="hp {"" if ok else "bad"} {cls}" title="{h(text)}"><i></i><span>{h(text)}</span></span>'
    bot_ok = s["running"] == 1
    out = pill(bot_ok, "Bot běží" if bot_ok else ("Bot neběží" if not s["running"] else f"Bot běží {s['running']}×"))
    out += pill(s["phone"], "Telefon" if s["phone"] else "Telefon odpojený")
    out += pill(s["online"], "Internet" if s["online"] else "Bez internetu")
    if quiet_now() and bot_ok:
        out += f'<span class="hp night" title="Noční klid – bot v noci neodpovídá"><i></i><span>Noční klid</span></span>'
    return out


def alert_html(s=None):
    s = s or bot_status()
    probs = []
    if s["running"] == 0:
        probs.append(("Bot neběží.", 'Nikdo teď neodpovídá a zprávy odsud čekají. Spusť ho v Terminálu ve složce messenger-bot: '
                      '<code>.venv/bin/python bot_telefon.py run</code>'))
    elif s["running"] > 1:
        probs.append((f"Bot běží {s['running']}× najednou.", "Nech běžet jen jedno okno Terminálu s botem, ostatní zavři (Ctrl+C)."))
    if not s["phone"]:
        probs.append(("Telefon není připojený.", "Připoj telefon kabelem k Macu a odemkni ho. Když to nepomůže, kabel odpoj a znovu zapoj."))
    elif not s["online"]:
        probs.append(("Telefon je bez internetu.", "Zkontroluj v telefonu Wi-Fi nebo mobilní data. Bot počká, až se připojí."))
    if not probs:
        return ""
    return "".join(f'<div class="alert"><div><b>{a}</b> {b}</div></div>' for a, b in probs)


def page(title, active, body, counts=None, extra_head="", body_class="", scripts=""):
    counts = counts if counts is not None else nav_counts()
    tabs = [("zpravy", "Zprávy", "/zpravy"), ("komentare", "Komentáře", "/komentare"), ("prispevky", "Příspěvky", "/prispevky"),
            ("problemy", "Problémy", "/problemy"), ("uceni", "Učení bota", "/uceni")]
    nav = "".join(
        f'<a class="{"on" if k == active else ""}" href="{u}">{t}'
        f'<span class="cnt {"" if k == "zpravy" else "soft"}" data-cnt="{k}" {"" if counts.get(k) else "hidden"}>{counts.get(k) or ""}</span></a>'
        for k, t, u in tabs)
    s = bot_status()
    return (
        '<!doctype html><html lang="cs"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        f'<title>{h(title)} – Lampičky</title><script>{THEME_JS}</script><style>{CSS}</style>{extra_head}</head>'
        f'<body class="{body_class}"><header class="top"><a class="brand" href="/zpravy"><i></i><span>Lampičky</span></a>'
        f'<nav class="tabs">{nav}</nav><div class="health" id="health">{health_html(s)}</div>'
        f'<button class="iconbtn" id="themebtn" onclick="cycleTheme()" title="Přepnout světlý/tmavý vzhled" aria-label="Vzhled">{ICON_THEME}</button></header>'
        f'<div id="alertbox">{alert_html(s)}</div>{body}<script>{COMMON_JS}</script>{scripts}</body></html>')


# ---------- schránka ----------

FILTERS = [("vse", "Vše"), ("ja", "Čeká na tebe"), ("bot", "Bot odpovídá"), ("zakaznik", "Čeká na zákazníka"), ("hotovo", "Hotovo")]


def conv_url(person, f="vse"):
    return f"/zpravy/{quote(person, safe='')}" + (f"?f={f}" if f and f != "vse" else "")


def filters_html(convs, f):
    counts = {k: sum(1 for c in convs if c["key"] == k) for k in ORDER}
    counts["vse"] = len(convs)
    return "".join(f'<a class="f-{k} {"on" if k == f else ""}" href="/zpravy{"?f=" + k if k != "vse" else ""}">{t} <b>{counts[k]}</b></a>'
                   for k, t in FILTERS)


def list_html(convs, f, sel):
    shown = [c for c in convs if f == "vse" or c["key"] == f]
    if not shown:
        msg = {"ja": ("Nic na tebe nečeká", "Bot všechno zvládá. Můžeš si dát kafe."),
               "bot": ("Bot teď nic neřeší", "Žádná čerstvá zpráva bez odpovědi.")}.get(f, ("Tady nic není", ""))
        return f'<div class="empty"><b>{msg[0]}</b>{msg[1]}</div>'
    out, cur = [], None
    for c in shown:
        if c["key"] != cur:
            cur = c["key"]
            n = sum(1 for x in shown if x["key"] == cur)
            out.append(f'<div class="sec s-{cur}">{STATUS[cur][0]} · {n}<small>{h(STATUS[cur][1]) if cur == "ja" else ""}</small></div>')
        last = c["last"]
        who = {"bot": '<span class="who">Bot: </span>', "manual": '<span class="who">Ty: </span>'}.get(last["dir"], "")
        prev = last["text"] or ("Fotka" if last.get("images") else "")
        badge = ""
        if c["key"] == "ja" and c["since"]:
            w = time.time() - c["since"]
            badge = f'<span class="wait {"hot" if w > 2 * 3600 else ""}">čeká {dur(w)}</span>'
        elif c["key"] == "bot":
            badge = '<span class="wait b">bot píše</span>'
        if c["pending"]:
            badge = '<span class="wait b">odesílá se…</span>' + badge
        if c["blocked"]:
            badge = '<span class="tag">zablokovaný</span>'
        elif c["paused"] and c["key"] != "ja":
            badge += '<span class="tag">píšeš ty</span>'
        sub = h(c["reason"]) if c["key"] in ("ja", "hotovo") else ""
        out.append(
            f'<a class="ci st-{c["key"]} {"sel" if c["person"] == sel else ""}" href="{conv_url(c["person"], f)}" data-name="{h(c["person"])}">'
            f'<span class="av">{h(initials(c["person"]))}</span><span class="cb">'
            f'<span class="r1"><span class="nm">{h(c["person"])}</span><span class="tm">{h(ago(c["last_ts"]))}</span></span>'
            f'<span class="r2 {"fromthem" if last["dir"] == "in" else ""}">{who}{h(prev[:140])}</span>'
            + (f'<span class="r3">{badge}<span class="t">{sub}</span></span>' if badge or sub else "")
            + '</span></a>')
    return "".join(out)


def banner_html(c):
    k = c["key"]
    title = {"ja": "Čeká na tebe", "bot": "Bot odpovídá", "zakaznik": "Čeká se na zákazníka", "hotovo": "Hotovo"}[k]
    if k == "ja" and c["since"]:
        title += f" – {dur(time.time() - c['since'])}"
    pend_act = [o for o in c["pending"] if o["kind"] in ("botovi", "prevzit")]
    if c["blocked"]:
        btn = ""
    elif pend_act:
        btn = f'<span class="small"><span class="spin"></span> {"Vracím botovi…" if pend_act[-1]["kind"] == "botovi" else "Přebírám…"}</span>'
    elif c["handed"] or c["paused"]:
        btn = ('<button class="btn sm" data-act="botovi" title="Bot zase začne v téhle konverzaci odpovídat sám">Vrátit botovi</button>')
    else:
        btn = ('<button class="btn sm" data-act="prevzit" title="Bot v téhle konverzaci přestane odpovídat, dokud ji nevrátíš">Převezmu sám</button>')
    hint = {"ja": "Napiš odpověď dole. Až bude vyřešeno, můžeš konverzaci vrátit botovi.",
            "bot": "Nemusíš nic dělat. Když chceš odpovědět sám, klikni na Převezmu sám.",
            "zakaznik": "",
            "hotovo": ""}[k]
    if c["handed"] and k == "ja":
        hint = "Bot tu nic nepíše, dokud konverzaci nevrátíš. " + hint.split(". ", 1)[-1]
    reason = h(c["reason"])
    return (f'<div class="banner k-{k}" id="banner"><div class="bt"><b>{title}</b>{reason}'
            + (f'<br><span>{h(hint)}</span>' if hint else "") + f'</div>{btn}</div>')


def msgs_html(c):
    out, prev_day = [], None
    ms = c["msgs"]
    labels = {"in": "", "bot": "Bot", "manual": "Ty"}
    old_shown = False
    for i, m in enumerate(ms):
        if m["ts"]:
            d = datetime.fromtimestamp(m["ts"]).date()
            if d != prev_day:
                out.append(f'<div class="day">{day_label(m["ts"])}</div>')
                prev_day = d
        elif not old_shown:
            out.append('<div class="day">Starší zprávy</div>')
            old_shown = True
        content = h(m["text"])
        if m.get("images"):
            content = (content + "<br>" if content else "") + f'<span class="photo">{ICON_PHOTO} fotka</span>'
        if not content:
            continue
        nxt = ms[i + 1] if i + 1 < len(ms) else None
        end_group = not nxt or nxt["dir"] != m["dir"] or (nxt["ts"] - m["ts"] > 900)
        meta = ""
        if end_group and (m["ts"] or m["dir"] != "in"):
            lab = labels.get(m["dir"], "")
            bits = ([f"<b>{lab}</b>"] if lab else []) + ([h(clock(m["ts"]))] if m["ts"] else [])
            meta = f'<div class="meta">{" · ".join(bits)}</div>'
        out.append(f'<div class="m {m["dir"]}"><div class="bb">{content}</div>{meta}</div>')
    if not ms:
        out.append('<div class="sys">Zprávy z téhle konverzace bot ještě neuložil.</div>')
    now = time.time()
    for o in c["outbox"]:
        if o["kind"] != "zprava":
            continue
        if o["status"] == "ceka":
            out.append(f'<div class="m pend"><div class="bb">{h(o["text"])}</div>'
                       f'<div class="meta"><span class="spin"></span> <b>Odesílá se přes telefon…</b> bot to napíše zhruba do 20 s</div></div>')
        elif o["status"] == "chyba" and now - (o["done_ts"] or o["ts"]) < 86400 and not any(
                x["ts"] > o["ts"] and x["status"] == "hotovo" and x["kind"] == "zprava" for x in c["outbox"]):
            out.append(f'<div class="m pend err"><div class="bb">{h(o["text"])}</div>'
                       f'<div class="meta" style="color:var(--bad)"><b>Nepodařilo se odeslat.</b> {h(o["error"][:160])} '
                       f'<button class="btn sm" data-retry="{h(o["text"])}">Zkusit znovu</button></div></div>')
        elif o["status"] == "hotovo" and o["done_ts"] and now - o["done_ts"] < 600:
            out.append(f'<div class="sys ok">Odesláno ✓ – bot zprávu napsal v telefonu ({h(clock(o["done_ts"]))})</div>')
    return "".join(out)


def side_html(c):
    t = c["t"]
    parts = []
    note = c["note"]
    if not note:  # záloha: poznámka bota ke komentáři, nebo důvod předání
        cn = next((x for x in reversed(c["comments"]) if x["note"]), None)
        ho = next((r for r in reversed(c["log"]) if r["action"].startswith("PŘEDÁNO") and r["detail"]), None)
        note = {"note": cn["note"], "ts": cn["ts"]} if cn else {"note": ho["detail"], "ts": ho["ts"]} if ho else None
    parts.append('<div class="sbox"><h3>Co chce</h3>' + (
        f'<p>{h(note["note"])}</p><div class="small" style="margin-top:6px">shrnutí od bota, {h(ago(note["ts"]))}</div>'
        if note else '<p class="small">Bot o tomhle zákazníkovi zatím nic nezapsal.</p>') + '</div>')
    facts, pieces = order_facts(c)
    got = sum(1 for f in facts if f[2])
    lis = "".join(f'<li class="{"y" if ok else "n"}"><span class="ck">{"✓" if ok else "–"}</span><span class="lbl">{h(lbl)}</span>'
                  f'<span class="val">{h(val) if ok else "zatím ne"}</span></li>' for lbl, val, ok in facts)
    parts.append(f'<div class="sbox"><h3>Objednávka · {got}/6</h3><ul class="facts">{lis}</ul>'
                 + (f'<div class="small" style="margin-top:6px">Zmínil {h(pieces)} – 690 Kč za kus, doprava jednou.</div>' if pieces else "")
                 + '<div class="small" style="margin-top:6px">Vyčteno ze zpráv zákazníka – ber jako nápovědu.</div></div>')
    if c["comments"]:
        cm = "".join(f'<div class="cmt"><div class="g">{h(x["grp"] or "skupina?")} · {h(ago(x["ts"]))}</div>„{h(x["text"])}“'
                     + (f'<div class="rp">↳ odpověď: {h(x["reply"])}</div>' if x["reply"] else "") + '</div>'
                     for x in c["comments"][-6:])
        parts.append(f'<div class="sbox"><h3>Psal pod příspěvky</h3>{cm}</div>')
    info = []
    if c["from_comment"]:
        info.append("Přišel z komentáře pod příspěvkem.")
    if t.get("botReplies"):
        info.append(f"Bot odpověděl {t.get('botReplies')}× za sebou (limit {config().get('maxBotRepliesInRow', 6)}).")
    if c["paused"]:
        info.append(f"Bot tu mlčí do {clock(c['paused_until'])}, protože jsi psal ty.")
    if c["handed"]:
        info.append("Konverzace je předaná tobě – bot nepíše.")
    if c["blocked"]:
        info.append("Zablokovaný – bot mu nikdy neodpoví.")
    if info:
        parts.append('<div class="sbox"><h3>Bot</h3>' + "".join(f'<p style="margin-bottom:4px">{h(x)}</p>' for x in info) + '</div>')
    if c["log"]:
        hl = "".join(f'<li><span class="w">{h(clock(r["ts"]))}</span><span>{h(plain_action(r["action"]))}</span></li>'
                     for r in reversed(c["log"][-8:]))
        parts.append(f'<div class="sbox"><h3>Co se dělo</h3><ul class="hist">{hl}</ul></div>')
    return "".join(parts)


def thread_html(c, f):
    s = bot_status()
    blocked = c["blocked"]
    quick = "".join(f'<button type="button" data-quick="{h(v)}" title="{h(v)}">{h(k)}</button>' for k, v in quick_texts())
    if blocked:
        composer = '<div class="composer"><div class="small">Zablokovaný zákazník – odsud se mu psát nedá.</div></div>'
    else:
        warn = ""
        if s["running"] == 0:
            warn = '<span class="st warn">Bot neběží – zpráva odejde, až ho spustíš.</span>'
        elif not s["phone"] or not s["online"]:
            warn = '<span class="st warn">Telefon je odpojený nebo bez internetu – zpráva počká.</span>'
        composer = (
            f'<form class="composer" id="composer" method="post" action="{conv_url(c["person"])}">'
            f'<input type="hidden" name="kind" value="zprava"><div class="quick">{quick}</div>'
            '<div class="cbox"><textarea id="tx" name="text" rows="1" placeholder="Napiš odpověď…" aria-label="Odpověď"></textarea>'
            '<button class="btn pri send" id="sendbtn">Odeslat</button></div>'
            f'<div class="chint"><span id="sendstate" class="st">{warn or "Bot zprávu napíše v telefonu za tebe."}</span>'
            '<span class="keys"><kbd>⌘</kbd>/<kbd>Ctrl</kbd>+<kbd>Enter</kbd> odešle · prázdný řádek = další zpráva</span></div></form>')
    return (
        f'<section class="col thread"><div class="thead"><a class="back" href="/zpravy{"?f=" + f if f != "vse" else ""}">‹ Zpět</a>'
        f'<div class="grow"><h1>{h(c["person"])}</h1><div class="sub">{h(STATUS[c["key"]][0])} · poslední aktivita {h(ago(c["last_ts"]))}</div></div>'
        f'<button class="iconbtn infobtn" onclick="document.body.classList.toggle(\'sideopen\')" title="Co o zákazníkovi víme" aria-label="Info">{ICON_INFO}</button></div>'
        f'{banner_html(c)}<div class="msgs" id="msgs">{msgs_html(c)}</div>{composer}</section>'
        f'<aside class="col side"><div class="sideclose"><button class="btn sm" onclick="document.body.classList.remove(\'sideopen\')">Zavřít</button></div>'
        f'<div id="sidein">{side_html(c)}</div></aside>')


def empty_thread_html(convs):
    n = sum(1 for c in convs if c["key"] == "ja")
    head = f"<b>{n} {plural(n, 'konverzace čeká', 'konverzace čekají', 'konverzací čeká')} na tebe</b>Vyber ji vlevo." if n else \
        "<b>Nic na tebe nečeká</b>Bot všechno zvládá. Vyber konverzaci vlevo, jestli se chceš podívat."
    legend = "".join(f'<div><span class="dot" style="background:var(--{v})"></span><span><b>{STATUS[k][0]}</b> – {STATUS[k][1]}</span></div>'
                     for k, v in (("ja", "ja"), ("bot", "bot"), ("zakaznik", "zak"), ("hotovo", "faint")))
    return f'<section class="col thread wide"><div class="nothread">{head}<div class="legend">{legend}</div></div></section>'


def same_origin():
    o = request.headers.get("Origin")
    return not o or o.split("://", 1)[-1] == request.host


def bad_origin():
    return jsonify(ok=False, error="Požadavek z cizí stránky – zamítnuto."), 403


# ======================================================================
#  STRÁNKY
# ======================================================================

@app.route("/")
def index():
    return redirect("/zpravy")


@app.route("/chat/<path:person>")
def old_chat(person):  # odkazy z původního dashboardu
    return redirect(conv_url(person))


@app.route("/zpravy")
@app.route("/zpravy/<path:person>", methods=["GET", "POST"])
def zpravy(person=None):
    if request.method == "POST":  # záloha bez JavaScriptu
        if not same_origin():
            abort(403)
        kind = request.form.get("kind")
        text = (request.form.get("text") or "").strip()
        if kind == "zprava" and text:
            data.outbox_add("zprava", person, text)
        elif kind in ("botovi", "prevzit"):
            data.outbox_add(kind, person)
        return redirect(conv_url(person))
    f = request.args.get("f", "vse")
    f = f if f in dict(FILTERS) else "vse"
    convs = conversations()
    sel = next((c for c in convs if c["person"] == person), None) if person else None
    if person and not sel:
        abort(404)
    counts = nav_counts(convs)
    left = (f'<section class="col list"><div class="lhead"><input id="search" class="search" type="search" placeholder="Hledat jméno…  ( / )" aria-label="Hledat">'
            f'<div class="filters" id="filters">{filters_html(convs, f)}</div></div>'
            f'<div class="litems" id="litems">{list_html(convs, f, person)}</div></section>')
    right = thread_html(sel, f) if sel else empty_thread_html(convs)
    st = {"person": person, "f": f, "sig": conv_sig(sel) if sel else "", "pending": bool(sel and sel["pending"]),
          "botDown": bot_status()["running"] == 0, "pauseH": config().get("pauseAfterManualReplyHours", 12)}
    st_json = json.dumps(st, ensure_ascii=False).replace("</", "<\\/")
    js = (f'<script type="application/json" id="S">{st_json}</script>'
          f'<script>{INBOX_JS}</script>')
    title = (f"({counts['zpravy']}) " if counts["zpravy"] else "") + (person or "Zprávy")
    return page(title, "zpravy", f'<main class="inbox">{left}{right}</main>', counts,
                body_class="hassel" if sel else "", scripts=js)


@app.route("/api/stav")
def api_stav():
    s = bot_status()
    return jsonify(health=health_html(s), alert=alert_html(s), counts=nav_counts(), running=s["running"], phone=s["phone"], online=s["online"])


@app.route("/api/prehled")
def api_prehled():
    f = request.args.get("f", "vse")
    f = f if f in dict(FILTERS) else "vse"
    person = request.args.get("c") or None
    convs = conversations()
    sel = next((c for c in convs if c["person"] == person), None) if person else None
    s = bot_status()
    return jsonify(list=list_html(convs, f, person), filters=filters_html(convs, f), counts=nav_counts(convs),
                   health=health_html(s), alert=alert_html(s), sig=conv_sig(sel) if sel else "")


@app.route("/api/vlakno/<path:person>")
def api_vlakno(person):
    c = next((c for c in conversations() if c["person"] == person), None)
    if not c:
        return jsonify(error="nenalezeno"), 404
    return jsonify(banner=banner_html(c), msgs=msgs_html(c), side=side_html(c), sig=conv_sig(c), pending=bool(c["pending"]),
                   key=c["key"])


@app.route("/api/poslat", methods=["POST"])
def api_poslat():
    if not same_origin():
        return bad_origin()
    person = (request.form.get("person") or "").strip()
    text = (request.form.get("text") or "").replace("\r\n", "\n").strip()
    if not person or not text:
        return jsonify(ok=False, error="Chybí text zprávy."), 400
    if state()["threads"].get(person, {}).get("blocked"):
        return jsonify(ok=False, error="Zákazník je zablokovaný."), 400
    data.outbox_add("zprava", person, text)
    with data.db() as con:
        oid = con.execute("SELECT max(id) FROM outbox WHERE person=? AND kind='zprava'", (person,)).fetchone()[0]
    return jsonify(ok=True, id=oid)


@app.route("/api/akce", methods=["POST"])
def api_akce():
    if not same_origin():
        return bad_origin()
    person = (request.form.get("person") or "").strip()
    kind = request.form.get("kind")
    if not person or kind not in ("botovi", "prevzit"):
        return jsonify(ok=False, error="Neznámá akce."), 400
    data.outbox_add(kind, person)
    with data.db() as con:
        oid = con.execute("SELECT max(id) FROM outbox WHERE person=? AND kind=?", (person, kind)).fetchone()[0]
    return jsonify(ok=True, id=oid)


@app.route("/api/odeslani/<int:oid>")
def api_odeslani(oid):
    with data.db() as con:
        r = con.execute("SELECT status, error, done_ts FROM outbox WHERE id=?", (oid,)).fetchone()
    if not r:
        return jsonify(status="nenalezeno"), 404
    return jsonify(status=r[0], error=r[1] or "", done=r[2])


# ---------- komentáře ----------

@app.route("/komentare")
def komentare():
    todo = comment_todo()
    with data.db() as con:
        rows = con.execute("SELECT ts, grp, person, text, indent, reply, dm, note FROM comments ORDER BY ts DESC LIMIT 300").fetchall()
        known = {r[0] for r in con.execute("SELECT DISTINCT person FROM messages")} | set(state()["threads"])
    body = ('<main class="page"><div class="wrap"><h1>Komentáře pod příspěvky</h1>'
            '<p class="lead">Bot čte komentáře pod tvými příspěvky ve skupinách, odpovídá na ně a zájemcům napíše do Messengeru.</p>')
    if todo:
        body += f'<h2 class="ja">Vyřiď ručně <span class="cnt">{len(todo)}</span></h2>'
        for r in todo:
            m = re.match(r"„(.*?)“:?\s*(.*)", r["detail"])
            quote_, why = (m.group(1), m.group(2)) if m else ("", r["detail"])
            sugg = re.match(r"→\s*(.*?)\s*\|\s*DM:", why)
            why = f"bot navrhl „{sugg.group(1)}“, ale neodeslal" if sugg else f"bot to nezvládl ({why[:200]})"
            body += (f'<div class="card ja"><div class="crow"><div class="grow"><b>{h(r["who"])}</b> <span class="small">· {h(ago(r["ts"]))}</span>'
                     + (f'<div class="q">„{h(quote_)}“</div>' if quote_ else "")
                     + f'<div class="small">{h(why[0].upper() + why[1:])}. Odpověz pod příspěvkem ve Facebooku.</div></div></div></div>')
    else:
        body += '<div class="card"><b>Všechny komentáře jsou vyřízené.</b> <span class="small">Bot zvládl odpovědět všude, kde měl.</span></div>'
    groups = {}
    for r in rows:
        groups.setdefault(r[1] or "Neznámá skupina", []).append(r)
    body += '<h2>Všechny komentáře</h2>'
    if not rows:
        body += '<div class="card small">Zatím žádné.</div>'
    for g, items in groups.items():
        body += f'<div class="card"><div style="font-weight:700;margin-bottom:6px">{h(g)} <span class="small">· {len(items)}</span></div>'
        for ts, grp, person, text, indent, reply, dm, note in items:
            who = f'<a class="plink" href="{conv_url(person)}">{h(person)}</a>' if person in known else f'<b>{h(person)}</b>'
            res = ('<span class="chip ok">odpověděl</span>' if reply else '<span class="chip">bez odpovědi</span>') + \
                  (' <span class="chip info">napsal do Messengeru</span>' if dm else "")
            body += (f'<div style="padding:10px 0;border-top:1px solid var(--line)"><div class="crow"><div class="grow">{who} '
                     f'<span class="small">· {h(ago(ts))}{" · odpověď v diskusi" if indent else ""}</span>'
                     f'<div class="q">„{h(text)}“</div>'
                     + (f'<div class="arrow">↳ {h(reply)}</div>' if reply else "")
                     + (f'<div class="arrow">✉ {h(dm)}</div>' if dm else "")
                     + (f'<details><summary>Proč to bot udělal</summary><p class="small">{h(note)}</p></details>' if note else "")
                     + f'</div><div>{res}</div></div></div>')
        body += '</div>'
    return page("Komentáře", "komentare", body + '</div></main>')


# ---------- příspěvky ----------

def post_photo(j):
    ph = (j.get("photos") or [j.get("photo")] or [None])[0]
    return f'/foto?p={quote(ph)}' if ph and (HERE / ph).is_file() else ""


@app.route("/prispevky")
def prispevky():
    jobs = queue()
    st = state()
    today = datetime.now().strftime("%Y-%m-%d")
    posted = st.get("postsByDay", {}).get(today, 0)
    maxp = config().get("maxPostsPerDay", 4)
    need = [j for j in jobs if POST_STATUS.get(j.get("status"), ("", "", False))[2]]
    waiting = [j for j in jobs if j.get("status", "").startswith("ceka")]
    done = [j for j in jobs if j not in need and j not in waiting]
    member_q = [r for r in log_rows() if "otázky ke členství" in r["action"] and time.time() - r["ts"] < 4 * 86400]

    def card(j):
        label, cls, _ = POST_STATUS.get(j.get("status"), (j.get("status", "?"), "", False))
        err = j.get("error") or ""
        technical = bool(re.search(r"[{}\[\]]|RPC|Selector|Exception", err))
        img = f'<img class="thumb" src="{post_photo(j)}" alt="" loading="lazy">' if post_photo(j) else ""
        due = ""
        if j.get("status") in ("ceka", "ceka-na-clenstvi") and j.get("due", 0) > time.time():
            due = f' · další pokus {h(clock(j["due"]))}'
        errhtml = ""
        if err:
            errhtml = (f'<details><summary>Technické detaily</summary><pre>{h(err)}</pre></details>' if technical
                       else f'<div class="small" style="margin-top:6px">{h(err)}</div>')
        return (f'<div class="card {"ja" if cls == "ja" else ""}"><div class="crow">{img}<div class="grow">'
                f'<div class="crow" style="gap:8px;align-items:center;flex-wrap:wrap"><b>{h(j.get("group"))}</b>'
                f'<span class="small">{h(j.get("members", ""))} členů</span><span class="chip {cls}">{h(label)}</span></div>'
                f'<div style="margin-top:4px">{h(j.get("text", ""))}</div>'
                f'<div class="small">{"naposledy " + h(j["last"]) if j.get("last") else ""}{due}</div>{errhtml}</div></div></div>')

    body = ('<main class="page"><div class="wrap"><h1>Příspěvky do skupin</h1>'
            '<p class="lead">Bot postupně zveřejňuje příspěvky z fronty – nejvýš pár denně, ať to nevypadá jako spam.</p>'
            f'<div class="kpis"><div class="kpi"><b>{posted} / {maxp}</b><span>zveřejněno dnes</span></div>'
            f'<div class="kpi"><b>{len(waiting)}</b><span>čeká ve frontě</span></div>'
            f'<div class="kpi"><b style="color:{"var(--ja)" if need else "inherit"}">{len(need)}</b><span>potřebuje tebe</span></div></div>')
    if need or member_q:
        body += f'<h2 class="ja">Potřebuje tebe</h2>' + "".join(card(j) for j in need)
        for r in member_q[-3:][::-1]:
            if any(j.get("group") == r["who"] for j in need):
                continue
            body += (f'<div class="card ja"><b>{h(r["who"])}</b> <span class="chip ja">Otázky ke členství</span>'
                     f'<div class="small" style="margin-top:4px">{h(r["detail"][:300])}</div></div>')
    if waiting:
        body += '<h2>Ve frontě</h2>' + "".join(card(j) for j in waiting)
    if done:
        body += '<h2>Hotové</h2>' + "".join(card(j) for j in done)
    if not jobs:
        body += '<div class="card small">Fronta je prázdná. Příspěvky se přidávají přes fronta.py.</div>'
    return page("Příspěvky", "prispevky", body + '</div></main>')


@app.route("/foto")
def foto():
    p = request.args.get("p", "")
    f = (HERE / p).resolve()
    if not any(str(f).startswith(str((HERE / d).resolve()) + "/") for d in ("posty", "fotky")) or not f.is_file():
        abort(404)
    return send_file(f)


# ---------- problémy ----------

@app.route("/problemy")
def problemy():
    items = problems()
    new = [p for p in items if p.get("status") == "novy"]
    groups = {}
    for p in new:
        groups.setdefault(p.get("kind", "?"), []).append(p)
    ordered = sorted(groups.items(), key=lambda kv: -max(x.get("lastTs", 0) for x in kv[1]))
    body = ('<main class="page"><div class="wrap"><h1>Kde se bot zasekl</h1>'
            '<p class="lead">Sem bot ukládá chvíle, kdy se v telefonu ztratil (se screenshotem). Většinou si poradí sám a zkusí to znovu. '
            'Projdi je s tím, kdo bota opravuje, a pak je označ jako vyřešené.</p>')
    if request.args.get("ok"):
        body += '<div class="saved">Označeno jako vyřešené ✓</div>'
    if not new:
        body += '<div class="card"><b>Žádné nové problémy.</b> <span class="small">Bot jede bez zádrhelů.</span></div>'
    for kind, ps in ordered:
        ps = sorted(ps, key=lambda x: x.get("lastTs", 0))
        last = ps[-1]
        total = sum(x.get("count", 1) for x in ps)
        shot = HERE / "problemy" / last["folder"] / "screenshot.png"
        img = (f'<a href="/shot/{quote(last["folder"])}" target="_blank"><img class="shot" src="/shot/{quote(last["folder"])}" alt="screenshot" loading="lazy"></a>'
               if shot.exists() else "")
        older = "".join(f'<li>{h(x["time"])} – {h(x.get("detail", ""))} ({x.get("count", 1)}×)'
                        + (f' · <a href="/shot/{quote(x["folder"])}" target="_blank">screenshot</a>' if (HERE / "problemy" / x["folder"] / "screenshot.png").exists() else "")
                        + '</li>' for x in reversed(ps))
        body += (f'<div class="card"><div class="crow"><div class="grow"><div class="crow" style="gap:8px;align-items:center;flex-wrap:wrap">'
                 f'<b>{h(kind)}</b><span class="chip ja">{total}×</span></div>'
                 f'<div class="small">naposledy {h(last["time"])} · {h(last.get("detail", "")[:220])}</div>'
                 + (f'<details><summary>Všechny výskyty ({len(ps)})</summary><ul class="small">{older}</ul></details>' if len(ps) > 1 else "")
                 + f'<form method="post" action="/problemy/vyreseno" style="margin-top:10px"><input type="hidden" name="kind" value="{h(kind)}">'
                 f'<button class="btn sm">Vyřešeno</button></form></div>{img}</div></div>')
    old = [p for p in items if p.get("status") != "novy"]
    if old:
        body += f'<h2>Vyřešené</h2><div class="card small">{len(old)} vyřešených problémů (příkaz <code>problemy.py vse</code> je vypíše).</div>'
    return page("Problémy", "problemy", body + '</div></main>')


@app.route("/problemy/vyreseno", methods=["POST"])
def problemy_vyreseno():
    if not same_origin():
        abort(403)
    kind = request.form.get("kind")
    f = HERE / "problemy" / "seznam.json"
    items = load_json(f, [])
    changed = False
    for p in items:
        if p.get("kind") == kind and p.get("status") == "novy":
            p["status"] = "vyreseno"
            changed = True
    if changed:
        f.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    return redirect("/problemy?ok=1")


@app.route("/shot/<folder>")
def shot(folder):
    f = (HERE / "problemy" / folder / "screenshot.png").resolve()
    if not str(f).startswith(str((HERE / "problemy").resolve()) + "/") or not f.exists():
        abort(404)
    return send_file(f)


# ---------- učení ----------

@app.route("/uceni", methods=["GET", "POST"])
def uceni():
    lekce = HERE / "lekce.md"
    if request.method == "POST":
        if not same_origin():
            abort(403)
        lekce.write_text(request.form.get("lekce", "").replace("\r\n", "\n"), encoding="utf-8")
        return redirect("/uceni?ulozeno=1")
    body = ('<main class="page"><div class="wrap"><h1>Učení bota</h1>'
            '<p class="lead">Tady bota učíš, jak má odpovídat. Lekce čte před každou odpovědí v Messengeru.</p>')
    if request.args.get("ulozeno"):
        body += '<div class="saved">Uloženo ✓ Bot lekce použije od další odpovědi.</div>'
    text = lekce.read_text(encoding="utf-8") if lekce.exists() else ""
    body += ('<h2>Lekce</h2><div class="card"><form method="post">'
             '<p class="small" style="margin-top:0">Jedna lekce = jeden řádek začínající pomlčkou. Nejdůležitější nahoru. '
             'Piš normálně, jako bys to vysvětloval brigádníkovi.</p>'
             f'<textarea class="lekce" name="lekce" spellcheck="true">{h(text)}</textarea>'
             '<div style="margin-top:10px;display:flex;gap:10px;align-items:center"><button class="btn pri">Uložit lekce</button>'
             '<span class="small">Ukládá se do lekce.md</span></div></form></div>')
    with data.db() as con:
        rows = con.execute("SELECT ts, person, note FROM decisions WHERE handoff=1 ORDER BY id DESC LIMIT 30").fetchall()
    body += '<h2>Co bot neuměl a předal tobě</h2><p class="small" style="margin-top:-4px">Když se něco opakuje, napiš na to lekci nahoře.</p>'
    if rows:
        body += '<div class="card">' + "".join(
            f'<div style="padding:8px 0;border-bottom:1px solid var(--line)"><a class="plink" href="{conv_url(p)}">{h(p)}</a> '
            f'<span class="small">· {h(ago(ts))}</span><div>{h(n or "")}</div></div>' for ts, p, n in rows) + '</div>'
    else:
        body += '<div class="card small">Zatím nic – bot všechno zvládl.</div>'
    ex = data.manual_examples(30) or ""
    pairs = re.findall(r"- Zákazník: „(.*?)“ → Prodejce: „(.*?)“$", ex, re.M)
    body += ('<h2>Tvoje odpovědi, ze kterých se bot učí styl</h2>'
             '<p class="small" style="margin-top:-4px">Zprávy, které jsi psal sám. Osobní údaje se botovi nepředávají.</p>')
    body += ('<div class="card">' + "".join(f'<div class="pair"><div class="c">{h(a)}</div><div class="s">{h(b)}</div></div>' for a, b in reversed(pairs))
             + '</div>') if pairs else '<div class="card small">Zatím žádné.</div>'
    return page("Učení bota", "uceni", body + '</div></main>')


@app.errorhandler(404)
def not_found(_e):
    return page("Nenalezeno", "", '<main class="page"><div class="wrap"><h1>Tohle tu není</h1>'
                '<p class="lead">Konverzace nebo stránka neexistuje. <a href="/zpravy">Zpět na zprávy</a></p></div></main>'), 404


if __name__ == "__main__":
    print("Dashboard: http://localhost:5005")
    app.run(host="127.0.0.1", port=5005, debug=False)
