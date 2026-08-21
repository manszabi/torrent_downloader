"""Egypéldányos zár: csak egy démon futhat egy adatkönyvtárral.

A zárat az operációs rendszer tartja, ezért összeomlás (SIGKILL, áramszünet)
után magától felszabadul – nem marad hátra „ragadt" zárfájl.
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
from pathlib import Path
from typing import IO

if sys.platform == "win32":  # pragma: no cover - Windowson fut
    import msvcrt
else:
    import fcntl


def read_pid(path: Path) -> int | None:
    """A zárfájlba írt folyamatazonosító, vagy None, ha nincs / olvashatatlan.

    Windowson a zárolt első bájtot nem lehet felülírni, ezért a szám előtt egy
    kitöltő bájt áll (lásd `_write_pid`). A számjegyeket ezért keresve olvassuk
    ki, nem a fájl teljes tartalmát alakítjuk számmá.
    """
    try:
        nyers = Path(path).read_text(errors="replace")
    except OSError:
        return None
    talalat = re.search(r"\d+", nyers)
    return int(talalat.group()) if talalat else None


class SingleInstanceLock:
    """Fájlzár. A `szerzes()` hamissal tér vissza, ha már fut egy példány."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.handle: IO[str] | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A fájlt szándékosan nyitva tartjuk: a zárolás addig él, amíg a
        # leíró nyitva van (ezért nincs "with" blokk).
        handle = self.path.open("a+")
        try:
            if sys.platform == "win32":  # pragma: no cover - Windowson fut
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        self._write_pid()
        return True

    def _write_pid(self) -> None:
        if self.handle is None:  # pragma: no cover - csak zár után hívjuk
            return
        # Windowson a zárolt első bájt nem írható felül, ezért utána írunk.
        offset = 1 if sys.platform == "win32" else 0
        self.handle.seek(offset)
        self.handle.truncate(offset)
        self.handle.write(("x" if offset else "") + str(os.getpid()) + "\n")
        self.handle.flush()

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            if sys.platform == "win32":  # pragma: no cover - Windowson fut
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self.handle, fcntl.LOCK_UN)
        except OSError:
            pass
        self.handle.close()
        self.handle = None
        with contextlib.suppress(OSError):
            self.path.unlink()
