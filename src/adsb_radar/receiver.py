#!/usr/bin/env python3
"""
receiver.py — ADS-B Reticulum receiver with live curses radar display.

Auto-discovers senders via RNS announce (adsb.radar aspect).
Multiple senders are merged into a single scope with per-sender coverage rings.

Known senders are saved to a config file so they are reconnected instantly on
next startup without waiting for announces.

Usage:
    adsb-receiver                                        # auto-discover all senders
    adsb-receiver --dest <HASH> [--dest <HASH2> ...]    # explicit sender(s)
    adsb-receiver --config /path/to/receiver.conf        # custom config path

Options:
    --dest HASH          Sender destination hash (optional, repeatable)
    --config PATH        Config file path (default: next to receiver.py in the package)
    --block HASH         Block a sender hash at startup (repeatable)
    --map PATH           Load extra landmarks from CSV file
    --alert-db PATH      Load plane-alert-db from PATH
    --alert-db auto      Download plane-alert-db from GitHub if not found locally
    --alert-db none      Disable alert DB entirely
    --home-lat LAT       Receiver home latitude for source gating (default: 41.88)
    --home-lon LON       Receiver home longitude for source gating (default: -87.63)
    --home-range NM      Receiver max useful range in nm; senders farther away are
                         auto-gated as 'out of range' (default: 150)

Keys:
    +/=    zoom in          -       zoom out
    w/a/s/d  pan N/W/S/E    c/Home  reset pan to center
    ↑/↓    select aircraft  PgUp/Dn scroll list
    Tab    toggle scope / sources page
    Space  (sources page) enable / disable selected source
    b      (sources page) block / unblock selected source
    r      reconnect        q/Esc   quit
"""

import argparse
import configparser
import curses
import logging
import math
import os
import queue
import sys
import threading
import time

import RNS

from adsb_radar.alerts import AlertDB
from adsb_radar.proto import (
    CENTER_LAT,
    CENTER_LON,
    GH,
    GW,
    MAX_RANGE,
    NUM_ALT_BANDS,
    alt_band,
    decode_announce_data,
    decode_frame,
    draw_scope,
    encode_view_request,
    get_dist,
    load_map_file,
    plot_targets,
    ring_radii,
    vrate_symbol,
)

# ── Logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)
_console_handler: logging.Handler = logging.NullHandler()  # replaced by setup_logging()


def setup_logging(level: int = logging.INFO, log_file: str = None) -> None:
    """Configure root logger.  Called once from main() before curses starts."""
    global _console_handler
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    _console_handler = logging.StreamHandler()
    _console_handler.setFormatter(fmt)
    root.addHandler(_console_handler)
    if log_file:
        fh = logging.FileHandler(log_file)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    # Suppress RNS internal transport/routing chatter
    RNS.loglevel = RNS.LOG_WARNING


# ── Zoom levels (nm) ─────────────────────────────────────────────────────────
ZOOM_LEVELS = [5, 8, 10, 15, 20, 25, 35, 50, 75, 100]
ZOOM_DEFAULT = 5  # 25 nm (index 5)

VIEW_DEBOUNCE_S = 0.20  # seconds after last pan/zoom key before view-request fires
MAX_AIRCRAFT_DB = 500  # cap on _aircraft_db entries (memory + rogue-sender defence)
MAX_SOURCES = 50  # cap on _sources entries (announce-flood defence)
_PACKET_MIN_INTERVAL = 0.10  # min seconds between accepted packets per source (10/sec)

# ── Thread-safe comms ────────────────────────────────────────────────────────
_frame_queue = queue.SimpleQueue()

# ── Per-sender source registry ────────────────────────────────────────────────
# src_id (dest_hash hex[:8]) → {dest_hash, link, center, range_nm, name,
#                               last_ts, last_rx, status, ac_count,
#                               enabled, dist_nm}
_sources = {}
_sources_lock = threading.Lock()

# ── Home position for geographic source gating ────────────────────────────────
# Set from --home-lat / --home-lon / --home-range in main()
_home_lat = CENTER_LAT
_home_lon = CENTER_LON
_home_range_nm = 150.0

# ── Merged aircraft database (all senders combined) ───────────────────────────
# icao_hex (lowercase) → {ac: dict, source: src_id, received: float}
_aircraft_db = {}
_aircraft_lock = threading.Lock()

# ── Alert DB (loaded at startup) ─────────────────────────────────────────────
_alert_db: AlertDB = AlertDB()

# ── Persistent config ─────────────────────────────────────────────────────────
_DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "receiver.conf")
_config_path = _DEFAULT_CONFIG
_known_senders = {}  # dest_hex (32 chars) → name
_blocked_senders = set()  # set of dest_hex (32 chars)
_config_lock = threading.Lock()


def load_config(path):
    global _config_path, _known_senders, _blocked_senders
    _config_path = path
    cfg = configparser.ConfigParser()
    cfg.read(path)
    with _config_lock:
        _known_senders = (
            dict(cfg.items("known_senders")) if cfg.has_section("known_senders") else {}
        )
        _blocked_senders = (
            set(cfg.options("blocked_senders")) if cfg.has_section("blocked_senders") else set()
        )


def save_config():
    cfg = configparser.ConfigParser()
    with _config_lock:
        ks = dict(_known_senders)
        bs = set(_blocked_senders)
    cfg["known_senders"] = ks
    cfg["blocked_senders"] = {h: "1" for h in bs}
    try:
        with open(_config_path, "w") as f:
            cfg.write(f)
    except OSError as e:
        log.warning("Could not save config to %s: %s", _config_path, e)


def _add_known_sender(dest_hex, name=""):
    """Record a sender hash persistently; writes config if anything changed."""
    with _config_lock:
        existing = _known_senders.get(dest_hex)
        label = name or dest_hex[:8]
        if existing == label:
            return
        _known_senders[dest_hex] = label
    save_config()


# ── RNS helpers ──────────────────────────────────────────────────────────────


def _set_source_status(src_id, status):
    with _sources_lock:
        if src_id in _sources:
            _sources[src_id]["status"] = status


def _on_link_closed(link, src_id):
    with _sources_lock:
        if src_id in _sources:
            _sources[src_id]["link"] = None
            # Only set 'disconnected' if the sender is still active —
            # don't overwrite 'disabled' or 'blocked' (user-controlled states)
            if _sources[src_id].get("enabled", True):
                _sources[src_id]["status"] = "disconnected"
    _frame_queue.put_nowait({"type": "link_closed", "src_id": src_id})


def _on_packet(message, packet, src_id):
    try:
        frame = decode_frame(bytes(message))
        now = time.time()
        nb = len(message)

        with _sources_lock:
            if src_id not in _sources:
                return
            if not _sources[src_id].get("enabled", True):
                return  # disabled or blocked — ignore in-flight packet from async teardown
            if now - _sources[src_id].get("_last_pkt_t", 0) < _PACKET_MIN_INTERVAL:
                return  # rate limit: max 10 packets/sec per source
            _sources[src_id]["_last_pkt_t"] = now
            _sources[src_id]["last_ts"] = frame["ts"]
            _sources[src_id]["last_rx"] = now
            _sources[src_id]["status"] = "linked"
            _sources[src_id]["ac_count"] = len(frame["aircraft"])
            _sources[src_id]["bytes_rx"] = _sources[src_id].get("bytes_rx", 0) + nb
            samples = _sources[src_id].setdefault("bw_samples", [])
            samples.append((now, nb))
            cutoff = now - 10.0
            _sources[src_id]["bw_samples"] = [s for s in samples if s[0] >= cutoff]

        # Merge aircraft — prefer the observation with the latest absolute fix time.
        # seen_pos is seconds-since-last-position at the sender; subtracting from the
        # frame timestamp gives a UTC estimate of when the position was decoded.
        # Using absolute time prevents jumps when multiple senders report the same
        # aircraft from different ADS-B ground stations with slightly different coords.
        # Clamp frame_ts: allow up to +30s future (clock drift) but no more.
        # Even a +1s future timestamp wins every merge decision for 20s; +30s cap
        # prevents a rogue sender from permanently holding any ICAO it claims.
        frame_ts = max(now - 300, min(now + 30, frame["ts"]))
        with _aircraft_lock:
            for ac in frame["aircraft"]:
                icao = (ac.get("hex") or ac.get("icao") or "").lower()
                if not icao:
                    continue
                seen = ac.get("seen_pos", ac.get("seen", 5))
                obs_ts = frame_ts - seen  # absolute UTC of position fix
                existing = _aircraft_db.get(icao)
                if existing is not None and obs_ts <= existing.get("obs_ts", 0):
                    continue  # existing fix is newer or equal; skip
                if len(_aircraft_db) >= MAX_AIRCRAFT_DB and icao not in _aircraft_db:
                    continue  # DB full; only update existing entries
                _aircraft_db[icao] = {"ac": ac, "source": src_id, "received": now, "obs_ts": obs_ts}

        _frame_queue.put_nowait({"type": "frame", "src_id": src_id})
    except Exception:
        log.debug("Packet decode error from %s", src_id, exc_info=True)


