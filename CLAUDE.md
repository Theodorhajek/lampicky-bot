# Bot na lampičky (Messenger / FB) – pro sezení, které ho ovládá (i z Telegramu)

Theodor prodává ručně dělané LED lampičky (690 Kč/ks, gravírované akrylové sklo na bukovém podstavci) přes FB profil „Martin Hejduk“.
Bot běží na Macu a ovládá telefon (Redmi, adb): odpovídá v Messengeru, pod příspěvky ve skupinách a postuje do skupin.

## Ovládání
- `./ovladani.sh stav|start|stop|restart` – bot běží na pozadí, log `logs/bot.out`, akce `log.csv`. Dashboard je zrušený (Theodor 23. 11.) – všechno se dělá přes Telegram a `lampicky_cli.py`.
- `.venv/bin/python lampicky_cli.py …` – `prehled` (co čeká na Theodora), konverzace, napsat zákazníkovi, vrátit/převzít konverzaci, návrhy (`navrhy`, `poslat-navrh`, `zamitnout-navrh`), objednávky (`objednavka`, `upravit-objednavku`, `poslat-objednavku`), platby, `lekce`, `skupiny`, `odkaz-skupiny` (viz docstring souboru).
- Návrhy obrázků a koncepty objednávek chodí do Telegramu jako vlastní vlákna s tlačítky; ovládací vlákno „🪔 Lampičky – ovládání“ je trvalé.
- Lekce z oprav: když Theodor opraví odpověď nebo postup, přidej lekci (`lampicky_cli.py lekce "…"`) – bot ji čte u každé odpovědi.
- Záložní internet: když Redmi nemá internet, bot sám zapne internet z Macu přes USB (`tethering.py`, gnirehtet, jen pro tento telefon; ručně `tethering.py start|stop|stav`). Každých 15 min zkusí, jestli už jde Wi-Fi, a tethering vypne.
- Noční pauza 01:15–05:00 českého času (`config.json` → `offHours`). Mac je v jiném časovém pásmu – vždy počítej s Europe/Prague.

## Pravidla (Theodorova, platí vždy)
- Zákazníkům nic nepiš bez jeho výslovného „pošli“ (nebo tlačítka Poslat). Do Shopify naostro (`poslat-objednavku`) jen po jeho „ano“.
- Automaticky (config `autoObjednavky`) odchází jen úplný koncept s jednoznačným výdejním místem, bez návrhu na míru, zaplacený (převod = celá částka, dobírka = poštovné). Ostatní koncepty čekají na Theodora.
- Když nevíš, nic nepiš a zeptej se ho. Nikdy nepiš „potvrdím“, „zjistím“, „ozvu se“.
- Styl odpovědí: krátce (1 věta, max 2–3 krátké zprávy), vykat (tykat jen tomu, kdo tyká nebo píše „Ahoj“), jen emoji 🙂,
  na potvrzení „OK“ / „Rozumím.“ (nikdy „Rozumím, děkuji“), nepotvrzovat města („pošlu kamkoliv v ČR/SK“), víc kusů = 690 × počet, doprava jednou.
- Fakta jen z `info.md` a `lekce.md`. Ceny, termíny a atypy rozhoduje Theodor.
- Osobní údaje zákazníků nikam neposílej a nedávej do gitu (repo je veřejné).
- Návrhy (obrázky) zákazníkům jen po jeho schválení. Platby si páruje sám (bot je jen ukazuje).
- K platebním údajům bot vždy pošle i QR platbu (`platba_qr.py`, číslo účtu jen v `.env`). U `lampicky_cli.py napis` se QR přidá samo, když zpráva obsahuje číslo účtu (nebo „QR“) a částku „… Kč“. Když zákazník chtěl QR kód, číslo účtu vůbec nepiš – jen částku a „posílám QR kód“.

## Soubory
- `bot_telefon.py` (hlavní smyčka), `fb.py` (komentáře, skupiny), `data.py` (SQLite `data/lampicky.db`), `dashboard.py`,
  `lumao.py` (objednávky → server Lumao → Shopify, `docs/lumao-api.md`, token v `.env`), `platby.py` (upozornění Air Bank z Gmailu),
  `navrh.py` (návrhy obrázků), `lekce.md` (lekce z oprav – nová lekce = nový řádek nahoru), `info.md` (fakta o prodeji).
