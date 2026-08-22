"""Grafikus felület a torrent letöltőhöz (tkinter).

A tényleges munkát ugyanaz a háttérdémon végzi, amit a parancssor is használ
(`torrentdl.client`), így az ablak bezárása után is fut tovább a letöltés.

Indítás Windows alatt: kattints duplán az `inditas.bat` fájlra. Máshol:

    python3 -m torrentdl gui
"""

from __future__ import annotations

import contextlib
import os
import queue
import sys
import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import ClassVar

from . import client
from . import config as cfgmod
from .format import (
    eta_seconds,
    human_bytes,
    human_rate,
    human_time,
    parse_duration,
    state_label,
)
from .naplo import naplo_vege

CIM = "Torrent letöltő"
FRISSITES_MP = 1000  # ilyen sűrűn kérdezzük le az állapotot munka közben (ms)
FRISSITES_TETLEN_MP = 3000  # ha nincs aktív munka, ennyi is elég
UZENET_MP = 100  # ilyen sűrűn nézzük meg, üzent-e a háttérszál
KONZOL_ELENGEDES_MP = 500  # ennyivel az ablak megnyitása után válunk le a konzolról

# Ezekben az állapotokban ment a munka, amikor a program megszakadt: ilyet
# magunktól is folytatunk (a szüneteltetett vagy hibára futott munkát nem).
FOLYTATHATO_ALLAPOTOK = ("downloading", "verifying", "seeding")

# Ennyiszer próbáljuk magunktól elindítani a háttérdémont egy ablak-élet alatt.
# Ha a démon indul, majd rögtön elszáll (és így körbe indítanánk újra), ez a
# határ állítja meg: a Folytatás gombbal a felhasználó továbbra is próbálkozhat.
DEMON_INDITAS_MAX = 3

# A háttérdémon jelzőpontja az ablak bal felső sarkában.
PONT = "\u25cf"
PONT_ZOLD = "#2e9e4f"
PONT_PIROS = "#c0392b"
PONT_SARGA = "#d68910"   # épp indul

# Ezt az indító parancsfájl (inditas.bat) állítja be: jelzi, hogy a konzolablak
# a mi indítónké, tehát a felület megnyitása után nincs rá szükség.
KONZOL_JELZO = "TORRENTDL_KONZOL"


def tcl_kornyezet(base: str | None = None, windows: bool | None = None) -> dict:
    """Windowson a virtuális környezetből induló Python nem találja a Tcl/Tk-t.

    A `.venv\\Scripts\\python.exe` mellett nincs `tcl` mappa, a Tk pedig ott
    keresi az `init.tcl`-t, ezért "Can't find a usable init.tcl" hibával elszáll
    az ablak megnyitása. A megoldás, hogy megmutatjuk neki az alap Python
    telepítés `tcl` mappáját. A beállított változókat adja vissza (teszthez).
    """
    if windows is None:
        windows = os.name == "nt"
    if not windows or os.environ.get("TCL_LIBRARY"):
        return {}
    alap = Path(base if base is not None else getattr(sys, "base_prefix", sys.prefix))
    gyoker = alap / "tcl"
    if not gyoker.is_dir():
        return {}
    beallitva: dict[str, str] = {}
    for konyvtar in sorted(gyoker.iterdir()):
        if not konyvtar.is_dir():
            continue
        if konyvtar.name.startswith("tcl8") and "TCL_LIBRARY" not in beallitva:
            beallitva["TCL_LIBRARY"] = str(konyvtar)
        elif konyvtar.name.startswith("tk8") and "TK_LIBRARY" not in beallitva:
            beallitva["TK_LIBRARY"] = str(konyvtar)
    os.environ.update(beallitva)
    return beallitva


def dpi_tudatossag() -> None:
    """Windows alatt a Tk alapból nem DPI-tudatos: 125-150%-os nagyításnál a
    rendszer utólag nagyítja fel az ablakot, amitől elmosódnak a betűk.
    A Tk létrehozása ELŐTT kell hívni."""
    if sys.platform != "win32":  # pragma: no cover - csak Windowson kell
        return
    import ctypes  # noqa: PLC0415 - csak Windowson, csak induláskor

    try:  # pragma: no cover - Windowson fut
        ctypes.OleDLL("shcore").SetProcessDpiAwareness(1)
    except (OSError, AttributeError):  # pragma: no cover - régebbi Windows
        with contextlib.suppress(OSError, AttributeError):
            ctypes.windll.user32.SetProcessDPIAware()


