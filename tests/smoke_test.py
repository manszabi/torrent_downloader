"""Végponttól végpontig működő füstteszt hálózat nélkül.

Amit ellenőriz:
  1. kész (már meglévő) fájlok esetén: hozzáadás -> ellenőrzés -> alapállapot
  2. szüneteltetés időzítéssel és automatikus folytatás
  3. megszakítás a fájlok törlésével
  4. összeomlás (SIGKILL) utáni folytatás a mentett állapotból

Futtatás:  python3 tests/smoke_test.py
"""

from __future__ import annotations

import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import libtorrent as lt

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from torrentdl import config as cfgmod  # noqa: E402
from torrentdl import lock as lock_modul  # noqa: E402
from torrentdl.format import kimenet_utf8  # noqa: E402

kimenet_utf8()

FAILURES = []


def run(home: Path, *args, check=True):
    env = dict(os.environ, TORRENTDL_HOME=str(home), PYTHONPATH=str(ROOT))
    proc = subprocess.run(
        [sys.executable, "-m", "torrentdl", *args],
        env=env,
        capture_output=True,
        # A gyerek UTF-8-cal ír (lásd kimenet_utf8); a text=True viszont a
        # rendszer kódlapjával olvasna vissza, ami Windowson elszáll.
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=False,  # a hibát mi magunk értékeljük ki
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"'{' '.join(args)}' hibára futott:\n{proc.stdout}\n{proc.stderr}"
            + naplo_reszlet(home)
        )
    return proc


def naplo_reszlet(home: Path, sorok: int = 25) -> str:
    """A démon naplójának vége – e nélkül egy indulási hiba okát nem látni."""
    naplo = home / "daemon.log"
    if not naplo.is_file():
        return "\n(nincs démonnapló)"
    vege = naplo.read_text(encoding="utf-8", errors="replace").splitlines()[-sorok:]
    return "\n--- a démon naplójának vége ---\n" + "\n".join(vege)


def make_torrent(source_dir: Path, out: Path) -> Path:
    fs = lt.file_storage()
    lt.add_files(fs, str(source_dir))
    torrent = lt.create_torrent(fs, piece_size=16 * 1024)
    torrent.set_creator("torrentdl smoke test")
    lt.set_piece_hashes(torrent, str(source_dir.parent))
    out.write_bytes(lt.bencode(torrent.generate()))
    return out


