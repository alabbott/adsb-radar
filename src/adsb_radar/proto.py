#!/usr/bin/env python3
"""
adsb_proto.py — shared encode/decode module for ADS-B over Reticulum.

Binary frame format v3 (~852 bytes for 40 aircraft):
  Header 12 bytes: magic(2) version(1) timestamp(4) count(1) clat(2) clon(2)
  Per-AC 13 bytes: icao(3) lat(2) lon(2) alt(2) track(1) gs(1) vrate(1) flags(1)
  Callsign (variable, if flags bit 4 set): len(1) + chars(1-7)

View-request packet (receiver → sender, 8 bytes):
  VIEW_REQ_MAGIC(2) clat_i(2) clon_i(2) range_nm(2)
  Receiver sends this when the user pans/zooms so the sender filters its data
  to match the receiver's current viewport.

Backward compat: v1 (8-byte header) and v2 frames are decoded.
v3 vs v2: altitude stored in 10-ft units (v2 stored raw feet, clipping at FL327).

Encoding limits:
  Latitude  : ±90° → encoded x200 → int16 ±163.84° → all valid latitudes fit
  Longitude : ±180° needed, but int16 x200 = ±163.84° → lons beyond ±163.8°
              (far-western Aleutians, Pacific dateline) will be clamped
  Altitude  : int16 x10 ft → ±327,670 ft — covers FL000-FL650 (v3)
  Groundspeed: uint8 x2 kt → 0-508 kt; >508 kt sets FL_GS_OVF flag
  Vertical rate: int8 x64 fpm → ±8,128 fpm; clipped values set FL_VR_OVF flag
  Track     : uint8 x2° → 0-358° at 2° resolution

Bandwidth notes (LoRa SF8/BW125 ~250-500 bytes/sec usable via Reticulum):
  20 aircraft → ~432 B  (fits in one RNS Link packet, MDU=431 with small frames)
  40 aircraft → ~852 B  (two RNS packets; ~2-4 s LoRa airtime per frame)
  Recommended: use MAX_AC=20 on LoRa-only links, 40 on TCP/backbone.
"""

import csv
import math
import struct
import time
import warnings

# ── Configuration ───────────────────────────────────────────────────────────
CENTER_LAT = 41.88
CENTER_LON = -87.63
MAX_RANGE = 25  # nautical miles — default sender coverage radius
MAX_AC = 40  # max aircraft per frame
CALL_MAX = 7  # max callsign chars encoded

# ── Default radar grid dimensions ──────────────────────────────────────────
GW = 72
GH = 36

ARROWS = ["↑", "↗", "→", "↘", "↓", "↙", "←", "↖"]

# ── Frame format ────────────────────────────────────────────────────────────
MAGIC = b"\xad\xb5"
VERSION = 3

# v1 (legacy): magic(2s) version(B) timestamp(I) count(B) = 8 bytes
HEADER_FMT_V1 = "!2sBIB"
HEADER_SZ_V1 = struct.calcsize(HEADER_FMT_V1)  # 8

# v2 (current): magic(2s) version(B) timestamp(I) count(B) clat(h) clon(h) = 12 bytes
HEADER_FMT_V2 = "!2sBIBhh"
HEADER_SZ_V2 = struct.calcsize(HEADER_FMT_V2)  # 12

HEADER_FMT = HEADER_FMT_V2
HEADER_SZ = HEADER_SZ_V2

AC_FMT = "!3shhhBBbB"  # icao(3s) lat(h) lon(h) alt(h) track(B) gs(B) vrate(b) flags(B)
AC_SZ = struct.calcsize(AC_FMT)  # 13

TRACK_NONE = 0xFF
GS_NONE = 0xFF
VRATE_NONE = -128

FL_ALT = 0x01  # altitude field is valid
FL_TRACK = 0x02  # track field is valid
FL_GS = 0x04  # groundspeed field is valid
FL_VRATE = 0x08  # vertical rate field is valid
FL_CALLSIGN = 0x10  # variable-length callsign follows the fixed record
FL_GS_OVF = 0x20  # groundspeed was clipped (actual ≥510 kt; displayed with '>')
FL_VR_OVF = 0x40  # vertical rate was clipped (actual magnitude > 8128 fpm)
FL_EMRG = 0x80  # emergency squawk active: 7700 (general), 7500 (hijack), 7600 (radio)


# ── View-request packet (receiver → sender) ──────────────────────────────────
VIEW_REQ_MAGIC = b"\xad\xb6"
VIEW_REQ_FMT = "!2shhH"  # magic(2s) clat_i(h) clon_i(h) range_nm(H)
VIEW_REQ_SZ = struct.calcsize(VIEW_REQ_FMT)  # 8


# ── Announce app_data format (sender → mesh, decoded by receivers) ────────────
# clat_i(h) clon_i(h) range_nm(H) name(8s) = 14 bytes, lat/lon scaled x 200
ANNOUNCE_FMT = "!hhH8s"
ANNOUNCE_SZ = struct.calcsize(ANNOUNCE_FMT)


