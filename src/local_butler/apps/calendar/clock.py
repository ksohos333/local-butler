"""An atomic clock for the calendar: local time corrected against NTP.

The header clock and the day timeline both read this, so what the user sees is
the machine's NTP-corrected wall clock rather than whatever the local clock
happens to say. Three sources are tried in order:

1. A direct SNTP query (UDP 123) to a handful of public servers -- one packet
   out, one packet back, no daemon and no privileges required.
2. ``chronyc tracking``, when chronyd is the thing keeping this machine honest.
3. The local clock itself, reported as such rather than pretending it is
   synchronised.

The offset is measured against :func:`time.time`, which is the same clock
:class:`Event` and the GTK timers run on, so applying it is one addition. A
failed resync keeps the last good offset and marks it stale instead of
snapping the displayed time backwards.
"""

from __future__ import annotations

import datetime as dt
import re
import socket
import struct
import subprocess
import threading
import time
from typing import Any, Dict, Optional, Sequence, Tuple

from local_butler.apps.calendar.events import local_timezone

NTP_HOSTS = ("time.google.com", "pool.ntp.org", "time.cloudflare.com", "time.apple.com")
NTP_PORT = 123
NTP_EPOCH = 2208988800  # 1900-01-01 -> 1970-01-01, in seconds
DEFAULT_RESYNC_SECONDS = 900.0


def build_request() -> bytes:
    """A minimal SNTP client request: LI=0, VN=4, Mode=3 (client)."""
    return b"\x1b" + 47 * b"\x00"


def parse_response(data: bytes, t1: float, t4: float) -> float:
    """Offset in seconds (server time minus local time) from an SNTP reply.

    ``t1`` is when the request left, ``t4`` when the reply arrived, both as
    Unix seconds. The classic SNTP equation is used:

        offset = ((t2 - t1) + (t3 - t4)) / 2

    which cancels out the round-trip delay as long as it is symmetric.
    """
    if len(data) < 48:
        raise ValueError("SNTP reply is only {0} bytes".format(len(data)))
    mode = data[0] & 0x07
    if mode != 4:
        raise ValueError("not a server reply (mode {0})".format(mode))
    stratum = data[1]
    if stratum == 0:
        raise ValueError("kiss-o'-death packet: {0!r}".format(data[4:8]))
    if stratum > 15:
        raise ValueError("unusable stratum {0}".format(stratum))

    # The server must echo our originate timestamp back, or the reply belongs
    # to some other exchange and its timestamps are meaningless to us.
    want = struct.pack("!II", int(t1) + NTP_EPOCH, _fraction(int(t1), t1))
    if data[24:32] != want:
        raise ValueError("reply does not echo the request timestamp")

    t2s, t2f, t3s, t3f = struct.unpack("!IIII", data[32:48])
    if t2s == 0 and t3s == 0:
        raise ValueError("reply carries no timestamps")
    t2 = t2s + t2f / 2**32 - NTP_EPOCH
    t3 = t3s + t3f / 2**32 - NTP_EPOCH
    return ((t2 - t1) + (t3 - t4)) / 2.0


def _fraction(seconds: int, exact: float) -> int:
    """The 32-bit fraction of ``exact`` expressed in NTP's fixed point."""
    return int(round((exact - seconds) * 2**32)) % (2**32)


def sntp_offset(host: str, timeout: float = 1.5) -> Tuple[float, float]:
    """Ask ``host`` what time it is; returns ``(offset_seconds, rtt_seconds)``."""
    infos = socket.getaddrinfo(host, NTP_PORT, socket.AF_INET, socket.SOCK_DGRAM)
    if not infos:
        raise OSError("cannot resolve {0}".format(host))
    address = infos[0][4]
    request = build_request()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        t1 = time.time()
        sock.sendto(request, address)
        data, _peer = sock.recvfrom(48)
        t4 = time.time()
    return parse_response(data, t1, t4), t4 - t1


_CHRONY_RE = re.compile(
    r"^System time\s*:\s*([0-9.]+) seconds (slow|fast) of NTP time", re.MULTILINE
)
_CHRONY_REF_RE = re.compile(r"^Reference ID\s*:\s*\S+\s+\(([^)]*)\)", re.MULTILINE)