def _konzolrol_levalas() -> bool:
    """A valódi leválasztás: a kimenet a semmibe, majd FreeConsole."""
    if sys.platform != "win32":  # pragma: no cover - csak Windowson értelmes
        return False
    # A leválasztás után a szabvány kimenet írása hibát adna (a leíró érvénytelen
    # lesz), ezért előbb a semmibe irányítjuk. Eddig sem látszott: a konzolablak
    # el volt rejtve. Ami fontos, az a naplófájlba megy.
    semmi = Path(os.devnull).open("w")  # noqa: SIM115 - a folyamat végéig kell
    sys.stdout = semmi
    sys.stderr = semmi
    import ctypes  # noqa: PLC0415 - csak Windowson, csak egyszer

    try:  # pragma: no cover - Windowson fut
        return bool(ctypes.windll.kernel32.FreeConsole())
    except (OSError, AttributeError):
        return False


def konzol_elengedes(
    windows: bool | None = None, levalaszto: Callable[[], bool] | None = None
) -> bool:
    """Végleg elengedjük az indító konzolablakát, amint áll a felület.

    Az indító (inditas.bat) konzolablakát az `indit.py` csak *elrejti*, a
    folyamatunk viszont továbbra is ahhoz a konzolhoz tartozik. Egy rejtett
    konzolt a Windows több alkalommal is visszahoz – például amikor a folyamat
    új gyermeket indít (beállítás mentésekor a háttérdémon újraindul) –, és a
    fekete ablak a felület elé ugrik, a gyermek nevével a címsorában.

    A FreeConsole véglegesen leválaszt: onnantól nincs mit visszahozni, és a
    később indított gyermekfolyamatok sem tudják megörökölni a konzolt.

    Csak akkor nyúlunk hozzá, ha a konzol a mi indítónké (TORRENTDL_KONZOL);
    ha valaki a saját termináljából indította a felületet, az az ablaka marad.
    """
    if windows is None:
        windows = sys.platform == "win32"
    if not windows or not os.environ.get(KONZOL_JELZO):
        return False
    return (levalaszto or _konzolrol_levalas)()


def gomb_allapotok(status: dict) -> dict:
    """Melyik gomb legyen aktív az állapot alapján. (Tk nélkül tesztelhető.)"""
    job = status.get("job")
    state = (job or {}).get("state")
    van_munka = job is not None
    fut = bool(status.get("daemon"))
    # A befejezett letöltés már nem "munka", de a fájljai a lemezen vannak:
    # a Törlés gombbal ezeket is el lehet takarítani.
    van_kesz = not van_munka and status.get("last") is not None
    # A kész torrent megosztása nem akadálya új letöltésnek: az indításkor
    # a megosztás magától véget ér.
    return {
        "indit": not van_munka or state == "seeding",
        # Amíg a démon nem fut, nincs mit szüneteltetni: a munka úgyis áll.
        "szunet": fut and van_munka and state in FOLYTATHATO_ALLAPOTOK,
        # Ha a démon nem fut (összeomlás, gépleállás után), a megszakadt munka
        # a Folytatás gombbal indítható újra. A felület magától is megpróbálja,
        # de a gomb legyen kéznél, ha az nem sikerült.
        "folytat": van_munka and (state in ("paused", "error") or not fut),
        "idozitett": fut and van_munka and state in FOLYTATHATO_ALLAPOTOK,
        "megszakit": van_munka,
        "torol": van_munka or van_kesz,
        # Ellenőrzés akkor van értelme, ha van fájl a lemezen és épp nem megy
        # már egy ellenőrzés.
        "ellenoriz": van_munka and state in ("downloading", "paused", "seeding", "error"),
    }


def demon_inditas_kell(
    status: dict, inditas_fut: bool = False, mar_probaltuk: bool = False
) -> bool:
    """Elinduljon-e magától a háttérdémon a mentett állapot alapján.

    Összeomlás vagy gépleállás után a felület csak a lemezre mentett állapotot
    látja: a démon nem fut, a letöltés áll. Ha a munka *ment*, amikor a program
    megszakadt, magunktól elindítjuk a démont – az a mentett állapotból
    visszaállítja a torrentet, ellenőrzi a lemezen lévő fájlokat, és onnan
    folytatja, ahol abbamaradt. Amit a felhasználó szüneteltetett, vagy ami
    hibára futott, azt nem indítjuk el a háta mögött.
    """
    if status.get("daemon") or inditas_fut or mar_probaltuk:
        return False
    return (status.get("job") or {}).get("state") in FOLYTATHATO_ALLAPOTOK




