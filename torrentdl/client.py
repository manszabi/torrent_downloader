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
from .protocol import DaemonError, NotRunning, request

DAEMON_START_TIMEOUT = 25.0


def ping(timeout: float = 2.0):
    """A futó démon adatai, vagy None, ha nem fut."""
    try:
        return request(cfgmod.read_endpoint(), "ping", timeout=timeout)
    except DaemonError:
        return None


# Windows folyamat-indítási jelző. A subprocess csak Windowson definiálja,
# ezért a dokumentált számértéket írjuk ide – így máshol is tesztelhető.
#
# FONTOS: a CREATE_NO_WINDOW-t a Windows csendben eldobja, ha DETACHED_PROCESS
# vagy CREATE_NEW_CONSOLE mellé kerül. Korábban így indítottuk a démont, és
# emiatt a konzolablak a felület elé ugorhatott (pl. beállítás mentésekor,
# amikor a démon újraindul). Ezért a jelzőt csak önmagában használjuk.
CREATE_NO_WINDOW = 0x08000000


def indito_jelzok(windows: bool | None = None) -> int:
    """A gyermekfolyamat indítási jelzői (Windowson konzolablak nélkül)."""
    if windows is None:
        windows = sys.platform == "win32"
    return CREATE_NO_WINDOW if windows else 0


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

    Windowson a `pythonw.exe` grafikus alkalmazásként indul: soha nem nyit
    konzolt, és nem is veszi el a fókuszt a felülettől. A kimenete nem hiányzik,
    mert a démon a saját naplófájljába ír.
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
    deadline = time.time() + wait
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
        if not ping(timeout=1.0):
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
                data = response.read()
        except OSError as exc:
            raise SourceError(f"nem sikerült letölteni a .torrent fájlt: {exc}") from exc
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
    "CREATE_NO_WINDOW",
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
