# adsb-radar

ADS-B radar shared over a [Reticulum](https://reticulum.network) mesh. Senders read aircraft from a local dump1090 or a public API and broadcast filtered frames over encrypted RNS links. Receivers display a merged terminal radar scope and discover senders automatically via announce.

```
  dump1090 / adsb.im                    laptop / Pi / desktop
  ┌─────────────────────┐               ┌──────────────────────┐
  │  adsb-sender        │               │  adsb-receiver       │
  │  announces coverage │──── RNS ─────▶│  discovers senders   │
  │  filters per-link   │               │  merges aircraft DB  │
  └─────────────────────┘               └──────────────────────┘
```

Works over LoRa (RNode), TCP, WiFi AutoInterface, I2P, or any Reticulum transport.

---

## Install

```bash
git clone https://github.com/alabbott/adsb-radar
cd adsb-radar
python3 -m venv .venv
.venv/bin/pip install -e .
```

Everything goes into `.venv/` in the project directory. `setup.sh` runs the same two pip commands if you prefer a script. Entry points: `.venv/bin/adsb-receiver`, `.venv/bin/adsb-sender`.

Activate once per shell session to drop the path prefix:

```bash
source .venv/bin/activate
```

---

## Receiver

```bash
adsb-receiver --home-lat 41.88 --home-lon -87.63
```

The receiver discovers senders via RNS announce and connects to those within range of `--home-lat/lon`. Known senders are cached in `src/adsb_radar/receiver.conf` and reconnected on next startup without waiting for announces.

You need at least one Reticulum interface configured — see [Reticulum](#reticulum).

### Options

| Option | Default | |
|--------|---------|--|
| `--home-lat`, `--home-lon` | 41.88, -87.63 | Geographic gating origin |
| `--home-range NM` | 150 | Senders beyond `dist(home, sender) > home_range + sender_range` start *out of range* |
| `--dest HASH` | — | Connect directly by 32-char hex hash (repeatable; bypasses gating) |
| `--config PATH` | package dir | Known/blocked sender config — created automatically |
| `--block HASH` | — | Block a sender at startup (repeatable) |
| `--map PATH` | — | Extra landmarks CSV overlaid on the scope — see [Landmarks](#extra-landmarks---map) |
| `--alert-db PATH\|auto\|none` | auto | [plane-alert-db](https://github.com/sdr-enthusiasts/plane-alert-db) CSV for mil/gov/LEA highlighting |
| `--log-file PATH` | — | Append log output to file (useful when running under tmux or screen) |

### Keys

| Key | Action |
|-----|--------|
| `+` / `=` | Zoom in |
| `-` | Zoom out |
| `w` `a` `s` `d` | Pan N / W / S / E |
| `c` / `Home` | Reset pan to home position |
| `↑` `↓` | Select aircraft in list |
| `PgUp` `PgDn` | Scroll aircraft list; scroll sources list when on sources page |
| `Tab` | Toggle scope ↔ sources page |
| `Space` | *(sources)* Enable / disable selected sender |
| `b` | *(sources)* Block / unblock selected sender |
| `r` | Reconnect all enabled disconnected senders |
| `q` / `Esc` | Quit |

### Scope

Aircraft are plotted by lat/lon and color-coded by altitude: purple (≥45 000 ft) → blue → green → yellow → orange (< 1 000 ft). Direction arrows show current heading. Emergency squawks (7700/7500/7600) are shown in white on red and blink. Alert-DB matches are colored by category: military (red), government (cyan), LEA (magenta).

Range rings and a compass overlay are always visible. Coverage rings for each sender appear briefly when panning or zooming.

The right-hand column shows callsign, altitude, speed, vertical rate, bearing, track, distance, squawk, and registration. Alert-DB enrichment (operator, type, notes) appends to the detail bar at the bottom when an aircraft is selected.

### Sources page (`Tab`)

```
  #  En  Name          Status        Center               Range    Dist   Last RX   Bw/s  Aircraft
  1   ✓  adsb-chi      linked        +41.880,-087.630    100nm      0nm      2s    1.2k       112
  2   ✓  adsb-wi       linked        +44.500,-089.500    100nm    175nm      3s    820b        74
  3   ✓  adsb-mi       linked        +43.800,-084.800    100nm    205nm      4s    950b        88
  4   ✗  adsb-msp      out of range  +44.880,-093.220     80nm    342nm      —      —           0

  11 sources  9 linked  RX 8.4k/s  347 unique AC

  Dest hash: a1b2c3d4 e5f60718 9abcdef0 12345678   ↑↓ nav   Space toggle   b block/unblock
```

| Status | Meaning |
|--------|---------|
| linked | Connected and receiving frames |
| out of range | Too far from home; no link formed, no bandwidth used |
| disabled | Manually disabled with `Space`; not auto-reconnected |
| blocked | Permanently blocked; saved to `receiver.conf` and ignored after restart |
| searching… / linking… | Connection in progress |
| disconnected | Link dropped; will auto-reconnect in 5 s |

**`Space`** — toggle enable/disable on the selected sender. Disabled senders are not auto-reconnected even when panning into range.

**`b`** — block or unblock. Blocked senders are written to `receiver.conf`. Press `b` again to unblock.

**`↑`/`↓`**, **`PgUp`/`PgDn`** — navigate and scroll. The list scrolls independently of the scope.

Panning the scope toward an out-of-range sender's coverage area will auto-enable it. Panning away does not disconnect it — use `Space` to manually disable.

---

## Sender

Reads aircraft from a JSON endpoint every 5 seconds, announces geographic coverage on the mesh, and streams filtered frames to each connected receiver based on their current viewport.

### Data source

**Option 1 — Local ADS-B receiver (adsb.im, dump1090, tar1090)**

Most adsb.im and dump1090 installations serve aircraft at `http://localhost:8080/data/aircraft.json`, which is the default `--url`. On a Pi already running adsb.im, no `--url` flag is needed:

```bash
adsb-sender --lat 41.88 --lon -87.63 --range 100 --name my-sender
```

**Option 2 — Public adsb.lol API (no local receiver)**

```bash
adsb-sender \
    --url   "https://api.adsb.lol/v2/point/41.88/-87.63/100" \
    --lat   41.88 --lon -87.63 --range 100 --name my-sender
```

### Options

| Option | Default | |
|--------|---------|--|
| `--url URL` | `http://localhost:8080/data/aircraft.json` | Aircraft JSON endpoint |
| `--lat`, `--lon` | 41.88, -87.63 | Sender center — broadcast in announces, used for geographic gating |
| `--range NM` | 25 | Coverage radius advertised to receivers |
| `--name NAME` | `adsb-radar` | Label shown in receiver sources page — should be unique per sender |
| `--interval S` | 5 | Seconds between data fetches and frame transmissions |
| `--identity PATH` | package dir | Persistent RNS keypair file. The destination hash receivers connect to is derived from this key — keep it across reinstalls |
| `--api-key KEY` | — | Sent as `api-auth` header. Also accepted via `ADSB_API_KEY` env var |

### Supported endpoints

Any URL returning `{"aircraft": [...]}` (readsb/dump1090 format) or `{"ac": [...]}` (adsb.fi/adsb.lol format) works without configuration.

| Source | URL | Auth |
|--------|-----|------|
| dump1090 / adsb.im / tar1090 | `http://localhost:8080/data/aircraft.json` | — |
| adsb.lol public API | `https://api.adsb.lol/v2/point/LAT/LON/NM` | — |
| adsb.lol feeder API | `https://re-api.adsb.lol?circle=LAT,LON,NM` | feeder IP |
| adsb.exchange | `https://adsbexchange.com/api/aircraft/v2/lat/LAT/lon/LON/dist/NM/` | `--api-key` |

`re-api.adsb.lol` and `api.adsb.lol` serve the same underlying data; the feeder passthrough includes additional fields (`seen_pos`) and is IP-restricted to active adsb.lol feeders.

### Keeping the sender running

**Option A — tmux**

```bash
tmux new-session -d -s adsb-sender \
    "$HOME/adsb-radar/.venv/bin/adsb-sender --lat 41.88 --lon -87.63 --name my-sender"

tmux attach -t adsb-sender   # view output
# Ctrl-b d                   # detach without stopping
```

**Option B — screen**

```bash
screen -dmS adsb-sender \
    $HOME/adsb-radar/.venv/bin/adsb-sender --lat 41.88 --lon -87.63 --name my-sender

screen -r adsb-sender         # attach
# Ctrl-a d                    # detach without stopping
```

**Option C — systemd (starts on boot, restarts on failure)**

All output goes to the journal: `journalctl -u adsb_sender -f`

Create the service file, editing `--lat`, `--lon`, `--range`, and `--name`:

```bash
cat > ~/adsb-radar/adsb_sender.service <<EOF
[Unit]
Description=ADS-B Reticulum sender
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$HOME/adsb-radar
ExecStart=$HOME/adsb-radar/.venv/bin/adsb-sender \
    --url   http://localhost:8080/data/aircraft.json \
    --name  adsb-$(hostname -s) \
    --lat   YOUR_LAT \
    --lon   YOUR_LON \
    --range 35
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
```

Install and start:

```bash
sudo cp ~/adsb-radar/adsb_sender.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now adsb_sender
systemctl status adsb_sender
```

Remove: `sudo systemctl disable --now adsb_sender && sudo rm /etc/systemd/system/adsb_sender.service`

`setup-sender.sh` generates this service file pre-filled with the current user and directory.

---

## Docker cluster

`docker-compose.yml` runs 19 senders covering the Great Lakes basin plus a shared Reticulum transport node. Senders pull from the public adsb.lol API — no feeder account or local ADS-B hardware needed.

**Local simulation** — senders and receiver are isolated to the Docker bridge network; no traffic leaves the machine. No `.env` needed.

**Live mesh** — set `RETICULUM_HOST` in `.env` to uplink the cluster to a Reticulum TCP hub.

### Running the cluster

Start all 19 senders and the transport:

```bash
docker compose up -d transport \
  sender-chicago sender-illinois-w sender-wisconsin sender-michigan \
  sender-indiana sender-ohio sender-cincinnati \
  sender-minnesota sender-iowa sender-missouri sender-michigan-up \
  sender-detroit sender-northern-michigan sender-sault \
  sender-lake-superior sender-duluth \
  sender-pittsburgh sender-buffalo sender-rochester
```

Run the interactive receiver (requires a TTY):

```bash
docker compose run --rm -it receiver
```

Bridge to the live mesh ([Chicagoland Reticulum Network](https://reticulum.world/)) (CRN):

```bash
cp .env.example .env
# RETICULUM_HOST=rns.noderage.org is pre-filled in .env.example
docker compose up -d transport
```

The transport container runs `rnsd` with a `TCPServerInterface` on port 7777 (internal) and, when `RETICULUM_HOST` is set, a `TCPClientInterface` uplink to your hub.

### Coverage

| Sender | Center | Radius | Area |
|--------|--------|--------|------|
| `sender-chicago` | 41.88, -87.63 | 100 nm | Chicago, Milwaukee, Rockford, Gary, South Bend |
| `sender-illinois-w` | 41.00, -89.50 | 75 nm | Peoria, Springfield, Quad Cities, Champaign |
| `sender-wisconsin` | 44.50, -89.50 | 100 nm | Entire Wisconsin |
| `sender-michigan` | 43.80, -84.80 | 100 nm | Central lower Michigan, Grand Rapids, Flint, Saginaw |
| `sender-indiana` | 39.80, -86.20 | 75 nm | Indianapolis, Fort Wayne, Evansville |
| `sender-ohio` | 40.50, -83.00 | 80 nm | Columbus, Cleveland, Toledo, Akron |
| `sender-cincinnati` | 39.10, -84.50 | 75 nm | Cincinnati, Dayton, Lexington |
| `sender-minnesota` | 44.88, -93.22 | 80 nm | Minneapolis, Rochester MN |
| `sender-iowa` | 41.60, -93.60 | 75 nm | Des Moines, Cedar Rapids |
| `sender-missouri` | 38.75, -90.37 | 75 nm | St. Louis, S Illinois |
| `sender-michigan-up` | 46.50, -87.00 | 75 nm | Central Upper Peninsula Michigan |
| `sender-detroit` | 42.50, -83.20 | 100 nm | Detroit, Ann Arbor, Windsor ON, Toledo, Lake Erie west |
| `sender-northern-michigan` | 44.80, -84.70 | 80 nm | Traverse City, Petoskey, Alpena, Mackinac Strait |
| `sender-sault` | 46.50, -84.00 | 80 nm | Sault Ste Marie, eastern UP, Lake Superior east shore |
| `sender-lake-superior` | 46.60, -89.00 | 75 nm | Ironwood, Ashland WI, Keweenaw Peninsula |
| `sender-duluth` | 46.80, -91.50 | 80 nm | Duluth MN, Superior WI, Iron Range, western Lake Superior |
| `sender-pittsburgh` | 41.50, -80.00 | 100 nm | Pittsburgh, Erie PA, Lake Erie corridor, Youngstown |
| `sender-buffalo` | 43.00, -78.50 | 80 nm | Buffalo, Niagara Falls, Lake Erie east, Lake Ontario west |
| `sender-rochester` | 43.15, -76.50 | 75 nm | Rochester NY, Syracuse NY, Lake Ontario south shore |

Each sender has a named Docker volume for its identity so its destination hash stays stable across container restarts.

---

## Reticulum

adsb-sender and adsb-receiver each start an embedded Reticulum transport — no separate `rnsd` process is needed. On first run, Reticulum creates `~/.reticulum/config`. Add at least one interface:

**TCP to the [Chicagoland Reticulum Network](https://reticulum.world/)** (CRN — public hub, works over the internet):

```ini
[[CRN]]
  type              = TCPClientInterface
  interface_enabled = True
  target_host       = rns.noderage.org
  target_port       = 4242
```

Community backup servers (any one works): `rns.chicagonomad.net:4242`, `rns.faultline.dev:4242`, `rns.indixo.dev:4242`, `rns.rishipanthee.com:4242`

**I2P** (optional, for anonymity or where TCP is blocked):

```ini
[[CRN I2P]]
  type              = I2PInterface
  interface_enabled = True
  peers             = hm2arylcoexb5h3y6kbgy776dfsqkl4tzb72foly2emdldhjbtq.b32.i2p
```

This connects to the CRN. The simulation senders in this project's Docker cluster announce on the CRN, so pointing a receiver here will show them immediately without running any local infrastructure.

**LoRa via RNode** (`/dev/ttyUSB0`):

```ini
[[RNode LoRa]]
  type              = RNodeInterface
  interface_enabled = True
  port              = /dev/ttyUSB0
  frequency         = 914875000
  bandwidth         = 125000
  txpower           = 17
  sf                = 8
  cr                = 5
```

If your RNode bridges LoRa to IP (i.e. also connects to the CRN via TCP), set `mode = boundary` (or `mode = ap` for a standalone access point) to prevent retransmitting IP-originated packets onto the LoRa spectrum.

**Local WiFi / LAN** (zero config, multicast):

```ini
[[AutoInterface]]
  type              = AutoInterface
  interface_enabled = True
```

Multiple interfaces can coexist in one config file and Reticulum routes across them. A Pi might have both a LoRa interface and a TCP uplink; receivers elsewhere reach senders via whichever path exists.

### Optional: rnsd as an always-on routing node

Running `rnsd` independently keeps a routing instance active even when adsb-radar is not running (useful for path accumulation), and lets multiple Reticulum applications on the same machine share one transport stack.

**Manually:**

```bash
.venv/bin/rnsd
```

**Via systemd:**

```bash
cat > /tmp/rnsd.service <<EOF
[Unit]
Description=Reticulum Network Stack
After=network.target

[Service]
ExecStart=$HOME/adsb-radar/.venv/bin/rnsd
Restart=on-failure
User=$USER

[Install]
WantedBy=multi-user.target
EOF
sudo cp /tmp/rnsd.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rnsd
```

Remove: `sudo systemctl disable --now rnsd && sudo rm /etc/systemd/system/rnsd.service`

If `rns` is installed system-wide via `pipx`, use its path instead:
```bash
ExecStart=$(pipx environment --value PIPX_LOCAL_VENVS)/rns/bin/rnsd
```

---

## Protocol

### Announces

Each sender calls `RNS.Destination.announce()` with an `app_data` payload encoding:

- Sender lat/lon (multiplied by 200, packed as int16)
- Coverage radius in nm (uint16)
- Sender name (up to 32 bytes UTF-8)

Receivers use these to decide whether to connect: `dist(home, sender) ≤ home_range + sender_range`. Senders outside that bound appear as *out of range* with no link formed. Panning the scope viewport to overlap a sender's coverage area triggers an automatic connection.

### Links

Each receiver-to-sender connection is an `RNS.Link` — end-to-end encrypted. Transport nodes in between cannot read the frame content.

### View requests (receiver → sender, 8 bytes)

After link establishment the receiver sends a view-request encoding `center_lat | center_lon | range_nm` as scaled integers. The sender maintains a per-link viewport and filters each outgoing frame to only the aircraft inside that circle. Pan and zoom events on the receiver send a new view-request (debounced to 200 ms of inactivity before sending).

### Frames (sender → receiver)

Binary v3:

```
Header (12 bytes):    magic(2) version(1) timestamp(4) count(1) clat(2) clon(2)
Per aircraft:         ICAO(3) lat(2) lon(2) alt(2) track(1) gs(1) vrate(1) flags(1) [callsign]
```

13 bytes fixed per aircraft plus a variable-length callsign suffix. At 50 aircraft the payload is ~800 bytes, within a LoRa budget at SF8/BW125.

### Multi-sender merge

Merge key is ICAO hex. For each received aircraft: `obs_time = frame_ts − seen_pos`. The database entry is replaced only when the incoming fix is strictly newer. This prevents position jitter when two senders decode the same aircraft from adjacent ADS-B ground stations.

`frame_ts` is clamped to `[now − 5 min, now + 30 s]` before any merge decision, preventing a rogue sender from winning merges by claiming a far-future timestamp.

---

## Extra landmarks (`--map`)

The built-in map covers airports, cities, and lake shorelines across the Midwest. Additional points can be loaded from a CSV:

```csv
# type,char,label,lat,lon
airport,+,YYZ,43.6777,-79.6248
city,@,Toronto,43.6532,-79.3832
water,~,,43.5000,-79.0000
```

| Column | Values | Notes |
|--------|--------|-------|
| `type` | `airport`, `city`, `water`, or any string | `water` rows use the shoreline layer |
| `char` | any single character | Displayed on the scope |
| `label` | short string | Empty = no label, dot only |
| `lat` / `lon` | decimal degrees | N/E positive |

```bash
adsb-receiver --map my-landmarks.csv
```

See `landmarks.csv` for a working example with comments.

---

## Files

| File | |
|------|-|
| `src/adsb_radar/sender.py` | Sender — fetches JSON, announces on RNS, streams filtered frames |
| `src/adsb_radar/receiver.py` | Receiver — discovers senders, merges aircraft DB, curses UI |
| `src/adsb_radar/proto.py` | Frame encode/decode, scope rendering, geographic helpers |
| `src/adsb_radar/alerts.py` | plane-alert-db loader and ICAO lookup |
| `setup.sh` | Creates `.venv/` and installs adsb-radar |
| `setup-sender.sh` | Same as setup.sh; also generates `adsb_sender.service` template |
| `docker-compose.yml` | 19-sender Great Lakes cluster |
| `docker/transport-entrypoint.sh` | Transport container startup |
| `docker/node-reticulum.conf` | Static RNS config for Docker sender/receiver containers |
| `.env.example` | Docker cluster environment template |
| `landmarks.csv` | Midwest airports, cities, lake shorelines |
| `plane-alert-db.csv` | Military/gov/LEA aircraft database |
| `src/adsb_radar/receiver.conf` | Auto-generated known/blocked sender hashes — do not edit by hand |
| `pyproject.toml` | Package metadata |