def encode_announce_data(lat, lon, range_nm, name="adsb-radar"):
    """Encode sender metadata for RNS announce app_data (14 bytes)."""
    clat_i = max(-32768, min(32767, int(round(lat * 200))))
    clon_i = max(-32768, min(32767, int(round(lon * 200))))
    r = max(1, min(65535, int(range_nm)))
    name_b = name.encode("ascii", errors="replace")
    if len(name_b) > 8:
        warnings.warn(
            f"Sender name {name!r} exceeds 8-byte announce limit and will be "
            f"truncated to {name_b[:8].decode('ascii', errors='replace')!r}",
            stacklevel=2,
        )
    name_b = name_b[:8].ljust(8, b"\x00")
    return struct.pack(ANNOUNCE_FMT, clat_i, clon_i, r, name_b)


def decode_announce_data(app_data):
    """
    Decode sender metadata from RNS announce app_data.
    Returns (lat, lon, range_nm, name) or None if invalid/missing.
    """
    if not app_data or len(app_data) < ANNOUNCE_SZ:
        return None
    try:
        clat_i, clon_i, r, name_b = struct.unpack_from(ANNOUNCE_FMT, app_data)
    except struct.error:
        return None
    lat_f = clat_i / 200.0
    lon_f = clon_i / 200.0
    if not (-90.0 <= lat_f <= 90.0) or not (-180.0 <= lon_f <= 180.0):
        return None
    return (lat_f, lon_f, int(r), name_b.rstrip(b"\x00").decode("ascii", errors="replace"))


def encode_view_request(center_lat, center_lon, range_nm):
    """Encode a view-request packet (receiver → sender, 8 bytes)."""
    clat_i = max(-32768, min(32767, int(round(center_lat * 200))))
    clon_i = max(-32768, min(32767, int(round(center_lon * 200))))
    r = max(1, min(65535, int(range_nm)))
    return struct.pack(VIEW_REQ_FMT, VIEW_REQ_MAGIC, clat_i, clon_i, r)


def decode_view_request(data):
    """
    Decode a view-request packet.
    Returns (center_lat, center_lon, range_nm) tuple, or None if invalid.
    """
    if len(data) < VIEW_REQ_SZ:
        return None
    try:
        magic, clat_i, clon_i, range_nm = struct.unpack_from(VIEW_REQ_FMT, data, 0)
    except struct.error:
        return None
    if magic != VIEW_REQ_MAGIC:
        return None
    return (clat_i / 200.0, clon_i / 200.0, int(range_nm))


# ── Altitude banding ─────────────────────────────────────────────────────────
# 15 bands, index 0 = highest, 14 = lowest.
ALT_BAND_THRESHOLDS = [
    45000,  # 0  purple
    40000,  # 1  violet
    35000,  # 2  pink-violet
    30000,  # 3  blue-violet
    25000,  # 4  royal blue
    20000,  # 5  sky blue
    17500,  # 6  cyan-blue
    15000,  # 7  cyan-green
    12500,  # 8  lime
    10000,  # 9  yellow-green
    7500,  # 10 bright yellow
    5000,  # 11 yellow
    2500,  # 12 orange-yellow
    1000,  # 13 orange
    0,  # 14 red-orange (lowest)
]
NUM_ALT_BANDS = len(ALT_BAND_THRESHOLDS)  # 15


def alt_band(alt_ft):
    """
    Return altitude band index 0 (highest, ≥45000 ft) … 14 (lowest, <1000 ft).
    Returns 6 (mid-range) for unknown altitude.
    """
    if alt_ft is None:
        return 6
    for i, threshold in enumerate(ALT_BAND_THRESHOLDS):
        if alt_ft >= threshold:
            return i
    return NUM_ALT_BANDS - 1


