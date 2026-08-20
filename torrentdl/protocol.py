"""Egyszerű JSON-soros protokoll a felület és a háttérdémon között.

A démon a hurokcímen (127.0.0.1) figyel egy véletlen porton; a portot és a
jelszót a `daemon.endpoint` fájl tartalmazza. Minden kérés tartalmazza a
jelszót, így a gépen futó más felhasználók nem tudják vezérelni a letöltést.
"""

from __future__ import annotations

import hmac
import json
import socket

ENCODING = "utf-8"
MAX_MESSAGE = 16 * 1024 * 1024  # egy .torrent tartalma is bőven átmegy rajta


class DaemonError(RuntimeError):
    pass


class NotRunning(DaemonError):
    """Nem fut a démon (nincs végpont-fájl, vagy nem fogadja a kapcsolatot)."""


def send_line(sock: socket.socket, obj) -> None:
    sock.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode(ENCODING))


def recv_line(sock: socket.socket, timeout: float | None = 10.0):
    """Egy sornyi JSON beolvasása. None, ha a másik fél lezárta a kapcsolatot."""
    sock.settimeout(timeout)
    chunks = []
    size = 0
    while True:
        data = sock.recv(65536)
        if not data:
            break
        chunks.append(data)
        size += len(data)
        if b"\n" in data:
            break
        if size > MAX_MESSAGE:
            raise DaemonError("túl hosszú üzenet")
    raw = b"".join(chunks)
    if not raw.strip():
        return None
    return json.loads(raw.split(b"\n", 1)[0].decode(ENCODING))


def tokens_match(expected: str, got) -> bool:
    return isinstance(got, str) and hmac.compare_digest(expected, got)


def request(endpoint, command: str, timeout: float = 15.0, **payload):
    """Parancs küldése a démonnak; a válasz 'data' mezőjét adja vissza.

    `endpoint` a `config.read_endpoint()` szótára. NotRunning hibát dob, ha a
    démon nem érhető el.
    """
    if not endpoint:
        raise NotRunning("nem fut a háttérdémon")
    try:
        with socket.create_connection(
            (endpoint.get("host", "127.0.0.1"), int(endpoint["port"])), timeout=timeout
        ) as sock:
            send_line(sock, {"command": command, "token": endpoint["token"], **payload})
            reply = recv_line(sock, timeout=timeout)
    except (OSError, ValueError) as exc:
        raise NotRunning(f"nem érhető el a háttérdémon: {exc}") from exc
    if reply is None:
        raise DaemonError("a démon nem válaszolt")
    if not reply.get("ok"):
        raise DaemonError(reply.get("error") or "ismeretlen hiba")
    return reply.get("data")
