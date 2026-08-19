"""Parancssori felület a torrentdl-hez."""

from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__
from . import config as cfgmod
from .protocol import DaemonError, request

DURATION_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[smhd]?)", re.IGNORECASE)
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "": 60}


def parse_duration(text: str) -> float:
    """Időtartam szövegből másodperc.

    Egység nélkül percet jelent ('30' = 30 perc); '45s', '2h', '1h30m' is használható.
    """
    total = 0.0
    pos = 0
    found = False
    for match in DURATION_RE.finditer(text.strip()):
        if match.start() != pos:
            break
        pos = match.end()
        total += float(match.group("value")) * UNIT_SECONDS[match.group("unit").lower()]
        found = True
    if not found or pos != len(text.strip()):
        raise argparse.ArgumentTypeError(f"értelmezhetetlen időtartam: {text}")
    return total


# ------------------------------------------------------------------ kiírás


def human_bytes(num: float) -> str:
    num = float(num or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024 or unit == "TiB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.2f} {unit}"
        num /= 1024
    return f"{num:.2f} TiB"


def human_rate(num: float) -> str:
    return human_bytes(num) + "/s"


def human_time(seconds: float) -> str:
    if seconds is None or seconds < 0 or seconds == float("inf"):
        return "?"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}n {hours}ó {minutes}p"
    if hours:
        return f"{hours}ó {minutes}p"
    if minutes:
        return f"{minutes}p {secs}mp"
    return f"{secs}mp"


def progress_bar(fraction: float, width: int = 30) -> str:
    fraction = max(0.0, min(1.0, float(fraction or 0)))
    filled = int(round(fraction * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


STATE_LABELS = {
    "downloading": "letöltés",
    "paused": "szüneteltetve",
    "verifying": "ellenőrzés",
    "error": "hiba",
}


def render_status(data: dict) -> str:
    lines = []
    job = data.get("job")
    if not job:
        lines.append("Nincs aktív letöltés (alapállapot).")
        last = data.get("last")
        if last:
            lines.append(
                "Utoljára befejezve: %s\n  helye: %s\n  mérete: %s, ellenőrizve: %s"
                % (
                    last.get("name"),
                    last.get("save_path"),
                    human_bytes(last.get("total_bytes", 0)),
                    "igen" if last.get("verified") else "nem",
                )
            )
        if not data.get("daemon"):
            lines.append("A háttérdémon nem fut.")
        return "\n".join(lines)

    state = job.get("state", "?")
    label = STATE_LABELS.get(state, state)
    if state == "paused" and job.get("paused_until"):
        remaining = job["paused_until"] - time.time()
        label += f" (folytatás {human_time(max(0, remaining))} múlva)"
    if job.get("lt_state") and state == "downloading":
        label += f" – {job['lt_state']}"

    progress = float(job.get("progress") or 0)
    total = float(job.get("total_bytes") or 0)
    done = float(job.get("downloaded") or 0)
    rate = float(job.get("download_rate") or 0)
    eta = (total - done) / rate if rate > 0 and total > done else None

    lines.append(f"Név:      {job.get('name') or job.get('source')}")
    lines.append(f"Állapot:  {label}")
    lines.append(f"Haladás:  {progress_bar(progress)} {progress * 100:5.1f}%")
    lines.append(f"Méret:    {human_bytes(done)} / {human_bytes(total)}")
    lines.append(
        "Sebesség: le %s | fel %s | peerek: %s (seed: %s) | DHT node: %s"
        % (
            human_rate(rate),
            human_rate(job.get("upload_rate") or 0),
            job.get("peers", 0),
            job.get("seeds", 0),
            job.get("dht_nodes", 0),
        )
    )
    if state == "downloading":
        lines.append(f"Hátralévő idő: {human_time(eta) if eta is not None else '?'}")
    lines.append(f"Cél:      {job.get('save_path')}")
    if job.get("error"):
        lines.append(f"Hiba:     {job['error']}")
    if not data.get("daemon"):
        lines.append(
            "FIGYELEM: a háttérdémon nem fut, a letöltés megszakadt. "
            "Folytatás: torrentdl resume"
        )
    return "\n".join(lines)


# ------------------------------------------------------------------ démon


def socket_path() -> Path:
    return cfgmod.socket_path()


def daemon_ping(timeout: float = 2.0):
    try:
        return request(socket_path(), "ping", timeout=timeout)
    except (OSError, socket.error, DaemonError, ValueError):
        return None


def spawn_daemon(wait: float = 20.0) -> bool:
    if daemon_ping():
        return True
    cmd = [sys.executable, "-m", "torrentdl", "daemon", "run"]
    with open(os.devnull, "wb") as devnull:
        subprocess.Popen(
            cmd,
            stdin=devnull,
            stdout=devnull,
            stderr=devnull,
            start_new_session=True,
            cwd=str(Path.cwd()),
        )
    deadline = time.time() + wait
    while time.time() < deadline:
        if daemon_ping(timeout=1.0):
            return True
        time.sleep(0.25)
    return False


def ensure_daemon() -> None:
    if not spawn_daemon():
        raise SystemExit(
            "Nem sikerült elindítani a háttérdémont. Napló: %s"
            % (cfgmod.home() / cfgmod.LOG_NAME)
        )


def call(command: str, **payload):
    ensure_daemon()
    try:
        return request(socket_path(), command, **payload)
    except DaemonError as exc:
        raise SystemExit(f"Hiba: {exc}")
    except OSError as exc:
        raise SystemExit(f"Nem érhető el a háttérdémon: {exc}")


# ------------------------------------------------------------------ forrás


def load_source(source: str) -> dict:
    """Magnet link, .torrent fájl vagy .torrent URL feldolgozása."""
    if source.startswith("magnet:"):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(source).query)
        name = (query.get("dn") or [""])[0]
        return {"source": source, "source_type": "magnet", "name": name}

    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=30) as response:
            data = response.read()
        if data[:1] != b"d":
            raise SystemExit("A megadott URL nem .torrent fájlt adott vissza.")
        return {
            "source": source,
            "source_type": "file",
            "torrent_data": data.hex(),
            "name": torrent_name(data),
        }

    path = Path(source).expanduser()
    if not path.is_file():
        raise SystemExit(f"Nincs ilyen fájl: {source}")
    data = path.read_bytes()
    return {
        "source": str(path.resolve()),
        "source_type": "file",
        "torrent_data": data.hex(),
        "name": torrent_name(data),
    }