class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(CIM)
        self.minsize(720, 560)
        self.gui_beallitas = cfgmod.read_json(cfgmod.path(cfgmod.GUI_NAME), {}) or {}

        self.munkak: queue.Queue = queue.Queue()
        self.valaszok: queue.Queue = queue.Queue()
        self.lekerdezes_fut = False
        # None: "még nem tudjuk" – így az ürítés utáni üres napló is kirajzolódik.
        self.utolso_naplo: str | None = ""
        self.utolso_allapot = None
        self.utolso_status: dict = {}
        # A megszakadt munkához magunktól elindítjuk a háttérdémont.
        self.demon_inditas_fut = False
        self.demon_inditas_volt = False
        self.demon_inditasok = 0

        self._epit()
        threading.Thread(target=self._munkas, daemon=True).start()
        self.protocol("WM_DELETE_WINDOW", self._kilepes)
        self.after(UZENET_MP, self._valaszok_feldolgozasa)
        self._allapot_frissites()

    # ------------------------------------------------------------- felület

    def _epit(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        # Jól látható jelzés: fut-e a háttérdémon (ő végzi a munkát).
        fejlec = ttk.Frame(self, padding=(10, 8, 10, 0))
        fejlec.grid(row=0, column=0, sticky="ew")
        self.demon_pont = ttk.Label(
            fejlec, text=PONT, foreground=PONT_PIROS, font=("TkDefaultFont", 14)
        )
        self.demon_pont.grid(row=0, column=0, sticky="w")
        ttk.Label(fejlec, text="Daemon aktív?").grid(row=0, column=1, sticky="w", padx=(4, 0))

        forras = ttk.LabelFrame(self, text="Új letöltés", padding=8)
        forras.grid(row=1, column=0, sticky="ew", padx=8, pady=(8, 4))
        forras.columnconfigure(1, weight=1)

        ttk.Label(forras, text="Torrent / magnet:").grid(row=0, column=0, sticky="w")
        self.forras_valtozo = tk.StringVar()
        self.forras_mezo = ttk.Entry(forras, textvariable=self.forras_valtozo)
        self.forras_mezo.grid(row=0, column=1, sticky="ew", padx=6, pady=2)
        ttk.Button(forras, text="Tallózás…", command=self._torrent_tallozas).grid(row=0, column=2)

        ttk.Label(forras, text="Célmappa:").grid(row=1, column=0, sticky="w")
        self.cel_valtozo = tk.StringVar(
            value=self.gui_beallitas.get("cel") or str(cfgmod.letoltes_mappa())
        )
        ttk.Entry(forras, textvariable=self.cel_valtozo).grid(
            row=1, column=1, sticky="ew", padx=6, pady=2
        )
        ttk.Button(forras, text="Tallózás…", command=self._mappa_tallozas).grid(row=1, column=2)

        self.indit_gomb = ttk.Button(
            forras, text="Letöltés indítása", command=self._inditas
        )
        self.indit_gomb.grid(row=2, column=1, columnspan=2, sticky="e", pady=(6, 0))

        allapot = ttk.LabelFrame(self, text="Aktuális letöltés", padding=8)
        allapot.grid(row=2, column=0, sticky="ew", padx=8, pady=4)
        allapot.columnconfigure(1, weight=1)

        self.nev_cimke = ttk.Label(allapot, text="—", font=("TkDefaultFont", 10, "bold"))
        self.nev_cimke.grid(row=0, column=0, columnspan=3, sticky="w")

        self.allapot_cimke = ttk.Label(allapot, text="Nincs aktív letöltés.")
        self.allapot_cimke.grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 6))

        self.halado = ttk.Progressbar(allapot, mode="determinate", maximum=1000)
        self.halado.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.szazalek_cimke = ttk.Label(allapot, text="0,0%", width=8, anchor="e")
        self.szazalek_cimke.grid(row=2, column=2, sticky="e", padx=(6, 0))

        self.reszlet_cimke = ttk.Label(allapot, text="", justify="left")
        self.reszlet_cimke.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        gombok = ttk.Frame(allapot)
        gombok.grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.szunet_gomb = ttk.Button(gombok, text="Szünet", command=self._szunet)
        self.szunet_gomb.grid(row=0, column=0, padx=(0, 6))
        self.idozitett_gomb = ttk.Button(
            gombok, text="Szünet időre…", command=self._idozitett_szunet
        )
        self.idozitett_gomb.grid(row=0, column=1, padx=6)
        self.folytat_gomb = ttk.Button(gombok, text="Folytatás", command=self._folytatas)
        self.folytat_gomb.grid(row=0, column=2, padx=6)
        self.megszakit_gomb = ttk.Button(
            gombok, text="Megszakítás", command=lambda: self._megszakitas(False)
        )
        # A felirat megosztás közben "Megosztás vége"-re vált (lásd _allapot_kirajzol).
        self.megszakit_gomb.grid(row=0, column=3, padx=6)
        self.ellenoriz_gomb = ttk.Button(gombok, text="Ellenőrzés", command=self._ellenorzes)
        self.ellenoriz_gomb.grid(row=0, column=4, padx=6)
        self.torol_gomb = ttk.Button(
            gombok, text="Törlés a fájlokkal", command=lambda: self._megszakitas(True)
        )
        self.torol_gomb.grid(row=0, column=5, padx=6)

        naplo_keret = ttk.LabelFrame(self, text="Napló", padding=6)
        naplo_keret.grid(row=3, column=0, sticky="nsew", padx=8, pady=4)
        naplo_keret.columnconfigure(0, weight=1)
        naplo_keret.rowconfigure(0, weight=1)
        self.naplo = tk.Text(naplo_keret, height=10, wrap="none", state="disabled")
        self.naplo.grid(row=0, column=0, sticky="nsew")
        gorgeto = ttk.Scrollbar(naplo_keret, orient="vertical", command=self.naplo.yview)
        gorgeto.grid(row=0, column=1, sticky="ns")
        self.naplo.configure(yscrollcommand=gorgeto.set)

        also = ttk.Frame(self, padding=(8, 0, 8, 8))
        also.grid(row=4, column=0, sticky="ew")
        also.columnconfigure(0, weight=1)
        self.statusz_cimke = ttk.Label(also, text="Indulás…")
        self.statusz_cimke.grid(row=0, column=0, sticky="w")
        ttk.Button(also, text="Napló ürítése", command=self._naplo_urites).grid(
            row=0, column=1, padx=(6, 0)
        )
        ttk.Button(also, text="Adatmappa…", command=self._mappa_megnyitas).grid(
            row=0, column=2, padx=6
        )
        ttk.Button(also, text="Beállítások…", command=self._beallitasok).grid(row=0, column=3)

    # --------------------------------------------------------- háttérszál

    def _munkas(self) -> None:
        """Egyetlen háttérszál futtatja a démonhívásokat, hogy az ablak ne fagyjon be."""
        while True:
            fn, cimke = self.munkak.get()
            try:
                self.valaszok.put((cimke, True, fn()))
            except Exception as exc:  # a hibát az ablak jeleníti meg
                self.valaszok.put((cimke, False, exc))

    def _kuld(self, fn, cimke: str) -> None:
        self.munkak.put((fn, cimke))

    def _valaszok_feldolgozasa(self) -> None:
        while True:
            try:
                cimke, sikeres, ertek = self.valaszok.get_nowait()
            except queue.Empty:
                break
            if cimke == "status":
                self.lekerdezes_fut = False
                if sikeres:
                    self._allapot_kirajzol(ertek)
                else:
                    self.statusz_cimke.config(text=f"Nem érhető el a háttérdémon: {ertek}")
            elif cimke in ("cancel", "discard") and sikeres:
                # A démon jelzi, ha a fájlok törlése nem sikerült maradéktalanul.
                figyelem = ertek.get("warning") if isinstance(ertek, dict) else None
                if figyelem:
                    messagebox.showwarning(CIM, str(figyelem), parent=self)
                self._allapot_frissites(azonnal=True)
            elif cimke == "demon":
                # A magunktól indított démon: hiba esetén sem ugrik fel ablak,
                # elég a felirat – a Folytatás gombbal újra lehet próbálni.
                self.demon_inditas_fut = False
                if not sikeres:
                    self.statusz_cimke.config(
                        text=f"Nem sikerült elindítani a háttérdémont: {ertek}"
                    )
                self._allapot_frissites(azonnal=True)
            elif sikeres:
                self._allapot_frissites(azonnal=True)
            else:
                messagebox.showerror(CIM, str(ertek), parent=self)
                self._allapot_frissites(azonnal=True)
        self.after(UZENET_MP, self._valaszok_feldolgozasa)

    def _allapot_frissites(self, azonnal: bool = False) -> None:
        if not self.lekerdezes_fut:
            self.lekerdezes_fut = True
            self._kuld(client.fetch_status, "status")
        if not azonnal:
            koz = FRISSITES_MP if self.utolso_allapot else FRISSITES_TETLEN_MP
            self.after(koz, self._allapot_frissites)

    # ------------------------------------------------------------ kirajzolás

    def _demon_ebresztes(self, status: dict) -> None:
        """Megszakadt munka esetén elindítja a háttérdémont.

        Egy megszakadt munkára egy indítás jut; ha a démon fut, a jelzőt
        visszaállítjuk (egy későbbi összeomlás után újra próbálhatjuk). Az
        elszálló-újrainduló démon körbeindítását a DEMON_INDITAS_MAX zárja ki.
        """
        if status.get("daemon"):
            self.demon_inditas_volt = False
            return
        eleget_probaltuk = (
            self.demon_inditas_volt or self.demon_inditasok >= DEMON_INDITAS_MAX
        )
        if not demon_inditas_kell(status, self.demon_inditas_fut, eleget_probaltuk):
            return
        self.demon_inditas_fut = True
        self.demon_inditas_volt = True
        self.demon_inditasok += 1
        self._kuld(client.ensure_daemon, "demon")

    def _allapot_kirajzol(self, status: dict) -> None:
        self._demon_ebresztes(status)
        self.utolso_status = status
        job = status.get("job")
        self.utolso_allapot = (job or {}).get("state")
        if job:
            szazalek = float(job.get("progress") or 0)
            self.nev_cimke.config(text=job.get("name") or job.get("source", ""))
            self.allapot_cimke.config(text=state_label(job).capitalize())
            self.halado.config(value=szazalek * 1000)
            self.szazalek_cimke.config(text=f"{szazalek * 100:.1f}%".replace(".", ","))
            eta = eta_seconds(job)
            self.reszlet_cimke.config(
                text=(
                    f"{human_bytes(job.get('downloaded'))} / {human_bytes(job.get('total_bytes'))}"
                    f"   •   le {human_rate(job.get('download_rate'))}"
                    f"   fel {human_rate(job.get('upload_rate'))}\n"
                    f"peerek: {job.get('peers', 0)} (seed: {job.get('seeds', 0)})"
                    f"   •   DHT node: {job.get('dht_nodes', 0)}"
                    f"   •   hátralévő idő: {human_time(eta) if eta is not None else '?'}\n"
                    f"cél: {job.get('save_path', '')}"
                    + (f"\nhiba: {job['error']}" if job.get("error") else "")
                )
            )
        else:
            utolso = status.get("last")
            self.nev_cimke.config(text=utolso.get("name") if utolso else "—")
            self.allapot_cimke.config(
                text=(
                    "Nincs aktív letöltés – az előző elkészült és ellenőrizve van."
                    if utolso
                    else "Nincs aktív letöltés."
                )
            )
            self.halado.config(value=1000 if utolso else 0)
            self.szazalek_cimke.config(text="100,0%" if utolso else "0,0%")
            self.reszlet_cimke.config(
                text=(
                    f"{human_bytes(utolso.get('total_bytes'))}   •   cél: {utolso.get('save_path')}"
                    if utolso
                    else "Adj meg egy magnet linket vagy .torrent fájlt, és indítsd el."
                )
            )

        self.megszakit_gomb.config(
            text="Megosztás vége" if self.utolso_allapot == "seeding" else "Megszakítás"
        )
        for nev, aktiv in gomb_allapotok(status).items():
            gomb = {
                "indit": self.indit_gomb,
                "szunet": self.szunet_gomb,
                "folytat": self.folytat_gomb,
                "idozitett": self.idozitett_gomb,
                "megszakit": self.megszakit_gomb,
                "torol": self.torol_gomb,
                "ellenoriz": self.ellenoriz_gomb,
            }[nev]
            gomb.state(["!disabled"] if aktiv else ["disabled"])

        cfg = status.get("config") or {}
        if status.get("daemon"):
            demon_szoveg, pont_szin = "Háttérdémon: fut", PONT_ZOLD
        elif self.demon_inditas_fut:
            demon_szoveg, pont_szin = "Háttérdémon: indul…", PONT_SARGA
        else:
            demon_szoveg, pont_szin = "Háttérdémon: áll", PONT_PIROS
        self.demon_pont.config(foreground=pont_szin)
        self.statusz_cimke.config(
            text=(
                demon_szoveg
                + f"   •   port: {cfg.get('listen_port', '?')}"
                + f"   •   DHT: {'be' if cfg.get('enable_dht') else 'ki'}"
                + f", PEX: {'be' if cfg.get('enable_pex') else 'ki'}"
                + f", titkosítás: {cfg.get('encryption', '?')}"
            )
        )
        self._naplo_frissites()

    def _naplo_frissites(self) -> None:
        szoveg = naplo_vege()
        if szoveg == self.utolso_naplo:
            return
        self.utolso_naplo = szoveg
        vegen_volt = self.naplo.yview()[1] > 0.999
        self.naplo.config(state="normal")
        self.naplo.delete("1.0", "end")
        self.naplo.insert("1.0", szoveg)
        self.naplo.config(state="disabled")
        if vegen_volt:
            self.naplo.see("end")

    # ------------------------------------------------------------- műveletek

    def _torrent_tallozas(self) -> None:
        utvonal = filedialog.askopenfilename(
            parent=self,
            title="Torrent fájl kiválasztása",
            filetypes=[("Torrent fájlok", "*.torrent"), ("Minden fájl", "*.*")],
        )
        if utvonal:
            self.forras_valtozo.set(utvonal)

    def _mappa_tallozas(self) -> None:
        mappa = filedialog.askdirectory(
            parent=self, title="Célmappa kiválasztása", initialdir=self.cel_valtozo.get()
        )
        if mappa:
            self.cel_valtozo.set(mappa)

    def _inditas(self) -> None:
        forras = self.forras_valtozo.get().strip()
        cel = self.cel_valtozo.get().strip()
        if not forras:
            messagebox.showwarning(
                CIM, "Adj meg egy magnet linket vagy .torrent fájlt.", parent=self
            )
            return
        if not cel:
            messagebox.showwarning(CIM, "Válassz célmappát.", parent=self)
            return
        try:
            adat = client.load_source(forras)
            cel_ut = Path(cel).expanduser()
            cel_ut.mkdir(parents=True, exist_ok=True)
        except (client.SourceError, OSError) as exc:
            messagebox.showerror(CIM, str(exc), parent=self)
            return
        cfgmod.write_json(cfgmod.path(cfgmod.GUI_NAME), {"cel": str(cel_ut)})
        self.forras_valtozo.set("")
        self._kuld(
            lambda: client.call("add", save_path=str(cel_ut), paused=False, **adat), "add"
        )
        self.statusz_cimke.config(text="Letöltés indítása…")

    def _szunet(self) -> None:
        self._kuld(lambda: client.call("pause", seconds=None), "pause")

    def _idozitett_szunet(self) -> None:
        parbeszed = IdoKerdes(self)
        self.wait_window(parbeszed)
        if parbeszed.masodperc:
            self._kuld(
                lambda mp=parbeszed.masodperc: client.call("pause", seconds=mp), "pause"
            )

    def _folytatas(self) -> None:
        self._kuld(lambda: client.call("resume"), "resume")

    def _ellenorzes(self) -> None:
        """A lemezen lévő adat teljes ellenőrzése; a hibás részt újratölti."""
        self._kuld(lambda: client.call("check"), "check")

    def _megszakitas(self, fajlokkal: bool) -> None:
        """Megszakítás vagy a kész letöltés elengedése – a fájlokról kérdezünk."""
        status = self.utolso_status or {}
        job = status.get("job")
        utolso = status.get("last")
        if job:
            megoszt = (job.get("state") == "seeding")
            cim = "Megosztás vége" if megoszt else "Megszakítás"
            nev = job.get("name") or job.get("source") or ""
            kerdes = (
                "Befejezed a megosztást?" if megoszt else "Megszakítod az aktuális letöltést?"
            )
            parancs = "cancel"
        elif utolso:
            cim = "Kész letöltés törlése"
            nev = utolso.get("name") or ""
            kerdes = "Törlöd ezt a kész letöltést a listáról?"
            parancs = "discard"
        else:
            return
        parbeszed = MegszakitasKerdes(self, cim, kerdes, nev, fajlokkal)
        self.wait_window(parbeszed)
        if not parbeszed.rendben:
            return
        torol = parbeszed.fajlokkal.get()
        self._kuld(lambda: client.call(parancs, delete_files=torol), parancs)

    def _naplo_urites(self) -> None:
        if not messagebox.askyesno(CIM, "Kiürítsem a naplófájlt?", parent=self):
            return
        # A kirajzolás a szöveg változásán múlik: az üres naplót is látni akarjuk.
        self.utolso_naplo = None
        self._kuld(client.naplo_urites, "naplo")

    def _mappa_megnyitas(self) -> None:
        """A beállítás- és naplófájlok mappája a fájlkezelőben."""
        try:
            client.mappa_megnyitas()
        except OSError as exc:
            messagebox.showerror(CIM, f"Nem sikerült megnyitni a mappát: {exc}", parent=self)

    def _beallitasok(self) -> None:
        parbeszed = Beallitasok(self)
        self.wait_window(parbeszed)
        if parbeszed.mentve:
            self._allapot_frissites(azonnal=True)

    def _kilepes(self) -> None:
        status = None
        with contextlib.suppress(Exception):
            status = client.fetch_status()
        if status and status.get("job"):
            folytat = messagebox.askyesno(
                CIM,
                "A letöltés a háttérben tovább fut az ablak bezárása után is.\n\n"
                "Bezárod az ablakot?",
                parent=self,
            )
            if not folytat:
                return
        elif status and status.get("daemon"):
            # Nincs mit csinálnia a háttérben: ne maradjon ott egy tétlen
            # folyamat a tétlenségi idő leteltéig. Csak a kérést küldjük el, a
            # kilépését nem várjuk meg – a mentéseivel már végzett.
            with contextlib.suppress(Exception):
                client.stop_daemon(wait=0)
        self.destroy()


