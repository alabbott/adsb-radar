#!/usr/bin/env python3
"""
sender.py — ADS-B → Reticulum sender.

Fetches aircraft.json every 5 seconds, encodes a compact binary v3 frame,
and pushes it to every receiver that has established an RNS Link.

Each receiver can send a view-request packet back over its link to tell the
sender which geographic area it cares about.  The sender then encodes a
per-link frame filtered to that receiver's viewport, so only data within the
receiver's current window is transmitted.

Usage:
    adsb-sender [--interval SECONDS] [--identity PATH]

On first run a new identity is generated and saved to sender_identity
(same directory as this script). The destination hash is printed on
startup — pass it to adsb-receiver --dest <hash>.
"""

import argparse
import json
import logging
import os
import threading
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

import RNS

from adsb_radar.proto import (
    CENTER_LAT,
    CENTER_LON,
    MAX_RANGE,
    decode_frame,
    decode_view_request,
    encode_announce_data,
    encode_frame,
)

# ── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_INTERVAL = 5  # seconds between frames
DEFAULT_IDENTITY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sender_identity")
DEFAULT_URL = "http://localhost:8080/data/aircraft.json"
ANNOUNCE_INTERVAL_IDLE = 60  # re-announce every 60 s when no receivers linked
ANNOUNCE_INTERVAL_LINKED = 60  # re-announce every 60s even when linked (was 300s)
FETCH_MAX_BACKOFF = 60  # cap exponential backoff at 60 s on repeated fetch errors
_FETCH_MAX_BYTES = 4 * 1024 * 1024  # 4 MB safety cap on fetch response

# ── Logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)

VIEW_REQ_MIN_INTERVAL = 0.10  # minimum seconds between accepted view-requests per link (10/sec)

# ── Global link registry (dict for O(1) add/remove) ─────────────────────────
_active_links = {}  # id(link) → link
_links_lock = threading.Lock()

# ── Per-link view state (updated by receiver view-request packets) ────────────
# Maps id(link) → (center_lat, center_lon, range_nm)
_link_views = {}
_link_views_lock = threading.Lock()

# ── Per-link view-request rate limiting ──────────────────────────────────────
# Maps id(link) → last-accepted time (float); prevents view-request floods.
_link_view_times = {}
_link_view_times_lock = threading.Lock()

# ── Announce state (set in main, read by on_link_closed) ─────────────────────
# Mutable container so the callback can reset the main-loop timer.
_dest = None
_app_data = None
_last_announce = [0.0]  # [timestamp] — single-element list for mutability from callback

# ── Per-link establishment timestamps (for link-duration logging) ─────────────
_link_times = {}  # id(link) → established_time
_link_times_lock = threading.Lock()


# ── RNS callbacks ─────────────────────────────────────────────────────────────


def on_link_established(link):
    link.set_link_closed_callback(on_link_closed)
    link.set_packet_callback(on_view_request)  # listen for pan/zoom requests
    with _links_lock:
        _active_links[id(link)] = link
    with _link_times_lock:
        _link_times[id(link)] = time.time()
    log.info("LINK_UP   hash=%s  peers=%d", link.hash.hex()[:8], _link_count())


def on_link_closed(link):
    with _links_lock:
        _active_links.pop(id(link), None)
    with _link_views_lock:
        _link_views.pop(id(link), None)
    with _link_view_times_lock:
        _link_view_times.pop(id(link), None)
    with _link_times_lock:
        t0 = _link_times.pop(id(link), None)
    up_for = f"{time.time() - t0:.0f}s" if t0 else "?"
    log.info("LINK_DOWN hash=%s  up_for=%s  peers=%d", link.hash.hex()[:8], up_for, _link_count())
    # Re-announce immediately so receivers can find a fresh path when
    # reconnecting.  Without this, the path can sit stale on the network for up
    # to ANNOUNCE_INTERVAL_LINKED (300 s), causing "no path" on reconnect.
    if _dest is not None:
        _dest.announce(app_data=_app_data)
        _last_announce[0] = time.time()
        log.info("ANNOUNCE  trigger=link_close")


