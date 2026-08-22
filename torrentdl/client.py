"""A háttérdémon vezérlése: indítás, parancsok, forrás beolvasása.

Ezt használja a parancssori felület és a grafikus felület is.
"""

from __future__ import annotations

import base64
import contextlib
import os
import subprocess
import sys
import sysconfig
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from . import config as cfgmod
from .format import human_bytes as _meret
from .lock import tartja_meg_valaki
from .protocol import DaemonError, NotRunning, request

# Egy .torrent fájl a gyakorlatban néhány száz kilobájt. A felső határ azért
# kell, mert a tartalom base64-ként megy át a vezérlőcsatornán, aminek 16 MB a
# korlátja – e nélkül egy rossz URL (pl. egy nagy telepítőkép) előbb teljesen
# beolvasódna a memóriába, és csak utána derülne ki, hogy nem is .torrent.
MAX_TORRENT_BYTES = 8 * 1024 * 1024

DAEMON_START_TIMEOUT = 25.0


def ping(timeout: float = 2.0):
    """A futó démon adatai, vagy None, ha nem fut."""
    try:
        return request(cfgmod.read_endpoint(), "ping", timeout=timeout)
    except DaemonError:
        return None


# Windows folyamat-indítási jelzők. A subprocess csak Windowson definiálja
# őket, ezért a dokumentált számértéket írjuk ide – így máshol is tesztelhető.
#
# Melyiket mikor? A Windows kétféle programot ismer: a KONZOLOS (python.exe) és
# a grafikus (pythonw.exe) alkalmazást.
#
# - DETACHED_PROCESS: a gyermek nem örökli a szülő konzolját, és nem is kap
#   újat. Ez kell akkor, ha a gyermek valóban grafikus program (pythonw.exe):
#   így semmilyen konzolablak nem keletkezik, és a szülő (esetleg elrejtett)
#   konzolablakát sem hozza vissza a Windows.
# - CREATE_NO_WINDOW: a gyermek kap konzolt, csak ablak nélkül. Ezt a Windows
#   viszont eldobja, ha a program nem konzolos ("This flag is ignored if the
#   application is not a console application"). Akkor jó, ha a gyermek konzolos
#   Python: az örökölt, ablak nélküli konzolt az általa indított folyamatok is
#   megkapják, tehát végig nem villan fel semmi.
#
# Ha a rossz párosítást választjuk, konzolablak nyílik: leválasztott (konzol
# nélküli) szülőből indított KONZOLOS program ugyanis kap egy vadonatúj,
# látható konzolablakot – ez ugrott a felület elé, és maradt ott a letöltés
# végéig (lásd a daemon_python() magyarázatát).
#
# A CREATE_NEW_PROCESS_GROUP azt zárja ki, hogy egy konzolban leütött Ctrl+C
# a háttérben futó démont is leállítsa.
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

# A virtuális környezet ebben a fájlban írja le, melyik telepítésből készült.
VENV_CFG = "pyvenv.cfg"


def indito_jelzok(windows: bool | None = None, valodi_pythonw: bool = True) -> int:
    """A gyermekfolyamat indítási jelzői (Windowson konzolablak nélkül).

    `valodi_pythonw`: sikerült-e a valódi, grafikus pythonw.exe-t megtalálni.
    Ha nem (marad a konzolos python.exe vagy a .venv átirányítója), akkor nem
    szabad leválasztani – ott az ablak nélküli konzol a helyes megoldás.
    """
    if windows is None:
        windows = sys.platform == "win32"
    if not windows:
        return 0
    if valodi_pythonw:
        return DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    return CREATE_NO_WINDOW


def _rejtett_ablak_beallitas():
    """STARTUPINFO rejtett ablakkal – öv és nadrágtartó a konzolvillanás ellen."""
    if sys.platform != "win32":  # pragma: no cover - csak Windowson értelmes
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return info


