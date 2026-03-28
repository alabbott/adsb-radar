#!/bin/sh
# Generate rnsd config from environment variables and start the transport node.
#
# Environment:
#   RETICULUM_HOST  — hostname/IP of your Reticulum TCP hub (optional)
#                     If unset, the cluster runs as an isolated local mesh —
#                     no traffic leaves the machine. Set this to connect to
#                     the live Reticulum network.
#   RETICULUM_PORT  — TCP port (default 4242)

set -e

RNS_DIR=/root/.reticulum
RETICULUM_PORT="${RETICULUM_PORT:-4242}"

mkdir -p "$RNS_DIR"

cat > "$RNS_DIR/config" <<EOF
[reticulum]
  enable_transport = True
  share_instance   = Yes

[interfaces]

  # TCP server — senders/receiver connect here by service name "transport"
  [[TCP Server]]
    type              = TCPServerInterface
    interface_enabled = True
    listen_ip         = 0.0.0.0
    listen_port       = 7777
EOF

if [ -n "${RETICULUM_HOST}" ]; then
  cat >> "$RNS_DIR/config" <<EOF

  # Uplink to the external Reticulum mesh hub
  [[TCP to reticulum_host]]
    type              = TCPClientInterface
    interface_enabled = True
    target_host       = ${RETICULUM_HOST}
    target_port       = ${RETICULUM_PORT}
EOF
fi

echo "[transport] Reticulum config written."
echo "[transport] Listening on 0.0.0.0:7777 for intra-cluster nodes."

if [ -n "${RETICULUM_HOST}" ]; then
  echo "[transport] Connecting to ${RETICULUM_HOST}:${RETICULUM_PORT} (live mesh)."
else
  echo "[transport] RETICULUM_HOST not set — running as isolated local mesh only."
fi

exec rnsd --config "$RNS_DIR"
