"""Příchozí platby z e-mailových upozornění Air Bank (hajek.theodor@gmail.com, čteno přes Telegram bridge – jen čtení).

Bere jen účet z config.json → "platbyUcet" (číslo účtu nebo jeho název v Air Bank, např. „…/3030“).
Platbu přiřadí k objednávce podle jména odesílatele (a variabilního symbolu = telefon / ID objednávky).
Zákazníkovi nic nepíše – v dashboardu jen ukáže „zaplaceno“ a pošle ti notifikaci.

  .venv/bin/python platby.py           – načíst nové platby a vypsat poslední
"""

import json
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

import data

HERE = Path(__file__).resolve().parent
GMAIL = Path.home() / "Developer" / "claude-telegram" / "gmail.mjs"
QUERY = 'from:info@airbank.cz subject:"Zvýšení zůstatku" newer_than:45d'

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY, msgid TEXT UNIQUE, ts REAL, account TEXT, account_no TEXT, amount REAL, sender TEXT,
  sender_acc TEXT, vs TEXT, message TEXT, booked TEXT, code TEXT, person TEXT, oid TEXT, seen INTEGER DEFAULT 0);
"""


def db():
    con = data.db()
    con.executescript(SCHEMA)
    return con


def config():
    """Účet lampiček je v .env (PLATBY_UCET), ne v configu – repo je veřejné."""
    try:
        cfg = json.loads((HERE / "config.json").read_text())
    except (OSError, ValueError):
        cfg = {}
    for line in (HERE / ".env").read_text().splitlines() if (HERE / ".env").exists() else []:
        if line.startswith("PLATBY_UCET="):
            cfg["platbyUcet"] = line.split("=", 1)[1].strip()
    return cfg


def gmail(*args):
    out = subprocess.run(["node", str(GMAIL), *args], capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise RuntimeError(f"gmail: {(out.stderr or out.stdout)[:200]}")
    return json.loads(out.stdout)


def num(s):
    return float(s.replace("\xa0", "").replace(" ", "").replace(",", "."))


def parse(body):
    """Text upozornění Air Bank → slovník (None, když to není příchozí úhrada)."""
    m = re.search(r"na účtu (.+?) číslo (\S+) se zvýšil", body)
    p = re.search(r"Příchozí úhrada z účtu (.+?) číslo (\S+)", body)
    a = re.search(r"Částka:\s*([\d\s\xa0]+,\d{2})", body)
    if not (m and a):
        return None
    get = lambda pat: (re.search(pat, body).group(1).strip() if re.search(pat, body) else "")
    return {"account": m.group(1).strip(), "account_no": m.group(2).strip(), "amount": num(a.group(1)),
            "sender": p.group(1).strip() if p else "", "sender_acc": p.group(2).strip() if p else "",
            "vs": get(r"Variabilní symbol:\s*(\S+)"), "message": get(r"Zpráva pro příjemce:\s*(.+)"),
            "booked": get(r"Datum zaúčtování:\s*(\S+)"), "code": get(r"Kód transakce:\s*(\S+)")}


def fetch_server():
    """Platby ze serveru Lumao (čte stejná upozornění Air Bank). id je stálé → msgid „srv:<id>“."""
    import lumao  # noqa: PLC0415
    import requests  # noqa: PLC0415
    acc = (config().get("platbyUcet") or "").strip()
    od = time.strftime("%Y-%m-%d", time.localtime(time.time() - 45 * 86400))
    r = requests.get(lumao.url().rsplit("/", 1)[0] + "/platby", params={"od": od}, headers=lumao.headers(), timeout=30)
    r.raise_for_status()
    new = []
    with db() as con:
        known = {x[0] for x in con.execute("SELECT msgid FROM payments")}
        have = {(x[0], x[1]) for x in con.execute("SELECT code, amount FROM payments WHERE code<>''")}
        for p in r.json().get("platby") or []:
            mid = f"srv:{p['id']}"
            if mid in known or (p.get("kod_transakce") and (p["kod_transakce"], float(p["castka"])) in have):
                continue  # už máme (i z mailu)
            row = {"account": "lampicky", "account_no": acc, "amount": float(p["castka"]), "sender": p.get("jmeno") or "",
                   "sender_acc": p.get("ucet") or "", "vs": p.get("vs") or "", "message": p.get("zprava") or "",
                   "booked": p.get("datum") or "", "code": p.get("kod_transakce") or ""}
            con.execute("INSERT OR IGNORE INTO payments(msgid, ts, account, account_no, amount, sender, sender_acc, vs, message, "
                        "booked, code) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (mid, time.time(), row["account"], acc, row["amount"], row["sender"], row["sender_acc"], row["vs"],
                         row["message"], row["booked"], row["code"]))
            new.append(row)
    return new


def fetch():
    """Načte nová upozornění z mailu. Vrátí seznam nových plateb na účet lampiček."""
    acc = (config().get("platbyUcet") or "").strip()
    if not acc:
        return []  # dokud není nastavený účet lampiček, nic neukládat
    with db() as con:
        known = {r[0] for r in con.execute("SELECT msgid FROM payments")}
    new = []
    for m in gmail("search", QUERY, "50"):
        if m["id"] in known:
            continue
        body = gmail("get", m["id"]).get("body", "")
        p = parse(body)
        if not p:
            continue
        ours = acc in (p["account"], p["account_no"])
        if ours and p["code"]:
            with db() as con:
                if con.execute("SELECT 1 FROM payments WHERE code=? AND amount=?", (p["code"], p["amount"])).fetchone():
                    con.execute("INSERT OR IGNORE INTO payments(msgid, ts, account, person) VALUES (?,?,'jiny','-')", (m["id"], time.time()))
                    continue  # stejná platba už přišla ze serveru
        with db() as con:
            if ours:
                con.execute("INSERT OR IGNORE INTO payments(msgid, ts, account, account_no, amount, sender, sender_acc, vs, "
                            "message, booked, code) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (m["id"], time.time(), p["account"], p["account_no"], p["amount"], p["sender"], p["sender_acc"],
                             p["vs"], p["message"], p["booked"], p["code"]))
                new.append(p)
            else:  # jiný účet – jen poznamenat, že je přečtený (bez částky a jmen)
                con.execute("INSERT OR IGNORE INTO payments(msgid, ts, account, person) VALUES (?,?,'jiny','-')", (m["id"], time.time()))
    return new


def fold(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return set(re.findall(r"[a-z]{3,}", s))


def candidates():
    """Lidé, od kterých čekáme peníze: koncepty/objednávky pro server + konverzace (jméno → (oid, telefon))."""
    out = {}
    try:
        import lumao  # noqa: PLC0415
        for r in lumao.rows(("koncept", "k_odeslani", "zapsano", "chyba")):
            pl = json.loads(r[4])
            out[r[2]] = (r[3], re.sub(r"\D", "", (pl.get("zakaznik") or {}).get("telefon", ""))[-9:],
                         (pl.get("zakaznik") or {}).get("jmeno", ""))
    except Exception:  # noqa: BLE001
        pass
    with data.db() as con:
        for (person,) in con.execute("SELECT DISTINCT person FROM messages WHERE ts > ?", (time.time() - 60 * 86400,)):
            out.setdefault(person, ("", "", person))
    return out


def match():
    """Přiřadí nepřiřazené platby (jen účet lampiček). Vrátí [(platba, person)] nově přiřazené."""
    acc = (config().get("platbyUcet") or "").strip()
    if not acc:
        return []
    cands = candidates()
    done = []
    with db() as con:
        rows = con.execute("SELECT id, sender, vs, message, amount FROM payments WHERE person IS NULL "
                           "AND (account=? OR account_no=? OR account='lampicky')", (acc, acc)).fetchall()
    for pid, sender, vs, msg, amount in rows:
        hit = None
        for person, (oid, phone, jmeno) in cands.items():
            ref = f"{vs} {msg}"
            if (oid and oid in ref) or (phone and len(phone) == 9 and phone in re.sub(r"\D", "", ref)):
                hit = (person, oid)
                break
            names = fold(sender)
            if names and (len(names & fold(person)) >= 2 or len(names & fold(jmeno)) >= 2):
                hit = hit or (person, oid)
        if hit:
            with db() as con:
                con.execute("UPDATE payments SET person=?, oid=? WHERE id=?", (hit[0], hit[1], pid))
            done.append(({"sender": sender, "amount": amount}, hit[0]))
    return done


def tick(notify=None):
    new = []
    try:
        new += fetch_server()
    except Exception as e:  # noqa: BLE001
        print("platby ze serveru:", e, flush=True)
    new += [p for p in fetch() if (p["code"], p["amount"]) not in {(n["code"], n["amount"]) for n in new}]
    matched = match()
    if notify:
        for p in new:
            notify("Lampičky – přišla platba", f"{p['sender'] or '?'}: {p['amount']:,.0f} Kč".replace(",", " "))
        for p, person in matched:
            notify("Lampičky – platba přiřazena", f"{person}: {p['amount']:,.0f} Kč".replace(",", " "))


def recent(limit=30):
    acc = (config().get("platbyUcet") or "").strip()
    with db() as con:
        return con.execute("SELECT ts, booked, amount, sender, vs, message, person, oid FROM payments "
                           "WHERE account<>'jiny' AND (account=? OR account_no=? OR account='lampicky') ORDER BY id DESC LIMIT ?",
                           (acc, acc, limit)).fetchall()


def paid_by(person):
    with db() as con:
        return con.execute("SELECT amount, booked FROM payments WHERE person=? ORDER BY id", (person,)).fetchall()


if __name__ == "__main__":
    print("nové:", fetch())
    print("přiřazeno:", match())
    acc = config().get("platbyUcet")
    print("účet:", acc or "NENASTAVENO (config.json → platbyUcet)")
    with db() as con:
        for r in con.execute("SELECT booked, account, amount, sender, person FROM payments WHERE account<>'jiny' ORDER BY id DESC LIMIT 10"):
            print(r)
    sys.exit(0)
