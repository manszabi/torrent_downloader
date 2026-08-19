"""Grafikus felület a torrent letöltőhöz (tkinter).

A tényleges munkát ugyanaz a háttérdémon végzi, amit a parancssor is használ
(`torrentdl.client`), így az ablak bezárása után is fut tovább a letöltés.

Indítás Windows alatt: kattints duplán az `inditas.bat` fájlra. Máshol:

    python3 -m torrentdl gui
"""

from __future__ import annotations

import contextlib
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

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

CIM = "Torrent letöltő"
FRISSITES_MP = 1000  # ilyen sűrűn kérdezzük le az állapotot (ezredmásodperc)
UZENET_MP = 100  # ilyen sűrűn nézzük meg, üzent-e a háttérszál
NAPLO_SOROK = 200


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


def gomb_allapotok(status: dict) -> dict:
    """Melyik gomb legyen aktív az állapot alapján. (Tk nélkül tesztelhető.)"""
    job = status.get("job")
    state = (job or {}).get("state")
    van_munka = job is not None
    # A kész torrent megosztása nem akadálya új letöltésnek: az indításkor
    # a megosztás magától véget ér.
    return {
        "indit": not van_munka or state == "seeding",
        "szunet": van_munka and state in ("downloading", "verifying", "seeding"),
        "folytat": van_munka and state in ("paused", "error"),
        "idozitett": van_munka and state in ("downloading", "verifying", "seeding"),
        "megszakit": van_munka,
        "torol": van_munka,
    }