class IdoKerdes(tk.Toplevel):
    """Mennyi ideig szüneteljen a letöltés."""

    def __init__(self, szulo: tk.Tk | tk.Toplevel):
        super().__init__(szulo)
        self.title("Szüneteltetés")
        self.masodperc: float | None = None
        self.transient(szulo)
        self.resizable(False, False)

        ttk.Label(
            self, text="Meddig szüneteljen?  (pl. 45s, 30m, 2h, 1h30m – egység nélkül perc)"
        ).grid(row=0, column=0, columnspan=2, padx=10, pady=(10, 4), sticky="w")
        self.valtozo = tk.StringVar(value="30m")
        mezo = ttk.Entry(self, textvariable=self.valtozo, width=18)
        mezo.grid(row=1, column=0, padx=10, pady=4, sticky="w")
        mezo.focus_set()
        mezo.bind("<Return>", lambda _e: self._ok())

        gombok = ttk.Frame(self)
        gombok.grid(row=2, column=0, columnspan=2, sticky="e", padx=10, pady=10)
        ttk.Button(gombok, text="Mégse", command=self.destroy).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(gombok, text="Rendben", command=self._ok).grid(row=0, column=1)
        self.grab_set()

    def _ok(self) -> None:
        try:
            self.masodperc = parse_duration(self.valtozo.get())
        except ValueError as exc:
            messagebox.showerror("Szüneteltetés", str(exc), parent=self)
            return
        self.destroy()


