#!/bin/sh
# Generate per-sender Reticulum config and exec adsb-sender.
#
# Environment:
#   RETICULUM_HOST — hostname/IP of the external Reticulum TCP hub (optional)
#                    Set: sender connects directly to hub (bypasses Docker transport,
#                         eliminating it as a single point of failure in live mesh mode).
#                    Unset: sender connects via the transport container (isolated mesh).
#   RETICULUM_PORT — TCP port (default 4242)

set -e

RNS_DIR=/root/.reticulum
RETICULUM_PORT="${RETICULUM_PORT:-4242}"

mkdir -p "$RNS_DIR"

if [ -n "${RETICULUM_HOST}" ]; then
  # Live mesh: connect directly to the external hub.
  # This eliminates the Docker transport as a hop and removes it as a
  # single point of failure — a transport blip no longer kills all 19 links.
  cat > "$RNS_DIR/config" <<EOF
[reticulum]
  enable_transport = False
  share_instance   = No

[interfaces]

  [[TCP to hub]]
    type              = TCPClientInterface
    interface_enabled = True
    target_host       = ${RETICULUM_HOST}
    target_port       = ${RETICULUM_PORT}
EOF
else
  # Isolated mesh: connect via the Docker transport bridge.
  cat > "$RNS_DIR/config" <<EOF
[reticulum]
  enable_transport = False
  share_instance   = No

[interfaces]

  [[TCP to transport]]
    type              = TCPClientInterface
    interface_enabled = True
    target_host       = transport
    target_port       = 7777
EOF
fi

exec "$@"