def naplo_vege(sorok: int = NAPLO_SOROK) -> str:
    """A démon naplójának utolsó néhány sora."""
    utvonal = cfgmod.home() / cfgmod.LOG_NAME
    try:
        with open(utvonal, "rb") as fh:
            fh.seek(0, 2)
            meret = fh.tell()
            fh.seek(max(0, meret - 64 * 1024))
            adat = fh.read()
    except OSError:
        return ""
    szoveg = adat.decode("utf-8", "replace")
    return "\n".join(szoveg.splitlines()[-sorok:])


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(CIM)
        self.minsize(720, 560)
        self.gui_beallitas = cfgmod.read_json(cfgmod.path(cfgmod.GUI_NAME), {}) or {}

        self.munkak: queue.Queue = queue.Queue()
        self.valaszok: queue.Queue = queue.Queue()
        self.lekerdezes_fut = False
        self.utolso_naplo = ""
        self.utolso_allapot = None

        self._epit()
        threading.Thread(target=self._munkas, daemon=True).start()
        self.protocol("WM_DELETE_WINDOW", self._kilepes)
        self.after(UZENET_MP, self._valaszok_feldolgozasa)
        self._allapot_frissites()

    # ------------------------------------------------------------- felület

    def _epit(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        forras = ttk.LabelFrame(self, text="Új letöltés", padding=8)
        forras.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        forras.columnconfigure(1, weight=1)

        ttk.Label(forras, text="Torrent / magnet:").grid(row=0, column=0, sticky="w")
        self.forras_valtozo = tk.StringVar()
        self.forras_mezo = ttk.Entry(forras, textvariable=self.forras_valtozo)
        self.forras_mezo.grid(row=0, column=1, sticky="ew", padx=6, pady=2)
        ttk.Button(forras, text="Tallózás…", command=self._torrent_tallozas).grid(row=0, column=2)

        ttk.Label(forras, text="Célmappa:").grid(row=1, column=0, sticky="w")
        self.cel_valtozo = tk.StringVar(value=self.gui_beallitas.get("cel", str(Path.home())))
        ttk.Entry(forras, textvariable=self.cel_valtozo).grid(
            row=1, column=1, sticky="ew", padx=6, pady=2
        )
        ttk.Button(forras, text="Tallózás…", command=self._mappa_tallozas).grid(row=1, column=2)

        self.indit_gomb = ttk.Button(
            forras, text="Letöltés indítása", command=self._inditas
        )
        self.indit_gomb.grid(row=2, column=1, columnspan=2, sticky="e", pady=(6, 0))

        allapot = ttk.LabelFrame(self, text="Aktuális letöltés", padding=8)
        allapot.grid(row=1, column=0, sticky="ew", padx=8, pady=4)
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
        self.torol_gomb = ttk.Button(
            gombok, text="Törlés a fájlokkal", command=lambda: self._megszakitas(True)
        )
        self.torol_gomb.grid(row=0, column=4, padx=6)

        naplo_keret = ttk.LabelFrame(self, text="Napló", padding=6)
        naplo_keret.grid(row=2, column=0, sticky="nsew", padx=8, pady=4)
        naplo_keret.columnconfigure(0, weight=1)
        naplo_keret.rowconfigure(0, weight=1)
        self.naplo = tk.Text(naplo_keret, height=10, wrap="none", state="disabled")
        self.naplo.grid(row=0, column=0, sticky="nsew")
        gorgeto = ttk.Scrollbar(naplo_keret, orient="vertical", command=self.naplo.yview)
        gorgeto.grid(row=0, column=1, sticky="ns")
        self.naplo.configure(yscrollcommand=gorgeto.set)

        also = ttk.Frame(self, padding=(8, 0, 8, 8))
        also.grid(row=3, column=0, sticky="ew")
        also.columnconfigure(0, weight=1)
        self.statusz_cimke = ttk.Label(also, text="Indulás…")
        self.statusz_cimke.grid(row=0, column=0, sticky="w")
        ttk.Button(also, text="Beállítások…", command=self._beallitasok).grid(row=0, column=1)

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
            self.after(FRISSITES_MP, self._allapot_frissites)

    # ------------------------------------------------------------ kirajzolás

    def _allapot_kirajzol(self, status: dict) -> None:
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
            }[nev]
            gomb.state(["!disabled"] if aktiv else ["disabled"])

        cfg = status.get("config") or {}
        self.statusz_cimke.config(
            text=(
                ("Háttérdémon: fut" if status.get("daemon") else "Háttérdémon: áll")
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
            messagebox.showwarning(CIM, "Adj meg egy magnet linket vagy .torrent fájlt.", parent=self)
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

    def _megszakitas(self, fajlokkal: bool) -> None:
        megoszt = self.utolso_allapot == "seeding"
        if fajlokkal:
            kerdes = "Biztosan törlöd ezt a torrentet a fájljaival együtt?"
        elif megoszt:
            kerdes = ("Befejezed a megosztást?\n"
                      "A letöltött fájlok a helyükön maradnak.")
        else:
            kerdes = ("Biztosan megszakítod az aktuális letöltést?\n"
                      "A részben letöltött fájlok a helyükön maradnak.")
        if not messagebox.askyesno(CIM, kerdes, parent=self, default="no" if fajlokkal else "yes"):
            return
        self._kuld(lambda: client.call("cancel", delete_files=fajlokkal), "cancel")

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
        self.destroy()


class IdoKerdes(tk.Toplevel):
    """Mennyi ideig szüneteljen a letöltés."""

    def __init__(self, szulo: tk.Misc):
        super().__init__(szulo)
        self.title("Szüneteltetés")
        self.masodperc = None
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


class Beallitasok(tk.Toplevel):
    """A démon beállításai (a mentés után újraindul a háttérdémon)."""

    MEZOK = [
        ("listen_port", "Bejövő port"),
        ("max_download_rate", "Letöltési korlát (kB/s, 0 = korlátlan)"),
        ("max_upload_rate", "Feltöltési korlát (kB/s, 0 = korlátlan)"),
        ("max_connections", "Kapcsolatok száma"),
        ("idle_timeout", "Tétlen démon leáll (mp, 0 = soha)"),
    ]
    KAPCSOLOK = [
        ("enable_dht", "DHT (tracker nélküli peer-keresés)"),
        ("enable_pex", "PEX (peer-csere)"),
        ("enable_lsd", "Helyi hálózati peer-keresés"),
        ("enable_utp", "µTP transzport"),
        ("seed_after_complete", "Megosztás folytatása a letöltés után"),
    ]

    def __init__(self, szulo: tk.Misc):
        super().__init__(szulo)
        self.title("Beállítások")
        self.transient(szulo)
        self.resizable(False, False)
        self.mentve = False
        self.cfg = cfgmod.load_config()

        keret = ttk.Frame(self, padding=10)
        keret.grid(row=0, column=0, sticky="nsew")
        self.valtozok = {}
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
            valtozo = tk.BooleanVar(value=bool(self.cfg[kulcs]))
            ttk.Checkbutton(keret, text=cimke, variable=valtozo).grid(
                row=sor, column=0, columnspan=2, sticky="w", pady=1
            )
            self.valtozok[kulcs] = valtozo
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
    dpi_tudatossag()
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
