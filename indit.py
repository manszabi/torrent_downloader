#!/usr/bin/env python3
"""Indito: megnezi, hogy minden fuggoseg megvan-e, es elinditja a feluletet.

A Windows parancsfajl (inditas.bat) szandekosan csak annyit csinal, hogy
megkeresi a Pythont, es atadja a vezerlest ennek a fajlnak. Minden ellenorzes
itt van, mert a Python kodot lehet tesztelni - a cmd.exe-t nem.

Ez a fajl szandekosan regi Python nyelvtannal keszult (nincs f-string, nincs
tipus-annotacio), hogy egy tul regi Python is le tudja forditani, es ertheto
uzenetet tudjon adni a verziorol - a tobbi fajl mar a mai nyelvtant hasznalja,
azokat egy regi Python el sem tudna olvasni.

Kimenete szandekosan ekezet nelkuli: a Windows parancssor a rendszer
kodlapjaval ir, es az ekezetes betuk ott konnyen szemette valnak.
"""

import os
import subprocess
import sys

MIN_VERZIO = (3, 10)

# A libtorrent csomaghoz eddig a Python-verzioig van kesz (leforditott) csomag
# a PyPI-n. Ujabb Python alatt a "pip install libtorrent" nem talal semmit -
# ilyenkor egy masik, telepitett Pythonra valtunk. Ha kesobb megjelenik az
# ujabb csomag, itt eleg atirni a szamot.
CSOMAG_MAX_VERZIO = (3, 13)

# Ezeket a valtozatokat keressuk a gepen, ha valtani kell (a legujabbtol).
KERESETT_VERZIOK = ["3.13", "3.12", "3.11", "3.10"]

# Ezzel jelezzuk a masik Pythonnak, hogy mar valtottunk egyszer.
VALTAS_JELZO = "TORRENTDL_INDITO_VALTOTT"

LETOLTES_URL = "https://www.python.org/downloads/"

# A program ezeket hasznalja a Python szabvany konyvtarabol.
ALAP_MODULOK = ["json", "socket", "threading", "queue", "selectors", "logging",
                "argparse", "secrets", "hmac", "subprocess", "urllib.request",
                "urllib.parse", "tempfile", "contextlib", "signal"]

# A program sajat fajljai.
SAJAT_FAJLOK = [os.path.join("torrentdl", "__init__.py"),
                os.path.join("torrentdl", "cli.py"),
                os.path.join("torrentdl", "engine.py"),
                os.path.join("torrentdl", "gui.py"),
                os.path.join("torrentdl", "client.py"),
                os.path.join("torrentdl", "config.py"),
                os.path.join("torrentdl", "format.py"),
                os.path.join("torrentdl", "lock.py"),
                os.path.join("torrentdl", "protocol.py")]

ITT = os.path.dirname(os.path.abspath(__file__))


def keret(szoveg):
    print("=" * 60)
    print("  " + szoveg)
    print("=" * 60)
    print("")


def verzio_gond():
    """Uzenet, ha tul regi a Python; kulonben None."""
    if sys.version_info[:2] < MIN_VERZIO:
        return ("Tul regi Python: %s (legalabb %d.%d kell)."
                % (sys.version.split()[0], MIN_VERZIO[0], MIN_VERZIO[1]))
    return None


def hianyzo_modulok(modulok=None):
    """A felsorolt modulok kozul melyik nem importalhato."""
    hianyzik = []
    for modul in (ALAP_MODULOK if modulok is None else modulok):
        try:
            __import__(modul)
        except ImportError:
            hianyzik.append(modul)
    return hianyzik


def hianyzo_fajlok(gyoker=None):
    """A program sajat fajljai kozul melyik hianyzik."""
    gyoker = ITT if gyoker is None else gyoker
    return [nev for nev in SAJAT_FAJLOK
            if not os.path.isfile(os.path.join(gyoker, nev))]


def csomag_sorok(utvonal):
    """A requirements.txt valodi csomag-sorai (a megjegyzesek nelkul).

    A kodolast kotelezo megadni: e nelkul a rendszer kodlapja dontene, es a
    fajl egy ekezetes megjegyzestol elszallna."""
    try:
        fh = open(utvonal, encoding="utf-8")
    except (OSError, ValueError):
        return []
    try:
        sorok = fh.read().splitlines()
    finally:
        fh.close()
    return [s.strip() for s in sorok if s.strip() and not s.strip().startswith("#")]