def connect_to_sender(src_id, timeout=30):
    with _sources_lock:
        if src_id not in _sources:
            return
        if not _sources[src_id].get("enabled", True):
            return
        dest_hash = _sources[src_id]["dest_hash"]

    _set_source_status(src_id, "searching…")
    if not RNS.Transport.has_path(dest_hash):
        RNS.Transport.request_path(dest_hash)
        deadline = time.time() + timeout
        while not RNS.Transport.has_path(dest_hash):
            if time.time() > deadline:
                _set_source_status(src_id, "no path")
                return
            time.sleep(0.2)

    identity = RNS.Identity.recall(dest_hash)
    if identity is None:
        _set_source_status(src_id, "identity unknown")
        return

    # Second enabled check — user may have pressed Space while we were searching
    with _sources_lock:
        if not _sources.get(src_id, {}).get("enabled", True):
            return

    destination = RNS.Destination(
        identity, RNS.Destination.OUT, RNS.Destination.SINGLE, "adsb", "radar"
    )
    _set_source_status(src_id, "linking…")

    def _on_established(link):
        # Guard: sender may have been disabled/blocked while we were connecting.
        # Tear down outside the lock to avoid deadlock with _on_link_closed.
        should_teardown = False
        sender_name = src_id
        with _sources_lock:
            if src_id not in _sources or not _sources[src_id].get("enabled", True):
                should_teardown = True
            else:
                _sources[src_id]["link"] = link
                _sources[src_id]["status"] = "linked"
                sender_name = _sources[src_id].get("name", src_id)
        if should_teardown:
            link.teardown()
            return
        link.set_packet_callback(lambda msg, pkt: _on_packet(msg, pkt, src_id))
        link.set_link_closed_callback(lambda lnk: _on_link_closed(lnk, src_id))
        _add_known_sender(dest_hash.hex(), sender_name)
        _frame_queue.put_nowait({"type": "link_up", "src_id": src_id})

    try:
        RNS.Link(
            destination,
            established_callback=_on_established,
            closed_callback=lambda lnk: _on_link_closed(lnk, src_id),
        )
    except Exception as e:
        log.error("Failed to create link to %s: %s", src_id, e, exc_info=True)
        _set_source_status(src_id, "error")


def _maybe_connect(dest_hash_bytes, lat, lon, range_nm, name):
    """Called when an announce is received; connect to new senders automatically."""
    dest_hex = dest_hash_bytes.hex()
    if dest_hex in _blocked_senders:
        return
    src_id = dest_hex[:8]
    dist_nm = get_dist(lat, lon, _home_lat, _home_lon)
    enabled = dist_nm <= _home_range_nm + range_nm

    with _sources_lock:
        if src_id in _sources:
            # Update metadata from announce even if already connected
            _sources[src_id].update(
                center=(lat, lon), range_nm=range_nm, name=name, dist_nm=dist_nm
            )
            st = _sources[src_id]["status"]
            if st in ("linked", "linking…", "searching…", "reconnecting…", "disabled", "blocked"):
                return  # user-controlled states — never auto-reconnect
            # Re-enable if a previously out-of-range sender moved into range
            if st == "out of range" and enabled:
                _sources[src_id]["enabled"] = True
                _sources[src_id]["status"] = "pending"
            elif not enabled:
                return  # still gated
        else:
            if len(_sources) >= MAX_SOURCES:
                log.warning("Source limit reached (%d); ignoring %s", MAX_SOURCES, src_id)
                return
            _sources[src_id] = {
                "dest_hash": dest_hash_bytes,
                "link": None,
                "center": (lat, lon),
                "range_nm": range_nm,
                "name": name,
                "last_ts": None,
                "last_rx": None,
                "status": "pending" if enabled else "out of range",
                "ac_count": 0,
                "enabled": enabled,
                "dist_nm": dist_nm,
                "bytes_rx": 0,
                "bw_samples": [],
            }
    _frame_queue.put_nowait({"type": "new_source", "src_id": src_id})
    if enabled:
        threading.Thread(target=connect_to_sender, args=(src_id,), daemon=True).start()


class AdsbAnnounceHandler:
    """RNS announce handler that auto-discovers adsb.radar senders on the mesh."""

    aspect_filter = "adsb.radar"

    def received_announce(self, destination_hash, announced_identity, app_data):
        info = decode_announce_data(app_data)
        if info is None:
            # Announce has no structured metadata — fall back to defaults
            lat, lon, range_nm, name = CENTER_LAT, CENTER_LON, MAX_RANGE, destination_hash.hex()[:8]
        else:
            lat, lon, range_nm, name = info
        _maybe_connect(destination_hash, lat, lon, range_nm, name)


def _send_view_request(center_lat, center_lon, range_nm):
    """Send a view-request to ALL active sender links."""
    with _sources_lock:
        links = [s["link"] for s in _sources.values() if s.get("link") is not None]
    if not links:
        return
    data = encode_view_request(center_lat, center_lon, range_nm)
    for link in links:
        try:
            if link.status == RNS.Link.ACTIVE:
                RNS.Packet(link, data).send()
        except Exception:
            pass


def _check_view_enables(view_lat, view_lon, view_range_nm):
    """Auto-connect any 'out of range' source whose coverage now overlaps the view."""
    # Snapshot blocked set first (different lock than _sources_lock)
    with _config_lock:
        blocked_snap = set(_blocked_senders)
    with _sources_lock:
        to_enable = []
        for sid, s in _sources.items():
            if s.get("status") != "out of range":
                continue
            if s["dest_hash"].hex() in blocked_snap:
                continue
            center = s.get("center")
            if center is None:
                continue
            dist = get_dist(center[0], center[1], view_lat, view_lon)
            if dist <= s.get("range_nm", MAX_RANGE) + view_range_nm:
                s["enabled"] = True
                s["status"] = "pending"
                to_enable.append(sid)
    for sid in to_enable:
        threading.Thread(target=connect_to_sender, args=(sid,), daemon=True).start()


def _get_aircraft():
    """Return merged aircraft list, expiring entries older than 20 seconds."""
    now = time.time()
    with _aircraft_lock:
        stale = [icao for icao, e in _aircraft_db.items() if now - e["received"] > 20.0]
        for icao in stale:
            del _aircraft_db[icao]
        return [e["ac"] for e in _aircraft_db.values()]


def _get_sender_centers():
    """Return [(lat, lon, range_nm)] for all sources that have a known center."""
    with _sources_lock:
        return [
            (s["center"][0], s["center"][1], s.get("range_nm", MAX_RANGE))
            for s in _sources.values()
            if s.get("center") is not None
        ]


def _bw_rate(samples, window=10.0):
    """Return bytes/sec rolling average over the last `window` seconds."""
    if not samples:
        return 0.0
    cutoff = time.time() - window
    return sum(b for t, b in samples if t >= cutoff) / window


