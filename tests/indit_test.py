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

# --- A libtorrent csomag es a Python-verzio viszonya --------------------
check("a 3.14 mar tul uj a kesz csomaghoz", indit.verzio_tul_uj((3, 14)), True)
check("a 3.12 meg jo", indit.verzio_tul_uj((3, 12)), False)
check("a hatarverzio meg jo", indit.verzio_tul_uj(indit.CSOMAG_MAX_VERZIO), False)

# Tul uj Python eseten meg csak meg sem probalja a telepitest.
eredeti_tul_uj = indit.verzio_tul_uj
indit.verzio_tul_uj = lambda *a: True
eredeti_hianyzo = indit.hianyzo_modulok
indit.hianyzo_modulok = lambda modulok=None: (["libtorrent"] if modulok == ["libtorrent"]
                                              else eredeti_hianyzo(modulok))
check("tul uj Python: nem telepit, hanem jelez",
      indit.csomagok_telepitese(os.path.join(indit.ITT, "requirements.txt")), False)
indit.verzio_tul_uj = eredeti_tul_uj
indit.hianyzo_modulok = eredeti_hianyzo

# Az ellenorzes megkulonbozteti a "csak a libtorrent hianyzik" esetet.
eredeti_telepites = indit.csomagok_telepitese
indit.csomagok_telepitese = lambda *a: False
if indit.hianyzo_modulok(["tkinter"]):
    print("kihagy  ellenorzes vegeredmenye (nincs tkinter)")
else:
    check("hianyzo libtorrent kulon eset", indit.ellenorzes(), "nincs_libtorrent")
indit.csomagok_telepitese = eredeti_telepites

# --- Masik Python keresese ---------------------------------------------
mas = indit.masik_python(["3.13", "3.12", "3.11", "3.10"])
if mas is None:
    print("kihagy  masik Python keresese (nincs masik valtozat a gepen)")
else:
    check("a talalt Python nem a mostani",
          os.path.abspath(mas) != os.path.abspath(sys.executable), True)
    check("a talalt Python letezik", os.path.isfile(mas), True)

check("nem letezo valtozatra nem talal semmit", indit.masik_python(["3.2"]), None)

# Vegtelen korbe-valtas ellen: ha mar valtottunk, nem valtunk ujra.
os.environ[indit.VALTAS_JELZO] = "1"
check("egyszer mar valtottunk: nincs ujabb valtas", indit.valtas_masik_pythonra(), None)
del os.environ[indit.VALTAS_JELZO]

# Az ellenorzes vegigfut, es nem inditja el a feluletet (indit=False).
if indit.hianyzo_modulok(["tkinter"]):
    # Ez a Python tkinter nelkul keszult: az inditonak eppen hogy hibat KELL
    # jeleznie, es a teendot kiirnia.
    check("tkinter nelkul jelzi, hogy masik Python kell",
          indit.ellenorzes(), "nincs_tkinter")
else:
    check("teljes ellenorzes lefut", indit.main(indit=False), 0)

print("\nAz indito tesztjei rendben." if not fail else "\nHIBA az inditoban.")
sys.exit(fail)
