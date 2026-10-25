"""Tests for the atomic clock: SNTP packets, chrony parsing, and offsets.

No socket is opened here. The packet builder/parser is exercised with crafted
replies and :class:`AtomicClock`'s transports are faked, so these run offline
and finish in milliseconds -- the same rule the rest of the suite follows.
"""

from __future__ import annotations

import datetime as dt
import struct
import threading
import time

import pytest

from local_butler.apps.calendar import clock as clock_module
from local_butler.apps.calendar.clock import (
    AtomicClock,
    build_request,
    chrony_offset,
    parse_response,
    shared_clock,
)
from local_butler.apps.calendar.events import local_timezone


def ntp_stamp(unix: float) -> bytes:
    seconds = int(unix)
    fraction = int(round((unix - seconds) * 2**32)) % (2**32)
    return struct.pack("!II", seconds + clock_module.NTP_EPOCH, fraction)


def server_reply(t1, t2, t3, stratum=2, mode=4, echo=True) -> bytes:
    data = bytearray(48)
    data[0] = mode | (4 << 3)  # LI=0, VN=4, mode=server
    data[1] = stratum
    if echo:
        data[24:32] = ntp_stamp(t1)  # originate: our request, echoed back
    data[32:40] = ntp_stamp(t2)  # receive
    data[40:48] = ntp_stamp(t3)  # transmit
    return bytes(data)


# -- the wire --------------------------------------------------------------


def test_request_is_a_standard_client_packet():
    request = build_request()
    assert len(request) == 48
    assert request[0] == 0x1B  # LI=0, VN=4, Mode=3 (client)
    assert request[1:] == 47 * b"\x00"


def test_offset_cancels_out_a_symmetric_round_trip():
    # Request out at t1, server receives/sends within 0.6s, reply in at t4.
    offset = parse_response(
        server_reply(t1=1000.0, t2=1000.4, t3=1000.6), t1=1000.0, t4=1001.0
    )
    assert offset == pytest.approx(((1000.4 - 1000.0) + (1000.6 - 1001.0)) / 2.0)


def test_reply_shorter_than_one_packet_is_rejected():
    with pytest.raises(ValueError, match="bytes"):
        parse_response(b"\x24" + b"\x00" * 10, t1=0.0, t4=1.0)


def test_client_mode_reply_is_rejected():
    with pytest.raises(ValueError, match="mode"):
        parse_response(server_reply(1000.0, 1000.1, 1000.2, mode=3), 1000.0, 1001.0)


def test_kiss_o_death_reply_is_rejected():
    with pytest.raises(ValueError, match="kiss"):
        parse_response(server_reply(1000.0, 1000.1, 1000.2, stratum=0), 1000.0, 1001.0)


def test_unusable_stratum_is_rejected():
    with pytest.raises(ValueError, match="stratum"):
        parse_response(server_reply(1000.0, 1000.1, 1000.2, stratum=16), 1000.0, 1001.0)


def test_reply_without_our_originate_timestamp_is_rejected():
    with pytest.raises(ValueError, match="echo"):
        parse_response(
            server_reply(999.0, 1000.1, 1000.2, echo=False), 1000.0, 1001.0
        )


def test_reply_with_no_timestamps_is_rejected():
    data = bytearray(server_reply(1000.0, 1000.1, 1000.2))
    data[32:48] = b"\x00" * 16
    with pytest.raises(ValueError, match="timestamps"):
        parse_response(bytes(data), 1000.0, 1001.0)


# -- chrony ----------------------------------------------------------------


def test_chrony_slow_clock_means_a_positive_offset():
    output = (
        "Reference ID       : GPS (GPS)\n"
        "Stratum            : 1\n"
        "System time        : 0.000021 seconds slow of NTP time\n"
    )
    assert chrony_offset(output) == pytest.approx(0.000021)


def test_chrony_fast_clock_means_a_negative_offset():
    output = "System time        : 0.5 seconds fast of NTP time\n"
    assert chrony_offset(output) == pytest.approx(-0.5)


def test_chrony_output_without_a_line_is_not_a_sync():
    assert chrony_offset("Reference ID : GPS (GPS)\n") is None
    assert chrony_offset("") is None