def _fmt_bw(bps):
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f}M"
    if bps >= 1000:
        return f"{bps / 1000:.1f}k"
    return f"{int(bps)}b"


# ── 256-colour altitude palette ──────────────────────────────────────────────

_ALT_COLORS_256 = [
    93,  # 0  ≥45000 ft  — purple
    129,  # 1  ≥40000     — violet
    165,  # 2  ≥35000     — pink-violet
    57,  # 3  ≥30000     — blue-violet
    27,  # 4  ≥25000     — royal blue
    33,  # 5  ≥20000     — sky blue
    45,  # 6  ≥17500     — cyan-blue
    51,  # 7  ≥15000     — cyan
    82,  # 8  ≥12500     — lime-green
    118,  # 9  ≥10000     — yellow-green
    154,  # 10 ≥7500      — light yellow-green
    190,  # 11 ≥5000      — bright yellow
    214,  # 12 ≥2500      — orange
    208,  # 13 ≥1000      — dark orange
    202,  # 14 <1000 ft   — red-orange
]

_ALT_COLORS_8 = [
    curses.COLOR_MAGENTA,
    curses.COLOR_MAGENTA,
    curses.COLOR_MAGENTA,
    curses.COLOR_BLUE,
    curses.COLOR_BLUE,
    curses.COLOR_CYAN,
    curses.COLOR_CYAN,
    curses.COLOR_GREEN,
    curses.COLOR_GREEN,
    curses.COLOR_GREEN,
    curses.COLOR_YELLOW,
    curses.COLOR_YELLOW,
    curses.COLOR_YELLOW,
    curses.COLOR_RED,
    curses.COLOR_RED,
]

_HIGHLIGHT_COLORS_256 = {"mil": 196, "gov": 39, "lea": 165}
_HIGHLIGHT_COLORS_8 = {
    "mil": curses.COLOR_RED,
    "gov": curses.COLOR_CYAN,
    "lea": curses.COLOR_MAGENTA,
}

# Curses pair numbers
PAIR_FIRST_ALT = 10  # pairs 10–24 → altitude bands 0–14
PAIR_MIL = 25
PAIR_GOV = 26
PAIR_LEA = 27
PAIR_STATUSB = 30
PAIR_BOTTOMB = 31
PAIR_HEADER = 32
PAIR_SCOPE_BG = 33  # dim green — rings, spokes
PAIR_COMPASS = 34  # bright white — N/S/E/W
PAIR_CROSSHAIR = 35  # bright cyan — ⊕
PAIR_SELMARK = 36  # bright yellow — selected aircraft
PAIR_WARN = 37
PAIR_DETAIL_KEY = 38
PAIR_DETAIL_VAL = 39
PAIR_ALERT_ROW = 40
PAIR_LANDMARK = 41  # dim white — airports, shoreline, city markers
PAIR_COVERAGE = 42  # dim amber — sender coverage boundary ring
PAIR_EMRG = 43  # bright white on red — emergency squawk 7700/7500/7600

_use_256 = False


def setup_colors():
    global _use_256
    curses.start_color()
    curses.use_default_colors()
    _use_256 = curses.COLORS >= 256

    colors = _ALT_COLORS_256 if _use_256 else _ALT_COLORS_8
    for i, fg in enumerate(colors):
        curses.init_pair(PAIR_FIRST_ALT + i, fg, -1)

    hc = _HIGHLIGHT_COLORS_256 if _use_256 else _HIGHLIGHT_COLORS_8
    curses.init_pair(PAIR_MIL, hc["mil"], -1)
    curses.init_pair(PAIR_GOV, hc["gov"], -1)
    curses.init_pair(PAIR_LEA, hc["lea"], -1)

    curses.init_pair(PAIR_STATUSB, curses.COLOR_BLACK, curses.COLOR_GREEN)
    curses.init_pair(PAIR_BOTTOMB, curses.COLOR_BLACK, curses.COLOR_GREEN)
    curses.init_pair(PAIR_HEADER, curses.COLOR_WHITE, curses.COLOR_BLUE)
    curses.init_pair(PAIR_SCOPE_BG, curses.COLOR_GREEN, -1)
    curses.init_pair(PAIR_COMPASS, curses.COLOR_WHITE, -1)
    curses.init_pair(PAIR_CROSSHAIR, curses.COLOR_CYAN, -1)
    curses.init_pair(PAIR_SELMARK, curses.COLOR_YELLOW, -1)
    curses.init_pair(PAIR_WARN, curses.COLOR_RED, -1)
    curses.init_pair(PAIR_DETAIL_KEY, curses.COLOR_WHITE, -1)
    curses.init_pair(PAIR_DETAIL_VAL, curses.COLOR_YELLOW, -1)
    curses.init_pair(PAIR_ALERT_ROW, curses.COLOR_WHITE, -1)

    lm_fg = 250 if _use_256 else curses.COLOR_WHITE
    curses.init_pair(PAIR_LANDMARK, lm_fg, -1)

    cv_fg = 136 if _use_256 else curses.COLOR_YELLOW
    curses.init_pair(PAIR_COVERAGE, cv_fg, -1)

    curses.init_pair(PAIR_EMRG, curses.COLOR_WHITE, curses.COLOR_RED)


def _alt_pair(b, highlight=None):
    if highlight == "mil":
        return curses.color_pair(PAIR_MIL)
    if highlight == "gov":
        return curses.color_pair(PAIR_GOV)
    if highlight == "lea":
        return curses.color_pair(PAIR_LEA)
    if 0 <= b < NUM_ALT_BANDS:
        return curses.color_pair(PAIR_FIRST_ALT + b)
    return curses.color_pair(PAIR_SCOPE_BG)


def _bg_attr(ch):
    """Attribute for a background scope character (band == -1)."""
    if ch in ("N", "S", "E", "W"):
        return curses.color_pair(PAIR_COMPASS) | curses.A_BOLD
    if ch == "⊕":
        return curses.color_pair(PAIR_CROSSHAIR) | curses.A_BOLD
    return curses.color_pair(PAIR_SCOPE_BG) | curses.A_DIM


# ── Safe draw helpers ─────────────────────────────────────────────────────────


def _s(win, y, x, s, attr=0):
    try:
        win.addstr(y, x, s, attr)
    except curses.error:
        pass


def _fill(win, y, x, w, attr=0):
    try:
        win.addstr(y, x, " " * w, attr)
    except curses.error:
        pass


# ── Layout ────────────────────────────────────────────────────────────────────


