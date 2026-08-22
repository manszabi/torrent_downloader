"""A grafikus felület ellenőrzése valódi Tk ablakkal, hálózat nélkül.

Linuxon fejkiszolgáló (Xvfb) kell hozzá:

    xvfb-run -a python3 tests/gui_test.py

Windowson egyszerűen: python tests\\gui_test.py
"""

from __future__ import annotations

import os
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MUNKA = Path(tempfile.mkdtemp(prefix="torrentdl-gui-"))
os.environ["TORRENTDL_HOME"] = str(MUNKA / "home")

import libtorrent as lt  # noqa: E402

from torrentdl import client  # noqa: E402
from torrentdl import config as cfgmod  # noqa: E402
from torrentdl.format import human_bytes, human_time, kimenet_utf8  # noqa: E402

kimenet_utf8()
from torrentdl.gui import (  # noqa: E402
    KONZOL_JELZO,
    App,
    demon_inditas_kell,
    gomb_allapotok,
    konzol_elengedes,
    tcl_kornyezet,
)

HIBAK = []


def check(nev, kapott, vart):
    if kapott == vart:
        print(f"ok    {nev}")
    else:
        HIBAK.append(nev)
        print(f"HIBA  {nev}: kapott={kapott!r} vart={vart!r}")


def porgeti(app, masodperc=1.0):
    """Az ablakot életben tartva vár (az események így lefutnak)."""
    veg = time.time() + masodperc
    while time.time() < veg:
        app.update()
        time.sleep(0.02)


def var(app, felteltel, masodperc=45, mit=""):
    veg = time.time() + masodperc
    while time.time() < veg:
        app.update()
        if felteltel():
            return True
        time.sleep(0.05)
    HIBAK.append(f"időtúllépés: {mit}")
    print(f"HIBA  időtúllépés: {mit}")
    return False


def torrent_keszit(forras: Path, cel: Path) -> Path:
    forras.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(99)
    (forras / "adat.bin").write_bytes(bytes(rnd.getrandbits(8) for _ in range(200_000)))
    fs = lt.file_storage()
    lt.add_files(fs, str(forras))
    t = lt.create_torrent(fs, piece_size=16 * 1024)
    lt.set_piece_hashes(t, str(forras.parent))
    cel.write_bytes(lt.bencode(t.generate()))
    return cel


def allapot():
    return cfgmod.read_json(cfgmod.path(cfgmod.JOB_NAME))