# ── Landmarks ─────────────────────────────────────────────────────────────────
# Built-in set covering Chicago metro + Great Lakes region.
# Extended at runtime by load_map_file() if landmarks.csv is present.
# (lat, lon, char, label)
LANDMARKS = [
    # ── Illinois / Chicago metro ──────────────────────────────────────────────
    (41.9742, -87.9073, "+", "ORD"),  # O'Hare International
    (41.7868, -87.7522, "+", "MDW"),  # Midway
    (42.1142, -87.9015, "+", "PWK"),  # Chicago Executive
    (41.6163, -87.4128, "+", "GYY"),  # Gary/Chicago Int'l
    (41.8789, -87.6359, "@", "CHI"),  # Willis Tower / downtown
    (40.6641, -89.6933, "+", "PIA"),  # Peoria Int'l
    (39.8441, -89.6783, "+", "SPI"),  # Springfield Capital
    (41.4485, -90.5075, "+", "MLI"),  # Quad Cities Int'l
    # ── Wisconsin ─────────────────────────────────────────────────────────────
    (42.9472, -87.8966, "+", "MKE"),  # Milwaukee Mitchell
    (42.9560, -87.9100, "@", "MKE"),  # Milwaukee city
    (43.1398, -89.3375, "+", "MSN"),  # Madison Dane County
    (44.4851, -88.1296, "+", "GRB"),  # Green Bay Austin Straubel
    (44.5133, -88.0157, "@", "GRB"),  # Green Bay city
    (44.2580, -88.5197, "+", "ATW"),  # Appleton Int'l
    (43.8791, -91.2567, "+", "LSE"),  # La Crosse Regional
    (44.8657, -91.4855, "+", "EAU"),  # Eau Claire Regional
    (45.5208, -92.8575, "+", "SUS"),  # Superior/Duluth edge
    # ── Michigan lower peninsula ──────────────────────────────────────────────
    (42.2121, -83.3534, "+", "DTW"),  # Detroit Metro Wayne County
    (42.3314, -83.0458, "@", "DET"),  # Detroit city
    (42.8808, -85.5228, "+", "GRR"),  # Gerald R. Ford Int'l
    (42.9633, -85.6680, "@", "GRR"),  # Grand Rapids city
    (42.7787, -84.5876, "+", "LAN"),  # Capital Region Int'l (Lansing)
    (42.9659, -83.7435, "+", "FNT"),  # Bishop Int'l (Flint)
    (43.5322, -84.0796, "+", "MBS"),  # MBS Int'l (Saginaw/Bay City)
    (44.7414, -85.5822, "+", "TVC"),  # Cherry Capital (Traverse City)
    (42.2345, -85.5522, "+", "AZO"),  # Kalamazoo/Battle Creek
    (41.5869, -83.8022, "+", "TOL"),  # Toledo Express
    # ── Michigan upper peninsula ──────────────────────────────────────────────
    (46.3536, -87.3954, "+", "MQT"),  # Sawyer Int'l (Marquette)
    (46.4744, -84.5094, "+", "CIU"),  # Chippewa County (Sault Ste Marie)
    (47.1682, -88.4891, "+", "CMX"),  # Houghton County
    (45.9223, -89.7301, "+", "RHI"),  # Rhinelander/Oneida
    # ── Indiana ───────────────────────────────────────────────────────────────
    (39.7173, -86.2944, "+", "IND"),  # Indianapolis Int'l
    (39.7684, -86.1581, "@", "IND"),  # Indianapolis city
    (40.9785, -85.1952, "+", "FWA"),  # Fort Wayne Int'l
    (41.7087, -86.3172, "+", "SBN"),  # South Bend Int'l
    (38.0369, -87.5317, "+", "EVV"),  # Evansville Regional
    # ── Ohio ──────────────────────────────────────────────────────────────────
    (39.9980, -82.8919, "+", "CMH"),  # John Glenn Columbus Int'l
    (39.9612, -82.9988, "@", "CMH"),  # Columbus city
    (41.4117, -81.8498, "+", "CLE"),  # Cleveland Hopkins Int'l
    (41.4993, -81.6944, "@", "CLE"),  # Cleveland city
    (39.9024, -84.2194, "+", "DAY"),  # Dayton Int'l
    (40.9161, -81.4422, "+", "CAK"),  # Akron-Canton
    (39.0488, -84.6678, "+", "CVG"),  # Cincinnati/Northern KY
    # ── Minnesota ─────────────────────────────────────────────────────────────
    (44.8820, -93.2218, "+", "MSP"),  # Minneapolis-St Paul Int'l
    (44.9778, -93.2650, "@", "MSP"),  # Minneapolis city
    (43.9086, -92.4998, "+", "RST"),  # Rochester Int'l
    (46.8421, -92.1936, "+", "DLH"),  # Duluth Int'l
    # ── Iowa ──────────────────────────────────────────────────────────────────
    (41.5340, -93.6631, "+", "DSM"),  # Des Moines Int'l
    (41.8847, -91.7108, "+", "CID"),  # Eastern Iowa (Cedar Rapids)
    (42.4020, -90.7094, "+", "DBQ"),  # Dubuque Regional
    # ── Missouri / S Illinois ─────────────────────────────────────────────────
    (38.7487, -90.3700, "+", "STL"),  # St. Louis Lambert Int'l
    (38.6270, -90.1994, "@", "STL"),  # St. Louis city
]

# Lake Michigan shoreline — full outline both shores, drawn as '~'
LAKE_SHORELINE = [
    # West shore south → north (Illinois / Wisconsin)
    (41.65, -87.50),
    (41.70, -87.53),
    (41.75, -87.56),
    (41.79, -87.58),
    (41.82, -87.60),
    (41.84, -87.62),
    (41.85, -87.61),
    (41.88, -87.61),
    (41.90, -87.61),
    (41.93, -87.62),
    (41.98, -87.64),
    (42.02, -87.66),
    (42.08, -87.69),
    (42.20, -87.75),
    (42.35, -87.78),
    (42.50, -87.81),
    (42.67, -87.83),
    (42.80, -87.79),
    (43.00, -87.75),
    (43.10, -87.87),
    (43.40, -87.90),
    (43.60, -87.70),
    (43.80, -87.55),
    (44.00, -87.45),
    (44.20, -87.40),
    (44.50, -87.44),
    (44.75, -87.50),
    (45.00, -87.40),
    (45.20, -87.10),
    (45.40, -86.80),
    (45.60, -86.60),
    (45.80, -86.40),
    # North tip / Straits of Mackinac
    (45.90, -86.70),
    (46.00, -86.90),
    # East shore north → south (Michigan)
    (45.80, -86.70),
    (45.50, -86.30),
    (45.20, -85.90),
    (44.90, -85.55),
    (44.70, -85.40),
    (44.50, -85.35),
    (44.25, -85.35),
    (44.00, -85.55),
    (43.75, -85.80),
    (43.50, -86.10),
    (43.25, -86.35),
    (43.00, -86.55),
    (42.75, -86.60),
    (42.50, -86.55),
    (42.25, -86.50),
    (42.00, -86.57),
    (41.75, -86.65),
    (41.50, -87.00),
    (41.30, -87.20),
]

