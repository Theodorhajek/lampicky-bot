#!/bin/zsh
# Ovládání bota nezávisle na terminálu:  ./ovladani.sh start|stop|restart|stav [bot|dashboard]  (dashboard je zrušený – vše jde přes Telegram; spustí se jen výslovně)
cd "$(dirname "$0")"
PY=.venv/bin/python
what=${2:-bot}
pidof() { [ -f "logs/$1.pid" ] && kill -0 "$(cat logs/$1.pid)" 2>/dev/null && cat "logs/$1.pid"; }
start1() {
  if pidof $1 >/dev/null; then echo "$1 už běží (pid $(pidof $1))"; return; fi
  if [ $1 = bot ]; then nohup $PY -W ignore -u bot_telefon.py run >> logs/bot.out 2>&1 &
  else nohup $PY -W ignore -u dashboard.py >> logs/dashboard.out 2>&1 & fi
  echo $! > logs/$1.pid; echo "$1 spuštěn (pid $!)"
}
stop1() {
  p=$(pidof $1) && { kill $p; sleep 2; kill -0 $p 2>/dev/null && kill -9 $p; echo "$1 zastaven"; } || echo "$1 neběží"
  rm -f logs/$1.pid
}
stav1() { p=$(pidof $1) && echo "$1: běží (pid $p)" || echo "$1: NEBĚŽÍ"; }
for w in bot dashboard; do
  [ "$what" = all ] || [ "$what" = $w ] || continue
  case $1 in
    start) start1 $w;; stop) stop1 $w;; restart) stop1 $w; start1 $w;;
    stav|status) stav1 $w;; *) echo "použití: ./ovladani.sh start|stop|restart|stav [bot|dashboard]"; exit 2;;
  esac
done
[ "$1" = stav ] && { echo "--- poslední záznamy bota:"; tail -8 logs/bot.out 2>/dev/null | cut -c1-220; } || true
