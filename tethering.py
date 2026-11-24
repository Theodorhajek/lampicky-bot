"""Záložní internet pro telefon přes USB z Macu (gnirehtet – „obrácený tethering“, bez rootu).

Jen pro tento jeden telefon (Redmi, sériové číslo níže). Další telefony by se zapínaly ručně.
Zapne se, jen když telefon nemá vlastní internet (Wi-Fi / data); jednou za čas se zkusí vypnout,
jestli už Wi-Fi zase jede. Telefon pak vidí VPN (klíček) – Android to aplikacím hlásí.

Instalace (už hotová): ~/.local/jre (Temurin JRE 21), ~/.local/gnirehtet/gnirehtet-java (v2.5.1),
aplikace gnirehtet v telefonu. Poprvé je na telefonu potřeba ručně povolit „Připojení VPN“.
Ručně: .venv/bin/python tethering.py start|stop|stav
"""

import os
import subprocess
import sys
import time
from pathlib import Path

SERIAL = "8871b0a1"  # Redmi Note 9 Pro – jen tento telefon
HOME = Path.home()
JAVA = HOME / ".local/jre/Contents/Home/bin/java"
JAR = HOME / ".local/gnirehtet/gnirehtet-java/gnirehtet.jar"
ADB = HOME / ".local/android/platform-tools/adb"
LOGS = Path(__file__).resolve().parent / "logs"
PIDFILE = LOGS / "gnirehtet.pid"
RECHECK = 15 * 60  # jak často zkusit, jestli už telefon má zase vlastní internet

_since = 0.0  # kdy se tethering naposledy zapnul


def available():
    return JAVA.exists() and JAR.exists() and SERIAL in _adb("get-serialno")


def _adb(*args, timeout=20):
    try:
        return subprocess.run([str(ADB), "-s", SERIAL, *args], capture_output=True, text=True, timeout=timeout).stdout
    except Exception:  # noqa: BLE001
        return ""


def _gn(*args, timeout=30):
    env = dict(os.environ, ADB=str(ADB))
    return subprocess.run([str(JAVA), "-jar", str(JAR), *args], capture_output=True, text=True, timeout=timeout, env=env)


def online():
    """Má telefon internet? HTTP dotaz z telefonu (ping přes tethering nefunguje – gnirehtet neumí ICMP)."""
    out = _adb("shell", "curl", "-s", "-m", "6", "-o", "/dev/null", "-w", "%{http_code}",
               "http://connectivitycheck.gstatic.com/generate_204", timeout=20).strip()
    return out == "204"


def active():
    """Běží v telefonu VPN gnirehtetu?"""
    return "tun0" in _adb("shell", "ip", "-o", "link")


def _relay_running():
    try:
        pid = int(PIDFILE.read_text())
        os.kill(pid, 0)
        return True
    except Exception:  # noqa: BLE001
        return False


def start():
    """Zapnout tethering (relay na Macu + VPN v telefonu). True = telefon má internet."""
    global _since
    LOGS.mkdir(exist_ok=True)
    if not _relay_running():
        env = dict(os.environ, ADB=str(ADB))
        with open(LOGS / "gnirehtet.out", "a") as out:
            p = subprocess.Popen([str(JAVA), "-jar", str(JAR), "relay"], stdout=out, stderr=out, env=env,
                                 start_new_session=True)
        PIDFILE.write_text(str(p.pid))
        time.sleep(2)
    _gn("start", SERIAL)  # adb reverse + spustí VPN v telefonu
    _since = time.time()
    for _ in range(8):
        time.sleep(2)
        if active() and online():
            return True
    return False


def stop():
    global _since
    _gn("stop", SERIAL)
    _adb("reverse", "--remove", "localabstract:gnirehtet")
    try:
        os.kill(int(PIDFILE.read_text()), 15)
    except Exception:  # noqa: BLE001
        pass
    PIDFILE.unlink(missing_ok=True)
    _since = 0.0


def ensure(say=print):
    """Volá hlavní smyčka bota. Vrací True, když má telefon internet (vlastní nebo přes Mac)."""
    if online():
        if active() and time.time() - _since > RECHECK:  # jede přes Mac – nejde už zase Wi-Fi?
            stop()
            time.sleep(8)
            if online():
                say("tethering vypnut – telefon má zase vlastní internet")
                return True
            say("Wi-Fi pořád nejde – tethering zase zapínám")
            return start()
        return True
    if not available():
        return False
    say("telefon je bez internetu – zapínám internet z Macu přes USB (gnirehtet)")
    ok = start()
    say("tethering běží, telefon má internet" if ok else "tethering se nepovedl (povolená VPN v telefonu? má Mac internet?)")
    return ok


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stav"
    if cmd == "start":
        print("internet OK" if start() else "telefon pořád bez internetu")
    elif cmd == "stop":
        stop()
        print("vypnuto")
    else:
        print(f"VPN v telefonu: {'ano' if active() else 'ne'}, relay: {'běží' if _relay_running() else 'neběží'}, "
              f"internet v telefonu: {'ano' if online() else 'NE'}")