# Lake Superior — sparse south shore (relevant for UP Michigan / Wisconsin)
LAKE_SUPERIOR_SHORELINE = [
    (46.50, -84.60),
    (46.60, -85.30),
    (46.65, -86.20),
    (46.58, -87.30),
    (46.50, -88.00),
    (46.60, -88.80),
    (46.70, -89.60),
    (46.80, -90.40),
    (46.72, -91.20),
    (46.60, -92.00),
    (46.75, -92.50),
    (46.83, -92.10),
]

# ── Extra landmarks loaded from landmarks.csv at runtime ─────────────────────
_extra_landmarks = []  # [(lat, lon, char, label), …]
_extra_shoreline = []  # [(lat, lon), …]


def load_map_file(path):
    """
    Load extra landmarks from a CSV file and add them to the module globals.

    CSV format (header required):
        type,char,label,lat,lon
        airport,+,ORD,41.9742,-87.9073
        city,@,CHI,41.8789,-87.6359
        water,~,,42.08,-87.69

    type  : any string — used only for documentation; 'water' items have no label
    char  : single display character
    label : text label (may be empty for water/shoreline points)
    lat   : float latitude
    lon   : float longitude

    Safe to call multiple times; each call replaces previously loaded extras.
    Silently ignores missing/unreadable files so it is always safe to call.
    """
    global _extra_landmarks, _extra_shoreline
    new_lm = []
    new_sh = []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    lat = float(row["lat"])
                    lon = float(row["lon"])
                    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                        continue
                    char = (row.get("char") or "~").strip() or "~"
                    lbl = (row.get("label") or "").strip()
                    typ = (row.get("type") or "").strip().lower()
                    if typ == "water" or not lbl:
                        new_sh.append((lat, lon))
                    else:
                        new_lm.append((lat, lon, char, lbl))
                except (KeyError, ValueError):
                    continue
    except OSError:
        return
    _extra_landmarks = new_lm
    _extra_shoreline = new_sh


# ── Projection helpers (module-level, use sender/default center) ─────────────


def get_dist(lat, lon, c_lat=CENTER_LAT, c_lon=CENTER_LON):
    cos_lat = math.cos(math.radians(c_lat))
    dx = (lon - c_lon) * cos_lat * 60
    dy = (lat - c_lat) * 60
    return math.sqrt(dx * dx + dy * dy)


def bearing_to(lat, lon, c_lat=CENTER_LAT, c_lon=CENTER_LON):
    cos_lat = math.cos(math.radians(c_lat))
    dlat = lat - c_lat
    dlon = (lon - c_lon) * cos_lat
    return math.degrees(math.atan2(dlon, dlat)) % 360


def get_arrow(track):
    """Directional arrow glyph for a given track (degrees). '?' for unknown."""
    if track is None:
        return "?"
    return ARROWS[round(track / 45) % 8]


def vrate_symbol(vr):
    if vr is None or abs(vr) < 200:
        return "→"
    return "↑" if vr > 0 else "↓"


# ── Dynamic ring radii ───────────────────────────────────────────────────────


def ring_radii(zoom_range):
    """Three sensible range-ring distances for the given zoom range (nm)."""
    if zoom_range <= 5:
        return [1, 2, 5]
    elif zoom_range <= 8:
        return [2, 5, 8]
    elif zoom_range <= 10:
        return [2, 5, 10]
    elif zoom_range <= 15:
        return [5, 10, 15]
    elif zoom_range <= 20:
        return [5, 10, 20]
    elif zoom_range <= 25:
        return [5, 10, 25]
    elif zoom_range <= 35:
        return [10, 20, 35]
    elif zoom_range <= 50:
        return [10, 25, 50]
    elif zoom_range <= 75:
        return [25, 50, 75]
    else:
        return [25, 50, 100]


# ── Scope drawing ────────────────────────────────────────────────────────────


