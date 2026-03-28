"""
Scratch file: hands-on examples of adsb_proto encode/decode.

Run with:  python scratch_encoding.py
"""

import struct
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import adsb_radar.proto as proto

# ── helpers ──────────────────────────────────────────────────────────────────

def hexdump(data, width=16):
    """Print a classic hex dump with ASCII side-panel."""
    for i in range(0, len(data), width):
        chunk = data[i:i+width]
        hex_part = ' '.join(f'{b:02X}' for b in chunk)
        asc_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        print(f'  {i:04X}  {hex_part:<{width*3}}  {asc_part}')

def print_flags(flags):
    """Decode the flags bitmask into readable names."""
    names = {
        proto.FL_ALT:      'ALT',
        proto.FL_TRACK:    'TRACK',
        proto.FL_GS:       'GS',
        proto.FL_VRATE:    'VRATE',
        proto.FL_CALLSIGN: 'CALLSIGN',
        proto.FL_GS_OVF:   'GS_OVF',
        proto.FL_VR_OVF:   'VR_OVF',
        proto.FL_EMRG:     'EMRG',
    }
    active = [name for bit, name in names.items() if flags & bit]
    return f'0x{flags:02X} ({" | ".join(active) or "none"})'

# ── example aircraft dicts (raw feed format) ─────────────────────────────────

# Positioned near Chicago O'Hare so they pass the default range filter
AIRCRAFT = [
    # Typical airliner — all fields present
    {
        'hex':      'a1b2c3',
        'lat':      41.85,
        'lon':      -87.75,
        'alt_baro': 32000,
        'track':    270,
        'gs':       460,
        'baro_rate': -128,
        'flight':   'AAL123',
        'squawk':   '1234',
        'airborne': 1,
    },
    # Minimal — only position and altitude, no callsign
    {
        'hex':      'deadbe',
        'lat':      41.90,
        'lon':      -87.55,
        'alt_baro': 5000,
        'track':    None,
        'gs':       None,
        'baro_rate': None,
        'flight':   '',
        'airborne': 1,
    },
    # Fast business jet — groundspeed overflow flag expected (≥510 kt)
    {
        'hex':      'cafe00',
        'lat':      41.70,
        'lon':      -87.80,
        'alt_baro': 45000,
        'track':    90,
        'gs':       600,        # > 510 → GS_OVF flag set, stored as 254
        'baro_rate': 9999,      # > 8128 → VR_OVF flag set
        'flight':   'BIZJET',
        'airborne': 1,
    },
    # Emergency squawk
    {
        'hex':      'f00ba4',
        'lat':      41.95,
        'lon':      -87.65,
        'alt_baro': 8000,
        'track':    180,
        'gs':       200,
        'baro_rate': -512,
        'flight':   'UAL99',
        'squawk':   '7700',     # → FL_EMRG set
        'airborne': 1,
    },
]

# ─────────────────────────────────────────────────────────────────────────────
# EXAMPLE 1 — Encode a single aircraft and inspect the raw bytes
# ─────────────────────────────────────────────────────────────────────────────

print('=' * 60)
print('EXAMPLE 1: Single aircraft — raw bytes dissected')
print('=' * 60)

frame = proto.encode_frame([AIRCRAFT[0]])
print(f'Total frame size: {len(frame)} bytes')
print()

# Header
magic    = frame[0:2]
version  = frame[2]
ts       = struct.unpack_from('!I', frame, 3)[0]
count    = frame[7]
clat_i, clon_i = struct.unpack_from('!hh', frame, 8)

print('Header (12 bytes):')
print(f'  magic   : {magic.hex().upper()}  ({magic!r})')
print(f'  version : {version}')
print(f'  ts      : {ts}')
print(f'  count   : {count} aircraft')
print(f'  clat_i  : {clat_i}  → {clat_i/200:.4f}°')
print(f'  clon_i  : {clon_i}  → {clon_i/200:.4f}°')
print()

# First aircraft record (13 bytes fixed)
offset = proto.HEADER_SZ_V2
icao_b, lat_i, lon_i, alt_i, trk_i, gs_i, vr_i, flags = \
    struct.unpack_from(proto.AC_FMT, frame, offset)
offset += proto.AC_SZ

print('Aircraft record (13 bytes):')
print(f'  icao    : {icao_b.hex().upper()}  → {icao_b.hex().upper()}')
print(f'  lat_i   : {lat_i}  → {lat_i/200:.4f}°  (orig {AIRCRAFT[0]["lat"]})')
print(f'  lon_i   : {lon_i}  → {lon_i/200:.4f}°  (orig {AIRCRAFT[0]["lon"]})')
print(f'  alt_i   : {alt_i}  → {alt_i*10} ft  (orig {AIRCRAFT[0]["alt_baro"]} ft, stored in 10-ft units)')
print(f'  track_i : {trk_i}  → {trk_i*2}°  (orig {AIRCRAFT[0]["track"]}°, stored as deg//2)')
print(f'  gs_i    : {gs_i}  → {gs_i*2} kt  (orig {AIRCRAFT[0]["gs"]} kt, stored as kt//2)')
print(f'  vrate_i : {vr_i}  → {vr_i*64} fpm  (orig {AIRCRAFT[0]["baro_rate"]} fpm, stored as fpm//64)')
print(f'  flags   : {print_flags(flags)}')

# Callsign suffix
if flags & proto.FL_CALLSIGN:
    clen = frame[offset]; offset += 1
    call = frame[offset:offset+clen].decode('ascii')
    print(f'  callsign: len={clen}  "{call}"')