def torrent_name(data: bytes) -> str:
    try:
        import libtorrent as lt

        return lt.torrent_info(lt.bdecode(data)).name()
    except Exception:
        return ""


# ------------------------------------------------------------------ parancsok


def cmd_add(args) -> int:
    payload = load_source(args.source)
    save_path = Path(args.dest).expanduser().resolve()
    save_path.mkdir(parents=True, exist_ok=True)
    data = call("add", save_path=str(save_path), paused=args.paused, **payload)
    print("Letöltés hozzáadva.\n")
    print(render_status(data))
    print("\nA letöltés a háttérben fut. Állapot: torrentdl status")
    return 0


def cmd_status(args) -> int:
    if args.watch:
        try:
            while True:
                data = fetch_status()
                sys.stdout.write("\033[2J\033[H")
                print(render_status(data))
                if not data.get("job"):
                    return 0
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0
    print(render_status(fetch_status()))
    return 0


def fetch_status() -> dict:
    """A démont kérdezzük; ha nem fut, a lemezre mentett állapotot mutatjuk."""
    if daemon_ping():
        try:
            return request(socket_path(), "status")
        except (DaemonError, OSError):
            pass
    job = cfgmod.read_json(cfgmod.path(cfgmod.JOB_NAME))
    return {
        "daemon": False,
        "job": job,
        "last": cfgmod.read_json(cfgmod.path(cfgmod.LAST_NAME)),
        "config": cfgmod.load_config(),
    }


def cmd_pause(args) -> int:
    seconds = args.duration
    data = call("pause", seconds=seconds)
    print(render_status(data))
    return 0


def cmd_resume(args) -> int:
    data = call("resume")
    print(render_status(data))
    return 0


def cmd_cancel(args) -> int:
    if args.delete_files and not args.yes:
        answer = input("Biztosan törlöd a letöltést a fájljaival együtt? [i/N] ").strip().lower()
        if answer not in ("i", "igen", "y", "yes"):
            print("Megszakítva.")
            return 1
    if not daemon_ping():
        # A démon nem fut: elég a lemezen tárolt állapotot rendbe tenni.
        return cancel_offline(args.delete_files)
    print_cancelled(call("cancel", delete_files=args.delete_files))
    return 0


def print_cancelled(data: dict) -> None:
    print(
        "Törölve: %s%s"
        % (data["cancelled"], " (a fájlokkal együtt)" if data["files_deleted"] else "")
    )
    if data.get("warning"):
        print(f"Figyelmeztetés: {data['warning']}")


def cancel_offline(delete_files: bool) -> int:
    """Ha nincs futó démon, indítsuk el, hogy a fájltörlést a libtorrent végezze."""
    job = cfgmod.read_json(cfgmod.path(cfgmod.JOB_NAME))
    if not job:
        print("Nincs aktív letöltés.")
        return 0
    ensure_daemon()
    print_cancelled(call("cancel", delete_files=delete_files))
    return 0