def _fajl_szoveg(ut: Path) -> str:
    """A fájl tartalma, vagy üres szöveg, ha nem olvasható."""
    try:
        return ut.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def venv_alap_mappa(
    prefix: str | None = None, olvaso: Callable[[Path], str] = _fajl_szoveg
) -> str | None:
    """A `pyvenv.cfg` "home" sora: melyik Python-telepítésből készült a .venv."""
    szoveg = olvaso(Path(prefix if prefix is not None else sys.prefix) / VENV_CFG)
    for sor in szoveg.splitlines():
        kulcs, egyenlo, ertek = sor.partition("=")
        if egyenlo and kulcs.strip().lower() == "home" and ertek.strip():
            return ertek.strip()
    return None


def daemon_python(
    windows: bool | None = None,
    futo: str | None = None,
    alap: str | None = None,
    prefix: str | None = None,
    letezik: Callable[[Path], bool] = Path.is_file,
    olvaso: Callable[[Path], str] = _fajl_szoveg,
) -> tuple[str, bool]:
    """Melyik Pythonnal induljon a démon, és valódi-e a konzoltalan változat.

    Windowson a `pythonw.exe` a grafikus (konzol nélküli) Python. A `.venv`
    `Scripts` mappájában viszont nem ez van, hanem egy *átirányító*: az csak
    megkeresi az alaptelepítés Pythonját, és azt indítja el gyermekfolyamatként.
    A Python 3.13.0-ig ez az átirányító nem tudott a pythonw.exe létezéséről,
    és mindig a KONZOLOS python.exe-t futtatta (CPython gh-126084, javítva a
    3.13.1-ben). Emiatt a démonhoz konzolablak nyílt – ráadásul az eredeti
    parancssorral a címsorában, ezért látszott ".venv\\Scripts\\pythonw.exe"
    néven –, ami a felület elé ugrott, és a démon élete végéig ott is maradt
    (az ablak bezárása után is, hiszen a letöltés szándékosan tovább fut).

    Ezért az átirányítót kikerüljük: az alaptelepítés valódi `pythonw.exe`-jét
    keressük meg (`sys._base_executable`, illetve a `pyvenv.cfg` "home" sora
    alapján), és azt indítjuk. A `.venv` csomagjait – köztük a libtorrentet –
    a `daemon_kornyezet()` adja hozzá a PYTHONPATH-on.

    Visszatérés: (futtatandó Python, valódi grafikus pythonw.exe-e).
    """
    futo = futo if futo is not None else sys.executable
    if windows is None:
        windows = sys.platform == "win32"
    if not windows:
        return futo, False
    alaputak = []
    jelolt = alap if alap is not None else getattr(sys, "_base_executable", "")
    if jelolt:
        alaputak.append(Path(jelolt))
    mappa = venv_alap_mappa(prefix, olvaso)
    if mappa:
        alaputak.append(Path(mappa) / "python.exe")
    for ut in alaputak:
        pythonw = ut.with_name("pythonw.exe")
        if letezik(pythonw):
            return str(pythonw), True
    # Nincs meg az alaptelepítés: marad a .venv pythonw.exe-je (átirányító
    # lehet, ezért nem "valódi"), végső soron a most futó Python.
    sajat = Path(futo).with_name("pythonw.exe")
    if letezik(sajat):
        return str(sajat), False
    return futo, False


def daemon_kornyezet(
    kornyezet: dict[str, str] | None = None,
    csomagok: list[str] | None = None,
    projekt: str | None = None,
) -> dict[str, str]:
    """A démon környezete: lássa a program fájljait és a .venv csomagjait.

    Az alaptelepítés Pythonja nem tud a `.venv`-ről (nincs mellette
    `pyvenv.cfg`), ezért a virtuális környezet csomagmappáit a PYTHONPATH-on
    adjuk át neki – enélkül nem találná meg a libtorrentet.
    """
    env = dict(os.environ if kornyezet is None else kornyezet)
    if csomagok is None:
        csomagok = [
            ut for ut in (sysconfig.get_path("purelib"), sysconfig.get_path("platlib")) if ut
        ]
    gyoker = projekt if projekt is not None else str(Path(__file__).resolve().parent.parent)
    utak = [gyoker, *dict.fromkeys(csomagok), env.get("PYTHONPATH", "")]
    env["PYTHONPATH"] = os.pathsep.join([u for u in utak if u])
    return env


