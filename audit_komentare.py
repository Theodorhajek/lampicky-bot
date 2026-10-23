"""Audit komentářů: projde všechna upozornění na komentáře pod mými příspěvky, otevře každý příspěvek
a vypíše všechny komentáře + jestli na ně Martin odpověděl. Nic nepíše ani neodesílá.

  .venv/bin/python audit_komentare.py          – výpis do konzole + audit_komentare.json
"""

import json
import sys
from pathlib import Path

import uiautomator2 as u2

import fb

HERE = Path(__file__).resolve().parent


def main():
    fb.d = u2.connect()
    fb.open_notifications()
    notifs = fb.all_comment_notifications(int(sys.argv[1]) if len(sys.argv) > 1 else 16)
    state = json.loads((HERE / "state_telefon.json").read_text(encoding="utf-8"))
    done = state.get("comments", {})
    report = []
    for n in notifs:
        print(f"\n=== {n['key']}")
        try:
            fb.open_notification(n["key"])
            comments = fb.read_post_comments()
        except Exception as e:  # noqa: BLE001
            print("   !! nepovedlo se otevřít:", e)
            report.append({"notif": n["key"], "error": str(e)})
            continue
        for c in comments:
            key = f"{c['name']}|{c['text'][:80]}"
            status = ("MARTIN" if c["name"] == fb.ME else "odpovězeno" if c["answered"] else
                      "bot-vyřízeno" if key in done else "STARÉ" if fb.too_old(c.get("age")) else "!!! BEZ ODPOVĚDI")
            print(f"   {'    ' if c['indent'] else ''}[{status}] {c['name']} ({c.get('age')}): {c['text'][:90]}")
            report.append({"notif": n["key"], **c, "status": status, "done_note": done.get(key, "")})
    (HERE / "audit_komentare.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
