"""Egyszerű JSON-soros protokoll a CLI és a háttérdémon között (unix socket)."""

from __future__ import annotations

import json
import socket

ENCODING = "utf-8"


def send_line(sock: socket.socket, obj) -> None:
    sock.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode(ENCODING))


def recv_line(sock: socket.socket, timeout: float | None = 10.0):
    """Egy sornyi JSON beolvasása. None, ha a másik fél lezárta a kapcsolatot."""
    sock.settimeout(timeout)
    chunks = []
    while True:
        data = sock.recv(65536)
        if not data:
            break
        chunks.append(data)
        if b"\n" in data:
            break
    raw = b"".join(chunks)
    if not raw.strip():
        return None
    line = raw.split(b"\n", 1)[0]
    return json.loads(line.decode(ENCODING))


class DaemonError(RuntimeError):
    pass


def request(sock_path, command: str, timeout: float = 15.0, **payload):
    """Parancs küldése a démonnak; a válasz 'data' mezőjét adja vissza."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(sock_path))
        send_line(sock, {"command": command, **payload})
        reply = recv_line(sock, timeout=timeout)
    if reply is None:
        raise DaemonError("a démon nem válaszolt")
    if not reply.get("ok"):
        raise DaemonError(reply.get("error") or "ismeretlen hiba")
    return reply.get("data")