def draw_scope(
    gw=GW,
    gh=GH,
    zoom_range=MAX_RANGE,
    center_lat=CENTER_LAT,
    center_lon=CENTER_LON,
    sender_centers=None,
):
    """
    Return (grid, band) arrays for a gwxgh radar scope background.

    grid[y][x] — character to display
    band[y][x] — sentinel values:
        -1  background chrome (rings, spokes)
        -2  landmark (airport, city, shoreline)
        -3  sender coverage boundary ring
        0-14  aircraft altitude band (set by plot_targets)

    center_lat/center_lon control the geographic center of the view.
    sender_centers is a list of (lat, lon, range_nm) tuples; one dashed
    boundary ring is drawn per entry when the view is panned away from it.
    """
    cx = gw // 2
    cy = gh // 2
    scale_x = zoom_range / cx
    scale_y = zoom_range / cy
    cos_lat = math.cos(math.radians(center_lat))

    grid = [[" "] * gw for _ in range(gh)]
    band = [[-1] * gw for _ in range(gh)]

    def _to_grid_pt(lat, lon):
        dx = (lon - center_lon) * cos_lat * 60
        dy = (lat - center_lat) * 60
        return (int(round(cx + dx / scale_x)), int(round(cy - dy / scale_y)))

    # ── Range rings ────────────────────────────────────────────────────────
    for radius in ring_radii(zoom_range):
        rx = radius / scale_x
        ry = radius / scale_y
        h_ = ((rx - ry) ** 2) / ((rx + ry) ** 2)
        peri = math.pi * (rx + ry) * (1 + 3 * h_ / (10 + math.sqrt(4 - 3 * h_)))
        pts = max(128, int(peri * 3))
        seen = set()
        for i in range(pts):
            angle = 2 * math.pi * i / pts
            gx_ = int(round(cx + rx * math.cos(angle)))
            gy_ = int(round(cy - ry * math.sin(angle)))
            if (gx_, gy_) in seen:
                continue
            seen.add((gx_, gy_))
            if 0 <= gx_ < gw and 0 <= gy_ < gh and grid[gy_][gx_] == " ":
                grid[gy_][gx_] = "·"

    # Range ring labels on the east side
    for radius in ring_radii(zoom_range):
        label = str(radius)
        rx_ = int(round(radius / scale_x))
        lx = cx + rx_ + 1
        for j, ch in enumerate(label):
            if 0 <= lx + j < gw:
                grid[cy][lx + j] = ch

    # ── Cardinal spokes ────────────────────────────────────────────────────
    for i in range(1, cy):
        if grid[cy - i][cx] == " ":
            grid[cy - i][cx] = "┊" if i % 4 == 0 else " "
    for i in range(1, gh - cy):
        if 0 <= cy + i < gh and grid[cy + i][cx] == " ":
            grid[cy + i][cx] = "┊" if i % 4 == 0 else " "
    for i in range(1, cx):
        if grid[cy][cx - i] == " ":
            grid[cy][cx - i] = "╌" if i % 6 == 0 else " "
    for i in range(1, gw - cx):
        if 0 <= cx + i < gw and grid[cy][cx + i] == " ":
            grid[cy][cx + i] = "╌" if i % 6 == 0 else " "

    # ── Crosshair / compass ────────────────────────────────────────────────
    grid[cy][cx] = "⊕"
    grid[0][cx] = "N"
    grid[gh - 1][cx] = "S"
    grid[cy][0] = "W"
    grid[cy][gw - 1] = "E"

    # ── Sender coverage boundary rings ────────────────────────────────────
    # Draw only when the viewport edge is approaching the sender boundary,
    # i.e. you might be missing aircraft beyond it.  Skip when fully inside
    # (viewport radius << sender range) to avoid cluttering a dense multi-
    # sender cluster like Chicago.
    for s_lat, s_lon, s_range in sender_centers or []:
        dist_from_sender = get_dist(s_lat, s_lon, center_lat, center_lon)
        # Ring shows when viewport edge is within 25% of sender range from the boundary.
        visible = dist_from_sender + zoom_range >= s_range * 0.75
        if visible and dist_from_sender > 1.0:  # suppress when receiver is at sender center
            scx, scy = _to_grid_pt(s_lat, s_lon)
            s_rx = s_range / scale_x
            s_ry = s_range / scale_y
            h_ = ((s_rx - s_ry) ** 2) / ((s_rx + s_ry) ** 2)
            peri = math.pi * (s_rx + s_ry) * (1 + 3 * h_ / (10 + math.sqrt(4 - 3 * h_)))
            pts = max(64, int(peri * 1.5))
            seen = set()
            for i in range(0, pts, 3):  # every 3rd point → dashed look
                angle = 2 * math.pi * i / pts
                gx_ = int(round(scx + s_rx * math.cos(angle)))
                gy_ = int(round(scy - s_ry * math.sin(angle)))
                if (gx_, gy_) in seen:
                    continue
                seen.add((gx_, gy_))
                if 0 <= gx_ < gw and 0 <= gy_ < gh and grid[gy_][gx_] == " ":
                    grid[gy_][gx_] = ":"
                    band[gy_][gx_] = -3

    # ── Build occupied set for landmark label placement ───────────────────
    # Includes ring dots, ring labels, compass chars, spokes, boundary ring.
    occupied_lmk = set()
    for gy_ in range(gh):
        for gx_ in range(gw):
            if grid[gy_][gx_] != " ":
                occupied_lmk.add((gx_, gy_))

    # ── Water features (shorelines) ───────────────────────────────────────
    for shoreline in (LAKE_SHORELINE, LAKE_SUPERIOR_SHORELINE, _extra_shoreline):
        for lat, lon in shoreline:
            gx_, gy_ = _to_grid_pt(lat, lon)
            if 0 <= gx_ < gw and 0 <= gy_ < gh and (gx_, gy_) not in occupied_lmk:
                grid[gy_][gx_] = "~"
                band[gy_][gx_] = -2
                occupied_lmk.add((gx_, gy_))

    # ── Airport / city landmarks ──────────────────────────────────────────
    # _try_place_label is defined later in this module; Python resolves it at
    # call time, so the forward reference is fine.
    for lat, lon, char, label in LANDMARKS + _extra_landmarks:
        gx_, gy_ = _to_grid_pt(lat, lon)
        if not (0 <= gx_ < gw and 0 <= gy_ < gh):
            continue
        grid[gy_][gx_] = char
        band[gy_][gx_] = -2
        occupied_lmk.add((gx_, gy_))
        _try_place_label(grid, band, occupied_lmk, gx_, gy_, label, -2, gw, gh)

    return grid, band


# ── Label placement ──────────────────────────────────────────────────────────


