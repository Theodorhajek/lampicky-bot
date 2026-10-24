"""Databáze všeho, co bot vidí a dělá (SQLite, data/lampicky.db) – podklad pro ladění a učení bota.

  messages   – zprávy v Messengeru: zákazník (in), bot (bot), ty ručně (manual)
  comments   – komentáře pod příspěvky a odpovědi na ně
  decisions  – každé rozhodnutí bota: co dostal, co navrhl, jestli předal tobě

  .venv/bin/python data.py                 – přehled (počty, poslední ruční odpovědi)
  .venv/bin/python data.py rucni           – tvoje ruční odpovědi (vzory pro bota)
  .venv/bin/python data.py predano         – konverzace, které bot předal tobě (co neumí)
"""

import json
import re
import sqlite3
import sys
import time
from pathlib import Path

DB = Path(__file__).resolve().parent / "data" / "lampicky.db"
PRIVATE = re.compile(r"\d{3}\s?\d{3}\s?\d{3}|@|\d{6,}/\d{4}|send .* CZK|\b\d{3}\s?\d{2}\b|adresa", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, ts REAL, person TEXT, direction TEXT, text TEXT, images INTEGER, nth INTEGER,
  UNIQUE(person, direction, text, nth));
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY, ts REAL, grp TEXT, person TEXT, text TEXT, indent INTEGER, reply TEXT, dm TEXT, note TEXT,
  UNIQUE(grp, person, text));
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY, ts REAL, person TEXT, file TEXT, motif TEXT, label TEXT, caption TEXT, status TEXT DEFAULT 'ke_schvaleni');
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY, ts REAL, kind TEXT, person TEXT, text TEXT, status TEXT DEFAULT 'ceka', error TEXT, done_ts REAL);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY, ts REAL, kind TEXT, person TEXT, input TEXT, output TEXT, handoff INTEGER, note TEXT);
"""


def db():
    DB.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    return con


def bot_history():
    """Všechny zprávy, které kdy poslal bot (log.csv) – ať se starší zprávy bota nepočítají jako tvoje ruční."""
    norm = lambda t: re.sub(r"\W+", "", (t or "").lower())  # bez emoji/teček (🙂 se v Messengeru čte jako „..“)
    out = set()
    log = DB.parent.parent / "log.csv"
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            parts = line.split(";", 3)
            if len(parts) == 4 and parts[2].startswith("odesl"):
                out |= {norm(x) for x in parts[3].strip('"').split(" / ")}
    return out


def save_conversation(person, messages, bot_sent):
    """Uloží zprávy z otevřené konverzace. Moje zprávy, které neposlal bot, jsou ruční (= vzor, jak odpovídáš ty).
    nth = kolikátá stejná zpráva od stejného člověka (ať se „OK“ neztratí, ale ani nezdvojí)."""
    norm = lambda t: re.sub(r"\W+", "", (t or "").lower())  # bez emoji/teček (🙂 se v Messengeru čte jako „..“)
    sent = {norm(s) for s in bot_sent} | bot_history()
    theirs = {norm(m["text"]) for m in messages if m["from"] == "them"}
    seen = {}
    with db() as con:
        for m in messages:
            direction = "in" if m["from"] == "them" else ("bot" if norm(m["text"]) in sent else "manual")
            unquoted = norm(re.sub(r"^[^,]{2,30}, ", "", m["text"]))
            if direction == "manual" and (unquoted in theirs or norm(m["text"]) in theirs):
                continue  # citace zákazníkovy zprávy (odpověď na konkrétní zprávu), ne moje zpráva
            k = (direction, m["text"])
            seen[k] = seen.get(k, 0) + 1
            con.execute("INSERT OR IGNORE INTO messages(ts, person, direction, text, images, nth) VALUES (?,?,?,?,?,?)",
                        (time.time(), person, direction, m["text"], m.get("images", 0), seen[k]))


def save_comment(group, person, text, indent, reply="", dm="", note=""):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO comments(ts, grp, person, text, indent, reply, dm, note) VALUES (?,?,?,?,?,?,?,?)",
                    (time.time(), group, person, text, int(bool(indent)), reply, dm, note))


def save_seen_comment(group, person, text, indent):
    """Každý přečtený komentář (i ten, na který se neodpovídá) – ať víme, kdo co psal."""
    with db() as con:
        con.execute("INSERT OR IGNORE INTO comments(ts, grp, person, text, indent, reply, dm, note) VALUES (?,?,?,?,?,'','','')",
                    (time.time(), group, person, text, int(bool(indent))))


def save_decision(kind, person, input_text, output, handoff=False, note=""):
    with db() as con:
        con.execute("INSERT INTO decisions(ts, kind, person, input, output, handoff, note) VALUES (?,?,?,?,?,?,?)",
                    (time.time(), kind, person, input_text, json.dumps(output, ensure_ascii=False), int(bool(handoff)), note))


def comments_of(person, limit=6):
    """Co tenhle člověk psal pod mými příspěvky (skupina + text) – kontext pro chat v Messengeru."""
    try:
        with db() as con:
            rows = con.execute("SELECT grp, text, reply FROM comments WHERE person=? ORDER BY ts DESC LIMIT ?", (person, limit)).fetchall()
        return "\n".join(f"- ve skupině {g or '?'}: „{t}“" + (f" (odpověděl jsem: „{r}“)" if r else "") for g, t, r in reversed(rows))
    except sqlite3.Error:
        return ""


def manual_examples(limit=8):
    """Poslední tvoje ruční odpovědi i se zprávou zákazníka před nimi – bot se z nich učí styl a postup."""
    try:
        with db() as con:
            rows = con.execute("SELECT id, person, text FROM messages WHERE direction='manual' AND length(text) > 1 "
                               "ORDER BY id DESC LIMIT ?", (limit * 3,)).fetchall()
            out = []
            for mid, person, text in reversed(rows):
                prev = con.execute("SELECT text FROM messages WHERE person=? AND direction='in' AND id < ? ORDER BY id DESC LIMIT 1",
                                   (person, mid)).fetchone()
                # bez osobních údajů (telefon, e-mail, účet) a bez citací
                if prev and not PRIVATE.search(prev[0] + " " + text) and not re.match(r"^[^,]{2,30}, ", text):
                    out.append(f"- Zákazník: „{prev[0][:200]}“ → Prodejce: „{text[:200]}“")
            return "\n".join(out[-limit:])
    except sqlite3.Error:
        return ""


# ---------- fronta pro bota z dashboardu (odpovědi a povely) ----------

def outbox_add(kind, person, text=""):
    """kind: 'zprava' (napsat zprávu do Messengeru), 'botovi' (vrátit konverzaci botovi), 'prevzit' (bot ať mlčí)."""
    with db() as con:
        con.execute("INSERT INTO outbox(ts, kind, person, text) VALUES (?,?,?,?)", (time.time(), kind, person, text))


def outbox_pending():
    with db() as con:
        return con.execute("SELECT id, kind, person, text FROM outbox WHERE status='ceka' ORDER BY id").fetchall()


def outbox_done(oid, ok=True, error=""):
    with db() as con:
        con.execute("UPDATE outbox SET status=?, error=?, done_ts=? WHERE id=?", ("hotovo" if ok else "chyba", error, time.time(), oid))


# ---------- návrhy pro zákazníky (bot vytvoří, ty schválíš v dashboardu) ----------

def proposal_add(person, file, motif, label, caption):
    with db() as con:
        con.execute("INSERT INTO proposals(ts, person, file, motif, label, caption) VALUES (?,?,?,?,?,?)",
                    (time.time(), person, str(file), motif, label, caption))


def proposals(status="ke_schvaleni"):
    with db() as con:
        return con.execute("SELECT id, ts, person, file, motif, label, caption, status FROM proposals WHERE status=? ORDER BY id",
                           (status,)).fetchall()


def proposal_set(pid, status, caption=None):
    with db() as con:
        if caption is not None:
            con.execute("UPDATE proposals SET status=?, caption=? WHERE id=?", (status, caption, pid))
        else:
            con.execute("UPDATE proposals SET status=? WHERE id=?", (status, pid))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "prehled"
    with db() as con:
        if cmd == "rucni":
            print(manual_examples(50) or "Zatím žádné ruční odpovědi.")
        elif cmd == "predano":
            for ts, person, note in con.execute("SELECT ts, person, note FROM decisions WHERE handoff=1 ORDER BY id DESC LIMIT 30"):
                print(time.strftime("%d. %m. %H:%M", time.localtime(ts)), person, "–", note)
        else:
            for table in ("messages", "comments", "decisions"):
                print(table, con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for d_, n in con.execute("SELECT direction, count(*) FROM messages GROUP BY direction"):
                print("  zprávy", d_, n)


if __name__ == "__main__":
    main()
