"""Dashboard bota na lampičky – lokální stránka jen pro tebe (http://localhost:5005).

  .venv/bin/python dashboard.py

Ukazuje stav bota, konverzace (kde co stojí), komentáře, frontu příspěvků, problémy a učení.
Odpověď napsaná tady jde do fronty a bot ji v telefonu napíše po znacích (telefon ovládá jen bot).
Nic z toho není na internetu – data obsahují osobní údaje zákazníků.
"""

import csv
import json
import subprocess
import time
from datetime import datetime
from html import escape
from pathlib import Path

from flask import Flask, abort, redirect, request, send_file, url_for

import data

HERE = Path(__file__).resolve().parent
ADB = str(Path.home() / ".local" / "android" / "platform-tools" / "adb")
app = Flask(__name__)
_status_cache = {"ts": 0, "val": None}


# ---------- data ----------

def state():
    try:
        return json.loads((HERE / "state_telefon.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"threads": {}}


def log_rows(limit=None):
    f = HERE / "log.csv"
    if not f.exists():
        return []
    rows = [r for r in csv.reader(f.read_text(encoding="utf-8").splitlines(), delimiter=";") if len(r) >= 3]
    return rows[-limit:] if limit else rows


def bot_status():
    if time.time() - _status_cache["ts"] < 20:
        return _status_cache["val"]
    ps = subprocess.run(["ps", "-ax"], capture_output=True, text=True).stdout
    running = sum(1 for l in ps.splitlines() if "bot_telefon.py run" in l and "Python" in l)
    devs = subprocess.run([ADB, "devices"], capture_output=True, text=True).stdout
    phone = any(l.strip().endswith("\tdevice") for l in devs.splitlines()[1:])
    online = False
    if phone:
        out = subprocess.run([ADB, "shell", "ping", "-c", "1", "-W", "2", "8.8.8.8"], capture_output=True, text=True).stdout
        online = " 1 received" in out
    last = log_rows(1)
    val = {"running": running, "phone": phone, "online": online, "last": last[0] if last else None}
    _status_cache.update(ts=time.time(), val=val)
    return val


def ago(ts):
    if not ts:
        return ""
    m = int((time.time() - ts) / 60)
    return f"{m} min" if m < 60 else f"{m // 60} h" if m < 48 * 60 else f"{m // 1440} d"


def conversations():
    st = state()["threads"]
    with data.db() as con:
        rows = con.execute("SELECT person, max(id), count(*) FROM messages GROUP BY person").fetchall()
        out = []
        for person, last_id, n in rows:
            ts, direction, text = con.execute("SELECT ts, direction, text FROM messages WHERE id=?", (last_id,)).fetchone()
            t = st.get(person, {})
            out.append({"person": person, "n": n, "ts": ts, "direction": direction, "text": text,
                        "handed": t.get("handedOff"), "blocked": t.get("blocked"),
                        "paused": t.get("pausedUntil", 0) > time.time() * 1000})
    for person, t in st.items():  # konverzace, které bot zná, ale nejsou v databázi
        if not any(c["person"] == person for c in out):
            out.append({"person": person, "n": 0, "ts": 0, "direction": "?", "text": t.get("preview", ""),
                        "handed": t.get("handedOff"), "blocked": t.get("blocked"), "paused": False})
    def rank(c):
        waiting = c["direction"] == "in" and not c["blocked"]
        return (0 if c["handed"] and not c["blocked"] else 1 if waiting else 2, -(c["ts"] or 0))
    return sorted(out, key=rank)


def handoff_reason(person):
    for r in reversed(log_rows()):
        if r[1] == person and r[2].startswith("PŘEDÁNO"):
            return (r[3] if len(r) > 3 else "", r[0])
    return ("", "")


# ---------- vzhled ----------

CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1b;--mute:#6b6a66;--line:#e4e2dc;--acc:#2d6a4f;--warn:#b5651d;--bad:#b23a3a;--in:#eef3ff;--me:#eef8f1;--man:#fff6e6}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--ink:#ecebe7;--mute:#9b9993;--line:#33322f;--acc:#6cc19a;--warn:#e0a060;--bad:#e07a7a;--in:#1d2433;--me:#1b2a22;--man:#2e2617}}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--ink)}
nav{display:flex;gap:4px;flex-wrap:wrap;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--card);position:sticky;top:0}
nav a{padding:6px 10px;border-radius:8px;color:var(--ink);text-decoration:none}nav a.on{background:var(--acc);color:#fff}
main{max-width:1100px;margin:0 auto;padding:16px}h1{font-size:20px;margin:6px 0 14px}h2{font-size:16px;margin:22px 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:10px}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.grow{flex:1;min-width:200px}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;border:1px solid var(--line);color:var(--mute)}
.ok{color:var(--acc);border-color:var(--acc)}.warn{color:var(--warn);border-color:var(--warn)}.bad{color:var(--bad);border-color:var(--bad)}
.mute{color:var(--mute);font-size:13px}a{color:var(--acc)}table{width:100%;border-collapse:collapse}td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.msg{max-width:75%;padding:8px 11px;border-radius:12px;margin:4px 0;white-space:pre-wrap}.in{background:var(--in)}.bot{background:var(--me);margin-left:auto}.manual{background:var(--man);margin-left:auto}
textarea{width:100%;min-height:80px;font:inherit;padding:8px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--ink)}
button{font:inherit;padding:7px 14px;border-radius:8px;border:1px solid var(--acc);background:var(--acc);color:#fff;cursor:pointer}button.sec{background:transparent;color:var(--acc)}
img.shot{max-width:260px;border-radius:8px;border:1px solid var(--line)}
"""


def page(title, body, active="", refresh=False):
    tabs = [("prehled", "Přehled"), ("konverzace", "Konverzace"), ("komentare", "Komentáře"), ("fronta", "Příspěvky"),
            ("problemy", "Problémy"), ("uceni", "Učení")]
    nav = "".join(f'<a class="{"on" if k == active else ""}" href="{url_for(k)}">{v}</a>' for k, v in tabs)
    meta = '<meta http-equiv="refresh" content="30">' if refresh else ""
    return (f'<!doctype html><html lang="cs"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'{meta}<title>Lampičky – {escape(title)}</title><style>{CSS}</style></head><body><nav>{nav}</nav><main>{body}</main></body></html>')


def status_bar():
    s = bot_status()
    pill = lambda ok, yes, no: f'<span class="pill {"ok" if ok else "bad"}">{yes if ok else no}</span>'
    bot = pill(s["running"] == 1, "bot běží", "bot NEBĚŽÍ" if not s["running"] else f"běží {s['running']}× (!)")
    last = f'<span class="mute">poslední akce: {escape(s["last"][0])} – {escape(s["last"][1])}: {escape(s["last"][2])}</span>' if s["last"] else ""
    return f'<div class="card row">{bot}{pill(s["phone"], "telefon připojený", "telefon ODPOJENÝ")}{pill(s["online"], "internet OK", "BEZ INTERNETU")}{last}</div>'


# ---------- stránky ----------

@app.route("/")
def prehled():
    convs = conversations()
    handed = [c for c in convs if c["handed"] and not c["blocked"]]
    waiting = [c for c in convs if c["direction"] == "in" and not c["handed"] and not c["blocked"]]
    st = state()
    today = datetime.now().strftime("%Y-%m-%d")
    posts = st.get("postsByDay", {}).get(today, 0)
    probs = json.loads((HERE / "problemy" / "seznam.json").read_text(encoding="utf-8")) if (HERE / "problemy" / "seznam.json").exists() else []
    new_probs = sum(1 for p in probs if p["status"] == "novy")
    body = f"<h1>Přehled</h1>{status_bar()}"
    body += f'<div class="row"><div class="card grow"><b>{len(handed)}</b> čeká na tebe</div><div class="card grow"><b>{len(waiting)}</b> zákazník čeká na odpověď</div>'
    body += f'<div class="card grow"><b>{posts}/4</b> příspěvky dnes</div><div class="card grow"><b>{new_probs}</b> nových problémů</div></div>'
    body += "<h2>Čeká na tebe</h2>"
    for c in handed:
        reason, when = handoff_reason(c["person"])
        body += (f'<div class="card"><div class="row"><a class="grow" href="{url_for("chat", person=c["person"])}"><b>{escape(c["person"])}</b></a>'
                 f'<span class="mute">{escape(when)}</span></div><div>{escape(reason)}</div><div class="mute">poslední: {escape((c["text"] or "")[:140])}</div></div>')
    if not handed:
        body += '<div class="card mute">Nic.</div>'
    if waiting:
        body += "<h2>Zákazník napsal, bot ještě neodpověděl</h2>"
        for c in waiting:
            body += f'<div class="card"><a href="{url_for("chat", person=c["person"])}"><b>{escape(c["person"])}</b></a> <span class="mute">před {ago(c["ts"])}</span><div>{escape((c["text"] or "")[:160])}</div></div>'
    body += "<h2>Poslední dění</h2><div class='card'><table>"
    for r in reversed(log_rows(25)):
        body += f"<tr><td class='mute'>{escape(r[0])}</td><td>{escape(r[1])}</td><td>{escape(r[2])}</td><td class='mute'>{escape((r[3] if len(r) > 3 else '')[:140])}</td></tr>"
    return page("Přehled", body + "</table></div>", "prehled", refresh=True)


@app.route("/konverzace")
def konverzace():
    body = f"<h1>Konverzace</h1>{status_bar()}<div class='card'><table><tr><th>Kdo</th><th>Stav</th><th>Poslední zpráva</th><th>Kdy</th></tr>"
    for c in conversations():
        if c["blocked"]:
            stav = '<span class="pill">zablokován</span>'
        elif c["handed"]:
            stav = '<span class="pill bad">čeká na tebe</span>'
        elif c["direction"] == "in":
            stav = '<span class="pill warn">zákazník čeká</span>'
        elif c["paused"]:
            stav = '<span class="pill">píšeš ty – bot mlčí</span>'
        else:
            stav = '<span class="pill ok">čeká na zákazníka</span>'
        who = {"in": "", "bot": "bot: ", "manual": "ty: "}.get(c["direction"], "")
        body += (f'<tr><td><a href="{url_for("chat", person=c["person"])}">{escape(c["person"])}</a></td><td>{stav}</td>'
                 f'<td>{escape(who + (c["text"] or "")[:120])}</td><td class="mute">{ago(c["ts"])}</td></tr>')
    return page("Konverzace", body + "</table></div>", "konverzace")


@app.route("/chat/<path:person>", methods=["GET", "POST"])
def chat(person):
    if request.method == "POST":
        kind = request.form.get("kind")
        text = (request.form.get("text") or "").strip()
        if kind == "zprava" and text:
            data.outbox_add("zprava", person, text)
        elif kind in ("botovi", "prevzit"):
            data.outbox_add(kind, person)
        return redirect(url_for("chat", person=person))
    t = state()["threads"].get(person, {})
    with data.db() as con:
        msgs = con.execute("SELECT direction, text, images FROM messages WHERE person=? ORDER BY id", (person,)).fetchall()
        comments = con.execute("SELECT grp, text, reply FROM comments WHERE person=? ORDER BY ts", (person,)).fetchall()
        queued = con.execute("SELECT kind, text, status, error FROM outbox WHERE person=? ORDER BY id DESC LIMIT 5", (person,)).fetchall()
    reason, when = handoff_reason(person)
    body = f"<h1>{escape(person)}</h1>"
    flags = []
    if t.get("blocked"):
        flags.append('<span class="pill">zablokován</span>')
    if t.get("handedOff"):
        flags.append(f'<span class="pill bad">předáno tobě</span> <span class="mute">{escape(reason)}</span>')
    if t.get("pausedUntil", 0) > time.time() * 1000:
        flags.append('<span class="pill">bot mlčí (píšeš ty)</span>')
    body += f'<div class="card">{" ".join(flags) or "<span class=pill>bot odpovídá</span>"}</div>'
    if comments:
        body += "<h2>Komentáře pod příspěvky</h2><div class='card'>" + "".join(
            f"<div><span class='mute'>{escape(g or '')}:</span> „{escape(c)}“" + (f" <span class='mute'>→ {escape(r)}</span>" if r else "") + "</div>"
            for g, c, r in comments) + "</div>"
    body += "<h2>Zprávy</h2><div class='card'>"
    for direction, text, images in msgs:
        label = {"in": "", "bot": "bot", "manual": "ty"}.get(direction, "")
        body += f'<div class="msg {direction}">{escape(text or "")}{" [fotka]" if images else ""}<div class="mute">{label}</div></div>'
    if not msgs:
        body += "<div class='mute'>Zatím žádné uložené zprávy.</div>"
    body += "</div><h2>Odpovědět</h2><div class='card'><form method='post'><input type='hidden' name='kind' value='zprava'>"
    body += "<textarea name='text' placeholder='Napiš odpověď… (prázdný řádek = další samostatná zpráva)'></textarea>"
    body += "<div class='row' style='margin-top:8px'><button>Poslat přes telefon</button><span class='mute'>Bot ji napíše po znacích a pak v konverzaci 12 h mlčí.</span></div></form></div>"
    body += "<div class='row'>"
    body += f"<form method='post'><input type='hidden' name='kind' value='botovi'><button class='sec'>Vrátit konverzaci botovi</button></form>"
    body += f"<form method='post'><input type='hidden' name='kind' value='prevzit'><button class='sec'>Převezmu sám (bot mlčí)</button></form></div>"
    if queued:
        body += "<h2>Fronta pro bota</h2><div class='card'>" + "".join(
            f"<div>{escape(k)}: {escape((x or '')[:80])} – <b>{escape(s)}</b> <span class='mute'>{escape(e or '')}</span></div>" for k, x, s, e in queued) + "</div>"
    return page(person, body, "konverzace")


@app.route("/komentare")
def komentare():
    with data.db() as con:
        rows = con.execute("SELECT ts, grp, person, text, indent, reply, dm, note FROM comments ORDER BY ts DESC LIMIT 200").fetchall()
    body = "<h1>Komentáře</h1><div class='card'><table><tr><th>Kdo</th><th>Skupina</th><th>Komentář</th><th>Odpověď bota</th></tr>"
    for ts, grp, person, text, indent, reply, dm, note in rows:
        body += (f"<tr><td><a href='{url_for('chat', person=person)}'>{escape(person)}</a></td><td class='mute'>{escape(grp or '')}</td>"
                 f"<td>{'↳ ' if indent else ''}{escape(text)}</td><td>{escape(reply or '')}{('<div class=mute>zpráva: ' + escape(dm) + '</div>') if dm else ''}</td></tr>")
    return page("Komentáře", body + "</table></div>", "komentare")


@app.route("/fronta")
def fronta():
    jobs = json.loads((HERE / "fronta.json").read_text(encoding="utf-8")) if (HERE / "fronta.json").exists() else []
    cls = {"zverejneno": "ok", "ceka": "warn", "chyba": "bad"}
    body = "<h1>Příspěvky do skupin</h1><div class='card'><table><tr><th>Skupina</th><th>Stav</th><th>Kdy</th><th>Poznámka</th></tr>"
    for j in jobs:
        body += (f"<tr><td>{escape(j['group'])} <span class='mute'>{escape(j.get('members', ''))}</span></td>"
                 f"<td><span class='pill {cls.get(j['status'], '')}'>{escape(j['status'])}</span></td><td class='mute'>{escape(j.get('last', ''))}</td>"
                 f"<td class='mute'>{escape((j.get('error') or '')[:160])}</td></tr>")
    return page("Příspěvky", body + "</table></div>", "fronta")


@app.route("/problemy")
def problemy():
    f = HERE / "problemy" / "seznam.json"
    items = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
    body = "<h1>Kde se bot zasekl</h1>"
    for i, p in reversed(list(enumerate(items, 1))):
        shot = HERE / "problemy" / p["folder"] / "screenshot.png"
        img = f"<a href='{url_for('shot', folder=p['folder'])}'><img class='shot' src='{url_for('shot', folder=p['folder'])}'></a>" if shot.exists() else ""
        body += (f"<div class='card row'><div class='grow'><span class='pill {'warn' if p['status'] == 'novy' else 'ok'}'>{escape(p['status'])}</span> "
                 f"<b>{escape(p['kind'])}</b> <span class='mute'>{escape(p['time'])} ({p['count']}×) – č. {i}</span><div>{escape(p['detail'])}</div></div>{img}</div>")
    return page("Problémy", body or "Žádné.", "problemy")


@app.route("/shot/<folder>")
def shot(folder):
    f = (HERE / "problemy" / folder / "screenshot.png").resolve()
    if not str(f).startswith(str((HERE / "problemy").resolve())) or not f.exists():
        abort(404)
    return send_file(f)


@app.route("/uceni", methods=["GET", "POST"])
def uceni():
    lekce = HERE / "lekce.md"
    if request.method == "POST":
        lekce.write_text(request.form.get("lekce", "").replace("\r\n", "\n"), encoding="utf-8")
        return redirect(url_for("uceni"))
    body = "<h1>Učení</h1><h2>Lekce (bot je čte při každé odpovědi)</h2><div class='card'><form method='post'>"
    body += f"<textarea name='lekce' style='min-height:240px'>{escape(lekce.read_text(encoding='utf-8'))}</textarea>"
    body += "<div style='margin-top:8px'><button>Uložit lekce</button></div></form></div>"
    body += "<h2>Tvoje ruční odpovědi (bot z nich bere styl)</h2><div class='card' style='white-space:pre-wrap'>"
    body += escape(data.manual_examples(30) or "Zatím žádné.") + "</div>"
    with data.db() as con:
        rows = con.execute("SELECT ts, person, note FROM decisions WHERE handoff=1 ORDER BY id DESC LIMIT 30").fetchall()
    body += "<h2>Co bot neuměl (předal tobě)</h2><div class='card'><table>"
    body += "".join(f"<tr><td class='mute'>{datetime.fromtimestamp(ts):%d. %m. %H:%M}</td><td>{escape(p)}</td><td>{escape(n or '')}</td></tr>" for ts, p, n in rows)
    return page("Učení", body + "</table></div>", "uceni")


if __name__ == "__main__":
    print("Dashboard: http://localhost:5005")
    app.run(host="127.0.0.1", port=5005, debug=False)