def _try_place_label(grid, band, occupied, gx, gy, label, b, gw, gh):
    """
    Attempt to place label near marker at (gx, gy).
    Tries 12 candidate positions, then shorter label lengths on failure.
    Returns True if placed.
    """

    def _candidates(llen):
        return [
            (gx + 1, gy),
            (gx - llen - 1, gy),
            (gx + 1, gy - 1),
            (gx + 1, gy + 1),
            (gx - llen - 1, gy - 1),
            (gx - llen - 1, gy + 1),
            (gx - llen // 2, gy - 1),
            (gx - llen // 2, gy + 1),
            (gx + 2, gy - 1),
            (gx + 2, gy + 1),
            (gx - llen // 2, gy - 2),
            (gx - llen // 2, gy + 2),
        ]

    def _fits(sx, sy, llen):
        if not (0 <= sy < gh):
            return False
        return all(0 <= sx + j < gw and (sx + j, sy) not in occupied for j in range(llen))

    def _place(sx, sy, txt):
        for j, ch in enumerate(txt):
            grid[sy][sx + j] = ch
            band[sy][sx + j] = b
            occupied.add((sx + j, sy))

    for try_len in sorted({len(label), min(4, len(label)), min(3, len(label))}, reverse=True):
        if try_len < 1:
            continue
        txt = label[:try_len]
        for sx, sy in _candidates(len(txt)):
            if _fits(sx, sy, len(txt)):
                _place(sx, sy, txt)
                return True
    return False


# ── Target plotting ──────────────────────────────────────────────────────────


def plot_targets(
    grid,
    band,
    aircraft,
    gw=GW,
    gh=GH,
    zoom_range=MAX_RANGE,
    center_lat=CENTER_LAT,
    center_lon=CENTER_LON,
):
    """
    Plot aircraft onto the grid/band arrays.

    Uses center_lat/center_lon as the geographic center for projection and
    distance filtering — pass the receiver's current pan position here.

    Returns list of target dicts (closest first), each including 'gx'/'gy'.
    """
    cx = gw // 2
    cy = gh // 2
    scale_x = zoom_range / cx
    scale_y = zoom_range / cy
    cos_lat = math.cos(math.radians(center_lat))

    def _to_grid(lat, lon):
        dx = (lon - center_lon) * cos_lat * 60
        dy = (lat - center_lat) * 60
        return (int(round(cx + dx / scale_x)), int(round(cy - dy / scale_y)))

    def _dist(lat, lon):
        dx = (lon - center_lon) * cos_lat * 60
        dy = (lat - center_lat) * 60
        return math.sqrt(dx * dx + dy * dy)

    def _bearing(lat, lon):
        dlat = lat - center_lat
        dlon = (lon - center_lon) * cos_lat
        return math.degrees(math.atan2(dlon, dlat)) % 360

    targets = []
    occupied = set()

    # Pre-mark non-empty background cells (includes landmarks)
    for gy_ in range(gh):
        for gx_ in range(gw):
            if grid[gy_][gx_] != " ":
                occupied.add((gx_, gy_))

    # Filter: positioned, airborne, recent, within zoom_range
    plotable = []
    for ac in aircraft or []:
        lat, lon = ac.get("lat"), ac.get("lon")
        if lat is None or lon is None:
            continue
        if ac.get("seen_pos", 0) > 60:
            continue
        alt = ac.get("alt_baro")
        if alt == "ground":
            continue
        dist = _dist(lat, lon)
        if dist > zoom_range:
            continue
        plotable.append((dist, ac))
    plotable.sort(key=lambda x: x[0])

    for dist, ac in plotable:
        lat, lon = ac["lat"], ac["lon"]
        gx_, gy_ = _to_grid(lat, lon)

        if not (1 <= gx_ < gw - 1 and 1 <= gy_ < gh - 1):
            continue

        track = ac.get("track")
        flight = (ac.get("flight") or "").strip()
        alt = ac.get("alt_baro")
        gs = ac.get("gs")
        vr = ac.get("baro_rate") if ac.get("baro_rate") is not None else ac.get("geom_rate")
        icao = ac.get("hex", "").upper()
        brg = _bearing(lat, lon)
        reg = ac.get("reg", "")
        is_alert = bool(ac.get("alert"))
        hl = ac.get("highlight")

        alt_val = alt if isinstance(alt, (int, float)) else None
        b = alt_band(alt_val)

        t = {
            "call": flight or reg or icao,
            "flight": flight,
            "icao": icao,
            "reg": reg,
            "alt": alt_val,
            "gs": gs,
            "dist": round(dist, 1),
            "trk": track,
            "brg": round(brg),
            "vr": vr,
            "lat": lat,
            "lon": lon,
            "sqk": ac.get("squawk"),
            "band": b,
            "alert": is_alert,
            "highlight": hl,
            "gs_ovf": bool(ac.get("gs_ovf")),
            "vr_ovf": bool(ac.get("vr_ovf")),
            "emrg": bool(ac.get("emrg")),
            "gx": gx_,
            "gy": gy_,
        }

        # Don't stack markers on the same cell.
        # Also skip if a landmark icon is there — overwriting just the icon
        # while leaving its label chars produces orphaned text artifacts.
        b_cell = band[gy_][gx_]
        if (gx_, gy_) in occupied and (b_cell >= 0 or b_cell == -2):
            targets.append(t)
            continue

        marker = get_arrow(track)
        grid[gy_][gx_] = marker
        band[gy_][gx_] = b
        occupied.add((gx_, gy_))

        label = (flight or reg or icao[:6])[:CALL_MAX]
        if label:
            _try_place_label(grid, band, occupied, gx_, gy_, label, b, gw, gh)

        targets.append(t)

    return targets


# ── Encode ──────────────────────────────────────────────────────────────────


def _filter_aircraft(
    aircraft_list, center_lat=CENTER_LAT, center_lon=CENTER_LON, range_nm=MAX_RANGE
):
    """Filter aircraft to those airborne, positioned, recent, within range_nm."""
    result = []
    for ac in aircraft_list or []:
        lat, lon = ac.get("lat"), ac.get("lon")
        if lat is None or lon is None:
            continue
        if ac.get("seen_pos", 0) > 60:
            continue
        if ac.get("alt_baro") == "ground":
            continue
        dist = get_dist(lat, lon, center_lat, center_lon)
        if dist > range_nm:
            continue
        result.append((dist, ac))
    result.sort(key=lambda x: x[0])
    return [ac for _, ac in result[:MAX_AC]]


def encode_frame(aircraft_list, center_lat=CENTER_LAT, center_lon=CENTER_LON, range_nm=MAX_RANGE):
    """
    Encode a list of raw aircraft dicts into a compact binary v3 frame.
    Filters to airborne, positioned, within range_nm of center_lat/center_lon.
    Returns bytes.
    """
    filtered = _filter_aircraft(aircraft_list, center_lat, center_lon, range_nm)
    ts = int(time.time())
    clat_i = max(-32768, min(32767, int(round(center_lat * 200))))
    clon_i = max(-32768, min(32767, int(round(center_lon * 200))))
    parts = [struct.pack(HEADER_FMT_V2, MAGIC, VERSION, ts, len(filtered), clat_i, clon_i)]

    for ac in filtered:
        lat = ac["lat"]
        lon = ac["lon"]
        alt = ac.get("alt_baro")
        trk = ac.get("track")
        gs = ac.get("gs")
        vr = ac.get("baro_rate") if ac.get("baro_rate") is not None else ac.get("geom_rate")
        call = (ac.get("flight") or "").strip()[:CALL_MAX]
        icao_hex = ac.get("hex", "000000").lower().zfill(6)

        flags = 0

        try:
            icao_bytes = bytes.fromhex(icao_hex[:6])
        except ValueError:
            icao_bytes = b"\x00\x00\x00"

        lat_i = max(-32768, min(32767, int(round(lat * 200))))
        lon_i = max(-32768, min(32767, int(round(lon * 200))))

        if isinstance(alt, (int, float)):
            # v3: store altitude in 10-ft units → covers ±327,670 ft (FL000-FL650)
            alt_i = max(-32768, min(32767, round(alt / 10)))
            flags |= FL_ALT
        else:
            alt_i = 0

        if trk is not None:
            trk_i = int(trk) // 2
            flags |= FL_TRACK
        else:
            trk_i = TRACK_NONE

        if gs is not None:
            gs_raw = int(gs)
            if gs_raw >= 510:
                flags |= FL_GS_OVF  # signal receiver that value was clipped
            gs_i = min(254, gs_raw // 2)
            flags |= FL_GS
        else:
            gs_i = GS_NONE

        if vr is not None:
            vr_clamped = max(-127, min(127, int(vr) // 64))
            if abs(int(vr)) > 8128:
                flags |= FL_VR_OVF
            vr_i = vr_clamped
            flags |= FL_VRATE
        else:
            vr_i = VRATE_NONE

        sqk = str(ac.get("squawk") or "")
        if sqk in ("7700", "7500", "7600"):
            flags |= FL_EMRG

        if call:
            flags |= FL_CALLSIGN

        parts.append(struct.pack(AC_FMT, icao_bytes, lat_i, lon_i, alt_i, trk_i, gs_i, vr_i, flags))
        if call:
            cb = call.encode("ascii", errors="replace")
            parts.append(struct.pack("B", len(cb)) + cb)

    return b"".join(parts)


# ── Decode ──────────────────────────────────────────────────────────────────


def decode_frame(data):
    """
    Decode a binary frame (v1, v2, or v3).
    Returns {'ts': int, 'aircraft': [...], 'sender_center': (lat, lon)}.
    Raises ValueError on bad magic or unsupported version.

    v3 vs v2: altitude stored in 10-ft units in v3 (covers FL000-FL650).
              v2 stores raw feet (clipped at FL327).
    """
    if len(data) < HEADER_SZ_V1:
        raise ValueError("Frame too short")

    magic, version, ts, count = struct.unpack_from(HEADER_FMT_V1, data, 0)
    if magic != MAGIC:
        raise ValueError(f"Bad magic: {magic!r}")

    if version in (2, 3):
        if len(data) < HEADER_SZ_V2:
            raise ValueError(f"v{version} frame too short for header")
        _, _, _, _, clat_i, clon_i = struct.unpack_from(HEADER_FMT_V2, data, 0)
        sender_center = (clat_i / 200.0, clon_i / 200.0)
        offset = HEADER_SZ_V2
    elif version == 1:
        sender_center = (CENTER_LAT, CENTER_LON)
        offset = HEADER_SZ_V1
    else:
        raise ValueError(f"Unsupported version: {version}")

    aircraft = []

    for _ in range(count):
        if offset + AC_SZ > len(data):
            break
        icao_bytes, lat_i, lon_i, alt_i, trk_i, gs_i, vr_i, flags = struct.unpack_from(
            AC_FMT, data, offset
        )
        offset += AC_SZ

        icao = icao_bytes.hex().upper()
        lat = lat_i / 200.0
        lon = lon_i / 200.0
        # v3: altitude in 10-ft units → multiply back.  v1/v2: raw feet.
        if flags & FL_ALT:
            alt = (alt_i * 10) if version == 3 else alt_i
        else:
            alt = None
        trk = trk_i * 2 if flags & FL_TRACK else None
        gs = gs_i * 2 if flags & FL_GS else None
        vr = vr_i * 64 if flags & FL_VRATE else None

        gs_ovf = bool(flags & FL_GS_OVF)
        vr_ovf = bool(flags & FL_VR_OVF)
        emrg = bool(flags & FL_EMRG)

        call = ""
        if flags & FL_CALLSIGN:
            if offset < len(data):
                clen = data[offset]
                offset += 1
                clen = min(clen, len(data) - offset)
                call = data[offset : offset + clen].decode("ascii", errors="replace")
                offset += clen

        s_lat, s_lon = sender_center
        dist = get_dist(lat, lon, s_lat, s_lon)
        brg = bearing_to(lat, lon, s_lat, s_lon)

        aircraft.append(
            {
                "icao": icao,
                "hex": icao.lower(),
                "lat": lat,
                "lon": lon,
                "alt_baro": alt,
                "track": trk,
                "gs": gs,
                "baro_rate": vr,
                "flight": call,
                "call": call or icao,
                "sender_dist": round(dist, 1),
                "sender_brg": round(brg),
                "flags": flags,
                "gs_ovf": gs_ovf,
                "vr_ovf": vr_ovf,
                "emrg": emrg,
            }
        )

    return {"ts": ts, "aircraft": aircraft, "sender_center": sender_center}


# ── Self-test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import datetime
    import json
    import sys
    from urllib.request import urlopen

    _DEFAULT_URL = "http://localhost:8080/data/aircraft.json"
    _AIRCRAFT_URL = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_URL
    print(f"Fetching aircraft.json from {_AIRCRAFT_URL} …")
    resp = urlopen(_AIRCRAFT_URL, timeout=8)
    raw = json.loads(resp.read()).get("aircraft", [])
    print(f"  {len(raw)} total aircraft from feed")

    frame = encode_frame(raw)
    print(f"  Encoded {len(_filter_aircraft(raw))} aircraft → {len(frame)} bytes (v3)")

    result = decode_frame(frame)
    ts_str = datetime.datetime.fromtimestamp(result["ts"], tz=datetime.timezone.utc).strftime(
        "%H:%M:%S"
    )
    sc = result["sender_center"]
    print(
        f"  Decoded: {len(result['aircraft'])} aircraft, ts={ts_str} UTC, "
        f"sender_center=({sc[0]:.3f},{sc[1]:.3f})"
    )
    print()

    # View-request round-trip test
    vr_bytes = encode_view_request(41.95, -87.70, 15)
    vr_dec = decode_view_request(vr_bytes)
    print(f"View-request round-trip: {vr_dec}")

    # Announce app_data round-trip test
    ann_bytes = encode_announce_data(41.97, -87.91, 25, "adsb-ord")
    ann_dec = decode_announce_data(ann_bytes)
    print(f"Announce round-trip:     {ann_dec}")
    print()

    for ac in result["aircraft"]:
        alt_v = ac["alt_baro"]
        b = alt_band(alt_v)
        alt_s = f"FL{alt_v // 100:03d}" if alt_v is not None else "---"
        arr = get_arrow(ac["track"])
        print(
            f"  {arr} {ac['call']:<9s}  {alt_s}  band={b:2d}  "
            f"{ac['sender_dist']:.1f}nm  gs={ac['gs']}  vr={ac['baro_rate']}"
        )

    print()
    print("Scope render test (including landmarks):")
    for zoom in [5, 10, 25]:
        gw, gh = 100, 50
        g, bnd = draw_scope(gw, gh, zoom)
        tgts = plot_targets(g, bnd, result["aircraft"], gw, gh, zoom)
        # Count cells occupied by landmark icons and labels
        n_lmk = sum(1 for row in bnd for v in row if v == -2)
        print(f"  zoom={zoom}nm  {gw}x{gh}  targets={len(tgts)}  landmark_cells={n_lmk}")

    print()
    print("Pan test (center shifted north 10nm):")
    pan_lat = 10 / 60.0
    gw, gh = 100, 50
    g, bnd = draw_scope(
        gw,
        gh,
        25,
        CENTER_LAT + pan_lat,
        CENTER_LON,
        sender_centers=[(CENTER_LAT, CENTER_LON, MAX_RANGE)],
    )
    n_bdy = sum(1 for row in bnd for v in row if v == -3)
    tgts = plot_targets(g, bnd, result["aircraft"], gw, gh, 25, CENTER_LAT + pan_lat, CENTER_LON)
    print(f"  boundary_cells={n_bdy}  targets_in_view={len(tgts)}")
