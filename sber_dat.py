"""Sběr dat: projde konverzace v Messengeru (i starší, posouvá nahoru) a uloží zprávy do data/lampicky.db.
Nic neposílá. Spouštět, když bot neběží.

  .venv/bin/python -W ignore sber_dat.py [počet_chatů]
"""

import sys
import time

sys.argv = [sys.argv[0], "dry"] + sys.argv[1:]  # bot_telefon v režimu „nic neposílat“
import bot_telefon as b  # noqa: E402
import data  # noqa: E402


def read_history(screens=6):
    """Zprávy v otevřeném chatu včetně starších (posouvá nahoru a skládá podle překryvu)."""
    found = b.read_messages()
    for _ in range(screens):
        b.d.swipe(540, 700, 540, 1700, 0.3)
        time.sleep(1.5)
        older = b.read_messages()
        key = lambda m: (m["from"], m["text"])
        k = next((k for k in range(min(len(older), len(found)), 0, -1)
                  if [key(m) for m in older[-k:]] == [key(m) for m in found[:k]]), 0)
        if k == len(older):
            break  # začátek konverzace
        found = older[:len(older) - k] + found
    return found


def main():
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    b.connect()
    b.go_home()
    names, idle = [], 0
    while len(names) < limit and idle < 2:
        new = [c["name"] for c in b.read_list() if c["name"] not in names]
        names += new
        idle = 0 if new else idle + 1
        b.d.swipe(540, 1900, 540, 900, 0.3)
        time.sleep(2)
    print(f"{len(names)} konverzací")
    for name in names[:limit]:
        try:
            b.go_home()
            for _ in range(8):  # chat může být níž v seznamu
                if b.d(descriptionStartsWith=f"{name}, ").exists:
                    break
                b.d.swipe(540, 1900, 540, 900, 0.3)
                time.sleep(1.5)
            b.open_chat(name)
            msgs = read_history()
            data.save_conversation(name, msgs, b.thread(name)["botSent"] if name in b.state["threads"] else [])
            print(f"  {name}: {len(msgs)} zpráv")
        except Exception as e:  # noqa: BLE001
            print(f"  {name}: CHYBA {e}")
    b.leave_app()


if __name__ == "__main__":
    main()
