"""Az indito (indit.py) fuggoseg-ellenorzeseinek tesztje."""
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import indit  # noqa: E402

fail = 0


def check(name, got, want):
    global fail
    if got == want:
        print(f"ok    {name:<48} {got!r}")
    else:
        fail = 1
        print(f"HIBA  {name:<48} kapott={got!r}  vart={want!r}")


check("a mostani Python megfelel", indit.verzio_gond(), None)
check("nem hianyzik alap modul", indit.hianyzo_modulok(), [])
check("nem letezo modult eszreveszi", indit.hianyzo_modulok(["nincs_ilyen_modul_xy"]),
      ["nincs_ilyen_modul_xy"])
check("a program fajljai megvannak", indit.hianyzo_fajlok(), [])

with tempfile.TemporaryDirectory() as ures:
    check("ures mappaban minden fajl hianyzik",
          len(indit.hianyzo_fajlok(ures)), len(indit.SAJAT_FAJLOK))

check("a requirements.txt-bol kiolvassa a csomagot",
      any("libtorrent" in sor for sor in
          indit.csomag_sorok(os.path.join(indit.ITT, "requirements.txt"))), True)
check("nem letezo requirements eseten ures lista",
      indit.csomag_sorok(os.path.join(indit.ITT, "nincs_ilyen.txt")), [])

# Az ellenorzes vegigfut, es nem inditja el a feluletet (indit=False).
if indit.hianyzo_modulok(["tkinter"]):
    # Ez a Python tkinter nelkul keszult: az inditonak eppen hogy hibat KELL
    # jeleznie, es a teendot kiirnia.
    check("tkinter nelkul hibat jelez", indit.main(indit=False), 1)
else:
    check("teljes ellenorzes lefut", indit.main(indit=False), 0)

print("\nAz indito tesztjei rendben." if not fail else "\nHIBA az inditoban.")
sys.exit(fail)
