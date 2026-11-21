# Bot na lampičky (Messenger / FB) – pro sezení, které ho ovládá (i z Telegramu)

Theodor prodává ručně dělané LED lampičky (690 Kč/ks, gravírované akrylové sklo na bukovém podstavci) přes FB profil „Martin Hejduk“.
Bot běží na Macu a ovládá telefon (Redmi, adb): odpovídá v Messengeru, pod příspěvky ve skupinách a postuje do skupin.

## Ovládání
- `./ovladani.sh stav|start|stop|restart [bot|dashboard]` – bot a dashboard (http://127.0.0.1:5005) běží na pozadí, log `logs/bot.out`, akce `log.csv`.
- `.venv/bin/python lampicky_cli.py …` – stav, konverzace, napsat zákazníkovi, vrátit/převzít konverzaci, objednávky, platby (viz `--help` v souboru).
- Noční pauza 01:15–05:50 českého času (`config.json` → `offHours`). Mac je v jiném časovém pásmu – vždy počítej s Europe/Prague.

## Pravidla (Theodorova, platí vždy)
- Zákazníkům nic nepiš bez jeho výslovného „pošli“ (nebo tlačítka Poslat). Do Shopify naostro (`poslat-objednavku`) jen po jeho „ano“.
- Automaticky (config `autoObjednavky`) odchází jen úplný koncept s jednoznačným výdejním místem, bez návrhu na míru, zaplacený (převod = celá částka, dobírka = poštovné). Ostatní koncepty čekají na Theodora.
- Když nevíš, nic nepiš a zeptej se ho. Nikdy nepiš „potvrdím“, „zjistím“, „ozvu se“.
- Styl odpovědí: krátce (1 věta, max 2–3 krátké zprávy), vykat (tykat jen tomu, kdo tyká nebo píše „Ahoj“), jen emoji 🙂,
  na potvrzení „OK“ / „Rozumím.“ (nikdy „Rozumím, děkuji“), nepotvrzovat města („pošlu kamkoliv v ČR/SK“), víc kusů = 690 × počet, doprava jednou.
- Fakta jen z `info.md` a `lekce.md`. Ceny, termíny a atypy rozhoduje Theodor.
- Osobní údaje zákazníků nikam neposílej a nedávej do gitu (repo je veřejné).
- Návrhy (obrázky) zákazníkům jen po jeho schválení. Platby si páruje sám (bot je jen ukazuje).
- K platebním údajům bot vždy pošle i QR platbu (`platba_qr.py`, číslo účtu jen v `.env`). U `lampicky_cli.py napis` se QR přidá samo, když zpráva obsahuje číslo účtu a částku „… Kč“.

## Soubory
- `bot_telefon.py` (hlavní smyčka), `fb.py` (komentáře, skupiny), `data.py` (SQLite `data/lampicky.db`), `dashboard.py`,
  `lumao.py` (objednávky → server Lumao → Shopify, `docs/lumao-api.md`, token v `.env`), `platby.py` (upozornění Air Bank z Gmailu),
  `navrh.py` (návrhy obrázků), `lekce.md` (lekce z oprav – nová lekce = nový řádek nahoru), `info.md` (fakta o prodeji).
