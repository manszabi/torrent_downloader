"""A háttérdémon: egyetlen torrent letöltése libtorrent motorral."""

from __future__ import annotations

import base64
import contextlib
import errno
import logging
import logging.handlers
import os
import selectors
import shutil
import signal
import socket
import time
from pathlib import Path
from typing import Any, cast

import libtorrent as lt

from . import config as cfgmod
from .format import human_bytes as _meret
from .lock import SingleInstanceLock
from .protocol import recv_line, send_line, tokens_match

log = logging.getLogger("torrentdl")

STATE_DOWNLOADING = "downloading"
STATE_PAUSED = "paused"
STATE_VERIFYING = "verifying"
STATE_SEEDING = "seeding"
STATE_ERROR = "error"

MAX_VERIFY_ATTEMPTS = 2

# Ennyiszer próbáljuk magunktól helyrehozni a lemezhibát (ellenőrzés + újratöltés),
# utána már a felhasználónak kell közbelépnie.
MAX_HIBA_JAVITAS = 3

# Az ellenőrzés indításának okai (a naplóban és az állapotban is ez látszik).
OK_BEFEJEZES = "befejezés"
OK_INDULAS = "nem tiszta leállás"
OK_LEMEZHIBA = "lemezhiba"
OK_KERES = "kérésre"

DHT_BOOTSTRAP = ",".join(
    [
        "router.bittorrent.com:6881",
        "router.utorrent.com:6881",
        "dht.transmissionbt.com:6881",
        "dht.libtorrent.org:25401",
    ]
)


DHT_NODES_METRIKA = "dht.dht_nodes"


def _dht_metrika_index() -> int:
    """A DHT-számláló helye a statisztikában (a régebbi, listás kötésekhez)."""
    for metrika in lt.session_stats_metrics():
        if metrika.name == DHT_NODES_METRIKA:
            return metrika.value_index
    return -1


DHT_NODES_INDEX = _dht_metrika_index()


def dht_nodes_ertek(alert) -> int | None:
    """A DHT-node szám a session statisztikából, vagy None.

    A régi session.status() elavult, helyette a post_session_stats() +
    session_stats_alert páros a támogatott út. A Python-kötés a számlálókat
    névvel indexelt szótárban adja (régebbi változatok listában), ezért
    mindkettőt kezeljük.
    """
    ertekek = getattr(alert, "values", None)
    if isinstance(ertekek, dict):
        ertek = ertekek.get(DHT_NODES_METRIKA)
    elif ertekek is not None and 0 <= DHT_NODES_INDEX < len(ertekek):
        ertek = ertekek[DHT_NODES_INDEX]
    else:
        ertek = None
    return int(ertek) if ertek is not None else None


# Ilyen sűrűn kérünk session statisztikát (csak amíg van munka).
STATISZTIKA_MP = 5.0


