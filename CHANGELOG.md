# Changelog

All notable changes to this project will be documented here.

## [0.3.0] — 2026-03-26

### Added
- Docker Compose cluster expanded from 11 to 19 regional senders, covering
  the entire Great Lakes basin with no coverage gaps. New senders fill the
  Detroit corridor (Ohio ↔ Lower MI gap), Straits of Mackinac, eastern UP,
  central Lake Superior south shore, Duluth, Pittsburgh/Lake Erie,
  Buffalo, and Rochester/Lake Ontario.
- `ADSB_API_KEY` environment variable as a preferred alternative to `--api-key`
  to avoid exposing the key in `ps aux` process listings.
- Input validation on `--lat`, `--lon`, and `--range` sender arguments.
- 4 MB response size cap in `fetch_aircraft()` to prevent memory exhaustion
  from a malformed or hostile server response.
- Warning when a sender `--name` exceeds the 8-byte announce wire limit.
- `decode_announce_data()` returns `None` for out-of-bounds coordinates so
  receivers silently discard malformed announces instead of showing phantom
  senders at impossible map positions.
- Atomic download in `--alert-db auto`: CSV is written to a temp file and
  renamed on success so a failed mid-stream download never leaves a corrupt
  file that silently loads next run.
- MIT LICENSE file.

### Fixed
- `install_rns.sh`: fallback Python path now consistently uses
  `$PIPX_LOCAL_VENVS` instead of the potentially-unset `$PIPX_HOME`.
- Sender main loop `except Exception` no longer swallows `KeyboardInterrupt`
  and `SystemExit`.
- Removed obsolete `version:` key from `docker-compose.yml` (suppresses the
  deprecation warning printed on every `docker compose` invocation).

---

## [0.2.0] — 2026-03-21

### Added
- Per-link view-request filtering: receivers send an 8-byte viewport packet so
  senders only transmit aircraft within that window
- Geographic source gating: receivers skip links to senders whose coverage
  doesn't overlap the home position + range
- Known-sender persistence: `receiver.conf` caches linked sender hashes for
  instant reconnect on next startup, bypassing the announce wait
- Sender block/unblock support (`b` key, persisted in `receiver.conf`)
- adsb.lol feeder API and adsb.exchange API key support in sender
- plane-alert-db integration: military / government / LEA aircraft highlighted
  by category
- Extra landmarks via `--map CSV` flag
- Docker Compose cluster: 11 regional senders covering the Midwest
- `install.sh` one-step installer for macOS and Linux
- `install_rns.sh` Raspberry Pi installer with systemd service generator

### Changed
- Binary frame format bumped to v2: includes frame timestamp and sender center
  coordinates for improved multi-sender merge accuracy
- Aircraft merge now uses `obs_time = frame_timestamp - seen_pos` to prevent
  position jitter when overlapping senders report the same aircraft

## [0.1.0] — initial release

- Basic sender/receiver pair over Reticulum
- curses radar scope with altitude color-coding and track history
- Range rings, compass overlay, sidebar aircraft list
