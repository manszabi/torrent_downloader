"""Emberi olvasásra szánt formázók (a parancssor és a grafikus felület is ezt használja)."""

from __future__ import annotations

import re

DURATION_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[smhd]?)", re.IGNORECASE)
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "": 60}

STATE_LABELS = {
    "downloading": "letöltés",
    "seeding": "megosztás",
    "paused": "szüneteltetve",
    "verifying": "ellenőrzés",
    "error": "hiba",
}


def parse_duration(text: str) -> float:
    """Időtartam szövegből másodperc.

    Egység nélkül percet jelent ('30' = 30 perc); '45s', '2h', '1h30m' is használható.
    """
    total = 0.0
    pos = 0
    found = False
    trimmed = text.strip()
    for match in DURATION_RE.finditer(trimmed):
        if match.start() != pos:
            break
        pos = match.end()
        total += float(match.group("value")) * UNIT_SECONDS[match.group("unit").lower()]
        found = True
    if not found or pos != len(trimmed):
        raise ValueError(f"értelmezhetetlen időtartam: {text}")
    return total


def human_bytes(num: float) -> str:
    num = float(num or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024 or unit == "TiB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.2f} {unit}"
        num /= 1024
    return f"{num:.2f} TiB"


def human_rate(num: float) -> str:
    return human_bytes(num) + "/s"


def human_time(seconds) -> str:
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


def eta_seconds(job: dict):
    """Becsült hátralévő idő másodpercben, vagy None."""
    total = float(job.get("total_bytes") or 0)
    done = float(job.get("downloaded") or 0)
    rate = float(job.get("download_rate") or 0)
    if rate > 0 and total > done:
        return (total - done) / rate
    return None


def state_label(job: dict) -> str:
    """Az állapot rövid, magyar leírása (a szünet hátralévő idejével együtt)."""
    import time

    state = job.get("state", "?")
    label = STATE_LABELS.get(state, state)
    if state == "seeding":
        return label + " (a letöltés kész és ellenőrizve van)"
    if state == "paused" and job.get("paused_until"):
        label += f" (folytatás {human_time(max(0, job['paused_until'] - time.time()))} múlva)"
    # A motor részletesebb állapota csak akkor érdekes, ha többet mond
    # (pl. "metaadat letöltése" vagy "ellenőrzés"), mint a saját feliratunk.
    reszlet = job.get("lt_state")
    if state == "downloading" and reszlet and reszlet != label:
        label += f" – {reszlet}"
    return label