print()
print('Hex dump:')
hexdump(frame)

# ─────────────────────────────────────────────────────────────────────────────
# EXAMPLE 2 — Encode all four aircraft, decode, compare
# ─────────────────────────────────────────────────────────────────────────────

print()
print('=' * 60)
print('EXAMPLE 2: Four aircraft — encode → decode round-trip')
print('=' * 60)

frame4 = proto.encode_frame(AIRCRAFT)
decoded = proto.decode_frame(frame4)

print(f'Encoded {len(AIRCRAFT)} aircraft → {len(frame4)} bytes')
print(f'Decoded {len(decoded["aircraft"])} aircraft')
print(f'Sender center: {decoded["sender_center"]}')
print()

for ac in decoded['aircraft']:
    print(f'  [{ac["icao"]}] {ac["call"]:<8}  '
          f'lat={ac["lat"]:8.4f}  lon={ac["lon"]:9.4f}  '
          f'alt={str(ac["alt_baro"])+"ft":<10}  '
          f'trk={str(ac["track"])+"°":<6}  '
          f'gs={str(ac["gs"])+"kt":<7}  '
          f'vr={str(ac["baro_rate"])+"fpm":<10}  '
          f'dist={ac["sender_dist"]}nm  '
          f'flags={print_flags(ac["flags"])}'
          + ('  *** EMRG ***' if ac['emrg'] else '')
          + ('  GS clipped' if ac['gs_ovf'] else '')
          + ('  VR clipped' if ac['vr_ovf'] else ''))

# ─────────────────────────────────────────────────────────────────────────────
# EXAMPLE 3 — Quantisation loss: what precision do we actually get?
# ─────────────────────────────────────────────────────────────────────────────

print()
print('=' * 60)
print('EXAMPLE 3: Quantisation error for each field')
print('=' * 60)

orig = AIRCRAFT[0]
dec  = next(a for a in decoded['aircraft'] if a['icao'] == 'A1B2C3')

lat_err  = abs(orig['lat']       - dec['lat'])       * 111_000   # metres
lon_err  = abs(orig['lon']       - dec['lon'])        * 85_000    # metres (≈ at 41°N)
alt_err  = abs(orig['alt_baro']  - dec['alt_baro'])              # feet
trk_err  = abs(orig['track']     - dec['track'])                  # degrees
gs_err   = abs(orig['gs']        - dec['gs'])                     # knots
vr_err   = abs(orig['baro_rate'] - dec['baro_rate'])              # fpm

print(f'  lat  : {orig["lat"]}  → {dec["lat"]}   (error ≈ {lat_err:.1f} m)')
print(f'  lon  : {orig["lon"]} → {dec["lon"]}  (error ≈ {lon_err:.1f} m)')
print(f'  alt  : {orig["alt_baro"]} ft  → {dec["alt_baro"]} ft   (error ≤ {alt_err} ft, step=10 ft)')
print(f'  track: {orig["track"]}°  → {dec["track"]}°       (error ≤ {trk_err}°, step=2°)')
print(f'  gs   : {orig["gs"]} kt  → {dec["gs"]} kt    (error ≤ {gs_err} kt, step=2 kt)')
print(f'  vrate: {orig["baro_rate"]} fpm  → {dec["baro_rate"]} fpm   (error ≤ {vr_err} fpm, step=64 fpm)')

# ─────────────────────────────────────────────────────────────────────────────
# EXAMPLE 4 — Overflow flags
# ─────────────────────────────────────────────────────────────────────────────

print()
print('=' * 60)
print('EXAMPLE 4: Overflow / clipping flags (fast jet)')
print('=' * 60)

jet = next(a for a in decoded['aircraft'] if a['icao'] == 'CAFE00')
print(f'  icao    : {jet["icao"]}')
print(f'  gs      : {jet["gs"]} kt  (gs_ovf={jet["gs_ovf"]} — actual ≥510 kt, stored max=254→508 kt)')
print(f'  vrate   : {jet["baro_rate"]} fpm  (vr_ovf={jet["vr_ovf"]} — actual magnitude >8128 fpm)')
print(f'  flags   : {print_flags(jet["flags"])}')

# ─────────────────────────────────────────────────────────────────────────────
# EXAMPLE 5 — Announce packet (14 bytes, sender → mesh)
# ─────────────────────────────────────────────────────────────────────────────

print()
print('=' * 60)
print('EXAMPLE 5: Announce packet (sender metadata, 14 bytes)')
print('=' * 60)

ann = proto.encode_announce_data(proto.CENTER_LAT, proto.CENTER_LON, 25, 'mynode')
print(f'Encoded: {len(ann)} bytes   {ann.hex().upper()}')
hexdump(ann)

lat_d, lon_d, rng_d, name_d = proto.decode_announce_data(ann)
print(f'Decoded: lat={lat_d}  lon={lon_d}  range={rng_d}nm  name="{name_d}"')

# ─────────────────────────────────────────────────────────────────────────────
# EXAMPLE 6 — View-request packet (8 bytes, receiver → sender)
# ─────────────────────────────────────────────────────────────────────────────

print()
print('=' * 60)
print('EXAMPLE 6: View-request packet (receiver → sender, 8 bytes)')
print('=' * 60)

vreq = proto.encode_view_request(41.88, -87.63, 50)
print(f'Encoded: {len(vreq)} bytes   {vreq.hex().upper()}')
hexdump(vreq)

center_lat, center_lon, range_nm = proto.decode_view_request(vreq)
print(f'Decoded: center=({center_lat}, {center_lon})  range={range_nm}nm')