def chrony_offset(output: Optional[str] = None) -> Optional[float]:
    """Offset from ``chronyc tracking``; ``None`` when chronyd is not available.

    "System time 0.0006 seconds slow of NTP time" means the local clock trails
    the true time by that much, so the correction to apply is positive.
    """
    if output is None:
        try:
            proc = subprocess.run(
                ["chronyc", "tracking"],
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        output = proc.stdout
    match = _CHRONY_RE.search(output or "")
    if not match:
        return None
    value = float(match.group(1))
    return value if match.group(2) == "slow" else -value


class AtomicClock:
    """Local wall clock with an NTP-measured offset applied.

    ``now()`` is safe to call from any thread; ``sync()`` blocks and
    ``sync_async()`` runs it on a daemon thread. ``maybe_resync()`` is what the
    GTK second-tick calls: it does nothing until ``resync_after`` seconds have
    passed since the last attempt.
    """

    def __init__(
        self,
        hosts: Sequence[str] = NTP_HOSTS,
        now: Any = time.time,
        auto_sync: bool = False,
        resync_after: float = DEFAULT_RESYNC_SECONDS,
    ) -> None:
        self._hosts = tuple(hosts)
        self._now = now
        self._resync_after = resync_after
        self._lock = threading.Lock()
        self._offset = 0.0
        self._source = "local clock"
        self._detail = ""
        self._delay_ms: Optional[float] = None
        self._synced = False
        self._stale = False
        self._last_sync: Optional[float] = None
        self._last_attempt = 0.0
        self._thread: Optional[threading.Thread] = None
        if auto_sync:
            self.sync_async()

    # -- synchronising ----------------------------------------------------
    def sync(self) -> bool:
        """Blocking resync; ``True`` when an authoritative source answered."""
        self._last_attempt = time.monotonic()
        for host in self._hosts:
            try:
                offset, delay = sntp_offset(host)
            except (OSError, ValueError, socket.timeout):
                continue
            with self._lock:
                self._offset = offset
                self._source = "NTP"
                self._detail = host
                self._delay_ms = delay * 1000.0
                self._synced = True
                self._stale = False
                self._last_sync = self._now()
            return True

        offset = chrony_offset()
        if offset is not None:
            with self._lock:
                self._offset = offset
                self._source = "chronyd"
                self._detail = "chronyc tracking"
                self._delay_ms = None
                self._synced = True
                self._stale = False
                self._last_sync = self._now()
            return True

        # Nothing answered. Keep the offset we already had -- dropping it would
        # jump the displayed time back to the unsynchronised system clock.
        with self._lock:
            self._stale = True
            self._synced = False
            if self._source == "local clock":
                self._detail = "no NTP source reachable"
        return False

    def sync_async(self) -> None:
        """Start a background resync unless one is already running."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self.sync, name="atomic-clock", daemon=True
        )
        self._thread.start()

    def maybe_resync(self) -> bool:
        """Resync in the background if the last attempt is old enough."""
        if time.monotonic() - self._last_attempt < self._resync_after:
            return False
        self.sync_async()
        return True

    # -- reading ----------------------------------------------------------
    def now(self) -> dt.datetime:
        """Corrected local wall-clock time, naive, to match :class:`Event`."""
        with self._lock:
            offset = self._offset
        stamp = self._now() + offset
        moment = dt.datetime.fromtimestamp(stamp, tz=local_timezone())
        return moment.replace(microsecond=0, tzinfo=None)

    @property
    def offset(self) -> float:
        with self._lock:
            return self._offset

    @property
    def synced(self) -> bool:
        with self._lock:
            return self._synced and not self._stale

    def status(self) -> Dict[str, Any]:
        """Everything a caller might want to show or log about the clock."""
        with self._lock:
            return {
                "source": self._source,
                "detail": self._detail,
                "offset": self._offset,
                "offset_ms": self._offset * 1000.0,
                "delay_ms": self._delay_ms,
                "synced": self._synced and not self._stale,
                "stale": self._stale,
                "last_sync": self._last_sync,
            }

    def status_text(self) -> str:
        """One short line for the header: which source and how far off."""
        info = self.status()
        offset_ms = info["offset_ms"]
        if info["source"] == "NTP":
            body = "NTP {0}".format(info["detail"])
        elif info["source"] == "chronyd":
            body = "chronyd synced"
        else:
            body = "local clock"
        if info["synced"]:
            return "{0} · {1:+.1f} ms".format(body, offset_ms)
        if info["stale"]:
            return "{0} (stale)".format(body)
        return "{0} — no time source".format(body)


_SHARED: Optional[AtomicClock] = None
_SHARED_LOCK = threading.Lock()


def shared_clock() -> AtomicClock:
    """The process-wide clock, syncing in the background on first use."""
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = AtomicClock(auto_sync=True)
        return _SHARED