def cmd_daemon(args) -> int:
    if args.action == "run":
        from .engine import Daemon

        return Daemon(foreground=args.foreground).run()
    if args.action == "start":
        if daemon_ping():
            print("A démon már fut.")
            return 0
        ensure_daemon()
        print("Démon elindítva.")
        return 0
    if args.action == "stop":
        if not daemon_ping():
            print("A démon nem fut.")
            return 0
        try:
            request(socket_path(), "shutdown")
        except (DaemonError, OSError):
            pass
        deadline = time.time() + 20
        while time.time() < deadline:
            if not daemon_ping(timeout=1.0):
                print("Démon leállítva.")
                return 0
            time.sleep(0.25)
        print("A démon nem állt le időben.")
        return 1
    if args.action == "status":
        info = daemon_ping()
        if info:
            print(f"Fut (pid={info['pid']}, libtorrent {info['version']})")
            return 0
        print("Nem fut.")
        return 1
    if args.action == "log":
        logfile = cfgmod.home() / cfgmod.LOG_NAME
        if not logfile.exists():
            print("Nincs naplófájl.")
            return 0
        lines = logfile.read_text(encoding="utf-8", errors="replace").splitlines()
        print("\n".join(lines[-args.lines :]))
        return 0
    return 1


def cmd_config(args) -> int:
    cfg = cfgmod.load_config()
    if args.set:
        for item in args.set:
            if "=" not in item:
                raise SystemExit(f"Formátum: kulcs=érték (kapott: {item})")
            key, raw = item.split("=", 1)
            key = key.strip()
            if key not in cfgmod.DEFAULT_CONFIG:
                raise SystemExit(
                    "Ismeretlen beállítás: %s (lehetséges: %s)"
                    % (key, ", ".join(sorted(cfgmod.DEFAULT_CONFIG)))
                )
            cfg[key] = cfgmod.coerce(key, raw)
        cfgmod.save_config(cfg)
        print("Beállítások mentve. Újraindítás után lépnek életbe: torrentdl daemon stop\n")
    width = max(len(k) for k in cfg)
    for key in sorted(cfg):
        print(f"{key.ljust(width)} = {cfg[key]}")
    print(f"\nAdatkönyvtár: {cfgmod.home()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="torrentdl",
        description="Egyszerű torrent letöltő (egyszerre egy torrent, háttérben, folytatható).",
    )
    parser.add_argument("--version", action="version", version=f"torrentdl {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="torrent vagy magnet link letöltése")
    add.add_argument("source", help="magnet: link, .torrent fájl vagy .torrent URL")
    add.add_argument(
        "-d", "--dest", required=True, help="célkönyvtár, ahová a fájlok kerülnek"
    )
    add.add_argument(
        "--paused", action="store_true", help="hozzáadás szüneteltetett állapotban"
    )
    add.set_defaults(func=cmd_add)

    status = sub.add_parser("status", help="az aktuális letöltés állapota")
    status.add_argument("-w", "--watch", action="store_true", help="folyamatos frissítés")
    status.add_argument(
        "-i", "--interval", type=float, default=2.0, help="frissítés (mp), --watch mellett"
    )
    status.set_defaults(func=cmd_status)

    pause = sub.add_parser("pause", help="letöltés felfüggesztése")
    pause.add_argument(
        "--for",
        dest="duration",
        type=parse_duration,
        default=None,
        metavar="IDŐ",
        help="ennyi idő után automatikus folytatás (pl. 45s, 30m, 2h, 1h30m; egység nélkül perc)",
    )
    pause.set_defaults(func=cmd_pause)

    resume = sub.add_parser("resume", help="felfüggesztett/megszakadt letöltés folytatása")
    resume.set_defaults(func=cmd_resume)

    cancel = sub.add_parser(
        "cancel", aliases=["remove"], help="az aktuális letöltés megszakítása/törlése"
    )
    cancel.add_argument(
        "--delete-files",
        action="store_true",
        help="a már letöltött fájlokat is törli",
    )
    cancel.add_argument("-y", "--yes", action="store_true", help="ne kérdezzen rá")
    cancel.set_defaults(func=cmd_cancel)

    daemon = sub.add_parser("daemon", help="a háttérdémon kezelése")
    daemon.add_argument("action", choices=["start", "stop", "status", "run", "log"])
    daemon.add_argument(
        "--foreground",
        action="store_true",
        help="'run' esetén a naplót a képernyőre is írja",
    )
    daemon.add_argument("-n", "--lines", type=int, default=40, help="'log' esetén sorok száma")
    daemon.set_defaults(func=cmd_daemon)

    conf = sub.add_parser("config", help="beállítások megtekintése/módosítása")
    conf.add_argument(
        "--set", action="append", metavar="KULCS=ÉRTÉK", help="beállítás módosítása"
    )
    conf.set_defaults(func=cmd_config)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