def test_missing_chronyc_is_reported_as_no_source(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("chronyc not found")

    monkeypatch.setattr(clock_module.subprocess, "run", boom)
    assert chrony_offset() is None


# -- AtomicClock -----------------------------------------------------------


def make_clock(monkeypatch, offset=1.5, sntp_ok=True, chrony=None):
    clock = AtomicClock(hosts=("pool.example",), now=lambda: 1_700_000_000.0)

    def fake_sntp(host, timeout=1.5):
        if not sntp_ok:
            raise OSError("network unreachable")
        return offset, 0.02

    monkeypatch.setattr(clock_module, "sntp_offset", fake_sntp)
    monkeypatch.setattr(clock_module, "chrony_offset", lambda output=None: chrony)
    return clock


def test_sync_applies_the_ntp_offset_to_now(monkeypatch):
    clock = make_clock(monkeypatch, offset=1.5)

    assert clock.sync() is True

    assert clock.offset == 1.5
    assert clock.synced is True
    status = clock.status()
    assert status["source"] == "NTP"
    assert status["detail"] == "pool.example"
    assert status["delay_ms"] == pytest.approx(20.0)

    expected = dt.datetime.fromtimestamp(
        1_700_000_000.0 + 1.5, tz=local_timezone()
    ).replace(microsecond=0, tzinfo=None)
    assert clock.now() == expected
    assert clock.now().tzinfo is None
    assert clock.now().microsecond == 0
    assert "NTP pool.example" in clock.status_text()


def test_now_is_what_the_offset_says_even_with_a_zero_offset(monkeypatch):
    clock = make_clock(monkeypatch, offset=0.0)
    clock.sync()
    expected = dt.datetime.fromtimestamp(1_700_000_000.0, tz=local_timezone()).replace(
        microsecond=0, tzinfo=None
    )
    assert clock.now() == expected
    assert "+0.0 ms" in clock.status_text()


def test_a_failed_resync_keeps_the_offset_and_marks_it_stale(monkeypatch):
    clock = make_clock(monkeypatch, offset=1.5)
    assert clock.sync() is True

    monkeypatch.setattr(
        clock_module, "sntp_offset", lambda *a, **k: (_ for _ in ()).throw(OSError())
    )
    assert clock.sync() is False

    # The offset must not snap back to the unsynchronised system clock.
    assert clock.offset == 1.5
    assert clock.synced is False
    assert clock.status()["stale"] is True
    assert "stale" in clock.status_text()


def test_chrony_is_the_fallback_when_no_ntp_host_answers(monkeypatch):
    clock = make_clock(monkeypatch, sntp_ok=False, chrony=0.25)

    assert clock.sync() is True

    assert clock.offset == pytest.approx(0.25)
    assert clock.status()["source"] == "chronyd"
    assert clock.synced is True
    assert "chronyd" in clock.status_text()


def test_no_source_at_all_reads_as_local_clock(monkeypatch):
    clock = make_clock(monkeypatch, sntp_ok=False, chrony=None)

    assert clock.sync() is False

    assert clock.status()["source"] == "local clock"
    assert clock.synced is False
    assert "local clock" in clock.status_text()


def test_maybe_resync_respects_the_cooldown(monkeypatch):
    clock = AtomicClock(hosts=(), resync_after=900.0)
    calls = []
    monkeypatch.setattr(clock, "sync_async", lambda: calls.append("sync"))

    clock._last_attempt = time.monotonic()
    assert clock.maybe_resync() is False
    assert calls == [], "a resync just happened; the second tick must not spam NTP"

    clock._last_attempt = time.monotonic() - 1000.0
    assert clock.maybe_resync() is True
    assert calls == ["sync"]


def test_sync_async_does_not_stack_threads(monkeypatch):
    clock = AtomicClock(hosts=())
    release = threading.Event()
    started = []
    monkeypatch.setattr(clock, "sync", lambda: (started.append(1), release.wait(5)))

    clock.sync_async()
    first = clock._thread
    clock.sync_async()  # a sync is already running: this must not start a second
    clock.sync_async()

    assert clock._thread is first
    release.set()
    first.join(timeout=5)
    assert started == [1]


def test_shared_clock_is_one_instance():
    assert shared_clock() is shared_clock()
    assert isinstance(shared_clock(), AtomicClock)
