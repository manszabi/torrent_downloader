"""A démon naplójának olvasása (a felület és a parancssor is ezt használja)."""

from __future__ import annotations

from . import config as cfgmod

NAPLO_SOROK = 200
OLVASOTT_BAJT = 64 * 1024  # ennyi elég az utolsó néhány száz sorhoz

_gyorsitotar: dict = {"kulcs": (), "szoveg": ""}


def naplo_vege(sorok: int = NAPLO_SOROK) -> str:
    """A napló utolsó néhány sora.

    Csak a fájl végét olvassuk (a napló akár 1 MB is lehet), és előbb a fájl
    adatait nézzük meg: ha méret és módosítási idő szerint nem változott, a
    korábbi szöveget adjuk vissza. Így a másodpercenkénti frissítés nem olvas
    és nem dekódol újra fölöslegesen.
    """
    utvonal = cfgmod.home() / cfgmod.LOG_NAME
    try:
        adatok = utvonal.stat()
    except OSError:
        return ""
    kulcs = (adatok.st_size, adatok.st_mtime_ns, sorok)
    if kulcs == _gyorsitotar["kulcs"]:
        return _gyorsitotar["szoveg"]
    try:
        with utvonal.open("rb") as fh:
            fh.seek(max(0, adatok.st_size - OLVASOTT_BAJT))
            adat = fh.read()
    except OSError:
        return ""
    szoveg = "\n".join(adat.decode("utf-8", "replace").splitlines()[-sorok:])
    _gyorsitotar["kulcs"] = kulcs
    _gyorsitotar["szoveg"] = szoveg
    return szoveg