def spawn_daemon(wait: float = DAEMON_START_TIMEOUT) -> bool:
    """Elindítja a démont a háttérben, és megvárja, amíg válaszol."""
    if ping():
        return True
    deadline = time.time() + wait
    # Egy előző démon lehet, hogy még leáll: a vezérlőcsatornáját már bezárta
    # (nem válaszol pingre), a zárat viszont csak a mentések végén engedi el.
    # Amíg tartja, az új példány rögtön kilépne – ezért megvárjuk.
    while tartja_meg_valaki(cfgmod.path(cfgmod.PID_NAME)) and time.time() < deadline:
        time.sleep(0.25)
        if ping():
            return True
    futtatando, valodi_pythonw = daemon_python()
    cmd = [futtatando, "-m", "torrentdl", "daemon", "run"]
    env = daemon_kornyezet()
    # A démon a saját naplójába ír, a csatornái ezért mehetnek a semmibe.
    # (A gyermekfolyamat Windowson és Linuxon is túléli a szülő kilépését.)
    if sys.platform == "win32":  # pragma: no cover - Windowson fut
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=indito_jelzok(True, valodi_pythonw),
            startupinfo=_rejtett_ablak_beallitas(),
        )
    else:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    while time.time() < deadline:
        if ping(timeout=1.0):
            return True
        time.sleep(0.25)
    return False


def naplo_urites() -> None:
    """A naplófájl kiürítése.

    Ha fut a démon, ő üríti: Windowson a megnyitott fájlt más nem törölheti, és
    a naplózását is újra kell nyitnia. Ha nem fut, magunk töröljük – csak ezért
    nem érdemes elindítani.
    """
    if ping():
        request(cfgmod.read_endpoint(), "clear_log")
        return
    naplo = cfgmod.path(cfgmod.LOG_NAME)
    for ut in (naplo, *sorted(naplo.parent.glob(naplo.name + ".*"))):
        with contextlib.suppress(OSError):
            ut.unlink()