def make_payload(directory: Path, size: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(1234)
    (directory / "adat.bin").write_bytes(bytes(rnd.getrandbits(8) for _ in range(size)))
    (directory / "olvass.txt").write_text("torrentdl teszt\n", encoding="utf-8")


def status(home: Path) -> dict:
    # Ugyanazzal az olvasóval, amit a program használ: UTF-8, és elviseli, ha a
    # fájlt épp atomi cserével írják felül (Windowson ez hibát adhatna).
    return {
        "job": cfgmod.read_json(home / "job.json"),
        "last": cfgmod.read_json(home / "last.json"),
    }


def wait_for(predicate, timeout=60, interval=0.5, what="", home=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    # Az időtúllépés önmagában semmit nem árul el. Ha megkaptuk az
    # adatkönyvtárat, kiírjuk, milyen állapotban ragadt a munka, és mit írt
    # közben a démon – e nélkül egy másik rendszeren (pl. a CI Windows-ágán)
    # nem lehet kideríteni, mi történt.
    reszletek = ""
    if home is not None:
        job = status(home)["job"] or {}
        reszletek = (
            f"\n  a munka állapota: {job.get('state')!r}"
            f" (haladás: {job.get('progress')!r},"
            f" ellenőrzés oka: {job.get('recheck_reason')!r},"
            f" hiba: {job.get('error')!r})" + naplo_reszlet(home)
        )
    raise AssertionError(f"időtúllépés: {what}{reszletek}")


def check(name: str, fn):
    print(f"--- {name}")
    try:
        fn()
        print(f"    OK: {name}")
    except Exception as exc:
        FAILURES.append(name)
        print(f"    HIBA: {name}: {exc}")


class HamisHibakod:
    def __init__(self, ertek=0):
        self._ertek = ertek

    def value(self):
        return self._ertek


class HamisAlert:
    """A libtorrent hibariasztóit utánozza (érték + üzenet)."""

    def __init__(self, kod=0, uzenet=""):
        self.error = HamisHibakod(kod)
        self._uzenet = uzenet

    def message(self):
        return self._uzenet


def test_hibafelismeres():
    """A tele lemezt meg kell különböztetni a többi hibától."""
    import errno as errno_modul

    from torrentdl import engine

    tele_kod = 112 if os.name == "nt" else errno_modul.ENOSPC
    assert engine._hely_hiba(HamisAlert(tele_kod, "write failed"))
    assert engine._hely_hiba(HamisAlert(0, "No space left on device"))
    assert engine._hely_hiba(HamisAlert(0, "There is not enough space on the disk"))
    assert not engine._hely_hiba(HamisAlert(errno_modul.EACCES, "permission denied"))
    assert not engine._hely_hiba(HamisAlert(0, "file not found"))


class HamisSession:
    """Amit a fő ciklus időzített része kér a sessiontől."""

    def post_session_stats(self):
        pass

    def pop_alerts(self):
        return []


class HamisHash:
    def get_best(self):
        return "0" * 40


class HamisAllapot:
    """A torrent_handle.status() mezői, amiket a démon megkérdez."""

    def __init__(self):
        # Nem "ellenőrzés" állapot: a félbehagyott ellenőrzés kiértékelése
        # pont ilyenkor futna le a hibás változatban.
        self.state = lt.torrent_status.states.downloading
        self.progress = 0.5          # a leállított ellenőrzés hiányosnak látszik
        self.has_metadata = False    # így a folytatási adat mentése kimarad
        self.errc = HamisHibakod()
        self.name = "teszt"
        self.info_hashes = HamisHash()
        self.total_wanted = 1000
        self.total_wanted_done = 500
        self.total_done = 500
        self.download_payload_rate = 0
        self.upload_payload_rate = 0
        self.total_payload_upload = 0
        self.num_peers = 0
        self.num_seeds = 0
        self.num_pieces = 0


class HamisHandle:
    """Annyit tud, amennyit a szüneteltetés és a folytatás megkérdez tőle."""

    def __init__(self):
        self.hivasok = []

    def is_valid(self):
        return True

    def status(self):
        return HamisAllapot()

    def __getattr__(self, nev):
        def naplozo(*_a, **_kw):
            self.hivasok.append(nev)

        return naplozo


def test_forras_meret_hatar():
    """Túl nagy fájl nem mehet át a vezérlőcsatornán: előbb derüljön ki."""
    from torrentdl import client

    munka = Path(tempfile.mkdtemp(prefix="torrentdl-forras-"))
    try:
        nagy = munka / "nagy.torrent"
        with nagy.open("wb") as fh:
            fh.write(b"d")
            fh.truncate(client.MAX_TORRENT_BYTES + 1)
        try:
            client.load_source(str(nagy))
            raise AssertionError("a túl nagy fájlt el kellett volna utasítani")
        except client.SourceError as exc:
            assert "túl nagy" in str(exc), exc

        kicsi = munka / "kicsi.torrent"
        kicsi.write_bytes(b"d3:foo3:bare")
        assert client.load_source(str(kicsi))["source_type"] == "file"
    finally:
        shutil.rmtree(munka, ignore_errors=True)


def test_zar_eszlelese():
    """A leálló démon zárát észre kell venni, különben az újraindítás elbukik.

    A démon a vezérlőcsatornát a mentések ELŐTT zárja be, a zárat viszont csak
    utánuk engedi el. Ha a leállítás csak a pingig vár, egy azonnal induló új
    példány a zárba ütközik és rögtön kilép ("nem sikerült elindítani a
    háttérdémont"). Ezt a köztes állapotot kell látnia a programnak.
    """
    munka = Path(tempfile.mkdtemp(prefix="torrentdl-zar-"))
    try:
        zarfajl = munka / "daemon.lock"
        assert not lock_modul.tartja_meg_valaki(zarfajl), "zárfájl nélkül nincs mit tartani"

        zar = lock_modul.SingleInstanceLock(zarfajl)
        assert zar.acquire(), "a zárat meg kellett volna kapni"
        assert lock_modul.read_pid(zarfajl) == os.getpid()
        assert lock_modul.tartja_meg_valaki(zarfajl), "a tartott zárat észre kell venni"
        zar.release()
        assert not lock_modul.tartja_meg_valaki(zarfajl), "elengedés után szabad a zár"

        # Összeomlás után ottmaradt zárfájl: a folyamat már nem él.
        halott = munka / "halott.lock"
        halott.write_text("999999999\n", encoding="utf-8")
        assert not lock_modul.pid_alive(999_999_999)
        assert not lock_modul.tartja_meg_valaki(halott), "a hátrahagyott zárfájl ne blokkoljon"
    finally:
        shutil.rmtree(munka, ignore_errors=True)


def test_szunet_ellenorzes_kozben():
    """Ellenőrzés közben megnyomott Szünet: a szünetnek meg kell maradnia.

    Korábban a félbehagyott ellenőrzést a program kiértékelte. A leállított
    torrent haladása hiányosnak látszik, ezért "sérült adatot" hitt, a
    szünetet felülírva letöltésre váltott, és magától újraindult.
    """
    from torrentdl import engine

    munka = Path(tempfile.mkdtemp(prefix="torrentdl-szunet-"))
    regi_home = os.environ.get("TORRENTDL_HOME")
    os.environ["TORRENTDL_HOME"] = str(munka / "home")
    try:
        cel = munka / "cel"
        cel.mkdir()
        daemon = engine.Daemon.__new__(engine.Daemon)  # session és zár nélkül
        engine.Daemon.__init__(daemon)
        daemon.handle = HamisHandle()
        daemon.ses = HamisSession()
        daemon.job = {
            "source": "teszt",
            "source_type": "file",
            "save_path": str(cel),
            "state": engine.STATE_VERIFYING,
            "recheck_reason": engine.OK_KERES,
            "paused_until": None,
        }
        daemon.recheck = {"ok": engine.OK_KERES, "kezdet": time.time(), "latott": True}

        daemon.cmd_pause({})
        assert daemon.job["state"] == engine.STATE_PAUSED, daemon.job["state"]
        assert daemon.recheck is None, "a szünet után nem maradhat futó ellenőrzés"
        assert daemon.job.get("recheck_pending") == engine.OK_KERES

        # A fő ciklus időzített hívása nem írhatja felül a szünetet.
        daemon._periodic()
        assert daemon.job["state"] == engine.STATE_PAUSED, (
            f"a szünetet felülírta az ellenőrzés kiértékelése: {daemon.job['state']}"
        )

        # Folytatáskor viszont az elhalasztott ellenőrzés induljon el.
        daemon._do_resume()
        assert daemon.job["state"] == engine.STATE_VERIFYING, daemon.job["state"]
        assert daemon.recheck is not None, "a folytatás nem indította újra az ellenőrzést"
        assert "force_recheck" in daemon.handle.hivasok
    finally:
        if regi_home is None:
            os.environ.pop("TORRENTDL_HOME", None)
        else:
            os.environ["TORRENTDL_HOME"] = regi_home
        shutil.rmtree(munka, ignore_errors=True)


def test_folyamat_inditas():
    """A háttérdémon indítási módja Windowson (konzolablak nélkül)."""
    from torrentdl import client

    # 1) Melyik Pythonnal induljon a démon?
    #
    # A .venv Scripts mappájában lévő pythonw.exe csak egy átirányító, ami az
    # alaptelepítés Pythonját indítja - a 3.13.0-ig a KONZOLOS python.exe-t
    # (CPython gh-126084). Ezért az alaptelepítés valódi pythonw.exe-jét
    # keressük: elsőként a sys._base_executable mellett.
    ut, valodi = client.daemon_python(
        windows=True,
        futo="/v/Scripts/python.exe",
        alap="/py313/python.exe",
        letezik=lambda p: True,
        olvaso=lambda p: "",
    )
    assert (ut, valodi) == (str(Path("/py313/pythonw.exe")), True), (ut, valodi)

    # Ha az nincs meg, a .venv pyvenv.cfg "home" sora mondja meg, hol keressük.
    ut, valodi = client.daemon_python(
        windows=True,
        futo="/v/Scripts/python.exe",
        alap="",
        letezik=lambda p: "py313" in str(p),
        olvaso=lambda p: "home = /py313\nversion = 3.13.0\n",
    )
    assert (ut, valodi) == (str(Path("/py313/pythonw.exe")), True), (ut, valodi)
    assert client.venv_alap_mappa("/v", lambda p: "home = /py313\n") == "/py313"
    assert client.venv_alap_mappa("/v", lambda p: "") is None

    # Ha az alaptelepítés sehogy sem érhető el, marad a .venv pythonw.exe-je -
    # de ez lehet a hibás átirányító, ezért nem "valódi".
    ut, valodi = client.daemon_python(
        windows=True, futo="/v/Scripts/python.exe", alap="", letezik=lambda p: True,
        olvaso=lambda p: "",
    )
    assert (ut, valodi) == (str(Path("/v/Scripts/pythonw.exe")), False), (ut, valodi)

    # Ha pythonw.exe sincs, a most futó Python marad (változatlan szövegként).
    ut, valodi = client.daemon_python(
        windows=True, futo="/v/Scripts/python.exe", alap="", letezik=lambda p: False,
        olvaso=lambda p: "",
    )
    assert (ut, valodi) == ("/v/Scripts/python.exe", False), (ut, valodi)

    # Windowson kívül nincs mit keresni.
    assert client.daemon_python(windows=False, futo="/x/python3") == ("/x/python3", False)

    # 2) Milyen jelzőkkel induljon?
    #
    # Valódi grafikus pythonw.exe: leválasztva, hogy se örökölt, se új konzol
    # ne legyen. A CREATE_NO_WINDOW-t a Windows úgyis eldobná mellette (és
    # grafikus programra amúgy sem vonatkozik).
    jelzok = client.indito_jelzok(windows=True, valodi_pythonw=True)
    assert jelzok & client.DETACHED_PROCESS, hex(jelzok)
    assert not jelzok & client.CREATE_NO_WINDOW, hex(jelzok)
    # Ctrl+C egy konzolban ne állítsa le a háttérben futó démont.
    assert jelzok & client.CREATE_NEW_PROCESS_GROUP, hex(jelzok)

    # Konzolos Python (vagy átirányító): NEM szabad leválasztani. A leválasztott
    # szülőből induló konzolos program vadonatúj, LÁTHATÓ konzolablakot kapna;
    # az ablak nélküli konzolt viszont a gyermekei is öröklik.
    tartalek = client.indito_jelzok(windows=True, valodi_pythonw=False)
    assert tartalek == client.CREATE_NO_WINDOW, hex(tartalek)

    assert client.indito_jelzok(windows=False) == 0, client.indito_jelzok(windows=False)

    # 3) A démon lássa a program fájljait és a .venv csomagjait (libtorrent):
    # az alaptelepítés Pythonja magától nem tud a virtuális környezetről.
    env = client.daemon_kornyezet({"PYTHONPATH": "/regi"}, ["/v/Lib/site-packages"], "/projekt")
    assert env["PYTHONPATH"].split(os.pathsep) == [
        "/projekt",
        "/v/Lib/site-packages",
        "/regi",
    ], env["PYTHONPATH"]
    ures = client.daemon_kornyezet({}, ["/v/Lib/site-packages"], "/projekt")
    assert ures["PYTHONPATH"].split(os.pathsep) == ["/projekt", "/v/Lib/site-packages"], ures


def test_session_statisztika():
    """A DHT-számláló kiolvasása a session statisztikából.

    A régi session.status() elavult; helyette a post_session_stats() +
    session_stats_alert utat használjuk. Ez a teszt azt őrzi, hogy a metrika
    neve és a mezők elérése a telepített libtorrenttel is működik (egy
    verzióváltás különben csendben nullázná a kijelzett DHT-node számot).
    """
    from torrentdl import engine

    assert engine.DHT_NODES_INDEX >= 0, "nincs meg a dht.dht_nodes metrika"
    ses = lt.session(
        {
            "alert_mask": lt.alert_category.stats,
            "enable_dht": False,
            "listen_interfaces": "127.0.0.1:0",
        }
    )
    try:
        ses.post_session_stats()
        hatarido = time.time() + 10
        while time.time() < hatarido:
            for alert in ses.pop_alerts():
                if isinstance(alert, lt.session_stats_alert):
                    ertek = engine.dht_nodes_ertek(alert)
                    assert isinstance(ertek, int) and ertek >= 0, ertek
                    return
            time.sleep(0.05)
        raise AssertionError("nem érkezett session_stats_alert")
    finally:
        del ses


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="torrentdl-test-"))
    home = work / "home"
    home.mkdir()
    check("hibafelismerés (tele lemez vagy más hiba)", test_hibafelismeres)
    check("session statisztika (DHT-számláló)", test_session_statisztika)
    check("folyamatindítás konzolablak nélkül", test_folyamat_inditas)
    check("szüneteltetés ellenőrzés közben", test_szunet_ellenorzes_kozben)
    check("forrás méretkorlátja", test_forras_meret_hatar)
    check("egypéldány-zár észlelése", test_zar_eszlelese)
    port = random.randint(20000, 40000)
    run(home, "config", "--set", f"listen_port={port}", "--set", "idle_timeout=30")

    # -------------------------------------------------- 1. kész letöltés
    def test_complete():
        src = work / "kesz" / "csomag"
        make_payload(src, 300_000)
        torrent = make_torrent(src, work / "kesz.torrent")
        dest = work / "cel-kesz"
        dest.mkdir()
        shutil.copytree(src, dest / src.name)  # a fájlok már megvannak: ellenőrzés kell csak
        out = run(home, "add", str(torrent), "-d", str(dest))
        assert "Letöltés hozzáadva" in out.stdout, out.stdout
        wait_for(
            lambda: status(home)["job"] is None and status(home)["last"] is not None,
            timeout=60,
            what="a letöltés befejeződése és alapállapotba állás",
            home=home,
        )
        last = status(home)["last"]
        assert last["verified"] is True, last
        assert last["name"] == "csomag", last
        assert (dest / "csomag" / "adat.bin").exists()
        text = run(home, "status").stdout
        assert "Nincs aktív letöltés" in text, text

    check("kész fájlok: ellenőrzés után alapállapot", test_complete)

    # -------------------------------------------------- 2. szünet + folytatás
    def test_pause_resume():
        src = work / "fut" / "nagy"
        make_payload(src, 400_000)
        torrent = make_torrent(src, work / "fut.torrent")
        dest = work / "cel-fut"
        run(home, "add", str(torrent), "-d", str(dest))
        wait_for(lambda: status(home)["job"] is not None, what="a letöltés megjelenése", home=home)
        run(home, "pause", "--for", "5s")
        assert status(home)["job"]["state"] == "paused"
        assert status(home)["job"]["paused_until"] is not None
        wait_for(
            lambda: status(home)["job"]["state"] == "downloading",
            timeout=30,
            what="automatikus folytatás a szünet lejártakor",
            home=home,
        )
        run(home, "pause")
        assert status(home)["job"]["state"] == "paused"
        run(home, "resume")
        assert status(home)["job"]["state"] == "downloading"

    check("szüneteltetés időzítéssel és folytatás", test_pause_resume)

    # -------------------------------------------------- 3. összeomlás utáni folytatás
    def test_crash_resume():
        pid = lock_modul.read_pid(home / "daemon.lock")
        assert pid is not None, "nincs folyamatazonosító a zárfájlban"
        kegyetlen_leallitas(pid)
        wait_for(
            lambda: not lock_modul.pid_alive(pid),
            timeout=20,
            what="a démon leállása",
            home=home,
        )
        assert status(home)["job"] is not None, "a megszakadt letöltés adatai elvesztek"
        text = run(home, "status").stdout
        assert "megszakadt" in text, text
        run(home, "resume")
        wait_for(
            lambda: status(home)["job"]["state"] == "downloading",
            timeout=30,
            what="folytatás összeomlás után",
            home=home,
        )

    check("összeomlás utáni folytatás", test_crash_resume)

    # -------------------------------------------------- 4. törlés fájlokkal
    def test_cancel():
        dest = work / "cel-fut"
        run(home, "cancel", "--delete-files", "-y")
        wait_for(
            lambda: status(home)["job"] is None, timeout=20, what="a letöltés törlése"
        )
        leftovers = [p for p in dest.rglob("*") if p.is_file()]
        assert not leftovers, f"maradtak fájlok: {leftovers}"
        proc = run(home, "cancel", check=False)
        assert proc.returncode != 0 or "Nincs aktív" in proc.stdout, proc.stdout

    check("megszakítás a fájlok törlésével", test_cancel)

    # -------------------------------------------------- 5. megosztás a letöltés után
    def test_seeding():
        run(home, "config", "--set", "seed_after_complete=true")
        run(home, "daemon", "stop", check=False)  # az új beállítás induláskor él
        src = work / "megoszt" / "keszlet"
        make_payload(src, 250_000)
        torrent = make_torrent(src, work / "megoszt.torrent")
        dest = work / "cel-megoszt"
        dest.mkdir()
        shutil.copytree(src, dest / src.name)  # már kész: az ellenőrzés után megosztás jön
        run(home, "add", str(torrent), "-d", str(dest))
        wait_for(
            lambda: (status(home)["job"] or {}).get("state") == "seeding",
            timeout=60,
            what="a megosztásba lépés a letöltés után",
            home=home,
        )
        assert status(home)["last"]["verified"] is True

        # A démon leállítása és újraindítása után is megy tovább a megosztás.
        run(home, "daemon", "stop")
        allapot = (status(home)["job"] or {}).get("state")
        assert allapot == "seeding", "a megosztás állapota elveszett"
        run(home, "daemon", "start")
        out = run(home, "status").stdout
        assert "megosztás" in out, out
        assert (status(home)["job"] or {}).get("state") == "seeding"

        # Megosztás közben is indítható új letöltés: az előzőt magától lezárja.
        masik = make_torrent(src, work / "megoszt2.torrent")
        run(home, "add", str(masik), "-d", str(work / "cel-megoszt2"))
        wait_for(
            lambda: status(home)["job"] is not None
            and status(home)["job"]["save_path"].endswith("cel-megoszt2"),
            timeout=30,
            what="az új letöltés indulása megosztás közben",
            home=home,
        )
        run(home, "cancel", "-y")
        # A megosztott fájlok a helyükön maradtak.
        assert (dest / src.name / "adat.bin").is_file()
        run(home, "config", "--set", "seed_after_complete=false")
        run(home, "daemon", "stop", check=False)

    check("megosztás a letöltés után, újraindítás után is", test_seeding)

    # -------------------------------------------------- 6-7. adatvesztés és javítás
    romlas = {}

    def test_serult_adat():
        """Megosztás közben megsérül egy fájl: az ellenőrzés újratöltendőnek jelöli."""
        run(home, "config", "--set", "seed_after_complete=true")
        run(home, "daemon", "stop", check=False)
        src = work / "ep" / "adatok"
        make_payload(src, 400_000)
        torrent = make_torrent(src, work / "ep.torrent")
        dest = work / "cel-ep"
        dest.mkdir()
        shutil.copytree(src, dest / src.name)
        run(home, "add", str(torrent), "-d", str(dest))
        wait_for(
            lambda: (status(home)["job"] or {}).get("state") == "seeding",
            timeout=60,
            what="a kész torrent megosztásba lépése",
            home=home,
        )
        fajl = dest / src.name / "adat.bin"
        romlas["fajl"] = fajl
        romlas["eredeti"] = fajl.read_bytes()

        # Egy darab közepét felülírjuk: pontosan ezt kell újratöltenie.
        rontott = bytearray(romlas["eredeti"])
        rontott[150_000:150_500] = b"X" * 500
        fajl.write_bytes(bytes(rontott))

        run(home, "check")
        wait_for(
            lambda: (status(home)["job"] or {}).get("state") == "downloading",
            timeout=90,
            what="a sérült darab felismerése",
            home=home,
        )
        job = status(home)["job"]
        assert job["progress"] < 1.0, "a program épnek hitte a sérült fájlt"
        assert job.get("repaired_bytes", 0) > 0, job

        # Helyreállítjuk a fájlt: az ellenőrzés után újra megosztás jön.
        fajl.write_bytes(romlas["eredeti"])
        run(home, "check")
        wait_for(
            lambda: (status(home)["job"] or {}).get("state") == "seeding",
            timeout=90,
            what="az ép fájl elfogadása",
            home=home,
        )

    check("sérült fájl felismerése és újratöltése", test_serult_adat)

    def test_nem_tiszta_leallas():
        """Áramszünet-szerű leállás: induláskor mindent újraellenőriz."""
        pid = lock_modul.read_pid(home / "daemon.lock")
        assert pid is not None, "nincs folyamatazonosító a zárfájlban"
        kegyetlen_leallitas(pid)
        wait_for(
            lambda: not lock_modul.pid_alive(pid),
            timeout=20,
            what="a démon leállása",
            home=home,
        )

        # Amíg a program áll, "elromlik" a lemezen az adat.
        fajl = romlas["fajl"]
        rontott = bytearray(romlas["eredeti"])
        rontott[20_000:20_800] = b"Y" * 800
        fajl.write_bytes(bytes(rontott))

        run(home, "daemon", "start")
        wait_for(
            lambda: (status(home)["job"] or {}).get("state") == "downloading",
            timeout=90,
            what="indulási ellenőrzés a nem tiszta leállás után",
            home=home,
        )
        job = status(home)["job"]
        assert job["progress"] < 1.0, "a program a mentett állapotot hitte el a lemez helyett"

        fajl.write_bytes(romlas["eredeti"])
        run(home, "cancel", "-y")
        run(home, "config", "--set", "seed_after_complete=false")
        run(home, "daemon", "stop", check=False)

    check("nem tiszta leállás után teljes ellenőrzés", test_nem_tiszta_leallas)

    def test_levalasztott_meghajto():
        """Ha a célmappa eltűnik, ne kezdjen újra letölteni máshová."""
        run(home, "config", "--set", "seed_after_complete=true")
        run(home, "daemon", "stop", check=False)
        src = work / "kulso" / "mentes"
        make_payload(src, 200_000)
        torrent = make_torrent(src, work / "kulso.torrent")
        dest = work / "cel-kulso"
        dest.mkdir()
        shutil.copytree(src, dest / src.name)
        run(home, "add", str(torrent), "-d", str(dest))
        wait_for(
            lambda: (status(home)["job"] or {}).get("state") == "seeding",
            timeout=60,
            what="a megosztásba lépés",
            home=home,
        )

        # "Leválasztjuk a meghajtót": a célmappa eltűnik, amíg a program áll.
        run(home, "daemon", "stop")
        dest.rename(work / "cel-kulso-elrejtve")
        run(home, "daemon", "start")
        wait_for(
            lambda: (status(home)["job"] or {}).get("state") == "error",
            timeout=30,
            what="a hiányzó célmappa felismerése",
            home=home,
        )
        job = status(home)["job"]
        assert "célmappa" in (job.get("error") or ""), job
        assert not dest.exists(), "a program újra létrehozta a célmappát"

        # "Visszacsatlakoztatjuk": folytatás után minden megy tovább.
        (work / "cel-kulso-elrejtve").rename(dest)
        run(home, "resume")
        wait_for(
            lambda: (status(home)["job"] or {}).get("state")
            in ("seeding", "verifying", "downloading"),
            timeout=60,
            what="folytatás a meghajtó visszacsatlakoztatása után",
            home=home,
        )
        run(home, "cancel", "-y")
        run(home, "config", "--set", "seed_after_complete=false")
        run(home, "daemon", "stop", check=False)

    check("leválasztott meghajtó: nem tölt újra máshová", test_levalasztott_meghajto)

    run(home, "daemon", "stop", check=False)
    if FAILURES:
        print(f"\n{len(FAILURES)} teszt bukott el: {', '.join(FAILURES)}")
        print(f"munkakönyvtár (vizsgálathoz): {work}")
        return 1
    shutil.rmtree(work, ignore_errors=True)
    print("\nMinden teszt rendben.")
    return 0


def kegyetlen_leallitas(pid: int) -> None:
    """Áramszünet-szerű leállítás: a folyamat nem takaríthat maga után.

    Unixon ezt a SIGKILL adja. Windowson nincs SIGKILL, ott az os.kill minden
    más jelzésre a TerminateProcess-t hívja – az pedig ugyanilyen azonnali,
    lekezelhetetlen leállás, tehát pontosan ezt a helyzetet állítja elő.
    """
    os.kill(pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)


if __name__ == "__main__":
    raise SystemExit(main())
