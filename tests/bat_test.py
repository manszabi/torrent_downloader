"""A Windows parancsfajl ellenorzese.

Nem futtatja (ahhoz Windows kellene), hanem azokat a hibakat keresi, amiktol a
cmd.exe hasznalhatatlanna valik - es amik Linuxon / a GitHub ZIP-ben
eszrevetlenul keletkeznek:

  * LF sorveg: a cmd.exe bajt-eltolas alapjan olvas, LF-nel elcsuszik,
  * ekezetes karakter: a cmd a rendszer kodlapjaval olvas, nem UTF-8-cal,
  * hianyzo .gitattributes szabaly: e nelkul a git a klonozasnal visszaalakitana
    a sorvegeket.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

fail = 0


def check(name, got, want):
    global fail
    if got == want:
        print(f"ok    {name:<48} {got!r}")
    else:
        fail = 1
        print(f"HIBA  {name:<48} kapott={got!r}  vart={want!r}")


batok = sorted(REPO.glob("*.bat"))
check("van parancsfajl", bool(batok), True)

for bat in batok:
    adat = bat.read_bytes()
    check(f"{bat.name}: minden sorveg CRLF", adat.count(b"\n") - adat.count(b"\r\n"), 0)
    check(f"{bat.name}: nincs ekezetes karakter", [b for b in adat if b > 127], [])
    check(f"{bat.name}: nincs BOM", adat[:3] == b"\xef\xbb\xbf", False)

    szoveg = adat.decode("ascii", "replace")
    for nev in ("indit.py",):
        if nev in szoveg:
            check(f"{bat.name}: hivatkozik ra, es megvan ({nev})", (REPO / nev).is_file(), True)
    # Tobbsoros zarojeles blokk: pont ezen csuszik el a cmd, ha a sorveg megis
    # LF lenne. Ezert egyetlen sor sem vegzodhet nyito zarojelre.
    check(
        f"{bat.name}: nincs tobbsoros zarojeles blokk",
        [sor for sor in szoveg.splitlines() if sor.rstrip().endswith("(")],
        [],
    )
    check(f"{bat.name}: kiirja az indito verziojat", "[indito v" in szoveg, True)

    cimkek = {
        sor.strip().lstrip(":").lower()
        for sor in szoveg.splitlines()
        if sor.strip().startswith(":")
    }
    ugrasok = {
        sor.strip().split()[1].lstrip(":").lower()
        for sor in szoveg.splitlines()
        if sor.strip().lower().startswith("goto ")
    }
    check(f"{bat.name}: minden goto-nak van cimkeje", sorted(ugrasok - cimkek), [])
    check(f"{bat.name}: hibaag vegen pause van", "pause" in szoveg, True)

attr = REPO / ".gitattributes"
check(".gitattributes letezik", attr.is_file(), True)
if attr.is_file():
    check(
        ".gitattributes CRLF-et ir elo a .bat fajlokra",
        "*.bat text eol=crlf" in attr.read_text(encoding="utf-8"),
        True,
    )

print("\nA parancsfajl ellenorzese rendben." if not fail else "\nHIBA a parancsfajlban.")
sys.exit(fail)
