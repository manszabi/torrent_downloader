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

    Windowson a zár az első bájtra vonatkozik, és ott ez KÖTELEZŐ zár: amíg él,
    azt a bájtot más leíró nem is olvashatja – egy egyszerű `read_text()` a
    fájl elejéről indulva sharing violation hibára futna. Ezért (ahogy az írás
    is: lásd `_write_pid`) a zárolt bájt UTÁN kezdünk olvasni.
    """
    kezdet = 1 if sys.platform == "win32" else 0
    try:
        with Path(path).open("rb") as fh:
            fh.seek(kezdet)
            nyers = fh.read()
    except OSError:
        return None
    talalat = re.search(r"\d+", nyers.decode("utf-8", "replace"))
    return int(talalat.group()) if talalat else None


def pid_alive(pid: int) -> bool:
    """Fut-e még ez a folyamat? (Csak kérdez, nem nyúl hozzá.)"""
    if sys.platform == "win32":  # pragma: no cover - Windowson fut
        # Windowson az os.kill nem kérdez, hanem LELŐ: még a 0-s "jelzés" is
        # TerminateProcess-szé válik. Ezért a folyamat leírójából olvassuk ki,
        # hogy fut-e még.
        import ctypes  # noqa: PLC0415

        SYNCHRONIZE = 0x0010_0000
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
        leiro = kernel.OpenProcess(
            SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not leiro:
            return False
        try:
            kod = ctypes.c_ulong()
            if not kernel.GetExitCodeProcess(leiro, ctypes.byref(kod)):
                return False
            return kod.value == STILL_ACTIVE
        finally:
            kernel.CloseHandle(leiro)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def tartja_meg_valaki(path: Path) -> bool:
    """Van-e még élő démon a zárfájl mögött.

    A leálló démon a vezérlőcsatornáját már bezárta (nem válaszol pingre), de a
    zárat csak a mentések végén engedi el – addig egy új példány azonnal
    kilépne. Ezt a köztes állapotot mutatja ki ez a függvény. Összeomlás után a
    zárfájl ottmaradhat, ezért a folyamatot magát is megkérdezzük.
    """
    pid = read_pid(path)
    if pid is None:
        return False
    return pid_alive(pid)


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
