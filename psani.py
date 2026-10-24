"""Psaní jako člověk: text se do aktivního pole vkládá po znacích (občas po dvou) s náhodnými pauzami,
místo vložení celé zprávy naráz. Podporuje češtinu (háčky, čárky) i emoji.

Znaky jdou přes schránku → vložit na pozici kurzoru (normální klávesnice Gboard zůstává, nic se neinstaluje).
"""

import random
import time

def _delay(ch, prev):
    if ch in " \n":
        base = random.uniform(0.12, 0.35)
    elif ch in ".,!?;:":
        base = random.uniform(0.2, 0.5)
    else:
        base = random.uniform(0.06, 0.22)
    if prev and prev in ".!?" and ch == " ":
        base += random.uniform(0.3, 0.9)  # konec věty
    if random.random() < 0.04:
        base += random.uniform(0.5, 1.5)  # chvilka přemýšlení
    return base


def _chunks(text):
    """Většinou po jednom znaku, občas dva rychle za sebou (jako když člověk píše plynule). Emoji vcelku."""
    i = 0
    while i < len(text):
        n = 2 if random.random() < 0.25 and text[i:i + 2].isalpha() else 1
        if ord(text[i]) > 0xFFFF or (i + 1 < len(text) and text[i + 1] == "️"):
            n = 2 if i + 1 < len(text) and text[i + 1] == "️" else 1
        yield text[i:i + n]
        i += n


def human_type(d, text):
    """Napíše text do právě aktivního (focused) pole. Pole musí být předem aktivní (klepnuto)."""
    time.sleep(random.uniform(0.4, 0.9))
    prev = ""
    for chunk in _chunks(text):
        d.clipboard = chunk
        d.jsonrpc.pasteClipboard()
        time.sleep(_delay(chunk[-1], prev))
        prev = chunk[-1]
    d.clipboard = " "  # ve schránce nenechat poslední kus zprávy
    time.sleep(random.uniform(0.5, 1.2))  # chvilka, než „klepne“ na Odeslat
