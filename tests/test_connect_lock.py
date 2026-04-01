"""Tests for per-source connection lock in connect_to_sender."""

import threading
import time

import pytest


def test_second_call_skips_while_lock_held():
    """Second concurrent call to connect_to_sender returns immediately."""
    from adsb_radar.receiver import _get_connect_lock

    src_id = "test0001"
    lock = _get_connect_lock(src_id)

    results = []

    def first():
        acquired = lock.acquire(blocking=False)
        results.append(("first", acquired))
        if acquired:
            time.sleep(0.05)
            lock.release()

    def second():
        time.sleep(0.01)  # let first acquire first
        acquired = lock.acquire(blocking=False)
        results.append(("second", acquired))
        if acquired:
            lock.release()

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert ("first", True) in results
    assert ("second", False) in results


def test_lock_released_after_first_completes():
    """Lock is available again after first holder releases it."""
    from adsb_radar.receiver import _get_connect_lock

    src_id = "test0002"
    lock = _get_connect_lock(src_id)

    # First acquire + release
    assert lock.acquire(blocking=False)
    lock.release()

    # Should be acquirable again
    assert lock.acquire(blocking=False)
    lock.release()


def test_different_sources_have_independent_locks():
    """Two different src_ids get independent locks."""
    from adsb_radar.receiver import _get_connect_lock

    lock_a = _get_connect_lock("aaaa0001")
    lock_b = _get_connect_lock("bbbb0002")

    assert lock_a is not lock_b

    assert lock_a.acquire(blocking=False)
    # lock_a held — lock_b should still be free
    assert lock_b.acquire(blocking=False)
    lock_a.release()
    lock_b.release()