def on_view_request(message, packet):
    """
    Called when a receiver sends a view-request packet.
    Updates the per-link view state so the next frame sent to that receiver
    will be filtered to its current viewport.

    Rate-limited to VIEW_REQ_MIN_INTERVAL per link to prevent flood attacks.
    The receiver debounces at 200ms so legitimate heavy panning is well below
    this threshold in normal use.
    """
    link = packet.link
    now = time.time()
    with _link_view_times_lock:
        if now - _link_view_times.get(id(link), 0) < VIEW_REQ_MIN_INTERVAL:
            log.debug("VIEW_REQ_DROPPED  hash=%s  reason=rate_limited", link.hash.hex()[:8])
            return
        _link_view_times[id(link)] = now

    data = bytes(message)
    result = decode_view_request(data)
    if result is None:
        log.debug("VIEW_REQ_DROPPED  hash=%s  reason=decode_error  len=%d", link.hash.hex()[:8], len(data))
        return
    c_lat, c_lon, r_nm = result
    with _link_views_lock:
        _link_views[id(link)] = (c_lat, c_lon, r_nm)
    log.info(
        "VIEW_REQ  hash=%s  lat=%.3f  lon=%.3f  range=%.0fnm",
        link.hash.hex()[:8], c_lat, c_lon, r_nm,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _link_count():
    with _links_lock:
        return len(_active_links)


def load_or_create_identity(path):
    if os.path.exists(path):
        identity = RNS.Identity.from_file(path)
        if identity is None:
            raise RuntimeError(f"Corrupt identity file: {path}")
        log.info("Loaded identity from %s", path)
    else:
        identity = RNS.Identity()
        identity.to_file(path)
        log.info("Created new identity → %s", path)
    return identity


def fetch_aircraft(url, timeout=6, api_key=None):
    """Fetch aircraft list from URL; auto-detects readsb ('aircraft') or adsb.fi ('ac') key."""
    req = Request(url)
    if api_key is not None:
        req.add_header("api-auth", api_key)
    with urlopen(req, timeout=timeout) as resp:
        raw_bytes = resp.read(_FETCH_MAX_BYTES)
    data = json.loads(raw_bytes)
    return data.get("aircraft") or data.get("ac") or []


def broadcast_frame(aircraft_list, sender_lat, sender_lon, sender_range):
    """
    Encode and send a frame to each active link.
    Each link gets its own frame filtered to that receiver's current viewport.
    If the receiver has not sent a view request, the sender's configured
    center (sender_lat/sender_lon) and coverage range are used.
    """
    with _links_lock:
        snapshot = list(_active_links.values())
    sent = 0
    total_ac = len(aircraft_list)
    for link in snapshot:
        if link.status != RNS.Link.ACTIVE:
            continue
        with _link_views_lock:
            view = _link_views.get(id(link))
        if view is not None:
            c_lat, c_lon, r_nm = view
            viewport_source = "receiver"
        else:
            c_lat, c_lon, r_nm = sender_lat, sender_lon, sender_range
            viewport_source = "default"
        try:
            frame = encode_frame(aircraft_list, center_lat=c_lat, center_lon=c_lon, range_nm=r_nm)
            pkt = RNS.Packet(link, frame)
            pkt.send()
            sent += 1
            # Count filtered AC by decoding the frame we just sent
            filtered_ac = len(decode_frame(frame).get("aircraft", []))
            log.debug(
                "FRAME_SENT  hash=%s  ac=%d/%d  bytes=%d  view=%s"
                "  lat=%.3f  lon=%.3f  range=%.0fnm",
                link.hash.hex()[:8], filtered_ac, total_ac, len(frame),
                viewport_source, c_lat, c_lon, r_nm,
            )
        except Exception as e:
            log.warning("Send error on %s: %s", link.hash.hex()[:8], e)
    return sent


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser(description="ADS-B Reticulum sender")
    ap.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        metavar="SECS",
        help="Frame interval in seconds (default 5)",
    )
    ap.add_argument(
        "--identity", default=DEFAULT_IDENTITY_PATH, metavar="PATH", help="Path to identity file"
    )
    ap.add_argument(
        "--url",
        default=DEFAULT_URL,
        metavar="URL",
        help=f"Aircraft JSON endpoint (default: {DEFAULT_URL})",
    )
    ap.add_argument(
        "--lat",
        type=float,
        default=CENTER_LAT,
        metavar="LAT",
        help=f"Sender center latitude (default: {CENTER_LAT})",
    )
    ap.add_argument(
        "--lon",
        type=float,
        default=CENTER_LON,
        metavar="LON",
        help=f"Sender center longitude (default: {CENTER_LON})",
    )
    ap.add_argument(
        "--range",
        type=float,
        default=MAX_RANGE,
        dest="range_nm",
        metavar="NM",
        help=f"Coverage radius in nautical miles (default: {MAX_RANGE})",
    )
    ap.add_argument(
        "--name",
        default="adsb-radar",
        metavar="NAME",
        help="Station name in announce (default: adsb-radar)",
    )
    ap.add_argument(
        "--api-key",
        default=None,
        metavar="KEY",
        help='API auth key sent as "api-auth" header (e.g. adsb.exchange). '
        "Can also be set via the ADSB_API_KEY environment variable "
        "(env var is preferred to avoid exposure in process listings)",
    )
    ap.add_argument(
        "--log-level",
        default="INFO",
        metavar="LEVEL",
        help="Python log level: DEBUG, INFO, WARNING, ERROR (default: INFO)",
    )
    ap.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="Append log output to this file (default: stderr)",
    )
    args = ap.parse_args()

    # Configure Python logging
    numeric_level = getattr(logging, args.log_level.upper(), logging.INFO)
    handlers = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file))
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )

    # Prefer env var for API key so it doesn't appear in `ps aux` output
    api_key = args.api_key if args.api_key is not None else os.environ.get("ADSB_API_KEY")

    sender_lat = args.lat
    sender_lon = args.lon
    sender_range = args.range_nm

    # Validate geographic arguments
    if not (-90.0 <= sender_lat <= 90.0):
        ap.error(f"--lat must be between -90 and 90 (got {sender_lat})")
    if not (-180.0 <= sender_lon <= 180.0):
        ap.error(f"--lon must be between -180 and 180 (got {sender_lon})")
    if sender_range <= 0:
        ap.error(f"--range must be a positive number (got {sender_range})")

    # Connect to running rnsd shared instance
    log.info("Connecting to Reticulum …")
    r = RNS.Reticulum()
    # Suppress RNS internal transport/routing chatter; we have our own logging
    RNS.loglevel = RNS.LOG_WARNING
    try:
        iface_names = [str(i) for i in r.transport.interfaces]
        log.info("RNS interfaces: %s", ", ".join(iface_names) if iface_names else "(none — embedded transport)")
    except Exception:
        log.debug("Could not enumerate RNS interfaces", exc_info=True)

    identity = load_or_create_identity(args.identity)

    dest = RNS.Destination(
        identity,
        RNS.Destination.IN,
        RNS.Destination.SINGLE,
        "adsb",
        "radar",
    )
    dest.set_proof_strategy(RNS.Destination.PROVE_ALL)
    dest.accepts_links(True)
    dest.set_link_established_callback(on_link_established)

    app_data = encode_announce_data(sender_lat, sender_lon, sender_range, args.name)

    log.info("Destination hash : %s", dest.hexhash)
    log.info("Receiver command : adsb-receiver --dest %s", dest.hexhash)
    log.info("URL              : %s", args.url)
    log.info("Center           : (%s, %s)  range: %snm", sender_lat, sender_lon, sender_range)
    log.info("Name             : %s", args.name)
    log.info(
        "SENDER_START  hash=%s  name=%s  lat=%.3f  lon=%.3f  range=%.0fnm  url=%s",
        dest.hexhash[:8], args.name, sender_lat, sender_lon, sender_range, args.url,
    )

    global _dest, _app_data
    _dest = dest
    _app_data = app_data

    dest.announce(app_data=app_data)
    log.info("ANNOUNCE  trigger=startup  hash=%s", dest.hexhash[:8])
    log.info("Broadcasting every %ss — waiting for receivers …", args.interval)

    _last_announce[0] = time.time()
    fetch_failures = 0

    while True:
        loop_start = time.time()

        try:
            raw = fetch_aircraft(args.url, api_key=api_key)
            with_pos = [a for a in raw if a.get("lat")]
            n_sent = broadcast_frame(raw, sender_lat, sender_lon, sender_range)
            fetch_failures = 0
            log.info(
                "%3d ac total | %d with pos | %d/%d link(s) sent",
                len(raw),
                len(with_pos),
                n_sent,
                _link_count(),
            )
        except (URLError, TimeoutError, OSError) as e:
            fetch_failures += 1
            backoff = min(FETCH_MAX_BACKOFF, args.interval * (2 ** (fetch_failures - 1)))
            log.warning("Fetch error (attempt %d, retry in %.0fs): %s", fetch_failures, backoff, e)
            time.sleep(backoff)
            continue
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            log.error("Loop error: %s", e, exc_info=True)

        # Periodic re-announce so receivers can discover/re-discover this sender.
        # Use ANNOUNCE_INTERVAL_IDLE when no receivers are linked so a newly
        # started receiver sees us quickly.  Back off to ANNOUNCE_INTERVAL_LINKED
        # once at least one link is active (announces are cheap but not free).
        announce_interval = (
            ANNOUNCE_INTERVAL_IDLE if _link_count() == 0 else ANNOUNCE_INTERVAL_LINKED
        )
        if time.time() - _last_announce[0] >= announce_interval:
            dest.announce(app_data=app_data)
            _last_announce[0] = time.time()
            log.info("ANNOUNCE  trigger=periodic  peers=%d", _link_count())

        # Sleep remainder of interval
        elapsed = time.time() - loop_start
        remaining = args.interval - elapsed
        if remaining > 0:
            time.sleep(remaining)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Sender stopped.")