class MegszakitasKerdes(tk.Toplevel):
    """Megszakítás/törlés megerősítése – töröljük-e a letöltött fájlokat is.

    A fájlok sorsát szándékosan itt, egy helyen kérdezzük meg: a jelölőnégyzet
    a torrent könyvtárára is vonatkozik (több fájlos torrentnél a fájlok a
    torrent saját mappájába kerülnek).
    """

    def __init__(
        self,
        szulo: tk.Tk | tk.Toplevel,
        cim: str,
        kerdes: str,
        nev: str = "",
        fajlokkal: bool = False,
    ):
        super().__init__(szulo)
        self.title(cim)
        self.transient(szulo)
        self.resizable(False, False)
        self.rendben = False
        self.fajlokkal = tk.BooleanVar(value=fajlokkal)

        keret = ttk.Frame(self, padding=12)
        keret.grid(row=0, column=0, sticky="nsew")
        ttk.Label(keret, text=kerdes).grid(row=0, column=0, columnspan=2, sticky="w")
        if nev:
            ttk.Label(keret, text=nev, font=("TkDefaultFont", 10, "bold")).grid(
                row=1, column=0, columnspan=2, sticky="w", pady=(4, 0)
            )
        ttk.Checkbutton(
            keret,
            text="A letöltött fájlokat (és a torrent mappáját) is töröld",
            variable=self.fajlokkal,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.figyelmeztet = ttk.Label(keret, text="", foreground=PONT_PIROS)
        self.figyelmeztet.grid(row=3, column=0, columnspan=2, sticky="w")
        self.fajlokkal.trace_add("write", self._figyelmeztetes)
        self._figyelmeztetes()

        gombok = ttk.Frame(keret)
        gombok.grid(row=4, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(gombok, text="Mégse", command=self.destroy).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(gombok, text="Rendben", command=self._ok).grid(row=0, column=1)
        self.grab_set()

    def _figyelmeztetes(self, *_args) -> None:
        self.figyelmeztet.config(
            text="A törölt fájlok nem kerülnek a Lomtárba." if self.fajlokkal.get() else ""
        )

    def _ok(self) -> None:
        self.rendben = True
        self.destroy()


class Beallitasok(tk.Toplevel):
    """A démon beállításai (a mentés után újraindul a háttérdémon)."""

    MEZOK: ClassVar[list[tuple[str, str]]] = [
        ("listen_port", "Bejövő port"),
        ("max_download_rate", "Letöltési korlát (kB/s, 0 = korlátlan)"),
        ("max_upload_rate", "Feltöltési korlát (kB/s, 0 = korlátlan)"),
        ("max_connections", "Kapcsolatok száma"),
        ("idle_timeout", "Tétlen démon leáll (mp, 0 = soha)"),
    ]
    KAPCSOLOK: ClassVar[list[tuple[str, str]]] = [
        ("enable_dht", "DHT (tracker nélküli peer-keresés)"),
        ("enable_pex", "PEX (peer-csere)"),
        ("enable_lsd", "Helyi hálózati peer-keresés"),
        ("enable_utp", "µTP transzport"),
        # A portnyitás kérés a routernek: ha nem támogatja vagy tiltja, a
        # letöltés attól még megy (csak kevesebb peer talál meg minket).
        ("enable_upnp", "Port nyitása a routeren (UPnP)"),
        ("enable_natpmp", "Port nyitása a routeren (NAT-PMP)"),
        ("seed_after_complete", "Megosztás folytatása a letöltés után"),
    ]

    def __init__(self, szulo: tk.Tk | tk.Toplevel):
        super().__init__(szulo)
        self.title("Beállítások")
        self.transient(szulo)
        self.resizable(False, False)
        self.mentve = False
        self.cfg = cfgmod.load_config()

        keret = ttk.Frame(self, padding=10)
        keret.grid(row=0, column=0, sticky="nsew")
        self.valtozok: dict[str, tk.Variable] = {}
        sor = 0
        for kulcs, cimke in self.MEZOK:
            ttk.Label(keret, text=cimke).grid(row=sor, column=0, sticky="w", pady=2)
            valtozo = tk.StringVar(value=str(self.cfg[kulcs]))
            ttk.Entry(keret, textvariable=valtozo, width=12).grid(
                row=sor, column=1, sticky="e", padx=(12, 0)
            )
            self.valtozok[kulcs] = valtozo
            sor += 1

        ttk.Label(keret, text="Titkosítás").grid(row=sor, column=0, sticky="w", pady=2)
        self.titkositas = tk.StringVar(value=str(self.cfg["encryption"]))
        ttk.Combobox(
            keret,
            textvariable=self.titkositas,
            values=["disabled", "enabled", "forced"],
            state="readonly",
            width=10,
        ).grid(row=sor, column=1, sticky="e", padx=(12, 0))
        sor += 1

        for kulcs, cimke in self.KAPCSOLOK:
            kapcsolo = tk.BooleanVar(value=bool(self.cfg[kulcs]))
            ttk.Checkbutton(keret, text=cimke, variable=kapcsolo).grid(
                row=sor, column=0, columnspan=2, sticky="w", pady=1
            )
            self.valtozok[kulcs] = kapcsolo
            sor += 1

        ttk.Label(
            keret,
            text="A mentés után a háttérdémon újraindul; a letöltés folytatódik.",
            foreground="#555555",
        ).grid(row=sor, column=0, columnspan=2, sticky="w", pady=(10, 0))
        sor += 1

        gombok = ttk.Frame(keret)
        gombok.grid(row=sor, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(gombok, text="Mégse", command=self.destroy).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(gombok, text="Mentés", command=self._mentes).grid(row=0, column=1)
        self.grab_set()

    def _mentes(self) -> None:
        uj = dict(self.cfg)
        for kulcs, valtozo in self.valtozok.items():
            ertek = valtozo.get()
            try:
                uj[kulcs] = ertek if isinstance(ertek, bool) else cfgmod.coerce(kulcs, str(ertek))
            except ValueError:
                messagebox.showerror(
                    "Beállítások", f"Hibás érték: {kulcs} = {ertek}", parent=self
                )
                return
        uj["encryption"] = self.titkositas.get()
        if not 1 <= int(uj["listen_port"]) <= 65535:
            messagebox.showerror("Beállítások", "A port 1 és 65535 közé essen.", parent=self)
            return
        cfgmod.save_config(uj)
        self.mentve = True
        # Újraindítás, hogy az új beállítások érvényre jussanak; a letöltés
        # állapota lemezen van, ezért a démon ott folytatja, ahol abbahagyta.
        threading.Thread(target=self._ujrainditas, daemon=True).start()
        self.destroy()

    @staticmethod
    def _ujrainditas() -> None:
        with contextlib.suppress(Exception):
            if client.ping():
                client.stop_daemon()
                client.spawn_daemon()


def main() -> int:
    tcl_kornyezet()
    dpi_tudatossag()
    app = App()
    # Az ablak megvan: az indító konzolja innentől csak útban lenne. Nem
    # azonnal engedjük el, hogy az indulás közbeni üzenetek még kiférjenek.
    app.after(KONZOL_ELENGEDES_MP, konzol_elengedes)
    app.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
