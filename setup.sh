#!/bin/bash
# setup.sh — Set up adsb-radar on macOS or Linux
#
# What this does (nothing outside this directory, no sudo):
#   1. Creates a Python virtual environment at .venv/
#   2. Installs adsb-radar and all dependencies (including RNS) into it
#
# Usage:
#   bash setup.sh
#
# After setup, run directly or activate the venv:
#   source .venv/bin/activate
#   adsb-receiver --home-lat <lat> --home-lon <lon>
#   adsb-sender   --lat <lat> --lon <lon> --name <name>
#
# Or without activating:
#   .venv/bin/adsb-receiver --home-lat <lat> --home-lon <lon>

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

OS="$(uname -s)"
echo "=== adsb-radar setup ==="
echo "Platform : $OS"
echo "Directory: $SCRIPT_DIR"
echo

# ── 1. Create virtual environment ─────────────────────────────────────────────
echo "[1/2] Creating virtual environment at .venv/…"
python3 -m venv "$SCRIPT_DIR/.venv"
echo "     Created: $SCRIPT_DIR/.venv"

# ── 2. Install adsb-radar and dependencies ────────────────────────────────────
echo "[2/2] Installing adsb-radar and dependencies (RNS, etc.)…"
"$SCRIPT_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$SCRIPT_DIR/.venv/bin/pip" install -e "$SCRIPT_DIR"
echo "     Done."

# ── Done ──────────────────────────────────────────────────────────────────────
echo
echo "=== Setup complete ==="
echo
echo "Activate the virtual environment (optional):"
echo "   source $SCRIPT_DIR/.venv/bin/activate"
echo
echo "Quick start — receiver:"
echo "   $SCRIPT_DIR/.venv/bin/adsb-receiver --home-lat <lat> --home-lon <lon>"
echo
echo "Quick start — sender (adsb.im / dump1090 running locally at port 8080):"
echo "   $SCRIPT_DIR/.venv/bin/adsb-sender --lat <lat> --lon <lon> --name <your-name>"
echo
echo "Quick start — sender (public adsb.lol API, no local ADS-B receiver needed):"
echo "   $SCRIPT_DIR/.venv/bin/adsb-sender \\"
echo "       --url \"https://api.adsb.lol/v2/point/<lat>/<lon>/<nm>\" \\"
echo "       --lat <lat> --lon <lon> --range <nm> --name <your-name>"
echo
echo "See README.md for Reticulum interface config and full usage."