def setup_logging(logfile: Path, to_stderr: bool = False) -> None:
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler = logging.handlers.RotatingFileHandler(
        str(logfile), maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(fmt)
    log.addHandler(handler)
    if to_stderr:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        log.addHandler(stream)


def _enc_policy(name: str) -> int:
    return {
        "disabled": lt.enc_policy.disabled,
        "enabled": lt.enc_policy.enabled,
        "forced": lt.enc_policy.forced,
    }.get(str(name).lower(), lt.enc_policy.enabled)


# Tele lemez hibakodja: POSIX-on ENOSPC, Windowson ERROR_DISK_FULL /
# ERROR_HANDLE_DISK_FULL. (A számokat platformonként külön nézzük, mert
# ugyanaz a szám máshol egészen mást jelent.)
HELY_HIBAKODOK = (112, 39) if os.name == "nt" else (errno.ENOSPC,)
HELY_SZOVEGEK = ("no space", "disk full", "not enough space", "nincs elég hely")


def _hely_hiba(alert) -> bool:
    """Igaz, ha a hibát a betelt lemez okozta."""
    hiba = getattr(alert, "error", None)
    if hiba is not None:
        with contextlib.suppress(Exception):
            if hiba.value() in HELY_HIBAKODOK:
                return True
    uzenet = alert.message().lower()
    return any(jel in uzenet for jel in HELY_SZOVEGEK)


def _torrent_bajtok(request: dict) -> bytes:
    """A kérésben érkező .torrent tartalma (base64, régebbi felülettől hex)."""
    if request.get("torrent_b64"):
        return base64.b64decode(request["torrent_b64"])
    return bytes.fromhex(request["torrent_data"])


def state_name(status) -> str:
    try:
        return {
            lt.torrent_status.states.checking_files: "ellenőrzés",
            lt.torrent_status.states.downloading_metadata: "metaadat letöltése",
            lt.torrent_status.states.downloading: "letöltés",
            lt.torrent_status.states.finished: "kész",
            lt.torrent_status.states.seeding: "megosztás",
            lt.torrent_status.states.checking_resume_data: "folytatás ellenőrzése",
        }[status.state]
    except (KeyError, AttributeError):
        return str(status.state)


def torrent_celpont(save_path: str | Path, nev: str) -> Path | None:
    """A torrent saját bejegyzése a célmappában: `save_path/<név>`.

    Egy fájlos torrentnél ez maga a fájl, több fájlosnál a torrent könyvtára.
    None, ha a név nem használható: üresre, útvonal-elválasztót vagy ".."-t
    tartalmazó névre nem törlünk (magnet linkből a név a felhasználótól jön,
    így nem mutathat a célmappán kívülre).
    """
    nev = (nev or "").strip()
    if not nev or nev in (".", "..") or any(jel in nev for jel in ("/", "\\", os.sep)):
        return None
    gyoker = Path(save_path)
    celpont = gyoker / nev
    try:
        if celpont.resolve().parent != gyoker.resolve():
            return None  # jelkapcsolat vagy trükkös név: kilépne a célmappából
    except OSError:
        return None
    return celpont


def maradt_fajl(konyvtar: Path) -> bool:
    """Maradt-e fájl a könyvtárban (bármilyen mélyen)."""
    return any(fajlok for _gyoker, _mappak, fajlok in os.walk(konyvtar))


def torrent_konyvtar_takaritas(save_path: str | Path, nev: str) -> None:
    """A torrent üresen maradt könyvtárának eltakarítása.

    A libtorrent a fájlokat törli, a könyvtárakat viszont otthagyja – pedig a
    felhasználó a torrentet a mappájával együtt akarta törölni. Csak akkor
    törlünk, ha tényleg nem maradt benne fájl (ha igen, azok nem a torrenthez
    tartoznak, és nem a mi dolgunk eltakarítani őket).
    """
    celpont = torrent_celpont(save_path, nev)
    if celpont is None or not celpont.is_dir() or celpont.is_symlink():
        return
    if maradt_fajl(celpont):
        log.info("a torrent könyvtára nem üres, marad: %s", celpont)
        return
    try:
        shutil.rmtree(celpont)
    except OSError as exc:
        log.warning("a torrent könyvtárát nem sikerült törölni: %s", exc)
    else:
        log.info("a torrent könyvtára törölve: %s", celpont)


def celmappan_belul(gyoker: Path, relativ: str) -> Path | None:
    """A célmappán belüli útvonal, vagy None, ha kilógna belőle.

    A fájllista a torrent metaadatából jön, a célmappa a felhasználótól – a
    kettő találkozásánál ellenőrizzük, hogy tényleg a célmappán belül maradunk.
    """
    if not relativ:
        return None
    ut = gyoker / relativ
    try:
        feloldott, gyoker_f = ut.resolve(), gyoker.resolve()
    except OSError:
        return None
    if feloldott == gyoker_f or gyoker_f not in feloldott.parents:
        return None
    return ut


def torrent_gyokermappa(fajlok: list[str], nev: str) -> str:
    """A torrent saját mappája a célmappában (több fájlos torrentnél)."""
    elsok = {Path(f).parts[0] for f in fajlok if Path(f).parts}
    tobb_szintu = any(len(Path(f).parts) > 1 for f in fajlok)
    if tobb_szintu and len(elsok) == 1:
        return elsok.pop()
    return nev


def torrent_fajllista(handle) -> list[str]:
    """A torrent fájljainak útvonala a célmappához képest."""
    try:
        info = handle.torrent_file()
        if info is None:
            return []
        fajlok = info.files()
        return [fajlok.file_path(i) for i in range(fajlok.num_files())]
    except Exception:  # pragma: no cover - metaadat nélkül nincs lista
        log.warning("a torrent fájllistája nem olvasható ki", exc_info=True)
        return []


def _torles(ut: Path) -> str | None:
    """Egy fájl (vagy mappa) törlése; hiba esetén a rövid üzenet."""
    try:
        if ut.is_symlink() or ut.is_file():
            ut.unlink()
        elif ut.is_dir():
            shutil.rmtree(ut)
    except OSError as exc:
        log.warning("nem sikerült törölni: %s (%s)", ut, exc)
        return f"{ut.name}: {exc}"
    return None


def torrent_fajlok_torlese(
    save_path: str | Path, nev: str, fajlok: list[str] | None = None
) -> str | None:
    """Egy már lezárt (session-ből kivett) torrent fájljainak törlése.

    A befejezett letöltést a libtorrent már nem ismeri, ezért magunk törlünk –
    de csak azt, ami a torrenthez tartozik: a befejezéskor mentett fájllistát.
    Ami mást talál a torrent mappájában (a felhasználó saját fájljai), az marad;
    maga a mappa csak akkor megy, ha üresen maradt.

    Ha a célmappa nem érhető el (pl. leválasztott meghajtó), kivételt dobunk:
    ilyenkor a hívó ne felejtse el a bejegyzést, hogy később újra lehessen
    próbálni. Részleges hiba esetén a figyelmeztetés szövegét adjuk vissza.
    """
    gyoker = Path(save_path)
    if not gyoker.is_dir():
        raise ValueError(f"a célmappa nem érhető el: {gyoker}")
    fajlok = list(fajlok or [])
    utak = []
    for relativ in fajlok:
        ut = celmappan_belul(gyoker, relativ)
        if ut is None:
            return f"a mentett fájllista nem használható, ezért nem törlök: {relativ}"
        utak.append(ut)
    if not utak:
        # Fájllista nélkül csak az egy fájlos torrentnél tudunk biztosat
        # mondani; könyvtárat vaktában nem törlünk.
        celpont = torrent_celpont(gyoker, nev)
        if celpont is None or celpont.is_dir():
            return "a letöltés fájllistája hiányzik, ezért nem törlök semmit"
        utak = [celpont]
    hibak = [uzenet for uzenet in map(_torles, utak) if uzenet]
    torrent_konyvtar_takaritas(gyoker, torrent_gyokermappa(fajlok, nev))
    if hibak:
        return "néhány fájlt nem sikerült törölni – " + "; ".join(hibak[:3])
    log.info("a kész letöltés fájljai törölve: %s", nev)
    return None


class Daemon:
    def __init__(self, foreground: bool = False):
        self.home = cfgmod.home()
        self.cfg = cfgmod.load_config()
        self.endpoint_path = cfgmod.endpoint_path()
        self.lock = SingleInstanceLock(self.home / cfgmod.PID_NAME)
        self.token = cfgmod.new_token()
        self.job_path = self.home / cfgmod.JOB_NAME
        self.last_path = self.home / cfgmod.LAST_NAME
        self.resume_path = self.home / cfgmod.RESUME_NAME
        self.torrent_copy = self.home / cfgmod.TORRENT_COPY_NAME
        self.session_state = self.home / cfgmod.SESSION_STATE_NAME
        self.foreground = foreground

        self.ses: lt.session | None = None
        self.port = 0
        self.handle: lt.torrent_handle | None = None
        self.job: dict | None = None
        self.running = True
        self._clean_exit = False  # csak rendes leállásnál lesz igaz
        # Folyamatban lévő ellenőrzés: {"ok": ..., "kezdet": ..., "latott": bool}
        self.recheck: dict | None = None
        self._hiba_javitasok = 0
        self._hash_hibak = 0
        self._dht_nodes = 0
        self._last_stats_keres = 0.0
        # A legutóbb befejezett letöltés adatai (a last.json gyorsítótára).
        self._last_info: dict | None = None
        self._last_resume_save = 0.0
        self._last_job_flush = 0.0
        self._last_job_sig: tuple = ()
        self._idle_since = time.time()
        self._pending_resume_writes = 0
        self._parancs_tabla: dict | None = None

    # ------------------------------------------------------------------ élet

    def run(self) -> int:
        setup_logging(self.home / cfgmod.LOG_NAME, to_stderr=self.foreground)
        if not self.lock.acquire():
            log.error("már fut egy démon ezzel az adatkönyvtárral")
            return 1
        for sig in (signal.SIGTERM, signal.SIGINT):
            # Néhány környezetben (pl. nem fő szál) nem állítható be a kezelő.
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, self._on_signal)

        try:
            self.ses = self._make_session()
            listener = self._bind_socket()
        except Exception:
            log.exception("a démon indítása sikertelen")
            return 1
        sel = selectors.DefaultSelector()
        sel.register(listener, selectors.EVENT_READ)
        log.info(
            "démon elindult (pid=%s, vezérlőport=%s, torrent port=%s)",
            os.getpid(),
            self.port,
            self.cfg["listen_port"],
        )

        # A legutóbb befejezett letöltés adatát szándékosan NEM olvassuk vissza:
        # az csak a démon életéig érdekes. Ha a fájlok megvannak és nincs
        # megosztás, a program újranyitásakor már nincs mit kezdeni vele – ne az
        # utolsó kész torrent fogadja a felhasználót. Egy korábbi példány (pl.
        # összeomlás) után maradt fájlt is eltakarítunk.
        self._last_takaritas()
        self._restore_job()
        try:
            while self.running:
                # Munka közben sűrűbben nézünk körül; tétlenül ritkábban ébredünk.
                events = sel.select(timeout=0.5 if self.job is not None else 2.0)
                for key, _ in events:
                    if key.fileobj is listener:
                        self._accept(listener)
                self._process_alerts()
                self._periodic()
            self._clean_exit = True
        except Exception:  # pragma: no cover - váratlan hiba naplózása
            log.exception("váratlan hiba a fő ciklusban")
            raise
        finally:
            sel.close()
            listener.close()
            self._shutdown()
        return 0

    def _last_takaritas(self) -> None:
        """A befejezett letöltés lemezre mentett adatának törlése."""
        with contextlib.suppress(OSError):
            self.last_path.unlink()

    def _on_signal(self, signum, _frame):
        log.info("jel érkezett (%s), leállás", signum)
        self.running = False

    def _bind_socket(self) -> socket.socket:
        """Vezérlőcsatorna a hurokcímen; a portot és a jelszót fájlba írjuk."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.listen(8)
        sock.setblocking(False)
        cfgmod.write_endpoint(self.port, self.token)
        return sock

    def _shutdown(self) -> None:
        if self.job is not None and self._clean_exit:
            # Ezzel jelezzük, hogy rendben álltunk le: indulásnál csak akkor
            # ellenőrizzük végig a fájlokat, ha ez a jelzés hiányzik.
            self.job["clean_shutdown"] = True
        if self.handle is not None and self.handle.is_valid():
            try:
                self.handle.pause()
                self._save_resume(blocking=True)
            except Exception:
                log.exception("nem sikerült menteni a folytatási adatot leálláskor")
        self._flush_job()
        if self.ses is not None:
            try:
                cfgmod.write_atomic(
                    self.session_state, lt.write_session_params_buf(self.ses.session_state())
                )
            except Exception:
                log.exception("session állapot mentése sikertelen")
        # A befejezett letöltés adata a démonnal együtt jár le: a következő
        # induláskor a felület alapállapotból indul, nem a régi, kész torrenttel.
        self._last_takaritas()
        with contextlib.suppress(OSError):
            self.endpoint_path.unlink()
        self.lock.release()
        log.info("démon leállt")

    # --------------------------------------------------------------- session

    def _make_session(self) -> lt.session:
        cfg = self.cfg
        port = int(cfg["listen_port"])
        policy = _enc_policy(cfg["encryption"])
        settings = {
            "user_agent": f"torrentdl/1.0 libtorrent/{lt.__version__}",
            "peer_fingerprint": lt.generate_fingerprint("TD", 1, 0, 0, 0),
            "alert_mask": (
                lt.alert_category.status
                | lt.alert_category.error
                | lt.alert_category.storage
                | lt.alert_category.performance_warning
                | lt.alert_category.stats
            ),
            "listen_interfaces": f"0.0.0.0:{port},[::]:{port}",
            "enable_dht": bool(cfg["enable_dht"]),
            "enable_lsd": bool(cfg["enable_lsd"]),
            "enable_upnp": bool(cfg["enable_upnp"]),
            "enable_natpmp": bool(cfg["enable_natpmp"]),
            "enable_outgoing_utp": bool(cfg["enable_utp"]),
            "enable_incoming_utp": bool(cfg["enable_utp"]),
            "dht_bootstrap_nodes": DHT_BOOTSTRAP,
            # Titkosítás (MSE/PE)
            "out_enc_policy": policy,
            "in_enc_policy": policy,
            "allowed_enc_level": lt.enc_level.both,
            "prefer_rc4": True,
            # Korlátok
            "download_rate_limit": int(cfg["max_download_rate"]) * 1024,
            "upload_rate_limit": int(cfg["max_upload_rate"]) * 1024,
            "connections_limit": int(cfg["max_connections"]),
            "announce_to_all_trackers": True,
            "announce_to_all_tiers": True,
            # Lemez és memória (a libtorrent "tuning" ajánlásai alapján):
            # - a hash-számítás külön szálakon fut, ez gyorsítja az ellenőrzést,
            #   amit a program minden befejezésnél és összeomlás után futtat;
            # - a nyitott fájlok gyorsítótára sok fájlból álló torrentnél és
            #   Windowson (vírusirtó a fájlnyitáson) számít sokat;
            # - egyetlen torrenthez nem kell több ezer peer nyilvántartása.
            "hashing_threads": max(1, min(4, os.cpu_count() or 1)),
            "file_pool_size": 128,
            "max_peerlist_size": 1000,
            "max_paused_peerlist_size": 200,
        }
        ismert = lt.default_settings()
        ismeretlen = [kulcs for kulcs in settings if kulcs not in ismert]
        for kulcs in ismeretlen:  # más libtorrent-verzió: ne szálljon el az indulás
            log.warning("ismeretlen libtorrent beállítás, kihagyva: %s", kulcs)
            settings.pop(kulcs)
        params = None
        if self.session_state.exists():
            try:
                params = lt.read_session_params(self.session_state.read_bytes())
                merged = dict(params.settings)
                merged.update(settings)
                # A kötés szótárt is elfogad, a típusleírás csak settings_pack-et.
                params.settings = cast("Any", merged)
            except Exception:
                log.warning("a mentett session állapot sérült, új session indul")
                params = None
        if params is None:
            params = lt.session_params(cast("Any", settings))
        ses = lt.session(params)
        log.info(
            "session kész: DHT=%s PEX=%s LSD=%s titkosítás=%s",
            cfg["enable_dht"],
            cfg["enable_pex"],
            cfg["enable_lsd"],
            cfg["encryption"],
        )
        return ses

    def _torrent_flags(self, base: int, paused: bool) -> int:
        flags = base
        flags &= ~lt.torrent_flags.auto_managed  # kézi vezérlés: mi döntünk a szüneteltetésről
        flags &= ~lt.torrent_flags.paused
        flags &= ~lt.torrent_flags.duplicate_is_error
        if not self.cfg["enable_dht"]:
            flags |= lt.torrent_flags.disable_dht
        if not self.cfg["enable_pex"]:
            flags |= lt.torrent_flags.disable_pex
        if not self.cfg["enable_lsd"]:
            flags |= lt.torrent_flags.disable_lsd
        if paused:
            flags |= lt.torrent_flags.paused
        return flags

    def _build_atp(self, job: dict, use_resume: bool = True):
        atp = None
        if use_resume and self.resume_path.exists():
            try:
                atp = lt.read_resume_data(self.resume_path.read_bytes())
                log.info("folytatási adat betöltve")
            except Exception:
                log.warning("a folytatási adat sérült, elölről indul az ellenőrzés")
                atp = None
        if atp is None:
            atp = self._atp_metaadatbol(job)
        atp.save_path = job["save_path"]
        atp.flags = self._torrent_flags(atp.flags, job["state"] == STATE_PAUSED)
        return atp

    def _atp_metaadatbol(self, job: dict):
        """Torrent-adatok folytatási adat nélkül (a metaadatot megtartva).

        A mentett .torrent másolat magnet linknél is megvan, ha a metaadat
        egyszer már megérkezett – így egy teljes ellenőrzéshez nem kell újra
        letölteni a hálózatról.
        """
        if self.torrent_copy.exists():
            try:
                atp = lt.add_torrent_params()
                atp.ti = lt.torrent_info(str(self.torrent_copy))
            except Exception:
                log.warning("a mentett .torrent nem olvasható, a forrást próbálom")
            else:
                return atp
        if job["source_type"] == "magnet":
            return lt.parse_magnet_uri(job["source"])
        atp = lt.add_torrent_params()
        atp.ti = lt.torrent_info(job["source"])
        return atp

    def _restore_job(self) -> None:
        job = cfgmod.read_json(self.job_path)
        if not job:
            return
        tiszta_leallas = bool(job.pop("clean_shutdown", False))
        if job.get("state") == STATE_SEEDING:
            log.info("a megosztás folytatódik: %s", job.get("name") or job["source"])
        if job.get("state") == STATE_VERIFYING:
            # a félbemaradt ellenőrzés a hozzáadáskori ellenőrzés után újraindul
            job["state"] = STATE_DOWNLOADING
        self.job = job
        # A jelzést azonnal töröljük a lemezről is: ha a program most, indulás
        # közben áll le csúnyán, a következő indulás ne higgye tiszta
        # leállásnak (és így ne hagyja ki a teljes ellenőrzést).
        self._flush_job()
        if not self._cel_elerheto():
            # Fontos, hogy ez a torrent hozzáadása ELŐTT dőljön el: különben a
            # libtorrent a hiányzó meghajtó helyén kezdene mappát/fájlt írni.
            self._on_error(
                f"a célmappa nem érhető el ({job['save_path']}) – csatlakoztasd a "
                "meghajtót, majd nyomd meg a Folytatás gombot"
            )
            return
        # Nem tiszta leállás után a mentett folytatási adat azt állíthatja, hogy
        # minden darab megvan – pedig a lemezen lévő adat sérülhetett. Ha ilyenkor
        # a folytatási adattal adnánk hozzá a torrentet, a teljes ellenőrzés csak
        # a force_recheck-en múlna, és ha az elkésik, a program a hibás adatot
        # épnek hinné. Ezért ilyenkor eleve folytatási adat nélkül adjuk hozzá:
        # a libtorrentnek így muszáj minden darabot újrahasholnia.
        kell_teljes_ellenorzes = not tiszta_leallas and bool(self.cfg["verify_after_crash"])
        try:
            self.handle = self.ses.add_torrent(
                self._build_atp(job, use_resume=not kell_teljes_ellenorzes)
            )
        except Exception as exc:
            log.exception("a mentett letöltés visszaállítása sikertelen")
            self.job["state"] = STATE_ERROR
            self.job["error"] = str(exc)
            self._flush_job()
            return
        log.info("letöltés folytatva: %s (%s)", job.get("name") or job["source"], job["state"])
        if not tiszta_leallas and not self.cfg["verify_after_crash"]:
            log.warning(
                "a program nem rendesen állt le, de az indulási ellenőrzés ki van kapcsolva "
                "(verify_after_crash=false)"
            )
        elif not tiszta_leallas:
            # Áramszünet, összeomlás vagy kilőtt folyamat után a lemezen lévő
            # adat és a mentett folytatási adat eltérhet (a rendszer nem írta ki
            # az utolsó darabokat). Ilyenkor mindent újraellenőrzünk, és ami
            # hiányzik vagy sérült, azt a program újratölti.
            log.warning("a program nem rendesen állt le, teljes ellenőrzés indul")
            self._ellenorzes_indit(OK_INDULAS)
        elif job.get("recheck_pending"):
            self._ellenorzes_indit(job["recheck_pending"])

    # ---------------------------------------------------------------- alertek

    def _process_alerts(self) -> None:
        for alert in self.ses.pop_alerts():
            # Egyetlen hibás alert ne állítsa meg a többi feldolgozását; a
            # try-blokk költsége elhanyagolható az alert kezeléséhez képest.
            try:
                self._handle_alert(alert)
            except Exception:  # noqa: PERF203 - hibatűrés fontosabb a nyereségnél
                log.exception("hiba az alert feldolgozásakor: %s", alert)

    def _handle_alert(self, alert) -> None:
        if isinstance(alert, lt.save_resume_data_alert):
            self._pending_resume_writes = max(0, self._pending_resume_writes - 1)
            cfgmod.write_atomic(self.resume_path, lt.write_resume_data_buf(alert.params))
            log.debug("folytatási adat mentve")
        elif isinstance(alert, lt.save_resume_data_failed_alert):
            self._pending_resume_writes = max(0, self._pending_resume_writes - 1)
            log.warning("folytatási adat mentése sikertelen: %s", alert.message())
        elif isinstance(alert, lt.metadata_received_alert):
            log.info("metaadat megérkezett")
            self._refresh_job_meta()
            self._save_torrent_copy()
            self._save_resume()
        elif isinstance(alert, lt.torrent_finished_alert):
            self._on_finished()
        elif isinstance(alert, lt.torrent_checked_alert):
            log.info("fájlellenőrzés lefutott")
        elif isinstance(alert, (lt.torrent_error_alert, lt.file_error_alert)):
            self._on_lemezhiba(alert)
        elif isinstance(alert, lt.hash_failed_alert):
            # A libtorrent magától újratölti a hibás darabot; mi csak számoljuk.
            self._hash_hibak += 1
            # Rossz peernél ez sűrűn jöhet: ne öntsük tele vele a naplót.
            if self._hash_hibak <= 5 or self._hash_hibak % 25 == 0:
                log.warning(
                    "hibás darab érkezett (%s. alkalom), a program újratölti", self._hash_hibak
                )
        elif isinstance(alert, lt.fastresume_rejected_alert):
            log.warning("folytatási adat elutasítva: %s", alert.message())
        elif isinstance(alert, lt.session_stats_alert):
            ertek = dht_nodes_ertek(alert)
            if ertek is not None:
                self._dht_nodes = ertek
        elif isinstance(alert, lt.state_changed_alert):
            log.debug("állapotváltás: %s", alert.message())

    def _on_finished(self) -> None:
        if self.job is None or self.handle is None or self.recheck:
            return
        if self.job.get("state") == STATE_SEEDING:
            return  # már kész és ellenőrizve: megosztás közben nem ellenőrzünk újra
        self.job["verify_attempts"] = int(self.job.get("verify_attempts", 0)) + 1
        log.info("letöltés kész, a fájlok épségének ellenőrzése indul")
        self._ellenorzes_indit(OK_BEFEJEZES)

    def _ellenorzes_indit(self, ok: str) -> bool:
        """Teljes fájlellenőrzés indítása.

        A libtorrent minden darabot újraolvas és hasheléssel összeveti a
        torrenttel; ami hiányzik vagy sérült, azt utána újratölti. Szüneteltetett
        munkánál az ellenőrzés a folytatásig várat magára.
        """
        if self.job is None or self.handle is None or not self.handle.is_valid():
            return False
        if self.recheck:
            return True  # már fut egy ellenőrzés
        if not self._cel_elerheto():
            self._on_error(
                f"a célmappa nem érhető el ({self.job['save_path']}) – az ellenőrzés "
                "csatlakoztatás után indítható"
            )
            return False
        if self.job["state"] == STATE_PAUSED:
            self.job["recheck_pending"] = ok
            self._flush_job()
            log.info("az ellenőrzés (%s) a folytatáskor indul el", ok)
            return True
        self.job["state"] = STATE_VERIFYING
        self.job["recheck_reason"] = ok
        self.job.pop("recheck_pending", None)
        self._flush_job()
        self.recheck = {"ok": ok, "kezdet": time.time(), "latott": False}
        with contextlib.suppress(Exception):
            self.handle.clear_error()
        self.handle.force_recheck()
        self.handle.resume()
        return True

    def _ellenorzes_figyeles(self) -> None:
        """Az ellenőrzés végét állapotlekérdezéssel követjük.

        A torrent_checked_alert megbízhatatlan időzítésű (a hozzáadáskori
        ellenőrzés alertje ugyanabban a csomagban érkezhet), ezért inkább azt
        figyeljük, mikor lép ki a torrent az ellenőrző állapotból.
        """
        if self.job is None or self.handle is None or not self.handle.is_valid():
            return
        status = self.handle.status()
        checking = (
            lt.torrent_status.states.checking_files,
            lt.torrent_status.states.checking_resume_data,
            lt.torrent_status.states.allocating,
        )
        if status.state in checking:
            self.recheck["latott"] = True
            return
        if not self.recheck["latott"] and time.time() - self.recheck["kezdet"] < 5:
            return  # az ellenőrzés még el sem indult

        ok = self.recheck["ok"]
        self.recheck = None
        self.job.pop("recheck_reason", None)
        if status.progress >= 1.0 and not status.errc.value():
            self._ellenorzes_rendben(ok)
            return
        self._ellenorzes_hianyt_talalt(ok, status)

    def _ellenorzes_rendben(self, ok: str) -> None:
        """Minden darab megvan és ép."""
        self._hiba_javitasok = 0
        self.job["verify_attempts"] = 0
        if ok == OK_BEFEJEZES or not self.job.get("completed_at"):
            log.info("az ellenőrzés sikeres, minden fájl ép")
            self._complete()
            return
        log.info("az ellenőrzés sikeres, a megosztás folytatódik")
        self.job["state"] = STATE_SEEDING
        self.job["error"] = None
        self._refresh_job_meta()
        self._flush_job()
        if self.handle is not None:
            self.handle.resume()

    def _ellenorzes_hianyt_talalt(self, ok: str, status) -> None:
        """Hiányzó vagy sérült adat: a hibás darabokat újratöltjük."""
        # A "wanted" mezők csak a letöltendő fájlokat számolják (ha egyszer
        # lesz fájlválasztás, akkor is jó marad).
        hianyzik = max(0, int(status.total_wanted) - int(status.total_wanted_done))
        if ok == OK_BEFEJEZES and int(self.job.get("verify_attempts", 0)) >= MAX_VERIFY_ATTEMPTS:
            self._on_error("az ellenőrzés ismételten hibás fájlokat talált, a letöltés leállt")
            return
        log.warning(
            "az ellenőrzés hiányzó/sérült adatot talált: %s (%.1f%% van meg) – újratöltés indul",
            _meret(hianyzik),
            status.progress * 100,
        )
        self.job["state"] = STATE_DOWNLOADING
        self.job["error"] = None
        self.job["repaired_bytes"] = int(self.job.get("repaired_bytes", 0)) + hianyzik
        self.job["repaired_at"] = time.time()
        self._refresh_job_meta()  # a mentett haladás is a lemez valóságát mutassa
        self._flush_job()
        if self.handle is not None:
            self.handle.resume()

    def _on_error(self, message: str) -> None:
        """Végleges hiba: a munka megáll, a felhasználónak kell közbelépnie."""
        if self.job is None:
            return
        log.error("hiba: %s", message)
        self.job["state"] = STATE_ERROR
        self.job["error"] = message
        self._flush_job()
        if self.handle is not None and self.handle.is_valid():
            self.handle.pause()

    def _cel_elerheto(self) -> bool:
        """A célmappa a helyén van-e.

        Ha egy külső meghajtót leválasztottak (vagy Windowson megváltozott a
        betűjele), a mappa eltűnik. Ilyenkor nem szabad ellenőrizni és újratölteni:
        a program a régi útvonalra kezdene el letölteni, a meglévő adat pedig
        elérhetetlen marad. Inkább megállunk, és szólunk a felhasználónak.
        """
        return self.job is not None and Path(self.job["save_path"]).is_dir()

    def _on_lemezhiba(self, alert) -> None:
        """Fájl- vagy tárolóhiba: ami menthető, azt ellenőrzéssel helyrehozzuk.

        Tele lemeznél vagy elérhetetlen mappánál nincs mit javítani (az
        újratöltés is ugyanabba a hibába futna), ilyenkor megállunk és szólunk.
        """
        uzenet = alert.message()
        if self.job is None:
            return
        if _hely_hiba(alert):
            self._on_error(f"nincs elég szabad hely a lemezen: {uzenet}")
            return
        if not self._cel_elerheto():
            self._on_error(
                f"a célmappa nem érhető el ({self.job['save_path']}) – ha külső "
                "meghajtóról van szó, csatlakoztasd, majd nyomd meg a Folytatás gombot"
            )
            return
        self._hiba_javitasok += 1
        if self._hiba_javitasok > MAX_HIBA_JAVITAS:
            self._on_error(
                f"ismétlődő lemezhiba ({self._hiba_javitasok - 1}x), "
                f"a letöltés leállt: {uzenet}"
            )
            return
        log.warning(
            "lemezhiba (%d/%d): %s – ellenőrzés és az érintett rész újratöltése",
            self._hiba_javitasok,
            MAX_HIBA_JAVITAS,
            uzenet,
        )
        self.job["error"] = None
        self._ellenorzes_indit(OK_LEMEZHIBA)

    def _complete(self) -> None:
        if self.job is None or self.handle is None:  # pragma: no cover - védelem
            return
        status = self.handle.status()
        info = {
            "name": self.job.get("name") or status.name,
            "save_path": self.job["save_path"],
            "source": self.job["source"],
            "info_hash": self.job.get("info_hash"),
            "total_bytes": int(status.total_wanted),
            "finished_at": time.time(),
            "verified": True,
            # A fájllista a törléshez kell: a kész torrent már nincs a
            # session-ben, így a libtorrent nem tudná megmondani, mi a sajátja.
            "files": torrent_fajllista(self.handle),
        }
        cfgmod.write_json(self.last_path, info)
        self._last_info = info

        if self.cfg["seed_after_complete"]:
            # A torrent a session-ben marad, és a munka is: így a felület
            # mutatja a megosztást, a démon nem lép ki tétlenség miatt, és
            # újraindítás után a mentett állapotból folytatódik a megosztás.
            self.job["state"] = STATE_SEEDING
            self.job["completed_at"] = info["finished_at"]
            self.job["progress"] = 1.0
            self.job["error"] = None
            self._flush_job()
            self._save_resume()
            log.info(
                "kész: %s -> %s (a megosztás folytatódik)", info["name"], info["save_path"]
            )
            return

        self.ses.remove_torrent(self.handle)
        self.handle = None
        self.job = None
        for stale in (self.resume_path, self.job_path, self.torrent_copy):
            with contextlib.suppress(OSError):
                stale.unlink()
        self._idle_since = time.time()
        log.info("kész: %s -> %s (alapállapot)", info["name"], info["save_path"])

    # -------------------------------------------------------------- időzített

    def _periodic(self) -> None:
        now = time.time()
        if self.job is not None and self.recheck:
            self._ellenorzes_figyeles()
        if self.job is not None:
            until = self.job.get("paused_until")
            if self.job["state"] == STATE_PAUSED and until and now >= until:
                log.info("a szüneteltetés lejárt, folytatás")
                self._do_resume()
            if now - self._last_resume_save >= float(self.cfg["resume_save_interval"]):
                self._last_resume_save = now
                if self.job["state"] in (STATE_DOWNLOADING, STATE_VERIFYING, STATE_SEEDING):
                    self._save_resume(only_if_modified=True)
            if now - self._last_job_flush >= 10:
                self._refresh_job_meta()
                self._flush_job(durable=False)
            if now - self._last_stats_keres >= STATISZTIKA_MP:
                self._last_stats_keres = now
                self.ses.post_session_stats()
        # Tétlenül nem tartjuk életben a folyamatot: ami dolga volt, azt a
        # lemezre mentette, és a felület újranyitásakor visszaáll.
        if self._tetlen():
            timeout = int(self.cfg["idle_timeout"])
            if timeout and now - self._idle_since >= timeout:
                log.info("nincs tennivaló %s másodperce, a démon kilép", timeout)
                self.running = False
        else:
            self._idle_since = now

    def _tetlen(self) -> bool:
        """Van-e most tennivalója a démonnak.

        Nem csak az alapállapot tétlen: a határozatlan időre szüneteltetett és
        a hibaállapotban álló munka mellett sem tölt és nem oszt meg semmit –
        ilyenkor is lejárhat a tétlenségi idő. Az *időzített* szünet viszont
        nem: annak a végén nekünk kell folytatnunk a letöltést.
        """
        if self.job is None:
            return True
        if self.recheck:
            return False
        allapot = self.job.get("state")
        if allapot == STATE_ERROR:
            return True
        return allapot == STATE_PAUSED and not self.job.get("paused_until")

    def _save_resume(self, blocking: bool = False, only_if_modified: bool = False) -> None:
        if self.handle is None or not self.handle.is_valid():
            return
        status = self.handle.status()
        if not status.has_metadata:
            return
        flags = lt.torrent_handle.save_info_dict | lt.torrent_handle.flush_disk_cache
        if only_if_modified:
            flags |= lt.torrent_handle.only_if_modified
        self._pending_resume_writes += 1
        self.handle.save_resume_data(flags)
        if blocking:
            deadline = time.time() + 10
            while self._pending_resume_writes > 0 and time.time() < deadline:
                self._process_alerts()
                time.sleep(0.05)

    def _save_torrent_copy(self) -> None:
        """A .torrent tartalmát elmentjük, hogy újraindítás után is meglegyen."""
        if self.handle is None or not self.handle.is_valid():
            return
        info = self.handle.torrent_file()
        if info is None:
            return
        try:
            data = lt.bencode(lt.create_torrent(info).generate())
            cfgmod.write_atomic(self.torrent_copy, data)
        except Exception:
            log.exception(".torrent másolat mentése sikertelen")

    def _refresh_job_meta(self) -> None:
        if self.job is None or self.handle is None or not self.handle.is_valid():
            return
        status = self.handle.status()
        if status.name:
            self.job["name"] = status.name
        self.job["info_hash"] = str(status.info_hashes.get_best())
        self.job["total_bytes"] = int(status.total_wanted)
        self.job["progress"] = float(status.progress)

    def _job_ujjlenyomat(self) -> tuple:
        """Azok a mezők, amiktől érdemes újraírni az állapotfájlt."""
        job = self.job or {}
        return (
            job.get("state"),
            job.get("error"),
            round(float(job.get("progress") or 0.0), 4),
            job.get("paused_until"),
            job.get("recheck_reason"),
            job.get("recheck_pending"),
            job.get("repaired_bytes"),
            job.get("clean_shutdown"),
        )

    def _flush_job(self, durable: bool = True) -> None:
        """Az állapot lemezre írása.

        A rendszeres haladás-mentésnél (durable=False) elhagyjuk az fsync-et, és
        át is ugorjuk az írást, ha semmi lényeges nem változott: így a program
        nem terheli fölöslegesen a lemezt egy több órás letöltés alatt.
        """
        self._last_job_flush = time.time()
        if self.job is None:
            return
        ujjlenyomat = self._job_ujjlenyomat()
        if not durable and ujjlenyomat == self._last_job_sig:
            return
        cfgmod.write_json(self.job_path, self.job, durable=durable)
        self._last_job_sig = ujjlenyomat

    # -------------------------------------------------------------- parancsok

    def _accept(self, listener: socket.socket) -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn:
            try:
                request = recv_line(conn, timeout=5.0)
                if request is None:
                    return
                data = self._dispatch(request)
                send_line(conn, {"ok": True, "data": data})
            except Exception as exc:
                log.exception("parancs hiba")
                with contextlib.suppress(OSError):
                    send_line(conn, {"ok": False, "error": str(exc)})

    def _parancsok(self) -> dict:
        """A parancsnevek és a hozzájuk tartozó eljárások (egyszer épül fel)."""
        if self._parancs_tabla is None:
            self._parancs_tabla = {
                "ping": self.cmd_ping,
                "status": lambda r: self.cmd_status(),
                "add": self.cmd_add,
                "pause": self.cmd_pause,
                "resume": self.cmd_resume,
                "cancel": self.cmd_cancel,
                "discard": self.cmd_discard,
                "clear_log": self.cmd_naplo_urites,
                "check": self.cmd_check,
                "shutdown": self.cmd_shutdown,
            }
        return self._parancs_tabla

    def cmd_ping(self, request: dict):
        return {"pid": os.getpid(), "version": lt.__version__, "port": self.port}

    def _dispatch(self, request: dict):
        if not tokens_match(self.token, request.get("token")):
            raise ValueError("érvénytelen jelszó")
        command = request.get("command")
        handler = self._parancsok().get(command)
        if handler is None:
            raise ValueError(f"ismeretlen parancs: {command}")
        return handler(request)

    def cmd_add(self, request: dict):
        if self.job is not None and self.job.get("state") == STATE_SEEDING:
            # A kész torrent megosztása nem akadálya új letöltésnek: lezárjuk.
            log.info("az előző letöltés megosztása véget ér: %s", self.job.get("name"))
            self.cmd_cancel({"delete_files": False})
        if self.job is not None:
            raise ValueError(
                "már fut egy letöltés (%s) – előbb fejezd be vagy szakítsd meg"
                % (self.job.get("name") or self.job["source"])
            )
        source = request["source"]
        source_type = request["source_type"]
        save_path = request["save_path"]
        Path(save_path).mkdir(parents=True, exist_ok=True)
        if source_type == "file":
            cfgmod.write_atomic(self.torrent_copy, _torrent_bajtok(request))
        with contextlib.suppress(OSError):
            self.resume_path.unlink()
        job = {
            "source": source,
            "source_type": source_type,
            "save_path": str(Path(save_path).resolve()),
            "name": request.get("name") or "",
            "added_at": time.time(),
            "state": STATE_PAUSED if request.get("paused") else STATE_DOWNLOADING,
            "paused_until": None,
            "error": None,
            "verify_attempts": 0,
        }
        self.job = job
        try:
            self.handle = self.ses.add_torrent(self._build_atp(job, use_resume=False))
        except Exception:
            # Hibás torrent esetén ne maradjon "félig hozzáadott" munka, ami
            # minden további letöltést blokkolna.
            self.job = None
            self.handle = None
            with contextlib.suppress(OSError):
                self.job_path.unlink()
            raise
        self._refresh_job_meta()
        self._flush_job()
        self._save_torrent_copy()
        log.info("új letöltés: %s -> %s", job.get("name") or source, job["save_path"])
        return self.cmd_status()

    def cmd_pause(self, request: dict):
        if self.job is None:
            raise ValueError("nincs aktív letöltés")
        seconds = request.get("seconds")
        if self.recheck:
            # Futó ellenőrzés közben is szabad szüneteltetni. A félbehagyott
            # ellenőrzést nem szabad kiértékelni: a leállított torrent haladása
            # ilyenkor hiányosnak látszik, a program "sérült adatot" hinne, és
            # a szünetet felülírva magától újratöltésbe kezdene. Ezért az
            # ellenőrzést elhalasztjuk a folytatásig.
            self.job["recheck_pending"] = self.recheck["ok"]
            self.job.pop("recheck_reason", None)
            self.recheck = None
            log.info("a futó ellenőrzés a folytatáskor indul újra")
        self.job["state"] = STATE_PAUSED
        self.job["paused_until"] = time.time() + float(seconds) if seconds else None
        if self.handle is not None and self.handle.is_valid():
            self.handle.pause()
        self._save_resume()
        self._flush_job()
        log.info("szüneteltetve%s", f" {int(seconds)} másodpercre" if seconds else "")
        return self.cmd_status()

    def _do_resume(self) -> bool:
        """Folytatás. Hamis, ha nem lehet (a hibát ilyenkor beállítja).

        Kivételt szándékosan nem dob: az időzített szünet lejártakor a fő ciklus
        hívja, és egy kivétel ott a démont állítaná le.
        """
        if not self._cel_elerheto():
            self._on_error(
                f"a célmappa nem érhető el ({self.job['save_path']}) – csatlakoztasd "
                "a meghajtót, vagy szakítsd meg a letöltést"
            )
            return False
        # A már befejezett letöltés megosztásba tér vissza, nem letöltésbe.
        self.job["state"] = STATE_SEEDING if self.job.get("completed_at") else STATE_DOWNLOADING
        self.job["paused_until"] = None
        self.job["error"] = None
        if self.handle is not None and self.handle.is_valid():
            self.handle.resume()
        self._flush_job()
        varo = self.job.pop("recheck_pending", None)
        if varo:
            self._ellenorzes_indit(varo)
        return True

    def cmd_resume(self, request: dict):
        if self.job is None:
            raise ValueError("nincs aktív letöltés")
        if not self._do_resume():
            raise ValueError(self.job.get("error") or "a folytatás nem sikerült")
        log.info("letöltés folytatva")
        return self.cmd_status()

    def cmd_cancel(self, request: dict):
        if self.job is None:
            raise ValueError("nincs aktív letöltés")
        delete_files = bool(request.get("delete_files"))
        name = self.job.get("name") or self.job["source"]
        save_path = self.job["save_path"]
        info_hash = self.job.get("info_hash")
        handle, self.handle = self.handle, None
        self.job = None
        self.recheck = None
        warning = None
        if handle is not None and handle.is_valid():
            # metaadat nélkül (pl. friss magnet) még nincs mit törölni a lemezről
            status = handle.status()
            had_files = status.has_metadata
            # A lemezen a torrent saját nevén szerepel (ez nem feltétlenül
            # ugyanaz, mint a munkába mentett név, pl. magnet "dn" mezője).
            nev_lemezen = status.name or name
            self.ses.remove_torrent(handle, lt.session.delete_files if delete_files else 0)
            if delete_files and had_files:
                warning = self._await_delete()
                if warning is None:
                    # A libtorrent a fájlokat törli, az üresen maradt könyvtárat
                    # viszont otthagyja – azt magunk takarítjuk el.
                    torrent_konyvtar_takaritas(save_path, nev_lemezen)
        for stale in (self.resume_path, self.job_path, self.torrent_copy):
            with contextlib.suppress(OSError):
                stale.unlink()
        # Ha ugyanezt a torrentet zártuk le, a "legutóbb befejezett" bejegyzés
        # is elévült: ne kínáljuk fel törlésre a most megszűnt letöltést.
        if self._last_info and self._last_info.get("info_hash") == info_hash:
            self._last_info = None
            self._last_takaritas()
        self._idle_since = time.time()
        log.info("megszakítva: %s (fájlok törlése: %s)", name, delete_files)
        return {"cancelled": name, "files_deleted": delete_files, "warning": warning}

    def _await_delete(self, timeout: float = 15.0) -> str | None:
        """Megvárja a törlés visszaigazolását; hiba esetén figyelmeztetést ad vissza."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for alert in self.ses.pop_alerts():
                if isinstance(alert, lt.torrent_deleted_alert):
                    return None
                if isinstance(alert, lt.torrent_delete_failed_alert):
                    log.warning("a fájlok törlése nem sikerült: %s", alert.message())
                    return f"a fájlok törlése nem sikerült maradéktalanul: {alert.message()}"
                self._handle_alert(alert)
            time.sleep(0.05)
        log.warning("a fájlok törlésének visszaigazolása időtúllépés")
        return "a fájlok törlésének visszaigazolása időtúllépéssel zárult"

    def cmd_discard(self, request: dict):
        """A befejezett letöltés elengedése – kérésre a fájljaival együtt.

        A kész torrent már nincs a session-ben (a program alapállapotba állt),
        ezért a libtorrent nem tudja törölni a fájljait: magunk tesszük meg.
        """
        if self.job is not None:
            raise ValueError("van aktív letöltés – azt a megszakítás zárja le")
        info = self._last_info
        if not info:
            raise ValueError("nincs befejezett letöltés")
        delete_files = bool(request.get("delete_files"))
        warning = None
        if delete_files:
            # Ha a célmappa nem érhető el, ez kivételt dob: a bejegyzés marad,
            # hogy a felhasználó a meghajtó csatlakoztatása után újra próbálja.
            warning = torrent_fajlok_torlese(
                info["save_path"], info.get("name") or "", info.get("files")
            )
        self._last_info = None
        self._last_takaritas()
        self._idle_since = time.time()
        log.info("a kész letöltés elengedve: %s (fájlok törlése: %s)",
                 info.get("name"), delete_files)
        return {"cancelled": info.get("name"), "files_deleted": delete_files, "warning": warning}

    def cmd_naplo_urites(self, request: dict):
        """A naplófájl kiürítése úgy, hogy a futó démon utána is tudjon írni.

        A kezelőt előbb lezárjuk: Windowson a megnyitott fájlt nem lehet
        törölni. A forgatott másolatok (daemon.log.1, .2) is mennek.
        """
        naplo = self.home / cfgmod.LOG_NAME
        for handler in list(log.handlers):
            with contextlib.suppress(Exception):
                handler.close()
        log.handlers.clear()
        for ut in (naplo, *sorted(self.home.glob(cfgmod.LOG_NAME + ".*"))):
            with contextlib.suppress(OSError):
                ut.unlink()
        setup_logging(naplo, to_stderr=self.foreground)
        log.info("a naplót a felhasználó kiürítette")
        return {"cleared": True}

    def cmd_check(self, request: dict):
        """Fájlok ellenőrzése kérésre; a hiányzó/sérült részt újratölti."""
        if self.job is None:
            raise ValueError("nincs aktív letöltés")
        self._ellenorzes_indit(OK_KERES)
        log.info("ellenőrzés kérésre")
        return self.cmd_status()

    def cmd_shutdown(self, request: dict):
        self.running = False
        return {"stopping": True}

    def cmd_status(self):
        data = {
            "daemon": True,
            "pid": os.getpid(),
            "config": self.cfg,
            "last": self._last_info,
            "job": None,
        }
        if self.job is None:
            return data
        job = dict(self.job)
        if self.handle is not None and self.handle.is_valid():
            st = self.handle.status()
            job.update(
                {
                    "name": st.name or job.get("name"),
                    "progress": float(st.progress),
                    "download_rate": int(st.download_payload_rate),
                    "upload_rate": int(st.upload_payload_rate),
                    "downloaded": int(st.total_done),
                    "uploaded": int(st.total_payload_upload),
                    "total_bytes": int(st.total_wanted),
                    "peers": int(st.num_peers),
                    "seeds": int(st.num_seeds),
                    "lt_state": state_name(st),
                    "has_metadata": bool(st.has_metadata),
                    "num_pieces": int(st.num_pieces),
                }
            )
        job["hash_errors"] = self._hash_hibak
        job["disk_repairs"] = self._hiba_javitasok
        job["dht_nodes"] = self._dht_nodes
        data["job"] = job
        return data