def mappa_megnyitas(
    mappa: Path | None = None, indito: Callable[[Path], None] | None = None
) -> None:
    """Az adatmappa megnyitása a rendszer fájlkezelőjében (új ablakban)."""
    cel = cfgmod.home() if mappa is None else Path(mappa)
    if indito is not None:
        indito(cel)
        return
    if sys.platform == "win32":  # pragma: no cover - Windowson fut
        # A startfile az Explorert nyitja meg – konzolablak nélkül.
        os.startfile(cel)  # noqa: S606 - saját, ismert útvonal, nem felhasználói parancs
        return
    parancs = ["open"] if sys.platform == "darwin" else ["xdg-open"]
    subprocess.Popen(
        [*parancs, str(cel)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


class DaemonUnavailable(DaemonError):
    """Nem sikerült elindítani a háttérdémont."""


def ensure_daemon() -> None:
    if not spawn_daemon():
        raise DaemonUnavailable(
            f"nem sikerült elindítani a háttérdémont (napló: {cfgmod.home() / cfgmod.LOG_NAME})"
        )


def call(command: str, **payload):
    """Parancs a démonnak; szükség esetén elindítja."""
    ensure_daemon()
    return request(cfgmod.read_endpoint(), command, **payload)


def stop_daemon(wait: float = 20.0) -> bool:
    """Leállítja a démont. Igaz, ha (már) nem fut."""
    if not ping():
        return True
    with contextlib.suppress(DaemonError):
        request(cfgmod.read_endpoint(), "shutdown")
    deadline = time.time() + wait
    while time.time() < deadline:
        # A ping elnémulása még nem jelenti, hogy a démon el is engedte a
        # zárat: a mentések utána következnek. Egy azonnal induló új példány
        # ilyenkor kilépne, ezért a folyamat tényleges kilépését várjuk meg.
        if not ping(timeout=1.0) and not tartja_meg_valaki(cfgmod.path(cfgmod.PID_NAME)):
            return True
        time.sleep(0.25)
    return False


def fetch_status() -> dict:
    """A démont kérdezzük; ha nem fut, a lemezre mentett állapotot mutatjuk."""
    try:
        data = request(cfgmod.read_endpoint(), "status")
        data["daemon"] = True
        return data
    except DaemonError:
        pass
    return {
        "daemon": False,
        "job": cfgmod.read_json(cfgmod.path(cfgmod.JOB_NAME)),
        "last": cfgmod.read_json(cfgmod.path(cfgmod.LAST_NAME)),
        "config": cfgmod.load_config(),
    }


# ------------------------------------------------------------------ forrás


class SourceError(ValueError):
    """A megadott torrent/magnet forrás nem használható."""


def load_source(source: str) -> dict:
    """Magnet link, .torrent fájl vagy .torrent URL feldolgozása."""
    source = source.strip()
    if source.startswith("magnet:"):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(source).query)
        name = (query.get("dn") or [""])[0]
        if not query.get("xt"):
            raise SourceError("hiányos magnet link (nincs benne 'xt' azonosító)")
        return {"source": source, "source_type": "magnet", "name": name}

    if source.startswith(("http://", "https://")):
        try:
            # A séma fentebb ellenőrizve (csak http/https), ezért biztonságos.
            with urllib.request.urlopen(source, timeout=30) as response:  # noqa: S310
                # Eggyel többet kérünk a határnál: így kiderül, ha túllóg.
                data = response.read(MAX_TORRENT_BYTES + 1)
        except OSError as exc:
            raise SourceError(f"nem sikerült letölteni a .torrent fájlt: {exc}") from exc
        if len(data) > MAX_TORRENT_BYTES:
            raise SourceError(
                "a megadott URL túl nagy fájlt ad vissza ehhez "
                f"({_meret(MAX_TORRENT_BYTES)} a határ) – biztosan .torrent fájlra mutat?"
            )
        if not data.startswith(b"d"):
            raise SourceError("a megadott URL nem .torrent fájlt adott vissza")
        return {
            "source": source,
            "source_type": "file",
            "torrent_b64": base64.b64encode(data).decode("ascii"),
            "name": torrent_name(data),
        }

    path = Path(source).expanduser()
    if not path.is_file():
        raise SourceError(f"nincs ilyen fájl: {source}")
    if path.stat().st_size > MAX_TORRENT_BYTES:
        raise SourceError(
            f"ez a fájl túl nagy .torrent fájlnak ({_meret(MAX_TORRENT_BYTES)} a határ): "
            f"{path.name}"
        )
    data = path.read_bytes()
    if not data.startswith(b"d"):
        raise SourceError(f"ez nem .torrent fájl: {path.name}")
    return {
        "source": str(path.resolve()),
        "source_type": "file",
        # base64: a hexnél másfélszer tömörebb, így a nagy .torrent fájlok is
        # gyorsabban jutnak át a vezérlőcsatornán.
        "torrent_b64": base64.b64encode(data).decode("ascii"),
        "name": torrent_name(data),
    }


def torrent_name(data: bytes) -> str:
    try:
        # Csak a név kiírásához kell; a parancssor enélkül is működik.
        import libtorrent as lt  # noqa: PLC0415

        # A kötés a kibontott (bdecode-olt) szerkezetet is elfogadja,
        # a típusleírás viszont csak a nyers bájtokat ismeri.
        return lt.torrent_info(cast("Any", lt.bdecode(data))).name()
    except Exception:
        return ""


__all__ = [
    "CREATE_NEW_PROCESS_GROUP",
    "CREATE_NO_WINDOW",
    "DETACHED_PROCESS",
    "DaemonError",
    "DaemonUnavailable",
    "NotRunning",
    "SourceError",
    "call",
    "daemon_kornyezet",
    "daemon_python",
    "ensure_daemon",
    "fetch_status",
    "indito_jelzok",
    "load_source",
    "mappa_megnyitas",
    "naplo_urites",
    "ping",
    "spawn_daemon",
    "stop_daemon",
    "venv_alap_mappa",
]
