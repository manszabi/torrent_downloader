"""A háttérdémon vezérlése: indítás, parancsok, forrás beolvasása.

Ezt használja a parancssori felület és a grafikus felület is.
"""

from __future__ import annotations

import base64
import contextlib
import os
import subprocess
import sys
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
# Miért nem a CREATE_NO_WINDOW? Mert azt a Windows csak KONZOLOS programra
# alkalmazza ("The process is a console application that is being run without
# a console window"): a démont viszont a grafikus alkalmazásnak számító
# pythonw.exe indítja, amire a jelző nem vonatkozik. A gyermek ilyenkor
# megörökli a szülő konzolját – és ha annak az ablaka el volt rejtve (az indító
# elrejti, amint megnyílik a felület), a Windows a folyamat indításakor
# visszahozza, immár a gyermek nevével a címsorában. Ez ugrott a felület elé
# "…\.venv\Scripts\pythonw.exe" címmel, például minden beállítás-mentés után,
# amikor a démon újraindul.
#
# A DETACHED_PROCESS az egyetlen jelző, ami mindkét fajta programra kimondja:
# a gyermek NEM örökli a szülő konzolját, és újat sem nyit helyette. A kettő
# egymást kizárja (DETACHED_PROCESS mellett a Windows eldobja a
# CREATE_NO_WINDOW-t), ezért a DETACHED_PROCESS-t használjuk önmagában.
#
# A CREATE_NEW_PROCESS_GROUP azt zárja ki, hogy egy konzolban leütött Ctrl+C
# a háttérben futó démont is leállítsa.
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def indito_jelzok(windows: bool | None = None) -> int:
    """A gyermekfolyamat indítási jelzői (Windowson konzol nélkül, leválasztva)."""
    if windows is None:
        windows = sys.platform == "win32"
    return DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP if windows else 0


def _rejtett_ablak_beallitas():
    """STARTUPINFO rejtett ablakkal – öv és nadrágtartó a konzolvillanás ellen."""
    if sys.platform != "win32":  # pragma: no cover - csak Windowson értelmes
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return info


def python_executable(
    alap: str | None = None,
    windows: bool | None = None,
    letezik: Callable[[Path], bool] = Path.is_file,
) -> str:
    """A démont konzol nélküli Pythonnal indítjuk.

    Windowson a `pythonw.exe` grafikus alkalmazásként indul: magától soha nem
    nyit konzolablakot. Ettől még a szülő konzoljához hozzá lehetne kapcsolva –
    ezt a DETACHED_PROCESS zárja ki (lásd fent). A kimenete nem hiányzik, mert
    a démon a saját naplófájljába ír.
    """
    futtatando = alap if alap is not None else sys.executable
    if windows is None:
        windows = sys.platform == "win32"
    if not windows:
        return futtatando
    jelolt = Path(futtatando).with_name("pythonw.exe")
    return str(jelolt) if letezik(jelolt) else futtatando


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
    cmd = [python_executable(), "-m", "torrentdl", "daemon", "run"]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parent.parent), env.get("PYTHONPATH", "")]
    ).strip(os.pathsep)
    # A démon a saját naplójába ír, a csatornái ezért mehetnek a semmibe.
    # (A gyermekfolyamat Windowson és Linuxon is túléli a szülő kilépését.)
    if sys.platform == "win32":  # pragma: no cover - Windowson fut
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=indito_jelzok(True),
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
    "ensure_daemon",
    "fetch_status",
    "indito_jelzok",
    "load_source",
    "ping",
    "python_executable",
    "spawn_daemon",
    "stop_daemon",
]
