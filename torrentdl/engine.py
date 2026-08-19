"""A háttérdémon: egyetlen torrent letöltése libtorrent motorral."""

from __future__ import annotations

import errno
import fcntl
import logging
import logging.handlers
import os
import selectors
import signal
import socket
import time
from pathlib import Path

import libtorrent as lt

from . import config as cfgmod
from .protocol import recv_line, send_line

log = logging.getLogger("torrentdl")

STATE_DOWNLOADING = "downloading"
STATE_PAUSED = "paused"
STATE_VERIFYING = "verifying"
STATE_ERROR = "error"

MAX_VERIFY_ATTEMPTS = 2

DHT_BOOTSTRAP = ",".join(
    [
        "router.bittorrent.com:6881",
        "router.utorrent.com:6881",
        "dht.transmissionbt.com:6881",
        "dht.libtorrent.org:25401",
    ]
)


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


class Daemon:
    def __init__(self, foreground: bool = False):
        self.home = cfgmod.home()
        self.cfg = cfgmod.load_config()
        self.sock_path = cfgmod.socket_path()
        self.pid_path = self.home / cfgmod.PID_NAME
        self.job_path = self.home / cfgmod.JOB_NAME
        self.last_path = self.home / cfgmod.LAST_NAME
        self.resume_path = self.home / cfgmod.RESUME_NAME
        self.torrent_copy = self.home / cfgmod.TORRENT_COPY_NAME
        self.session_state = self.home / cfgmod.SESSION_STATE_NAME
        self.foreground = foreground

        self.ses: lt.session | None = None
        self.handle = None
        self.job: dict | None = None
        self.running = True
        self.verifying = False
        self._verify_started = 0.0
        self._verify_saw_checking = False
        self._pid_file = None
        self._last_resume_save = 0.0
        self._last_job_flush = 0.0
        self._idle_since = time.time()
        self._pending_resume_writes = 0

    # ------------------------------------------------------------------ élet

    def run(self) -> int:
        setup_logging(self.home / cfgmod.LOG_NAME, to_stderr=self.foreground)
        if not self._acquire_pidfile():
            log.error("már fut egy démon")
            return 1
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

        try:
            self.ses = self._make_session()
            listener = self._bind_socket()
        except Exception:
            log.exception("a démon indítása sikertelen")
            return 1
        sel = selectors.DefaultSelector()
        sel.register(listener, selectors.EVENT_READ)
        log.info("démon elindult (pid=%s, port=%s)", os.getpid(), self.cfg["listen_port"])

        self._restore_job()
        try:
            while self.running:
                events = sel.select(timeout=0.5)
                for key, _ in events:
                    if key.fileobj is listener:
                        self._accept(listener)
                self._process_alerts()
                self._periodic()
        except Exception:  # pragma: no cover - váratlan hiba naplózása
            log.exception("váratlan hiba a fő ciklusban")
            raise
        finally:
            sel.close()
            listener.close()
            self._shutdown()
        return 0

    def _on_signal(self, signum, _frame):
        log.info("jel érkezett (%s), leállás", signum)
        self.running = False

    def _acquire_pidfile(self) -> bool:
        self._pid_file = open(self.pid_path, "a+")
        try:
            fcntl.flock(self._pid_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
        self._pid_file.seek(0)
        self._pid_file.truncate()
        self._pid_file.write(str(os.getpid()))
        self._pid_file.flush()
        return True

    def _bind_socket(self) -> socket.socket:
        if self.sock_path.exists():
            self.sock_path.unlink()  # a pidfile-zár garantálja, hogy ez elárvult socket
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(self.sock_path))
        os.chmod(self.sock_path, 0o600)
        sock.listen(8)
        sock.setblocking(False)
        return sock

    def _shutdown(self) -> None:
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
        try:
            self.sock_path.unlink()
        except OSError:
            pass
        if self._pid_file is not None:
            try:
                fcntl.flock(self._pid_file, fcntl.LOCK_UN)
                self._pid_file.close()
                self.pid_path.unlink()
            except OSError:
                pass
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
        }
        params = None
        if self.session_state.exists():
            try:
                params = lt.read_session_params(self.session_state.read_bytes())
                merged = dict(params.settings)
                merged.update(settings)
                params.settings = merged
            except Exception:
                log.warning("a mentett session állapot sérült, új session indul")
                params = None
        if params is None:
            params = lt.session_params(settings)
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
            if job["source_type"] == "magnet":
                atp = lt.parse_magnet_uri(job["source"])
            else:
                atp = lt.add_torrent_params()
                atp.ti = lt.torrent_info(str(self.torrent_copy))
        atp.save_path = job["save_path"]
        atp.flags = self._torrent_flags(atp.flags, job["state"] == STATE_PAUSED)
        return atp

    def _restore_job(self) -> None:
        job = cfgmod.read_json(self.job_path)
        if not job:
            return
        if job.get("state") == STATE_VERIFYING:
            # a félbemaradt ellenőrzés a hozzáadáskori ellenőrzés után újraindul
            job["state"] = STATE_DOWNLOADING
        self.job = job
        try:
            self.handle = self.ses.add_torrent(self._build_atp(job))
        except Exception as exc:
            log.exception("a mentett letöltés visszaállítása sikertelen")
            self.job["state"] = STATE_ERROR
            self.job["error"] = str(exc)
            self._flush_job()
            return
        log.info("letöltés folytatva: %s (%s)", job.get("name") or job["source"], job["state"])

    # ---------------------------------------------------------------- alertek

    def _process_alerts(self) -> None:
        for alert in self.ses.pop_alerts():
            try:
                self._handle_alert(alert)
            except Exception:
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
            self._on_error(alert.message())
        elif isinstance(alert, lt.fastresume_rejected_alert):
            log.warning("folytatási adat elutasítva: %s", alert.message())
        elif isinstance(alert, lt.state_changed_alert):
            log.info("állapotváltás: %s", alert.message())

    def _on_finished(self) -> None:
        if self.job is None or self.handle is None or self.verifying:
            return
        attempts = int(self.job.get("verify_attempts", 0))
        log.info("letöltés kész, a fájlok épségének ellenőrzése indul")
        self.job["state"] = STATE_VERIFYING
        self.job["verify_attempts"] = attempts + 1
        self._flush_job()
        self.verifying = True
        self._verify_started = time.time()
        self._verify_saw_checking = False
        self.handle.force_recheck()

    def _poll_verification(self) -> None:
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
            self._verify_saw_checking = True
            return
        if not self._verify_saw_checking and time.time() - self._verify_started < 5:
            return  # az újraellenőrzés még el sem indult

        self.verifying = False
        if status.progress >= 1.0 and not status.errc.value():
            log.info("az ellenőrzés sikeres, minden fájl ép")
            self._complete()
            return
        attempts = int(self.job.get("verify_attempts", 0))
        if attempts >= MAX_VERIFY_ATTEMPTS:
            self._on_error("az ellenőrzés hibás fájlokat talált, a letöltés leállt")
            return
        log.warning(
            "az ellenőrzés hiányzó/sérült darabokat talált (%.1f%%), letöltés folytatódik",
            status.progress * 100,
        )
        self.job["state"] = STATE_DOWNLOADING
        self._flush_job()
        self.handle.resume()

    def _on_error(self, message: str) -> None:
        if self.job is None:
            return
        log.error("hiba: %s", message)
        self.job["state"] = STATE_ERROR
        self.job["error"] = message
        self._flush_job()
        if self.handle is not None and self.handle.is_valid():
            self.handle.pause()

    def _complete(self) -> None:
        status = self.handle.status()
        info = {
            "name": self.job.get("name") or status.name,
            "save_path": self.job["save_path"],
            "source": self.job["source"],
            "info_hash": self.job.get("info_hash"),
            "total_bytes": int(status.total_wanted),
            "finished_at": time.time(),
            "verified": True,
        }
        if not self.cfg["seed_after_complete"]:
            self.ses.remove_torrent(self.handle)
        self.handle = None
        self.job = None
        cfgmod.write_json(self.last_path, info)
        for stale in (self.resume_path, self.job_path, self.torrent_copy):
            try:
                stale.unlink()
            except OSError:
                pass
        self._idle_since = time.time()
        log.info("kész: %s -> %s (alapállapot)", info["name"], info["save_path"])

    # -------------------------------------------------------------- időzített

    def _periodic(self) -> None:
        now = time.time()
        if self.job is not None:
            if self.verifying:
                self._poll_verification()
        if self.job is not None:
            until = self.job.get("paused_until")
            if self.job["state"] == STATE_PAUSED and until and now >= until:
                log.info("a szüneteltetés lejárt, folytatás")
                self._do_resume()
            if now - self._last_resume_save >= float(self.cfg["resume_save_interval"]):
                self._last_resume_save = now
                if self.job["state"] in (STATE_DOWNLOADING, STATE_VERIFYING):
                    self._save_resume(only_if_modified=True)
            if now - self._last_job_flush >= 10:
                self._refresh_job_meta()
                self._flush_job()
        else:
            timeout = int(self.cfg["idle_timeout"])
            if timeout and now - self._idle_since >= timeout:
                log.info("nincs letöltés %s másodperce, a démon kilép", timeout)
                self.running = False

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

    def _flush_job(self) -> None:
        self._last_job_flush = time.time()
        if self.job is None:
            return
        cfgmod.write_json(self.job_path, self.job)

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
                try:
                    send_line(conn, {"ok": False, "error": str(exc)})
                except OSError:
                    pass

    def _dispatch(self, request: dict):
        command = request.get("command")
        handler = {
            "ping": lambda r: {"pid": os.getpid(), "version": lt.__version__},
            "status": lambda r: self.cmd_status(),
            "add": self.cmd_add,
            "pause": self.cmd_pause,
            "resume": self.cmd_resume,
            "cancel": self.cmd_cancel,
            "shutdown": self.cmd_shutdown,
        }.get(command)
        if handler is None:
            raise ValueError(f"ismeretlen parancs: {command}")
        return handler(request)

    def cmd_add(self, request: dict):
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
            data = bytes.fromhex(request["torrent_data"])
            cfgmod.write_atomic(self.torrent_copy, data)
        try:
            self.resume_path.unlink()
        except OSError:
            pass
        job = {
            "source": source,
            "source_type": source_type,
            "save_path": os.path.abspath(save_path),
            "name": request.get("name") or "",
            "added_at": time.time(),
            "state": STATE_PAUSED if request.get("paused") else STATE_DOWNLOADING,
            "paused_until": None,
            "error": None,
            "verify_attempts": 0,
        }
        self.job = job
        self.handle = self.ses.add_torrent(self._build_atp(job, use_resume=False))
        self._refresh_job_meta()
        self._flush_job()
        self._save_torrent_copy()
        log.info("új letöltés: %s -> %s", job.get("name") or source, job["save_path"])
        return self.cmd_status()

    def cmd_pause(self, request: dict):
        if self.job is None:
            raise ValueError("nincs aktív letöltés")
        seconds = request.get("seconds")
        self.job["state"] = STATE_PAUSED
        self.job["paused_until"] = time.time() + float(seconds) if seconds else None
        self.handle.pause()
        self._save_resume()
        self._flush_job()
        log.info("szüneteltetve%s", f" {int(seconds)} másodpercre" if seconds else "")
        return self.cmd_status()

    def _do_resume(self) -> None:
        self.job["state"] = STATE_DOWNLOADING
        self.job["paused_until"] = None
        self.job["error"] = None
        if self.handle is not None and self.handle.is_valid():
            self.handle.resume()
        self._flush_job()

    def cmd_resume(self, request: dict):
        if self.job is None:
            raise ValueError("nincs aktív letöltés")
        self._do_resume()
        log.info("letöltés folytatva")
        return self.cmd_status()

    def cmd_cancel(self, request: dict):
        if self.job is None:
            raise ValueError("nincs aktív letöltés")
        delete_files = bool(request.get("delete_files"))
        name = self.job.get("name") or self.job["source"]
        handle, self.handle = self.handle, None
        self.job = None
        self.verifying = False
        warning = None
        if handle is not None and handle.is_valid():
            # metaadat nélkül (pl. friss magnet) még nincs mit törölni a lemezről
            had_files = handle.status().has_metadata
            self.ses.remove_torrent(handle, lt.session.delete_files if delete_files else 0)
            if delete_files and had_files:
                warning = self._await_delete()
        for stale in (self.resume_path, self.job_path, self.torrent_copy):
            try:
                stale.unlink()
            except OSError:
                pass
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

    def cmd_shutdown(self, request: dict):
        self.running = False
        return {"stopping": True}

    def cmd_status(self):
        data = {
            "daemon": True,
            "pid": os.getpid(),
            "config": self.cfg,
            "last": cfgmod.read_json(self.last_path),
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
        job["dht_nodes"] = int(self.ses.status().dht_nodes) if hasattr(self.ses, "status") else 0
        data["job"] = job
        return data
