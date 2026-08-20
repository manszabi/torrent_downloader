"""Az összes teszt futtatása egyben.

    python3 tests/futtato.py

A grafikus felület tesztjéhez képernyő kell; Linuxon a futtató magától
`xvfb-run`-t használ, ha van. Ha nincs, az a teszt kimarad (ezt kiírja).

A tesztek mellett lefut a statikus ellenőrzés is (ruff, mypy), ha telepítve
vannak; enélkül ezek a sorok "kihagyva" jelzéssel jelennek meg:

    pip install ruff mypy
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ITT = Path(__file__).resolve().parent


def futtat(nev: str, parancs: list[str]) -> str:
    print("\n" + "=" * 70)
    print(f"  {nev}")
    print("=" * 70)
    proc = subprocess.run(parancs, cwd=str(ITT.parent), check=False)
    return "rendben" if proc.returncode == 0 else "HIBA"


def eszkoz_parancs(modul: str) -> list[str] | None:
    """Az ellenőrző indítása: modulként vagy a PATH-ról, ha van."""
    modulkent = [sys.executable, "-m", modul]
    if subprocess.run([*modulkent, "--version"], capture_output=True, check=False).returncode == 0:
        return modulkent
    utvonal = shutil.which(modul)
    return [utvonal] if utvonal else None


def eszkoz_futtat(nev: str, modul: str, argumentumok: list[str]) -> str:
    """Statikus ellenőrző futtatása, ha telepítve van."""
    parancs = eszkoz_parancs(modul)
    if parancs is None:
        return f"kihagyva (nincs {modul})"
    return futtat(nev, [*parancs, *argumentumok])


def main() -> int:
    py = sys.executable
    eredmenyek = {
        "statikus elemzés (ruff)": eszkoz_futtat("Statikus elemzés", "ruff", ["check", "."]),
        "típusellenőrzés (mypy)": eszkoz_futtat("Típusellenőrzés", "mypy", ["torrentdl"]),
        "parancsfájl (bat)": futtat("Parancsfájl", [py, str(ITT / "bat_test.py")]),
        "indító": futtat("Indító", [py, str(ITT / "indit_test.py")]),
        "letöltő motor": futtat("Letöltő motor", [py, str(ITT / "smoke_test.py")]),
    }

    gui_parancs = [py, str(ITT / "gui_test.py")]
    if sys.platform != "win32" and shutil.which("xvfb-run"):
        gui_parancs = ["xvfb-run", "-a", *gui_parancs]
    try:
        import tkinter  # noqa: F401
    except ImportError:
        eredmenyek["grafikus felület"] = "kihagyva (nincs tkinter)"
    else:
        eredmenyek["grafikus felület"] = futtat("Grafikus felület", gui_parancs)

    print("\n" + "=" * 70)
    for nev, allapot in eredmenyek.items():
        print(f"  {nev:<24} {allapot}")
    print("=" * 70)
    return 1 if any(v == "HIBA" for v in eredmenyek.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
