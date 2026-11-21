"""Ovládání bota z příkazové řádky (používá ho i sezení v Telegramu).

  .venv/bin/python lampicky_cli.py stav                     – běží bot? poslední akce, předané konverzace
  .venv/bin/python lampicky_cli.py konverzace "Jméno"       – posledních 25 zpráv + co o něm bot zapsal
  .venv/bin/python lampicky_cli.py napis "Jméno" "text"     – bot napíše zprávu v telefonu (prázdný řádek = další zpráva)
  .venv/bin/python lampicky_cli.py botovi "Jméno"           – bot zase odpovídá sám
  .venv/bin/python lampicky_cli.py prevzit "Jméno"          – bot mlčí, převezmeš to ty
  .venv/bin/python lampicky_cli.py fronta                   – co čeká na odeslání z dashboardu/Telegramu
  .venv/bin/python lampicky_cli.py objednavky               – objednávky pro server (Shopify) a jejich stav
  .venv/bin/python lampicky_cli.py test-objednavky ID       – ověřit objednávku na serveru (test, nic se nezapíše)
  .venv/bin/python lampicky_cli.py poslat-objednavku ID     – NAOSTRO do Shopify (jen po Theodorově „ano“)
  .venv/bin/python lampicky_cli.py platby                   – poslední příchozí platby
  .venv/bin/python lampicky_cli.py telegram zap|vyp         – zapnout / úplně vypnout napojení na Telegram
"""

import subprocess
import sys
import time
from pathlib import Path

import data

HERE = Path(__file__).resolve().parent


def main():
    a = sys.argv[1:] or ["stav"]
    cmd = a[0]
    if cmd == "stav":
        print(subprocess.run([str(HERE / "ovladani.sh"), "stav"], capture_output=True, text=True).stdout)
        log = HERE / "log.csv"
        if log.exists():
            lines = log.read_text(encoding="utf-8").splitlines()
            print("--- předáno tobě (posledních 10):")
            for l in [x for x in lines if "PŘEDÁNO" in x or "ručně" in x.lower()][-10:]:
                print(l[:220])
    elif cmd == "konverzace":
        person = a[1]
        with data.db() as con:
            rows = con.execute("SELECT direction, text FROM messages WHERE person=? ORDER BY id DESC LIMIT 25", (person,)).fetchall()
        for d_, t in reversed(rows):
            print({"in": "Zákazník", "bot": "Bot", "manual": "Theodor"}.get(d_, d_) + ":", t)
        print("--- komentáře:", data.comments_of(person) or "žádné")
        log = HERE / "log.csv"
        if log.exists():
            print("--- log bota:")
            for l in [x for x in log.read_text(encoding="utf-8").splitlines() if person in x][-8:]:
                print(l[:220])
    elif cmd == "napis":
        data.outbox_add("zprava", a[1], a[2])
        print("zařazeno – bot to napíše při nejbližší kontrole (do pár minut, mimo noční pauzu 01:15–05:50)")
    elif cmd in ("botovi", "prevzit"):
        data.outbox_add(cmd, a[1])
        print("zařazeno")
    elif cmd == "fronta":
        with data.db() as con:
            for r in con.execute("SELECT id, kind, person, status, coalesce(error,''), substr(text,1,80) FROM outbox ORDER BY id DESC LIMIT 15"):
                print(r)
    elif cmd == "objednavky":
        import lumao
        for r in lumao.rows(tuple(lumao.STATES)):
            print(f"#{r[0]} {r[3]} {r[2]} – {lumao.STATES.get(r[6], r[6])} {r[7] or ''} {lumao.SERVER_STATES.get(r[8] or '', r[8] or '')} {r[10] or ''}")
            print("   ", r[4].replace("\n", " ")[:400])
    elif cmd in ("test-objednavky", "poslat-objednavku"):
        import lumao
        ok, msg = lumao.send(int(a[1]), test=cmd.startswith("test"))
        print("OK" if ok else "CHYBA", msg)
    elif cmd == "platby":
        import platby
        for r in platby.recent(15):
            print(time.strftime("%d.%m.", time.localtime(r[0])), r[1:])
    elif cmd == "telegram":
        import json
        c = HERE / "config.json"
        cfg = json.loads(c.read_text())
        if len(a) > 1:
            cfg["telegram"] = a[1] in ("zap", "on", "1", "ano")
            c.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
        print("Telegram:", "ZAPNUTO" if cfg.get("telegram") else "VYPNUTO (bot nic do Telegramu neposílá)")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
