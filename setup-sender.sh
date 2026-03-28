#!/bin/bash
# setup-sender.sh — Set up adsb-radar as an ADS-B sender on this machine
# Intended for Raspberry Pi or any Linux box running a local ADS-B receiver.
#
#   scp -r ~/adsb-radar pi@<pi-address>:~/adsb-radar
#   ssh pi@<pi-address>
#   cd ~/adsb-radar && bash setup-sender.sh
#
# What this script does (nothing beyond the project directory, except for the
# optional systemd service file you install manually at the end):
#   1. Checks that Python 3.9+ is available
#   2. Creates .venv/ in this directory and installs adsb-radar + RNS into it
#   3. Writes adsb_sender.service (a template you copy to systemd if you want)
#
# Nothing is written outside this directory without your explicit action.
# No sudo is used by this script.

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== adsb-radar — Raspberry Pi Sender Installer ==="
echo "Script dir: $SCRIPT_DIR"
echo

# ── 1. Check Python version ───────────────────────────────────────────────────
echo "[1/3] Checking Python version..."
PYTHON_OK=$(python3 -c "import sys; print('ok' if sys.version_info >= (3,9) else 'old')" 2>/dev/null || echo "missing")
if [ "$PYTHON_OK" != "ok" ]; then
    echo
    echo "ERROR: Python 3.9 or newer is required."
    echo "  adsb.im / Raspberry Pi OS Bookworm ships Python 3.11 — if you're"
    echo "  on an older image, upgrade with: sudo apt-get install python3"
    exit 1
fi
python3 --version

# ── 2. Create venv and install ────────────────────────────────────────────────
echo "[2/3] Creating virtual environment and installing adsb-radar (this may take a minute)..."
python3 -m venv "$SCRIPT_DIR/.venv"
"$SCRIPT_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$SCRIPT_DIR/.venv/bin/pip" install -e "$SCRIPT_DIR"

# Smoke test
"$SCRIPT_DIR/.venv/bin/adsb-sender" --help > /dev/null 2>&1 \
    && echo "     adsb-sender — OK" \
    || { echo "ERROR: adsb-sender not found after install"; exit 1; }

# ── 3. Generate systemd service for adsb_sender ───────────────────────────────
echo "[3/3] Writing systemd service file..."

SERVICE_FILE="$SCRIPT_DIR/adsb_sender.service"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=ADS-B Reticulum sender
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$SCRIPT_DIR
ExecStart=$SCRIPT_DIR/.venv/bin/adsb-sender \\
    --url   http://localhost:8080/data/aircraft.json \\
    --name  adsb-$(hostname -s) \\
    --lat   YOUR_LAT \\
    --lon   YOUR_LON \\
    --range 35
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

echo "  Wrote: $SERVICE_FILE"
echo
echo "  Edit --lat, --lon, --range, and --name to match your location,"
echo "  then install to systemd (this is the only step that requires sudo):"
echo
echo "    sudo cp $SERVICE_FILE /etc/systemd/system/"
echo "    sudo systemctl daemon-reload"
echo "    sudo systemctl enable --now adsb_sender"
echo "    journalctl -u adsb_sender -f    # watch output"
echo
echo "  To remove:  sudo systemctl disable --now adsb_sender"
echo "              sudo rm /etc/systemd/system/adsb_sender.service"
echo

# ── Done ──────────────────────────────────────────────────────────────────────
echo "=== Install complete ==="
echo
echo "Next steps:"
echo
echo "  1. Configure rnsd with your interface (LoRa, TCP, or AutoInterface)."
echo "     Edit ~/.reticulum/config — see README.md for examples."
echo "     rnsd is in the venv: $SCRIPT_DIR/.venv/bin/rnsd"
echo
echo "  2. Test the sender (Ctrl-C to stop):"
echo "     $SCRIPT_DIR/.venv/bin/adsb-sender --lat <lat> --lon <lon> --name <your-name>"
echo "     You should see '42 ac total | 39 with pos | 0/0 link(s) sent' every 5s."
echo
echo "  3. Edit $SERVICE_FILE with your real --lat, --lon, --name, then"
echo "     install it (instructions above) if you want the sender to run on boot."
echo
echo "  Data source URLs:"
echo "    adsb.im / dump1090:   --url http://localhost:8080/data/aircraft.json  (default)"
echo "    adsb.lol public API:  --url \"https://api.adsb.lol/v2/point/<lat>/<lon>/<nm>\""
echo "    adsb.lol feeder API:  --url \"https://re-api.adsb.lol?circle=<lat>,<lon>,<nm>\""
echo "      (re-api is free for active adsb.lol feeders, IP-gated)"
