# torrentdl

Egyszerű torrent letöltő Pythonban, [libtorrent](https://libtorrent.org/) motorral.
Egyszerre **egy** torrentet (vagy magnet linket) tölt, a háttérben fut, és
összeomlás után is folytatja onnan, ahol abbahagyta. Van grafikus felülete és
parancssori felülete is – mindkettő ugyanazt a háttérdémont vezérli.

![A program ablaka](docs/kepernyokep.png)

## Mit tud

- **Forrás**: magnet link, helyi `.torrent` fájl vagy `.torrent` URL.
- **Célkönyvtár**: minden letöltésnél megadható (`-d`).
- **Háttérben fut**: a parancs kiadása után a démon dolgozik tovább; a terminál bezárható.
- **Folytatás**: a haladás (fast-resume) rendszeresen mentésre kerül, így áramszünet,
  összeomlás vagy kilépés után a letöltés folytatódik, nem indul elölről.
- **Szüneteltetés**: azonnal vagy megadott időre (`--for 30m`), ami után magától folytatódik.
- **Megszakítás/törlés**: a letöltés eldobható, ha kell, a már letöltött fájlokkal együtt.
- **Épség-ellenőrzés**: ha elkészült, a program újraellenőrzi az összes darabot, és csak
  akkor jelenti késznek, ha minden fájl ép — utána **alapállapotba** áll (nem seedel tovább),
  jöhet a következő torrent.
- **DHT**, **PEX**, **LSD** (helyi hálózati peer-keresés) — alapból bekapcsolva.
- **Titkosítás** (MSE/PE protokoll-titkosítás) — alapból bekapcsolva, kikényszeríthető.

## Indítás Windows alatt

Kattints duplán az **`inditas.bat`** fájlra. Ez megkeresi a Pythont, és átadja
a vezérlést az `indit.py`-nak, ami:

1. létrehoz egy **saját virtuális környezetet a `.venv` mappában** (csak az első
   indításkor), így a libtorrent nem a rendszer Pythonjába kerül, és nem
   keveredik más programok csomagjaival,
2. abban ellenőrzi a függőségeket, és szükség esetén telepíti a libtorrentet,
3. elindítja az ablakot.

Ha nincs Python a gépen, a parancsfájl megmondja, honnan töltsd le. A `.venv`
mappa bármikor törölhető: a következő indításkor újra elkészül. Ha nem kéred a
virtuális környezetet, a `TORRENTDL_NINCS_VENV=1` környezeti változóval
kikapcsolható.

> **Melyik Python kell?** A `libtorrent`-hez a készítői a **Python 3.13-ig**
> adnak kész csomagot, ennél újabb Pythonhoz (pl. 3.14) nincs. Az indító ezt
> felismeri: ha talál a gépen egy alkalmas változatot, azzal készíti el a
> `.venv`-et; ha nincs ilyen, kiírja, hogy telepítsd a 3.13-at (a meglévő
> Python maradhat mellette).

## Indítás máshol

```bash
python3 indit.py                     # ugyanaz, mint Windowson: .venv + felület
```

vagy kézzel, virtuális környezet nélkül:

```bash
pip install -r requirements.txt      # libtorrent
python3 -m torrentdl gui             # grafikus felület
python3 -m torrentdl status          # parancssor
# vagy telepítve:
pip install .                        # utána: torrentdl ... / torrentdl-gui
```

Python 3.10–3.13 és tkinter szükséges (a felső határt a libtorrent szabja meg;
a tkinter a Windows-telepítő része, Linuxon a `python3-tk` csomag). A
parancssori rész tkinter nélkül is működik.

## A grafikus felület

- **Torrent / magnet** mező: beilleszthető magnet link, vagy `.torrent` fájl
  tallózható; **Célmappa**: ide kerülnek a fájlok (a program megjegyzi).
- **Letöltés indítása** – amíg fut egy letöltés, a gomb inaktív (egyszerre egy
  torrent tölthető).
- **Szünet**, **Szünet időre…** (pl. `45s`, `30m`, `2h`, `1h30m` – egység nélkül
  perc), **Folytatás**, **Megszakítás**, **Törlés a fájlokkal** (rákérdez).
- Alul a **napló** a háttérdémon üzeneteit mutatja, a státuszsor pedig a démon
  állapotát, a portot, a DHT/PEX és a titkosítás beállítását.
- A **Beállítások…** ablakban állítható a port, a sebességkorlátok, a
  kapcsolatszám, a titkosítás módja és a DHT/PEX/LSD/µTP; mentés után a démon
  újraindul, a letöltés pedig ott folytatódik, ahol tartott.
- Az ablak bezárása **nem** szakítja meg a letöltést: a háttérdémon tovább
  dolgozik, és az ablak újranyitásakor ott folytatódik a kijelzés.

## Használat parancssorból

```bash
# letöltés indítása (magnet link vagy .torrent fájl)
torrentdl add "magnet:?xt=urn:btih:..." -d /media/filmek
torrentdl add ~/Letoltesek/valami.torrent -d /media/filmek

# hol tart?
torrentdl status
torrentdl status -w            # folyamatosan frissülő nézet (Ctrl+C a kilépéshez)

# szüneteltetés
torrentdl pause                # amíg vissza nem indítod
torrentdl pause --for 45m      # 45 perc múlva magától folytatja (45s, 2h, 1h30m is jó)
torrentdl resume

# megszakítás
torrentdl cancel                    # a részben letöltött fájlok a helyükön maradnak
torrentdl cancel --delete-files     # a fájlokat is törli (rákérdez; -y kihagyja)

# háttérdémon
torrentdl daemon status
torrentdl daemon stop          # a letöltés állapota megmarad, később: torrentdl resume
torrentdl daemon log -n 50     # napló
```

Példa állapotkijelzés:

```
Név:      ubuntu-24.04.1-desktop-amd64.iso
Állapot:  letöltés
Haladás:  [############------------------]  41.3%
Méret:    2.42 GiB / 5.86 GiB
Sebesség: le 4.10 MiB/s | fel 512.00 KiB/s | peerek: 37 (seed: 12) | DHT node: 284
Hátralévő idő: 14p 20mp
Cél:      /media/filmek
```

## Beállítások

```bash
torrentdl config                                  # aktuális beállítások
torrentdl config --set max_download_rate=2048     # kB/s, 0 = korlátlan
torrentdl config --set encryption=forced
torrentdl daemon stop                             # a változások újraindítás után lépnek életbe
```

| kulcs | alapérték | jelentés |
|---|---|---|
| `listen_port` | 6881 | bejövő kapcsolatok portja |
| `enable_dht` | true | DHT (tracker nélküli peer-keresés) |
| `enable_pex` | true | Peer Exchange |
| `enable_lsd` | true | helyi hálózati peer-keresés |
| `enable_utp` | true | µTP transzport |
| `enable_upnp` / `enable_natpmp` | true | portnyitás a routeren |
| `encryption` | enabled | `disabled` / `enabled` (ha a peer is tudja) / `forced` (csak titkosítva) |
| `max_download_rate` | 0 | letöltési korlát kB/s-ban (0 = korlátlan) |
| `max_upload_rate` | 0 | feltöltési korlát kB/s-ban |
| `max_connections` | 200 | egyidejű kapcsolatok száma |
| `seed_after_complete` | false | ha `true`, kész letöltés után seedel tovább |
| `resume_save_interval` | 30 | ennyi másodpercenként menti a folytatási adatot |
| `idle_timeout` | 600 | ennyi tétlen másodperc után a démon kilép (0 = soha) |

## Hogyan működik

- A `torrentdl` parancsok egy háttérdémonnal beszélgetnek unix socketen keresztül;
  a démon az első `add` / `resume` parancsra magától elindul.
- Az adatkönyvtár alapból `~/.local/share/torrentdl` (felülírható a `TORRENTDL_HOME`
  környezeti változóval). Itt található:
  - `job.json` – az aktuális letöltés állapota (ezt olvassa a `status` akkor is, ha nem fut a démon)
  - `resume.dat` – a libtorrent folytatási adata (melyik darab van meg)
  - `current.torrent` – a torrent metaadatának másolata, hogy magnet link is folytatható legyen
  - `session.state` – DHT-node gyorsítótár, `last.json` – az utolsó befejezett letöltés
  - `daemon.log` – napló
- Amikor a letöltés eléri a 100%-ot, a program `force_recheck`-kel újraellenőrzi az összes
  darabot. Ha minden ép: a torrentet eltávolítja a session-ből, törli a munkaállományokat,
  és alapállapotba áll. Ha hibát talál, a hiányzó darabokat újratölti (legfeljebb kétszer,
  utána hibaállapotba kerül, és a `status` kiírja az okot).
- Egyszerre csak egy letöltés lehet aktív: ha van futó munka, az `add` hibaüzenettel elutasít.

## Fájlok

| fájl | mit csinál |
|---|---|
| `inditas.bat` | Windows-indító: megkeresi a Pythont, elindítja az `indit.py`-t |
| `indit.py` | `.venv` létrehozása, függőség-ellenőrzés (Python-verzió, tkinter, libtorrent), majd a felület indítása |
| `torrentdl/gui.py` | a grafikus felület (tkinter) |
| `torrentdl/cli.py` | a parancssori felület |
| `torrentdl/engine.py` | a háttérdémon: libtorrent session, állapotgép, vezérlőcsatorna |
| `torrentdl/client.py` | a démon indítása és vezérlése (ezt hívja a GUI és a CLI is) |
| `torrentdl/config.py`, `format.py`, `lock.py`, `protocol.py` | beállítások, formázás, egypéldány-zár, protokoll |

## Tesztek

Minden teszt hálózat nélkül fut:

```bash
python3 tests/futtato.py        # mind egyben
```

- `tests/smoke_test.py` – a letöltő motor: kész fájlok ellenőrzése és
  alapállapotba állás, szüneteltetés időzítéssel, `SIGKILL` utáni folytatás,
  törlés a fájlokkal.
- `tests/gui_test.py` – valódi Tk ablakkal: indítás, hibás forrás kezelése,
  gombok engedélyezése, szünet/folytatás, törlés (Linuxon `xvfb-run` kell hozzá,
  a futtató magától használja).
- `tests/bat_test.py` – a `.bat` fájl CRLF sorvégei, ASCII tartalma, `goto`
  címkéi (ezeken szokott elcsúszni a `cmd.exe`).
- `tests/indit_test.py` – az indító függőség-ellenőrzései, a `.venv` kezelése
  és a Python-verzió választása.

## Jogi megjegyzés

A program csak letöltőmotor; azért, hogy milyen tartalmat töltesz le vele, a felhasználó felel.
