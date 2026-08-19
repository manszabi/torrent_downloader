"""Útvonalak és beállítások kezelése."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

DEFAULT_CONFIG = {
    # Hálózat
    "listen_port": 6881,
    "enable_dht": True,
    "enable_pex": True,
    "enable_lsd": True,
    "enable_utp": True,
    "enable_upnp": True,
    "enable_natpmp": True,
    # Titkosítás: "disabled" | "enabled" (ha a peer is tudja) | "forced" (csak titkosítva)
    "encryption": "enabled",
    # Korlátok (kB/s, 0 = korlátlan)
    "max_download_rate": 0,
    "max_upload_rate": 0,
    "max_connections": 200,
    # Működés
    "seed_after_complete": False,  # kész letöltés után nem seedelünk, alapállapotba állunk
    "resume_save_interval": 30,    # másodperc: ilyen gyakran mentjük a folytatási adatot
    "idle_timeout": 600,           # ennyi tétlen másodperc után kilép a démon (0 = soha)
}

CONFIG_KEY_TYPES = {k: type(v) for k, v in DEFAULT_CONFIG.items()}


def home() -> Path:
    """A program adatkönyvtára (felülírható a TORRENTDL_HOME környezeti változóval)."""
    env = os.environ.get("TORRENTDL_HOME")
    if env:
        base = Path(env).expanduser()
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg).expanduser() / "torrentdl" if xdg else Path.home() / ".local" / "share" / "torrentdl"
    base.mkdir(parents=True, exist_ok=True)
    return base


def path(name: str) -> Path:
    return home() / name


SOCKET_NAME = "daemon.sock"
PID_NAME = "daemon.pid"
LOG_NAME = "daemon.log"
SESSION_STATE_NAME = "session.state"
JOB_NAME = "job.json"
LAST_NAME = "last.json"
RESUME_NAME = "resume.dat"
TORRENT_COPY_NAME = "current.torrent"
CONFIG_NAME = "config.json"

# A unix socket teljes útvonala nem lehet hosszabb ~107 bájtnál, ezért nagyon
# mély adatkönyvtár esetén a /tmp alatt hozunk létre egy rövid, egyedi nevet.
MAX_SOCKET_PATH = 100


def socket_path() -> Path:
    candidate = home() / SOCKET_NAME
    if len(str(candidate).encode("utf-8")) <= MAX_SOCKET_PATH:
        return candidate
    digest = hashlib.sha1(str(home()).encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"torrentdl-{digest}.sock"


def write_atomic(target: Path, data: bytes) -> None:
    """Atomi fájlírás, hogy összeomlás esetén se maradjon félkész állapotfájl."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(target: Path, obj) -> None:
    write_atomic(target, json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8"))


def read_json(target: Path, default=None):
    try:
        with open(target, "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError):
        return default


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    stored = read_json(path(CONFIG_NAME), {}) or {}
    for key, value in stored.items():
        if key in cfg:
            cfg[key] = value
    return cfg


def save_config(cfg: dict) -> None:
    write_json(path(CONFIG_NAME), {k: v for k, v in cfg.items() if k in DEFAULT_CONFIG})


def coerce(key: str, raw: str):
    """Szöveges CLI érték átalakítása a beállítás típusára."""
    kind = CONFIG_KEY_TYPES[key]
    if kind is bool:
        low = raw.strip().lower()
        if low in ("1", "true", "yes", "igen", "on"):
            return True
        if low in ("0", "false", "no", "nem", "off"):
            return False
        raise ValueError(f"{key}: logikai érték kell (true/false)")
    if kind is int:
        return int(raw)
    return raw