def main() -> int:
    # --- Tk nélkül is ellenőrizhető logika
    check("gombok üres állapotban", gomb_allapotok({"job": None})["indit"], True)
    check(
        "letöltés közben nincs új indítás",
        gomb_allapotok({"job": {"state": "downloading"}})["indit"],
        False,
    )
    check(
        "szüneteltetve folytatható",
        gomb_allapotok({"job": {"state": "paused"}})["folytat"],
        True,
    )
    check(
        "megosztás közben indítható új letöltés",
        gomb_allapotok({"job": {"state": "seeding"}})["indit"],
        True,
    )
    check(
        "ellenőrzés gomb megosztás közben is aktív",
        gomb_allapotok({"job": {"state": "seeding"}})["ellenoriz"],
        True,
    )
    check(
        "ellenőrzés gomb munka nélkül inaktív",
        gomb_allapotok({"job": None})["ellenoriz"],
        False,
    )

    # --- Megszakadt letöltés: a felület magától elindítja a háttérdémont.
    # (Összeomlás vagy gépleállás után a mentett állapot megvan, de a démon áll.)
    megszakadt = {"daemon": False, "job": {"state": "downloading"}}
    check("megszakadt letöltéshez elindítjuk a démont", demon_inditas_kell(megszakadt), True)
    check(
        "amíg indul, nem indítjuk újra",
        demon_inditas_kell(megszakadt, inditas_fut=True),
        False,
    )
    check(
        "sikertelen indítás után nem próbálkozunk körbe-körbe",
        demon_inditas_kell(megszakadt, mar_probaltuk=True),
        False,
    )
    check(
        "szüneteltetett munkát nem indítunk el magunktól",
        demon_inditas_kell({"daemon": False, "job": {"state": "paused"}}),
        False,
    )
    check(
        "hibára futott munkát sem",
        demon_inditas_kell({"daemon": False, "job": {"state": "error"}}),
        False,
    )
    check(
        "futó démonhoz nincs teendő",
        demon_inditas_kell({"daemon": True, "job": {"state": "downloading"}}),
        False,
    )
    check("munka nélkül nincs mit indítani", demon_inditas_kell({"daemon": False}), False)

    # A gombok is igazodnak ahhoz, hogy fut-e a démon.
    check("álló démonnál a Folytatás aktív", gomb_allapotok(megszakadt)["folytat"], True)
    check("álló démonnál a Szünet inaktív", gomb_allapotok(megszakadt)["szunet"], False)
    check(
        "futó démonnál a Szünet aktív",
        gomb_allapotok({"daemon": True, "job": {"state": "downloading"}})["szunet"],
        True,
    )

    # Windowson a .venv-ből induló Python nem találja magától a Tcl/Tk-t.
    with tempfile.TemporaryDirectory() as alap:
        os.makedirs(os.path.join(alap, "tcl", "tcl8.6"))
        os.makedirs(os.path.join(alap, "tcl", "tk8.6"))
        regi = {k: os.environ.pop(k, None) for k in ("TCL_LIBRARY", "TK_LIBRARY")}
        try:
            check(
                "Windowson beállítja a Tcl/Tk könyvtárat",
                tcl_kornyezet(base=alap, windows=True),
                {
                    "TCL_LIBRARY": os.path.join(alap, "tcl", "tcl8.6"),
                    "TK_LIBRARY": os.path.join(alap, "tcl", "tk8.6"),
                },
            )
            os.environ.pop("TCL_LIBRARY", None)
            os.environ.pop("TK_LIBRARY", None)
            check("máshol nem nyúl hozzá", tcl_kornyezet(base=alap, windows=False), {})
            os.environ["TCL_LIBRARY"] = "/mar/be/van/allitva"
            check("meglévő beállítást nem ír felül", tcl_kornyezet(base=alap, windows=True), {})
        finally:
            for kulcs, ertek in regi.items():
                os.environ.pop(kulcs, None)
                if ertek is not None:
                    os.environ[kulcs] = ertek

    # Az indító konzolját az ablak megnyitása után végleg elengedjük: amíg a
    # folyamathoz tartozik, a Windows visszahozhatja a felület elé (például
    # amikor beállítás mentésekor újraindul a háttérdémon).
    hivasok = []
    regi_jelzo = os.environ.pop(KONZOL_JELZO, None)
    try:
        check(
            "saját terminálból indítva nem válunk le",
            konzol_elengedes(windows=True, levalaszto=lambda: hivasok.append(1) or True),
            False,
        )
        check("ilyenkor semmit nem hívunk", hivasok, [])
        os.environ[KONZOL_JELZO] = "1"
        check(
            "az indító konzolját elengedjük",
            konzol_elengedes(windows=True, levalaszto=lambda: hivasok.append(1) or True),
            True,
        )
        check("egyszer, a leválasztóval", hivasok, [1])
        check(
            "Windowson kívül nincs mit tenni",
            konzol_elengedes(windows=False, levalaszto=lambda: hivasok.append(1) or True),
            False,
        )
        check("máshol sem hívunk semmit", hivasok, [1])
    finally:
        os.environ.pop(KONZOL_JELZO, None)
        if regi_jelzo is not None:
            os.environ[KONZOL_JELZO] = regi_jelzo

    check("méret formázás", human_bytes(1536), "1.50 KiB")
    check("idő formázás", human_time(3725), "1ó 2p")

    cfgmod.save_config(
        dict(cfgmod.load_config(), listen_port=random.randint(20000, 40000), idle_timeout=30)
    )

    torrent = torrent_keszit(MUNKA / "adatok" / "csomag", MUNKA / "teszt.torrent")
    celmappa = MUNKA / "cel"
    celmappa.mkdir()
    # A fájlok már a helyükön vannak: a letöltés az ellenőrzéssel azonnal kész lesz.
    shutil.copytree(MUNKA / "adatok" / "csomag", celmappa / "csomag")

    app = App()
    porgeti(app, 0.5)
    check("ablak címe", app.title(), "Torrent letöltő")
    check("induláskor nincs munka", app.megszakit_gomb.instate(["disabled"]), True)

    # --- hibás forrás: hibaüzenet, nem indul letöltés
    app.forras_valtozo.set("ez-nem-letezik.torrent")
    app.cel_valtozo.set(str(celmappa))
    hibak_elott = list(HIBAK)
    import tkinter.messagebox as mb

    eredeti = mb.showerror
    latott = []
    mb.showerror = lambda *a, **k: latott.append(a)
    app._inditas()
    porgeti(app, 0.3)
    mb.showerror = eredeti
    check("hibás forrásnál hibaüzenet", bool(latott), True)
    check("hibás forrásnál nincs munka", allapot(), None)
    HIBAK[:] = hibak_elott

    # --- valódi indítás: a démon elindul, a fájlok ellenőrzése lefut
    app.forras_valtozo.set(str(torrent))
    app.cel_valtozo.set(str(celmappa))
    app._inditas()
    check("indítás után a mentett célmappa",
          (cfgmod.read_json(cfgmod.path(cfgmod.GUI_NAME)) or {}).get("cel"), str(celmappa))
    var(app, lambda: client.ping() is not None, 40, "a háttérdémon elindulása")
    var(app, lambda: allapot() is not None or app.utolso_naplo, 40, "a letöltés megjelenése")
    porgeti(app, 1.5)
    check("letöltés közben a szünet gomb aktív vagy már kész",
          app.szunet_gomb.instate(["!disabled"]) or allapot() is None, True)

    # --- kész: alapállapot, a napló és a felirat frissül
    var(app, lambda: allapot() is None and cfgmod.read_json(cfgmod.path(cfgmod.LAST_NAME)),
        60, "az ellenőrzés befejezése és alapállapot")
    porgeti(app, 1.2)
    check("kész letöltés után nincs aktív munka", app.megszakit_gomb.instate(["disabled"]), True)
    check("kész letöltés után indítható új", app.indit_gomb.instate(["!disabled"]), True)
    check("a napló megjelent az ablakban", "démon elindult" in app.utolso_naplo, True)
    check("a felirat a kész letöltésről szól",
          "elkészült" in app.allapot_cimke.cget("text"), True)

    # --- új letöltés indítása, majd szüneteltetés és megszakítás
    masik = torrent_keszit(MUNKA / "adatok2" / "masik", MUNKA / "masik.torrent")
    app.forras_valtozo.set(str(masik))
    app.cel_valtozo.set(str(MUNKA / "cel2"))
    app._inditas()
    var(app, lambda: allapot() is not None, 40, "a második letöltés megjelenése")
    app._szunet()
    var(app, lambda: (allapot() or {}).get("state") == "paused", 20, "szüneteltetés")
    porgeti(app, 1.2)
    check("szünet után a folytatás gomb aktív", app.folytat_gomb.instate(["!disabled"]), True)
    app._folytatas()
    var(app, lambda: (allapot() or {}).get("state") == "downloading", 20, "folytatás")

    import tkinter.messagebox as mb2

    eredeti_kerdes = mb2.askyesno
    mb2.askyesno = lambda *a, **k: True
    app._megszakitas(True)
    mb2.askyesno = eredeti_kerdes
    var(app, lambda: allapot() is None, 30, "megszakítás a fájlokkal")
    maradek = [p for p in (MUNKA / "cel2").rglob("*") if p.is_file()]
    check("a fájlok törlődtek", maradek, [])

    app.destroy()
    client.stop_daemon()

    if HIBAK:
        print(f"\n{len(HIBAK)} ellenőrzés bukott el: {', '.join(HIBAK)}")
        print(f"munkakönyvtár: {MUNKA}")
        return 1
    shutil.rmtree(MUNKA, ignore_errors=True)
    print("\nA grafikus felület tesztjei rendben.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
