"""Tests for _connect_backoff exponential backoff helper."""

from adsb_radar.receiver import _connect_backoff


def test_backoff_starts_at_five():
    assert _connect_backoff(0) == 5.0


def test_backoff_doubles_each_attempt():
    assert _connect_backoff(1) == 10.0
    assert _connect_backoff(2) == 20.0
    assert _connect_backoff(3) == 40.0
    assert _connect_backoff(4) == 80.0
    assert _connect_backoff(5) == 160.0


def test_backoff_caps_at_300():
    assert _connect_backoff(6) == 300.0
    assert _connect_backoff(10) == 300.0
    assert _connect_backoff(100) == 300.0


def test_backoff_never_negative():
    for i in range(20):
        assert _connect_backoff(i) > 0