def _futtat(parancs):
    try:
        return subprocess.call(parancs) == 0
    except OSError:
        return False


def _futtat_csendben(parancs):
    """Mint a _futtat, de a gyermek kimenetet elnyeljuk (probalkozasokhoz)."""
    try:
        semmi = open(os.devnull, "w")
    except OSError:
        return _futtat(parancs)
    try:
        return subprocess.call(parancs, stdout=semmi, stderr=semmi) == 0
    except OSError:
        return False
    finally:
        semmi.close()


def _kimenet(parancs):
    """A parancs kimenete szovegkent, vagy ures szoveg, ha nem futtathato."""
    try:
        nyers = subprocess.check_output(parancs, stderr=subprocess.STDOUT)
    except (OSError, subprocess.CalledProcessError):
        return ""
    return nyers.decode("utf-8", "replace").strip()


def verzio_tul_uj(verzio=None):
    """Igaz, ha ehhez a Pythonhoz mar nincs kesz libtorrent csomag."""
    verzio = sys.version_info[:2] if verzio is None else verzio
    return tuple(verzio) > CSOMAG_MAX_VERZIO


def _jelolt_parancs(verzio):
    """Ezzel a paranccsal probaljuk elerni az adott Python-valtozatot."""
    if os.name == "nt":
        return ["py", "-" + verzio]
    return ["python" + verzio]


def masik_python(verziok=None):
    """Egy masik, telepitett Python, amihez van libtorrent csomag.

    Elsokent azt valasztjuk, amiben a libtorrent mar telepitve van; ha ilyen
    nincs, akkor a legujabb olyat, amihez letezik kesz csomag. None, ha nincs
    hasznalhato.
    """
    tartalek = None
    for verzio in (KERESETT_VERZIOK if verziok is None else verziok):
        ut = _kimenet(_jelolt_parancs(verzio) + ["-c", "import sys; print(sys.executable)"])
        if not ut or not os.path.isfile(ut):
            continue
        if os.path.abspath(ut) == os.path.abspath(sys.executable):
            continue
        if not _futtat_csendben([ut, "-c", "import tkinter"]):
            continue  # a felulethez tkinter is kell
        if _futtat_csendben([ut, "-c", "import libtorrent"]):
            return ut
        if tartalek is None:
            tartalek = ut
    return tartalek


def valtas_masik_pythonra():
    """Ujrainditja az indit.py-t egy alkalmas Pythonnal. None, ha nincs ilyen."""
    if os.environ.get(VALTAS_JELZO):
        return None  # egyszer mar valtottunk, ne menjunk korbe
    ut = masik_python()
    if not ut:
        return None
    print("")
    print("[..]   Ebben a Pythonban hianyzik valami a futashoz, ezert atadom")
    print("       a vezerlest ennek: %s" % ut)
    print("")
    kornyezet = dict(os.environ)
    kornyezet[VALTAS_JELZO] = "1"
    try:
        return subprocess.call([ut, os.path.join(ITT, "indit.py")], env=kornyezet)
    except OSError:
        return None


def tkinter_tanacs():
    """Mit tegyen a felhasznalo, ha nincs tkinter."""
    print("")
    print("       Inditsd el a Python telepitot ujra (Modify), es pipald")
    print("       be a 'tcl/tk and IDLE' komponenst:")
    print("       %s" % LETOLTES_URL)
    print("")
    print("       Addig is hasznalhato a parancssoros valtozat:")
    print("       %s -m torrentdl status" % sys.executable)


def libtorrent_tanacs():
    """Mit tegyen a felhasznalo, ha sehogy sem megy a libtorrent."""
    print("")
    print("       A libtorrent csomaghoz a keszitoi a Python %d.%d verziojaig"
          % CSOMAG_MAX_VERZIO)
    print("       adnak kesz (leforditott) valtozatot, a te Pythonod pedig %s."
          % sys.version.split()[0])
    print("")
    print("       Megoldas: telepitsd a Python %d.%d-at (a mostani maradhat):"
          % CSOMAG_MAX_VERZIO)
    print("       %s" % LETOLTES_URL)
    print("       A telepitonel pipald be az \"Add python.exe to PATH\" opciot")
    print("       es a \"tcl/tk and IDLE\" komponenst, majd inditsd ujra a")
    print("       programot - onnantol magatol azt a Pythont fogja hasznalni.")


