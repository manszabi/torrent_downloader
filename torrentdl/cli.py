"""Parancssori felület a torrentdl-hez."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__
from . import client
from . import config as cfgmod
from .format import (
    eta_seconds,
    human_bytes,
    human_rate,
    human_time,
    parse_duration,
    progress_bar,
    state_label,
)


def duration_arg(text: str) -> float:
    try:
        return parse_duration(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


# ------------------------------------------------------------------ kiírás


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

    progress = float(job.get("progress") or 0)
    eta = eta_seconds(job)
    lines.append(f"Név:      {job.get('name') or job.get('source')}")
    lines.append(f"Állapot:  {state_label(job)}")
    lines.append(f"Haladás:  {progress_bar(progress)} {progress * 100:5.1f}%")
    lines.append(
        f"Méret:    {human_bytes(job.get('downloaded'))} / {human_bytes(job.get('total_bytes'))}"
    )
    lines.append(
        "Sebesség: le %s | fel %s | peerek: %s (seed: %s) | DHT node: %s"
        % (
            human_rate(job.get("download_rate")),
            human_rate(job.get("upload_rate")),
            job.get("peers", 0),
            job.get("seeds", 0),
            job.get("dht_nodes", 0),
        )
    )
    if job.get("state") == "downloading":
        lines.append(f"Hátralévő idő: {human_time(eta) if eta is not None else '?'}")
    if job.get("state") == "seeding":
        total = float(job.get("total_bytes") or 0)
        uploaded = float(job.get("uploaded") or 0)
        arany = f" (arány: {uploaded / total:.2f})" if total else ""
        lines.append(f"Feltöltve: {human_bytes(uploaded)}{arany}")
    lines.append(f"Cél:      {job.get('save_path')}")
    if job.get("repaired_bytes"):
        lines.append(
            "Javítás:  %s hiányzó/sérült adat újratöltése" % human_bytes(job["repaired_bytes"])
        )
    if job.get("hash_errors"):
        lines.append(f"Hibás darabok (újratöltve): {job['hash_errors']}")
    if job.get("error"):
        lines.append(f"Hiba:     {job['error']}")
    if not data.get("daemon"):
        lines.append(
            "FIGYELEM: a háttérdémon nem fut, a letöltés megszakadt. "
            "Folytatás: torrentdl resume"
        )
    return "\n".join(lines)


def call(command: str, **payload):
    """Parancs a démonnak; hibát olvasható üzenettel adunk tovább."""
    try:
        return client.call(command, **payload)
    except client.DaemonError as exc:
        raise SystemExit(f"Hiba: {exc}")


# ------------------------------------------------------------------ parancsok


def cmd_add(args) -> int:
    try:
        payload = client.load_source(args.source)
    except client.SourceError as exc:
        raise SystemExit(f"Hiba: {exc}")
    save_path = Path(args.dest).expanduser().resolve()
    try:
        save_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit(f"Hiba: nem hozható létre a célkönyvtár: {exc}")
    data = call("add", save_path=str(save_path), paused=args.paused, **payload)
    print("Letöltés hozzáadva.\n")
    print(render_status(data))
    print("\nA letöltés a háttérben fut. Állapot: torrentdl status")
    return 0


def cmd_status(args) -> int:
    if args.watch:
        try:
            while True:
                data = client.fetch_status()
                sys.stdout.write("\033[2J\033[H")
                print(render_status(data))
                if not data.get("job"):
                    return 0
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0
    print(render_status(client.fetch_status()))
    return 0


def cmd_pause(args) -> int:
    print(render_status(call("pause", seconds=args.duration)))
    return 0


def cmd_resume(args) -> int:
    print(render_status(call("resume")))
    return 0


def cmd_check(args) -> int:
    data = call("check")
    print("Fájlok ellenőrzése elindult; ami hiányzik vagy sérült, azt a program újratölti.\n")
    print(render_status(data))
    return 0


def cmd_cancel(args) -> int:
    if args.delete_files and not args.yes:
        answer = input("Biztosan törlöd a letöltést a fájljaival együtt? [i/N] ").strip().lower()
        if answer not in ("i", "igen", "y", "yes"):
            print("Megszakítva.")
            return 1
    if not client.ping() and not cfgmod.read_json(cfgmod.path(cfgmod.JOB_NAME)):
        print("Nincs aktív letöltés.")
        return 0
    # A törlést a libtorrent végzi, ezért ehhez is a démon kell.
    data = call("cancel", delete_files=args.delete_files)
    print(
        "Törölve: %s%s"
        % (data["cancelled"], " (a fájlokkal együtt)" if data["files_deleted"] else "")
    )
    if data.get("warning"):
        print(f"Figyelmeztetés: {data['warning']}")
    return 0


def cmd_gui(args) -> int:
    from .gui import main as gui_main

    return gui_main()


def cmd_daemon(args) -> int:
    if args.action == "run":
        from .engine import Daemon

        return Daemon(foreground=args.foreground).run()
    if args.action == "start":
        if client.ping():
            print("A démon már fut.")
            return 0
        try:
            client.ensure_daemon()
        except client.DaemonError as exc:
            raise SystemExit(f"Hiba: {exc}")
        print("Démon elindítva.")
        return 0
    if args.action == "stop":
        if not client.ping():
            print("A démon nem fut.")
            return 0
        if client.stop_daemon():
            print("Démon leállítva.")
            return 0
        print("A démon nem állt le időben.")
        return 1
    if args.action == "status":
        info = client.ping()
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
            try:
                cfg[key] = cfgmod.coerce(key, raw)
            except ValueError as exc:
                raise SystemExit(f"Hibás érték: {exc}")
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
    add.add_argument("-d", "--dest", required=True, help="célkönyvtár, ahová a fájlok kerülnek")
    add.add_argument("--paused", action="store_true", help="hozzáadás szüneteltetett állapotban")
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
        type=duration_arg,
        default=None,
        metavar="IDŐ",
        help="ennyi idő után automatikus folytatás (pl. 45s, 30m, 2h, 1h30m; egység nélkül perc)",
    )
    pause.set_defaults(func=cmd_pause)

    resume = sub.add_parser("resume", help="felfüggesztett/megszakadt letöltés folytatása")
    resume.set_defaults(func=cmd_resume)

    check = sub.add_parser(
        "check",
        aliases=["ellenoriz"],
        help="a letöltött fájlok ellenőrzése; a hibás részt újratölti",
    )
    check.set_defaults(func=cmd_check)

    cancel = sub.add_parser(
        "cancel", aliases=["remove"], help="az aktuális letöltés megszakítása/törlése"
    )
    cancel.add_argument(
        "--delete-files", action="store_true", help="a már letöltött fájlokat is törli"
    )
    cancel.add_argument("-y", "--yes", action="store_true", help="ne kérdezzen rá")
    cancel.set_defaults(func=cmd_cancel)

    gui = sub.add_parser("gui", help="grafikus felület indítása")
    gui.set_defaults(func=cmd_gui)

    daemon = sub.add_parser("daemon", help="a háttérdémon kezelése")
    daemon.add_argument("action", choices=["start", "stop", "status", "run", "log"])
    daemon.add_argument(
        "--foreground", action="store_true", help="'run' esetén a naplót a képernyőre is írja"
    )
    daemon.add_argument("-n", "--lines", type=int, default=40, help="'log' esetén sorok száma")
    daemon.set_defaults(func=cmd_daemon)

    conf = sub.add_parser("config", help="beállítások megtekintése/módosítása")
    conf.add_argument("--set", action="append", metavar="KULCS=ÉRTÉK", help="beállítás módosítása")
    conf.set_defaults(func=cmd_config)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
