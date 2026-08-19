"""Végponttól végpontig működő füstteszt hálózat nélkül.

Amit ellenőriz:
  1. kész (már meglévő) fájlok esetén: hozzáadás -> ellenőrzés -> alapállapot
  2. szüneteltetés időzítéssel és automatikus folytatás
  3. megszakítás a fájlok törlésével
  4. összeomlás (SIGKILL) utáni folytatás a mentett állapotból

Futtatás:  python3 tests/smoke_test.py
"""

from __future__ import annotations

import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import libtorrent as lt

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILURES = []


def run(home: Path, *args, check=True):
    env = dict(os.environ, TORRENTDL_HOME=str(home), PYTHONPATH=str(ROOT))
    proc = subprocess.run(
        [sys.executable, "-m", "torrentdl", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"'{' '.join(args)}' hibára futott:\n{proc.stdout}\n{proc.stderr}")
    return proc


def make_torrent(source_dir: Path, out: Path) -> Path:
    fs = lt.file_storage()
    lt.add_files(fs, str(source_dir))
    torrent = lt.create_torrent(fs, piece_size=16 * 1024)
    torrent.set_creator("torrentdl smoke test")
    lt.set_piece_hashes(torrent, str(source_dir.parent))
    out.write_bytes(lt.bencode(torrent.generate()))
    return out


def make_payload(directory: Path, size: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(1234)
    (directory / "adat.bin").write_bytes(bytes(rnd.getrandbits(8) for _ in range(size)))
    (directory / "olvass.txt").write_text("torrentdl teszt\n", encoding="utf-8")


def status(home: Path) -> dict:
    job = json.loads((home / "job.json").read_text()) if (home / "job.json").exists() else None
    last = json.loads((home / "last.json").read_text()) if (home / "last.json").exists() else None
    return {"job": job, "last": last}


def wait_for(predicate, timeout=60, interval=0.5, what=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    raise AssertionError(f"időtúllépés: {what}")


def check(name: str, fn):
    print(f"--- {name}")
    try:
        fn()
        print(f"    OK: {name}")
    except Exception as exc:
        FAILURES.append(name)
        print(f"    HIBA: {name}: {exc}")


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="torrentdl-test-"))
    home = work / "home"
    home.mkdir()
    port = random.randint(20000, 40000)
    run(home, "config", "--set", f"listen_port={port}", "--set", "idle_timeout=30")

    # -------------------------------------------------- 1. kész letöltés
    def test_complete():
        src = work / "kesz" / "csomag"
        make_payload(src, 300_000)
        torrent = make_torrent(src, work / "kesz.torrent")
        dest = work / "cel-kesz"
        dest.mkdir()
        shutil.copytree(src, dest / src.name)  # a fájlok már megvannak: ellenőrzés kell csak
        out = run(home, "add", str(torrent), "-d", str(dest))
        assert "Letöltés hozzáadva" in out.stdout, out.stdout
        wait_for(
            lambda: status(home)["job"] is None and status(home)["last"] is not None,
            timeout=60,
            what="a letöltés befejeződése és alapállapotba állás",
        )
        last = status(home)["last"]
        assert last["verified"] is True, last
        assert last["name"] == "csomag", last
        assert (dest / "csomag" / "adat.bin").exists()
        text = run(home, "status").stdout
        assert "Nincs aktív letöltés" in text, text

    check("kész fájlok: ellenőrzés után alapállapot", test_complete)

    # -------------------------------------------------- 2. szünet + folytatás
    def test_pause_resume():
        src = work / "fut" / "nagy"
        make_payload(src, 400_000)
        torrent = make_torrent(src, work / "fut.torrent")
        dest = work / "cel-fut"
        run(home, "add", str(torrent), "-d", str(dest))
        wait_for(lambda: status(home)["job"] is not None, what="a letöltés megjelenése")
        run(home, "pause", "--for", "5s")
        assert status(home)["job"]["state"] == "paused"
        assert status(home)["job"]["paused_until"] is not None
        wait_for(
            lambda: status(home)["job"]["state"] == "downloading",
            timeout=30,
            what="automatikus folytatás a szünet lejártakor",
        )
        run(home, "pause")
        assert status(home)["job"]["state"] == "paused"
        run(home, "resume")
        assert status(home)["job"]["state"] == "downloading"

    check("szüneteltetés időzítéssel és folytatás", test_pause_resume)

    # -------------------------------------------------- 3. összeomlás utáni folytatás
    def test_crash_resume():
        pid = int((home / "daemon.lock").read_text().strip())
        os.kill(pid, signal.SIGKILL)
        wait_for(lambda: not pid_alive(pid), timeout=20, what="a démon leállása")
        assert status(home)["job"] is not None, "a megszakadt letöltés adatai elvesztek"
        text = run(home, "status").stdout
        assert "megszakadt" in text, text
        run(home, "resume")
        wait_for(
            lambda: status(home)["job"]["state"] == "downloading",
            timeout=30,
            what="folytatás összeomlás után",
        )

    check("összeomlás utáni folytatás", test_crash_resume)

    # -------------------------------------------------- 4. törlés fájlokkal
    def test_cancel():
        dest = work / "cel-fut"
        run(home, "cancel", "--delete-files", "-y")
        wait_for(
            lambda: status(home)["job"] is None, timeout=20, what="a letöltés törlése"
        )
        leftovers = [p for p in dest.rglob("*") if p.is_file()]
        assert not leftovers, f"maradtak fájlok: {leftovers}"
        proc = run(home, "cancel", check=False)
        assert proc.returncode != 0 or "Nincs aktív" in proc.stdout, proc.stdout

    check("megszakítás a fájlok törlésével", test_cancel)

    # -------------------------------------------------- 5. megosztás a letöltés után
    def test_seeding():
        run(home, "config", "--set", "seed_after_complete=true")
        run(home, "daemon", "stop", check=False)  # az új beállítás induláskor él
        src = work / "megoszt" / "keszlet"
        make_payload(src, 250_000)
        torrent = make_torrent(src, work / "megoszt.torrent")
        dest = work / "cel-megoszt"
        dest.mkdir()
        shutil.copytree(src, dest / src.name)  # már kész: az ellenőrzés után megosztás jön
        run(home, "add", str(torrent), "-d", str(dest))
        wait_for(
            lambda: (status(home)["job"] or {}).get("state") == "seeding",
            timeout=60,
            what="a megosztásba lépés a letöltés után",
        )
        assert status(home)["last"]["verified"] is True

        # A démon leállítása és újraindítása után is megy tovább a megosztás.
        run(home, "daemon", "stop")
        assert (status(home)["job"] or {}).get("state") == "seeding", "a megosztás állapota elveszett"
        run(home, "daemon", "start")
        out = run(home, "status").stdout
        assert "megosztás" in out, out
        assert (status(home)["job"] or {}).get("state") == "seeding"

        # Megosztás közben is indítható új letöltés: az előzőt magától lezárja.
        masik = make_torrent(src, work / "megoszt2.torrent")
        run(home, "add", str(masik), "-d", str(work / "cel-megoszt2"))
        wait_for(
            lambda: status(home)["job"] is not None
            and status(home)["job"]["save_path"].endswith("cel-megoszt2"),
            timeout=30,
            what="az új letöltés indulása megosztás közben",
        )
        run(home, "cancel", "-y")
        # A megosztott fájlok a helyükön maradtak.
        assert (dest / src.name / "adat.bin").is_file()
        run(home, "config", "--set", "seed_after_complete=false")
        run(home, "daemon", "stop", check=False)

    check("megosztás a letöltés után, újraindítás után is", test_seeding)

    run(home, "daemon", "stop", check=False)
    if FAILURES:
        print(f"\n{len(FAILURES)} teszt bukott el: {', '.join(FAILURES)}")
        print(f"munkakönyvtár (vizsgálathoz): {work}")
        return 1
    shutil.rmtree(work, ignore_errors=True)
    print("\nMinden teszt rendben.")
    return 0


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