def csomagok_telepitese(utvonal):
    """pip install -r ..., ha kell. Igaz, ha minden rendben."""
    if not csomag_sorok(utvonal):
        print("[OK]   Kulso csomag nem szukseges.")
        return True
    if not hianyzo_modulok(["libtorrent"]):
        print("[OK]   A libtorrent megvan.")
        return True
    if verzio_tul_uj():
        # Nincs ertelme telepiteni probalni: ehhez a verziohoz nincs csomag.
        print("[..]   Ehhez a Python-verziohoz (%s) nincs kesz libtorrent csomag."
              % sys.version.split()[0])
        return False
    print("[..]   A libtorrent telepitese (ez az elso inditasnal par percig tarthat)...")
    alap = [sys.executable, "-m", "pip", "install",
            "--disable-pip-version-check", "-r", utvonal]
    if not _futtat(alap):
        print("[..]   Ujraprobalom felhasznaloi modban...")
        if not _futtat(alap[:4] + ["--user"] + alap[4:]):
            print("[HIBA] Nem sikerult telepiteni a libtorrent csomagot.")
            print("       Lehetseges okok: nincs internetkapcsolat, tuzfal vagy proxy.")
            return False
    if hianyzo_modulok(["libtorrent"]):
        print("[HIBA] A libtorrent a telepites utan sem importalhato.")
        print("       Probald kezzel: %s -m pip install libtorrent" % sys.executable)
        return False
    print("[OK]   A libtorrent telepitve.")
    return True


def ellenorzes():
    """Minden fuggoseg-ellenorzes.

    Visszaadott ertek: "ok", "nincs_libtorrent" (masik Pythonnal meg mehet),
    vagy "hiba". A teendot minden esetben kiirja."""
    print("[OK]   Python: %s" % sys.version.split()[0])

    gond = verzio_gond()
    if gond:
        print("[HIBA] " + gond)
        print("       Telepitsd a friss Pythont: " + LETOLTES_URL)
        return "hiba"

    hianyzik = hianyzo_modulok()
    if hianyzik:
        print("[HIBA] Hianyzik a Python alap modulja: %s" % ", ".join(hianyzik))
        print("       Telepitsd ujra a Pythont a hivatalos telepitovel.")
        return "hiba"
    print("[OK]   Alap modulok megvannak.")

    if hianyzo_modulok(["tkinter"]):
        print("[HIBA] Hianyzik a tkinter - enelkul nincs grafikus felulet.")
        return "nincs_tkinter"
    print("[OK]   tkinter megvan.")

    hianyzo = hianyzo_fajlok()
    if hianyzo:
        print("[HIBA] Nem talalom ezeket a fajlokat itt: %s" % ITT)
        for nev in hianyzo:
            print("       - %s" % nev)
        print("       Ugy tunik, hianyos a kicsomagolt mappa.")
        return "hiba"
    print("[OK]   A program fajljai megvannak.")

    if not csomagok_telepitese(os.path.join(ITT, "requirements.txt")):
        return "nincs_libtorrent"
    return "ok"


def main(indit=True):
    keret("Torrent letolto")
    allapot = ellenorzes()
    if allapot in ("nincs_libtorrent", "nincs_tkinter"):
        # Talan van a gepen egy masik Python, amiben minden megvan.
        kod = valtas_masik_pythonra()
        if kod is not None:
            return kod
        if allapot == "nincs_libtorrent":
            libtorrent_tanacs()
        else:
            tkinter_tanacs()
        return 1
    if allapot != "ok":
        return 1
    if not indit:
        return 0
    print("")
    print("Indul a grafikus felulet...")
    sys.path.insert(0, ITT)
    from torrentdl.gui import main as gui_main
    try:
        return gui_main()
    except Exception as hiba:
        # Peldaul: nincs megjelenito (tavoli/szerver kornyezet).
        print("")
        print("[HIBA] Nem sikerult megnyitni az ablakot: %s" % hiba)
        print("       A parancssoros valtozat ilyenkor is mukodik:")
        print("       %s -m torrentdl status" % sys.executable)
        return 1


if __name__ == "__main__":
    sys.exit(main())