def calc_layout(rows, cols):
    """
    Side-by-side layout: scope left (~60%), aircraft list right.

      row 0:        status bar (full width)
      rows 1..gh:   scope (cols 0..gw-1)  │  list (cols gw+1..cols-1)
      row gh+1:     legend (left part)     │  list continues
      row gh+2:     detail bar (full width)
      row rows-1:   bottom bar (full width)

    Falls back to stacked layout (scope above list) if terminal is too narrow.
    """
    MIN_H, MIN_W, MIN_LIST_W = 20, 40, 25

    # Fixed rows: status + legend + detail + bottom = 4
    avail_h = rows - 4
    scope_w = max(MIN_W, (cols * 60 // 100) & ~1)
    list_w = cols - scope_w - 1  # 1-col separator

    use_side = avail_h >= MIN_H and scope_w >= MIN_W and list_w >= MIN_LIST_W

    if use_side:
        gw = scope_w
        gh = max(MIN_H, avail_h & ~1)
        scope_r0 = 1
        legend_r = scope_r0 + gh
        detail_r = legend_r + 1
        bottom_r = rows - 1
        list_r0 = 1
        list_col0 = gw + 1
        list_rows = gh + 2  # header row + gh entry rows + legend row
        return dict(
            show=True,
            sideby=True,
            gw=gw,
            gh=gh,
            col_off=0,
            scope_r0=scope_r0,
            legend_r=legend_r,
            detail_r=detail_r,
            list_r0=list_r0,
            list_col0=list_col0,
            list_w=list_w,
            list_rows=list_rows,
            bottom_r=bottom_r,
        )

    # Stacked fallback: scope top, list below
    avail_w = cols - 2
    if avail_h >= MIN_H and avail_w >= MIN_W:
        gw = avail_w & ~1
        gh = max(MIN_H, (avail_h // 2) & ~1)
        scope_r0 = 1
        legend_r = scope_r0 + gh
        detail_r = legend_r + 1
        list_r0 = detail_r + 1
        bottom_r = rows - 1
        list_rows = max(2, bottom_r - list_r0)
        return dict(
            show=True,
            sideby=False,
            gw=gw,
            gh=gh,
            col_off=max(0, (cols - gw) // 2),
            scope_r0=scope_r0,
            legend_r=legend_r,
            detail_r=detail_r,
            list_r0=list_r0,
            list_col0=0,
            list_w=cols,
            list_rows=list_rows,
            bottom_r=bottom_r,
        )

    # Too small — list only
    return dict(
        show=False,
        sideby=False,
        gw=GW,
        gh=GH,
        col_off=0,
        scope_r0=-1,
        legend_r=-1,
        detail_r=-1,
        list_r0=1,
        list_col0=0,
        list_w=cols,
        list_rows=max(2, rows - 2),
        bottom_r=rows - 1,
    )


# ── Rendering ─────────────────────────────────────────────────────────────────


def render_status_bar(stdscr, zoom_range, cols, pan_lat=0.0, pan_lon=0.0):
    db_s = f"  db:{len(_alert_db):,}" if _alert_db else ""
    rings = "/".join(str(r) for r in ring_radii(zoom_range))

    if pan_lat != 0.0 or pan_lon != 0.0:
        v_lat = _home_lat + pan_lat
        v_lon = _home_lon + pan_lon
        pan_s = f"  ctr:({v_lat:.2f},{v_lon:.2f})"
    else:
        pan_s = ""

    now = time.time()
    with _sources_lock:
        sources_snapshot = list(_sources.items())
    with _aircraft_lock:
        n_unique = len(_aircraft_db)

    total_bw = sum(_bw_rate(s.get("bw_samples", [])) for _, s in sources_snapshot)
    bw_s = f"  RX:{_fmt_bw(total_bw)}/s" if sources_snapshot else ""
    ac_s = f"  AC:{n_unique}"

    if not sources_snapshot:
        src_s = "  [listening for announces…]"
    else:
        parts = []
        for src_id, s in sources_snapshot:
            name = s.get("name", src_id)[:10]
            status = s.get("status", "?")
            rx = s.get("last_rx")
            age_s = f"{int(now - rx)}s" if rx else "—"
            n_ac = s.get("ac_count", 0)
            parts.append(f"[{name}:{status} {age_s}|{n_ac}ac]")
        src_s = "  " + "  ".join(parts)

    bar = f" ✈ ADS-B LIVE  zoom:{zoom_range}nm rings:{rings}{ac_s}{bw_s}{pan_s}{db_s}{src_s}"
    _s(
        stdscr,
        0,
        0,
        bar[: cols - 1].ljust(cols - 1),
        curses.color_pair(PAIR_STATUSB) | curses.A_BOLD,
    )


def render_scope(stdscr, grid, band, targets, sel_idx, row0, col0, gw, gh):
    t_by_icao = {t["icao"]: t for t in targets}
    sel_icao = targets[sel_idx]["icao"] if 0 <= sel_idx < len(targets) else None

    for gy in range(gh):
        for gx in range(gw):
            ch = grid[gy][gx]
            b = band[gy][gx]

            if b == -2:
                # Landmark (airport, shoreline, city)
                attr = curses.color_pair(PAIR_LANDMARK) | curses.A_DIM
            elif b == -3:
                # Sender coverage boundary ring
                attr = curses.color_pair(PAIR_COVERAGE) | curses.A_DIM
            elif b >= 0:
                attr = _alt_pair(b) | curses.A_BOLD
            else:
                attr = _bg_attr(ch)

            _s(stdscr, row0 + gy, col0 + gx, ch, attr)

    # Overlay selected aircraft marker in yellow
    if sel_icao and sel_icao in t_by_icao:
        t = t_by_icao[sel_icao]
        gx_ = t.get("gx", -1)
        gy_ = t.get("gy", -1)
        if 0 <= gx_ < gw and 0 <= gy_ < gh:
            _s(
                stdscr,
                row0 + gy_,
                col0 + gx_,
                grid[gy_][gx_],
                curses.color_pair(PAIR_SELMARK) | curses.A_BOLD | curses.A_REVERSE,
            )


def render_legend(stdscr, row, col0, gw, zoom_range):
    rings_s = "  ".join(f"·{r}nm" for r in ring_radii(zoom_range))
    leg = f"  ↑=hdg ?=no-trk ⊕=rcvr +=apt @=city ~=lake  rings:{rings_s}  alt: pur-blu-grn-yel-org"
    _s(stdscr, row, col0, leg[:gw], curses.color_pair(PAIR_SCOPE_BG) | curses.A_DIM)


def render_detail_bar(stdscr, row, cols, targets, sel_idx):
    _fill(stdscr, row, 0, cols - 1, curses.color_pair(PAIR_DETAIL_KEY) | curses.A_DIM)

    if not (0 <= sel_idx < len(targets)):
        _s(
            stdscr,
            row,
            1,
            "no aircraft selected  (↑↓ navigate)",
            curses.color_pair(PAIR_DETAIL_KEY) | curses.A_DIM,
        )
        return

    t = targets[sel_idx]
    alt = t.get("alt")
    b = t.get("band", 7)
    hl = t.get("highlight")
    emrg = t.get("emrg", False)
    gs_ovf = t.get("gs_ovf", False)
    vr_ovf = t.get("vr_ovf", False)

    if isinstance(alt, (int, float)) and alt > 0:
        alt_s = f"FL{int(alt) // 100:03d}"
    elif isinstance(alt, (int, float)):
        alt_s = "SFC"
    else:
        alt_s = "---"

    gs = t.get("gs")
    vr = t.get("vr")
    vs = vrate_symbol(vr)
    vrs = f"{vr:+d}fpm" if vr is not None else ""
    if vr_ovf and vr is not None:
        vrs = f">{abs(vr)}fpm"

    spd_s = ">508kt" if gs_ovf else (f"{gs:.0f}kt" if gs else "---")
    sqk = t.get("sqk") or "----"
    sqk_s = f"SQK:{sqk}"
    if emrg:
        sqk_s = f"SQK:{sqk}(!)"

    base_fields = [
        f"◉ {t.get('call', ''):<9s}",
        f"ICAO:{t.get('icao', '')}",
        f"REG:{t.get('reg', '---') or '---'}",
        f"ALT:{alt_s}",
        f"SPD:{spd_s}",
        f"VS:{vs}{vrs}",
        f"DIST:{t.get('dist', 0):.1f}nm",
        f"BRG:{t.get('brg', '')}°" if t.get("brg") is not None else "BRG:---",
        f"TRK:{t.get('trk', 0):.0f}°" if t.get("trk") is not None else "TRK:---",
        sqk_s,
    ]
    if emrg:
        base_fields.insert(0, "⚠ EMERGENCY")

    alert_fields = []
    if t.get("alert") and _alert_db:
        entry = _alert_db.lookup(t["icao"])
        if entry:
            if entry.operator:
                alert_fields.append(f"OP:{entry.operator[:20]}")
            if entry.type_desc:
                alert_fields.append(f"TYPE:{entry.type_desc[:12]}")
            if entry.tag_str:
                alert_fields.append(f"[{entry.tag_str}]")
            if entry.notes:
                alert_fields.append(f"※{entry.notes[:30]}")

    line = "  ".join(base_fields + alert_fields)
    if emrg:
        attr = curses.color_pair(PAIR_EMRG) | curses.A_BOLD | curses.A_BLINK
    else:
        attr = _alt_pair(b, hl) | curses.A_BOLD
    _s(stdscr, row, 1, line[: cols - 2], attr)


def render_list(stdscr, targets, row0, n_rows, sel_idx, scroll, col0=0, w=None):
    if w is None:
        w = 80
    w = max(w, 20)

    HDR = " {:<9s} {:>6s}  {:>6s}  {:>7s}  {:>4s}  {:>4s}  {}  {:<8s}  {:<9s}  {:<6s}  {}".format(
        "✈ CALLSIGN", "ALT", "SPD", "DIST", "BRG", "TRK", "V", "LAT", "LON", "REG", "NOTE"
    )
    _s(
        stdscr,
        row0,
        col0,
        HDR[: w - 1].ljust(w - 1),
        curses.color_pair(PAIR_HEADER) | curses.A_BOLD,
    )

    if not targets:
        _s(stdscr, row0 + 1, col0 + 2, "No aircraft in range", curses.color_pair(PAIR_WARN))
        return

    FMT = " {:<9s} {:>6s}  {:>6s}  {:>7s}  {:>4s}  {:>4s}  {}  {:<8s}  {:<9s}  {:<6s}  {}"
    for i, t in enumerate(targets[scroll : scroll + n_rows - 1]):
        row = row0 + 1 + i
        abs_idx = scroll + i

        alt = t.get("alt")
        emrg = t.get("emrg", False)
        gs_ovf = t.get("gs_ovf", False)
        vr_ovf = t.get("vr_ovf", False)

        if isinstance(alt, (int, float)) and alt > 0:
            alt_s = f"FL{int(alt) // 100:03d}"
        elif isinstance(alt, (int, float)):
            alt_s = "  SFC"
        else:
            alt_s = "  ---"

        gs = t.get("gs")
        spd_s = ">508kt" if gs_ovf else (f"{gs:4.0f}kt" if gs else "   ---")
        brg = t.get("brg")
        brg_s = f"{brg:3d}°" if brg is not None else " ---"
        trk = t.get("trk")
        trk_s = f"{trk:3.0f}°" if trk is not None else " ---"
        vr = t.get("vr")
        vs_s = vrate_symbol(vr)
        if vr_ovf and vr is not None:
            vs_s = "^^" if vr > 0 else "vv"  # extreme climb/descent indicator
        dist_s = f"{t.get('dist', 0):.1f}nm"
        lat_s = f"{t['lat']:+.4f}"
        lon_s = f"{t['lon']:+.4f}"
        call = (t.get("call") or "")[:9]
        reg = (t.get("reg") or "")[:6]

        note = ""
        if emrg:
            sqk = t.get("sqk") or "????"
            note = f"⚠{sqk}"
        elif t.get("alert") and _alert_db:
            entry = _alert_db.lookup(t["icao"])
            if entry:
                note = entry.tag_str[:8]

        line = FMT.format(call, alt_s, spd_s, dist_s, brg_s, trk_s, vs_s, lat_s, lon_s, reg, note)

        b = t.get("band", 7)
        hl = t.get("highlight")

        if abs_idx == sel_idx:
            attr = curses.color_pair(PAIR_SELMARK) | curses.A_BOLD | curses.A_REVERSE
        elif emrg:
            attr = curses.color_pair(PAIR_EMRG) | curses.A_BOLD | curses.A_BLINK
        elif t.get("alert"):
            attr = _alt_pair(b, hl) | curses.A_BOLD
        else:
            attr = _alt_pair(b, hl)

        _s(stdscr, row, col0, line[: w - 1], attr)


def render_bottom_bar(
    stdscr, row, cols, targets, sel_idx, scroll, zoom_range, pan_lat=0.0, pan_lon=0.0
):
    n = len(targets)
    zi = ZOOM_LEVELS.index(zoom_range) if zoom_range in ZOOM_LEVELS else -1
    sel_s = f"{sel_idx + 1}/{n}" if n else "—"
    panned = pan_lat != 0.0 or pan_lon != 0.0
    pan_hint = "  c=center" if panned else ""
    msg = (
        f" {n} targets  zoom:{zoom_range}nm [{zi + 1}/{len(ZOOM_LEVELS)}]"
        f"  sel:{sel_s}{pan_hint}"
        f"  +/= in  - out  wasd pan  c ctr  ↑↓ sel  Tab=sources  r rcnct  q quit"
    )
    _s(
        stdscr,
        row,
        0,
        msg[: cols - 1].ljust(cols - 1),
        curses.color_pair(PAIR_BOTTOMB) | curses.A_BOLD,
    )


# ── Alert DB enrichment ───────────────────────────────────────────────────────


def _enrich(aircraft):
    if not _alert_db:
        return
    for ac in aircraft:
        entry = _alert_db.lookup(ac.get("hex", "") or ac.get("icao", ""))
        if entry:
            ac["alert"] = True
            ac["highlight"] = entry.highlight
            if not (ac.get("flight") or "").strip():
                ac["reg"] = entry.scope_label
            else:
                ac["reg"] = entry.reg


# ── Sources page ──────────────────────────────────────────────────────────────


def render_sources_page(stdscr, rows, cols, zoom_range, pan_lat, pan_lon, src_sel, src_scroll=0):
    """
    Full-screen table of all known senders.

    Row 0  : existing status bar
    Row 1  : separator
    Row 2  : title
    Row 4  : column headers
    Row 5+ : one row per source (color-coded by status), scrollable
    rows-1 : full dest hash of selected source + key hints

    Returns (src_sel, src_scroll, sources_list) so the caller can act on Space key.
    """
    render_status_bar(stdscr, zoom_range, cols, pan_lat, pan_lon)

    # Separator
    _s(stdscr, 1, 0, "─" * (cols - 1), curses.color_pair(PAIR_SCOPE_BG) | curses.A_DIM)

    # Title
    _s(
        stdscr,
        2,
        2,
        "SOURCES   Tab=scope   r=reconnect   Space=enable/disable   b=block/unblock",
        curses.color_pair(PAIR_HEADER) | curses.A_BOLD,
    )

    # Column headers
    HDR = (
        f"  {'#':>2}  {'En':>2}  {'Name':<12}  {'Status':<14}  "
        f"{'Center':<19}  {'Range':>5}  {'Dist':>6}  {'Last RX':>7}  {'Bw/s':>6}  {'Aircraft':>8}"
    )
    _s(
        stdscr,
        4,
        0,
        HDR[: cols - 1].ljust(cols - 1),
        curses.color_pair(PAIR_HEADER) | curses.A_BOLD,
    )

    now = time.time()
    with _sources_lock:
        sources_list = sorted(
            _sources.items(),
            key=lambda kv: (kv[1].get("dist_nm") is None, kv[1].get("dist_nm") or 0),
        )

    # Clamp cursor and scroll
    n_src = len(sources_list)
    src_sel = max(0, min(src_sel, n_src - 1)) if n_src else 0
    max_rows = rows - 7  # reserve rows for header (5) + totals + separator + bottom
    src_scroll = max(0, min(src_scroll, max(0, n_src - max_rows)))

    visible = sources_list[src_scroll : src_scroll + max_rows]
    for i, (src_id, s) in enumerate(visible):
        abs_i = src_scroll + i
        row = 5 + i

        name = (s.get("name") or src_id)[:12]
        status = s.get("status", "?")
        enabled = s.get("enabled", True)
        center = s.get("center")
        range_nm = s.get("range_nm", 0)
        dist_nm = s.get("dist_nm")
        last_rx = s.get("last_rx")
        ac_count = s.get("ac_count", 0)
        inactive = status in ("out of range", "disabled", "blocked")

        if center:
            ctr_s = f"{center[0]:+.3f},{center[1]:+.3f}"
        else:
            ctr_s = "—"

        age_s = f"{int(now - last_rx)}s" if (last_rx and not inactive) else "—"
        rng_s = f"{range_nm}nm"
        dist_s = f"{dist_nm:.0f}nm" if dist_nm is not None else "—"
        en_ch = "✓" if enabled else "✗"
        bw_s = _fmt_bw(_bw_rate(s.get("bw_samples", []))) if not inactive else "—"

        line = (
            f"  {abs_i + 1:>2}  {en_ch:>2}  {name:<12}  {status[:14]:<14}  "
            f"{ctr_s:<19}  {rng_s:>5}  {dist_s:>6}  {age_s:>7}  {bw_s:>6}  {ac_count:>8}"
        )

        if abs_i == src_sel:
            attr = curses.color_pair(PAIR_SELMARK) | curses.A_BOLD | curses.A_REVERSE
        elif status == "linked":
            attr = curses.color_pair(PAIR_SCOPE_BG) | curses.A_BOLD
        elif status in ("searching…", "linking…", "reconnecting…", "pending"):
            attr = curses.color_pair(PAIR_DETAIL_VAL)
        elif status == "blocked":
            attr = curses.color_pair(PAIR_WARN) | curses.A_BOLD  # bright red — blocked
        else:
            attr = curses.color_pair(PAIR_WARN) | curses.A_DIM  # dim red — disabled/out of range

        _s(stdscr, row, 0, line[: cols - 1].ljust(cols - 1), attr)

    if not sources_list:
        _s(
            stdscr,
            5,
            4,
            "No senders discovered yet — listening for announces…",
            curses.color_pair(PAIR_WARN),
        )

    # Totals line
    n_linked = sum(1 for _, s in sources_list if s.get("status") == "linked")
    total_bw = sum(_bw_rate(s.get("bw_samples", [])) for _, s in sources_list)
    with _aircraft_lock:
        n_unique_ac = len(_aircraft_db)
    totals = (
        f"  {len(sources_list)} sources  {n_linked} linked  "
        f"RX {_fmt_bw(total_bw)}/s  {n_unique_ac} unique AC"
    )
    totals_row = min(5 + len(sources_list), rows - 3)
    _s(
        stdscr,
        totals_row,
        0,
        totals[: cols - 1].ljust(cols - 1),
        curses.color_pair(PAIR_DETAIL_VAL),
    )

    # Bottom separator
    _s(stdscr, rows - 2, 0, "─" * (cols - 1), curses.color_pair(PAIR_SCOPE_BG) | curses.A_DIM)

    # Detail row: full dest hash of selected source
    if sources_list and 0 <= src_sel < len(sources_list):
        _, sel_s = sources_list[src_sel]
        dh = sel_s["dest_hash"].hex()
        dh_fmt = " ".join(dh[i : i + 8] for i in range(0, 32, 8))
        with _config_lock:
            blocked_hint = "  [BLOCKED]" if dh in _blocked_senders else ""
        detail = f"  Dest hash: {dh_fmt}{blocked_hint}   ↑↓ nav   Space toggle   b block/unblock   Tab=scope   r=reconnect"
    else:
        detail = "  No senders"
    _s(
        stdscr,
        rows - 1,
        0,
        detail[: cols - 1].ljust(cols - 1),
        curses.color_pair(PAIR_BOTTOMB) | curses.A_BOLD,
    )

    stdscr.noutrefresh()
    curses.doupdate()
    return src_sel, src_scroll, sources_list


# ── Full redraw ───────────────────────────────────────────────────────────────


def redraw(stdscr, zoom_range, sel_idx, scroll, layout, pan_lat=0.0, pan_lon=0.0, show_rings=False):
    rows, cols = stdscr.getmaxyx()
    L = layout

    view_lat = _home_lat + pan_lat
    view_lon = _home_lon + pan_lon

    render_status_bar(stdscr, zoom_range, cols, pan_lat, pan_lon)

    aircraft = _get_aircraft()
    _enrich(aircraft)

    gw, gh = L["gw"], L["gh"]

    if L["show"]:
        sc = _get_sender_centers() if show_rings else []
        grid, band = draw_scope(
            gw, gh, zoom_range, center_lat=view_lat, center_lon=view_lon, sender_centers=sc
        )
        targets = plot_targets(
            grid, band, aircraft, gw, gh, zoom_range, center_lat=view_lat, center_lon=view_lon
        )
        render_scope(stdscr, grid, band, targets, sel_idx, L["scope_r0"], L["col_off"], gw, gh)
        render_legend(stdscr, L["legend_r"], L["col_off"], gw, zoom_range)

        # Vertical separator for side-by-side layout
        if L.get("sideby"):
            sep_col = L["col_off"] + gw
            for r in range(1, L["legend_r"] + 1):
                _s(stdscr, r, sep_col, "│", curses.color_pair(PAIR_SCOPE_BG) | curses.A_DIM)
    else:
        # No scope — build target list from raw aircraft
        targets = []
        cos_lat = math.cos(math.radians(view_lat))
        for ac in aircraft:
            lat, lon = ac.get("lat"), ac.get("lon")
            if lat is None or lon is None:
                continue
            if ac.get("alt_baro") == "ground":
                continue
            dx = (lon - view_lon) * cos_lat * 60
            dy = (lat - view_lat) * 60
            dist = math.sqrt(dx * dx + dy * dy)
            if dist > zoom_range:
                continue
            alt = ac.get("alt_baro")
            alt_v = alt if isinstance(alt, (int, float)) else None
            dlat = lat - view_lat
            dlon = (lon - view_lon) * cos_lat
            brg = math.degrees(math.atan2(dlon, dlat)) % 360
            targets.append(
                {
                    "call": (ac.get("flight") or "").strip()
                    or ac.get("reg", "")
                    or ac.get("hex", "").upper(),
                    "flight": (ac.get("flight") or "").strip(),
                    "icao": ac.get("hex", "").upper(),
                    "reg": ac.get("reg", ""),
                    "alt": alt_v,
                    "gs": ac.get("gs"),
                    "dist": round(dist, 1),
                    "trk": ac.get("track"),
                    "brg": round(brg),
                    "vr": ac.get("baro_rate")
                    if ac.get("baro_rate") is not None
                    else ac.get("geom_rate"),
                    "lat": lat,
                    "lon": lon,
                    "sqk": ac.get("squawk"),
                    "band": alt_band(alt_v),
                    "alert": bool(ac.get("alert")),
                    "highlight": ac.get("highlight"),
                    "gs_ovf": bool(ac.get("gs_ovf")),
                    "vr_ovf": bool(ac.get("vr_ovf")),
                    "emrg": bool(ac.get("emrg")),
                    "gx": -1,
                    "gy": -1,
                }
            )
        targets.sort(key=lambda a: a["dist"])

    # Clamp sel and scroll
    sel_idx = min(sel_idx, max(0, len(targets) - 1))
    max_scroll = max(0, len(targets) - (L["list_rows"] - 1))
    if sel_idx < scroll:
        scroll = sel_idx
    elif sel_idx >= scroll + L["list_rows"] - 1:
        scroll = sel_idx - L["list_rows"] + 2
    scroll = max(0, min(scroll, max_scroll))

    if L["detail_r"] >= 0:
        render_detail_bar(stdscr, L["detail_r"], cols, targets, sel_idx)
    render_list(
        stdscr,
        targets,
        L["list_r0"],
        L["list_rows"],
        sel_idx,
        scroll,
        col0=L["list_col0"],
        w=L["list_w"],
    )
    render_bottom_bar(
        stdscr, L["bottom_r"], cols, targets, sel_idx, scroll, zoom_range, pan_lat, pan_lon
    )

    stdscr.noutrefresh()
    curses.doupdate()

    return targets, sel_idx, scroll


# ── Main curses loop ──────────────────────────────────────────────────────────


def _curses_main(stdscr):
    curses.curs_set(0)
    stdscr.timeout(500)
    setup_colors()

    # While curses owns the terminal, remove the console handler so logging
    # messages don't corrupt the display.  File handler (if any) stays active.
    # Also redirect any stray print()/RNS output to /dev/null.
    _null_stream = open(os.devnull, "w")
    _old_stdout = sys.stdout
    _old_stderr = sys.stderr
    sys.stdout = _null_stream
    sys.stderr = _null_stream
    logging.getLogger().removeHandler(_console_handler)

    zoom_idx = ZOOM_DEFAULT
    sel_idx = 0
    scroll = 0
    pan_lat = 0.0  # degrees north of _home_lat
    pan_lon = 0.0  # degrees east of _home_lon
    needs_redraw = True
    view_mode = "scope"  # 'scope' or 'sources'
    src_sel = 0  # selected row in sources page
    src_scroll = 0  # scroll offset for sources page
    _last_sources_list = []  # snapshot from last render_sources_page call
    _pan_stamp = 0.0  # time of last pan/zoom — rings shown for 3s after
    # View-request debounce: send only after VIEW_DEBOUNCE_S of inactivity
    _view_req_pending = False
    _view_req_args = None  # (center_lat, center_lon, range_nm)
    _last_pan_key_t = 0.0

    rows, cols = stdscr.getmaxyx()
    layout = calc_layout(rows, cols)

    while True:
        # Flush debounced view-request once inactivity threshold is reached
        if _view_req_pending and time.time() - _last_pan_key_t >= VIEW_DEBOUNCE_S:
            _send_view_request(*_view_req_args)
            _view_req_pending = False

        while True:
            try:
                item = _frame_queue.get_nowait()
            except queue.Empty:
                break
            needs_redraw = True
            if isinstance(item, dict) and item.get("type") == "link_closed":
                src_id = item["src_id"]

                def _rc(sid=src_id):
                    time.sleep(5)
                    with _sources_lock:
                        if not _sources.get(sid, {}).get("enabled", True):
                            return
                    connect_to_sender(sid)

                threading.Thread(target=_rc, daemon=True).start()
            elif isinstance(item, dict) and item.get("type") == "link_up":
                # Push current viewport to the newly linked sender immediately
                # so it filters its frames to our window from the first packet.
                _send_view_request(_home_lat + pan_lat, _home_lon + pan_lon, ZOOM_LEVELS[zoom_idx])

        if needs_redraw:
            stdscr.erase()
            zoom_range = ZOOM_LEVELS[zoom_idx]
            if view_mode == "sources":
                src_sel, src_scroll, _last_sources_list = render_sources_page(
                    stdscr, rows, cols, zoom_range, pan_lat, pan_lon, src_sel, src_scroll
                )
            else:
                show_rings = (time.time() - _pan_stamp) < 3.0
                _, sel_idx, scroll = redraw(
                    stdscr,
                    zoom_range,
                    sel_idx,
                    scroll,
                    layout,
                    pan_lat,
                    pan_lon,
                    show_rings=show_rings,
                )
            needs_redraw = False

        key = stdscr.getch()
        if key == -1:
            needs_redraw = True  # timeout — refresh age counter
            continue

        needs_redraw = True
        zoom_range = ZOOM_LEVELS[zoom_idx]

        # Pan step = zoom_range / 5 nm, converted to degrees.
        # Use current view latitude for longitude scaling so panning stays
        # accurate when far from home.
        view_lat_now = _home_lat + pan_lat
        step_lat = zoom_range / 5 / 60.0
        step_lon = zoom_range / 5 / (60.0 * math.cos(math.radians(view_lat_now)))

        if key == curses.KEY_RESIZE:
            rows, cols = stdscr.getmaxyx()
            layout = calc_layout(rows, cols)
            stdscr.clear()

        elif key == ord("\t"):
            view_mode = "sources" if view_mode == "scope" else "scope"
            stdscr.clear()

        elif key in (ord("+"), ord("=")):
            zoom_idx = max(0, zoom_idx - 1)
            zoom_range = ZOOM_LEVELS[zoom_idx]
            _pan_stamp = time.time()
            _view_req_pending = True
            _view_req_args = (_home_lat + pan_lat, _home_lon + pan_lon, zoom_range)
            _last_pan_key_t = _pan_stamp
            _check_view_enables(_home_lat + pan_lat, _home_lon + pan_lon, zoom_range)

        elif key == ord("-"):
            zoom_idx = min(len(ZOOM_LEVELS) - 1, zoom_idx + 1)
            zoom_range = ZOOM_LEVELS[zoom_idx]
            _pan_stamp = time.time()
            _view_req_pending = True
            _view_req_args = (_home_lat + pan_lat, _home_lon + pan_lon, zoom_range)
            _last_pan_key_t = _pan_stamp
            _check_view_enables(_home_lat + pan_lat, _home_lon + pan_lon, zoom_range)

        elif key in (ord("w"), ord("W")):
            pan_lat += step_lat
            _pan_stamp = time.time()
            _view_req_pending = True
            _view_req_args = (_home_lat + pan_lat, _home_lon + pan_lon, zoom_range)
            _last_pan_key_t = _pan_stamp
            _check_view_enables(_home_lat + pan_lat, _home_lon + pan_lon, zoom_range)

        elif key in (ord("s"), ord("S")):
            pan_lat -= step_lat
            _pan_stamp = time.time()
            _view_req_pending = True
            _view_req_args = (_home_lat + pan_lat, _home_lon + pan_lon, zoom_range)
            _last_pan_key_t = _pan_stamp
            _check_view_enables(_home_lat + pan_lat, _home_lon + pan_lon, zoom_range)

        elif key in (ord("a"), ord("A")):
            pan_lon -= step_lon
            _pan_stamp = time.time()
            _view_req_pending = True
            _view_req_args = (_home_lat + pan_lat, _home_lon + pan_lon, zoom_range)
            _last_pan_key_t = _pan_stamp
            _check_view_enables(_home_lat + pan_lat, _home_lon + pan_lon, zoom_range)

        elif key in (ord("d"), ord("D")):
            pan_lon += step_lon
            _pan_stamp = time.time()
            _view_req_pending = True
            _view_req_args = (_home_lat + pan_lat, _home_lon + pan_lon, zoom_range)
            _last_pan_key_t = _pan_stamp
            _check_view_enables(_home_lat + pan_lat, _home_lon + pan_lon, zoom_range)

        elif key in (ord("c"), ord("C"), curses.KEY_HOME):
            pan_lat = 0.0
            pan_lon = 0.0
            _pan_stamp = time.time()
            _view_req_pending = True
            _view_req_args = (_home_lat, _home_lon, zoom_range)
            _last_pan_key_t = _pan_stamp
            _check_view_enables(_home_lat, _home_lon, zoom_range)

        elif key == curses.KEY_UP:
            if view_mode == "sources":
                src_sel = max(0, src_sel - 1)
                if src_sel < src_scroll:
                    src_scroll = src_sel
            else:
                sel_idx = max(0, sel_idx - 1)

        elif key == curses.KEY_DOWN:
            if view_mode == "sources":
                src_sel += 1
                _src_max_rows = rows - 7
                if src_sel >= src_scroll + _src_max_rows:
                    src_scroll = src_sel - _src_max_rows + 1
            else:
                sel_idx += 1

        elif key == curses.KEY_PPAGE:
            if view_mode == "sources":
                _src_max_rows = rows - 7
                src_scroll = max(0, src_scroll - _src_max_rows)
                src_sel = max(src_sel, src_scroll)
            else:
                scroll = max(0, scroll - (layout["list_rows"] - 2))

        elif key == curses.KEY_NPAGE:
            if view_mode == "sources":
                _src_max_rows = rows - 7
                src_scroll += _src_max_rows
                src_sel = max(src_sel, src_scroll)
            else:
                scroll += layout["list_rows"] - 2

        elif key == ord(" ") and view_mode == "sources":
            if _last_sources_list and 0 <= src_sel < len(_last_sources_list):
                sid, s = _last_sources_list[src_sel]
                with _sources_lock:
                    s["enabled"] = not s.get("enabled", True)
                    if not s["enabled"]:
                        lnk = s.get("link")
                        if lnk:
                            threading.Thread(target=lnk.teardown, daemon=True).start()
                        s["status"] = "disabled"
                    else:
                        s["status"] = "pending"
                        threading.Thread(target=connect_to_sender, args=(sid,), daemon=True).start()

        elif key in (ord("b"), ord("B")) and view_mode == "sources":
            if _last_sources_list and 0 <= src_sel < len(_last_sources_list):
                sid, s = _last_sources_list[src_sel]
                dest_hex = s["dest_hash"].hex()
                with _config_lock:
                    already_blocked = dest_hex in _blocked_senders
                if already_blocked:
                    # Unblock: remove from blocked set, re-enable.
                    # Also add to known_senders so it persists across restarts
                    # even if the reconnect attempt fails.
                    with _sources_lock:
                        sender_name = s.get("name", sid)
                    with _config_lock:
                        _blocked_senders.discard(dest_hex)
                        _known_senders[dest_hex] = sender_name
                    save_config()
                    with _sources_lock:
                        s["enabled"] = True
                        s["status"] = "pending"
                    threading.Thread(target=connect_to_sender, args=(sid,), daemon=True).start()
                else:
                    # Block: add to blocked set, disconnect, mark blocked
                    with _config_lock:
                        _blocked_senders.add(dest_hex)
                    save_config()
                    with _sources_lock:
                        s["enabled"] = False
                        lnk = s.get("link")
                        if lnk:
                            threading.Thread(target=lnk.teardown, daemon=True).start()
                        s["link"] = None
                        s["status"] = "blocked"

        elif key in (ord("r"), ord("R")):
            # Reconnect all enabled+disconnected sources (works in both modes)
            with _sources_lock:
                to_reconnect = [
                    sid
                    for sid, s in _sources.items()
                    if s["status"] not in ("linked", "linking…", "searching…")
                    and s.get("enabled", True)
                ]
            for sid in to_reconnect:
                _set_source_status(sid, "reconnecting…")
                threading.Thread(target=connect_to_sender, args=(sid,), daemon=True).start()

        elif key in (ord("q"), ord("Q"), 27):
            break

        else:
            needs_redraw = False

    # Restore I/O and re-attach console handler
    try:
        sys.stdout = _old_stdout
        sys.stderr = _old_stderr
        logging.getLogger().addHandler(_console_handler)
    finally:
        _null_stream.close()


# ── Entry point ───────────────────────────────────────────────────────────────


def _register_sender(dest_hex, name=None, status="pending"):
    """Add a sender to _sources without starting a connection thread."""
    dest_hash = bytes.fromhex(dest_hex)
    src_id = dest_hex[:8]
    with _sources_lock:
        if src_id not in _sources:
            _sources[src_id] = {
                "dest_hash": dest_hash,
                "link": None,
                "center": None,
                "range_nm": MAX_RANGE,
                "name": name or src_id,
                "last_ts": None,
                "last_rx": None,
                "status": status,
                "ac_count": 0,
                "enabled": status != "blocked",
                "dist_nm": None,
                "bytes_rx": 0,
                "bw_samples": [],
            }
    return src_id


def main():
    global _alert_db, _home_lat, _home_lon, _home_range_nm

    ap = argparse.ArgumentParser(
        description="ADS-B live radar receiver",
        epilog=(
            "With no --dest, the receiver auto-discovers senders via RNS announce. "
            "Multiple --dest values can be given to connect to specific senders. "
            "Known senders are persisted to the config file and reconnected on startup."
        ),
    )
    ap.add_argument(
        "--dest",
        action="append",
        metavar="HASH",
        default=[],
        help="Sender destination hash (optional, repeatable)",
    )
    ap.add_argument(
        "--config",
        metavar="PATH",
        default=_DEFAULT_CONFIG,
        help=f"Config file path (default: {_DEFAULT_CONFIG})",
    )
    ap.add_argument(
        "--block",
        action="append",
        metavar="HASH",
        default=[],
        help="Block a sender hash at startup (repeatable)",
    )
    ap.add_argument(
        "--map",
        metavar="PATH",
        default=None,
        help="Extra landmarks CSV file (type,char,label,lat,lon)",
    )
    ap.add_argument(
        "--alert-db",
        metavar="PATH|auto|none",
        default=None,
        help='plane-alert-db path, "auto" to download, "none" to disable',
    )
    ap.add_argument(
        "--home-lat",
        type=float,
        default=CENTER_LAT,
        metavar="LAT",
        help="Receiver home latitude for source gating",
    )
    ap.add_argument(
        "--home-lon",
        type=float,
        default=CENTER_LON,
        metavar="LON",
        help="Receiver home longitude for source gating",
    )
    ap.add_argument(
        "--home-range",
        type=float,
        default=150.0,
        metavar="NM",
        help="Receiver useful range in nm; senders farther away are auto-gated (default: 150)",
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
        help="Append log output to this file (default: stderr only before curses)",
    )
    args = ap.parse_args()

    numeric_level = getattr(logging, args.log_level.upper(), logging.INFO)
    setup_logging(level=numeric_level, log_file=args.log_file)

    # Load config (known/blocked senders)
    load_config(args.config)

    # CLI --block flags add to blocked set immediately
    for h in args.block:
        h = h.strip().lower()
        if len(h) == 32 and all(c in "0123456789abcdef" for c in h):
            with _config_lock:
                _blocked_senders.add(h)
        else:
            print(f"Warning: --block value ignored (not 32 hex chars): {h!r}")

    # Validate dest hashes
    dest_hexes = []
    for d in args.dest:
        d = d.strip().lower()
        if len(d) != 32 or not all(c in "0123456789abcdef" for c in d):
            print(f"Error: --dest must be 32 hex chars, got: {d!r}")
            sys.exit(1)
        dest_hexes.append(d)

    _home_lat, _home_lon, _home_range_nm = args.home_lat, args.home_lon, args.home_range

    # Load extra map file
    if args.map:
        try:
            load_map_file(args.map)
            log.info("Map file loaded: %s", args.map)
        except Exception as e:
            log.warning("Could not load map file %r: %s", args.map, e)

    alert_db_arg = args.alert_db
    if alert_db_arg and alert_db_arg.lower() != "none":
        _alert_db = AlertDB.load(alert_db_arg if alert_db_arg != "auto" else "auto", verbose=True)
    else:
        _alert_db = AlertDB.load(verbose=False)

    log.info("Connecting to Reticulum …")
    RNS.Reticulum()

    # Register announce handler for auto-discovery of senders on the mesh
    RNS.Transport.register_announce_handler(AdsbAnnounceHandler())

    # Connect to any explicitly specified senders
    for dest_hex in dest_hexes:
        if dest_hex in _blocked_senders:
            log.info("Skipping %s… [blocked]", dest_hex[:8])
            continue
        src_id = _register_sender(dest_hex)
        log.info("Connecting to %s… [explicit]", dest_hex[:8])
        threading.Thread(target=connect_to_sender, args=(src_id,), daemon=True).start()

    # Auto-connect to known senders from config (not blocked, not already queued)
    with _config_lock:
        known_copy = dict(_known_senders)
        blocked_copy = set(_blocked_senders)
    for dest_hex, name in known_copy.items():
        if dest_hex in blocked_copy:
            continue
        src_id = dest_hex[:8]
        if src_id in _sources:
            continue  # already queued via --dest
        _register_sender(dest_hex, name)
        log.info("Connecting to %s (%s) [known]", dest_hex[:8], name)
        threading.Thread(target=connect_to_sender, args=(src_id,), daemon=True).start()

    # Register blocked senders in _sources so they appear in the sources page
    # and can be unblocked with 'b' — don't connect, just show them.
    for dest_hex in blocked_copy:
        src_id = dest_hex[:8]
        if src_id not in _sources:
            name = known_copy.get(dest_hex, dest_hex[:8])
            _register_sender(dest_hex, name, status="blocked")

    if not dest_hexes and not known_copy:
        log.info("Auto-discovery mode — listening for adsb.radar announces …")
    elif not dest_hexes:
        log.info("Auto-discovery also active — listening for new announces …")

    time.sleep(0.3)

    try:
        curses.wrapper(_curses_main)
    except KeyboardInterrupt:
        pass
    log.info("Receiver stopped.")


if __name__ == "__main__":
    main()
