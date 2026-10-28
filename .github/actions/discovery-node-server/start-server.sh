#!/usr/bin/env bash
# Start discovery-node-server.sh in the background and wait until it is ready.
set -euo pipefail

: "${CONTROL_DIR:?}" "${NETWORK_NAME:?}" "${PARKED_CONTAINER:?}" "${CONFIG_CONTAINER:?}"
ALTERNATE_HOST="${ALTERNATE_HOST:-}"
READY_TIMEOUT="${READY_TIMEOUT:-30}"
LOG_FILE="${LOG_FILE:-${RUNNER_TEMP:-/tmp}/discovery-node-server.log}"
server="$(dirname "${BASH_SOURCE[0]}")/discovery-node-server.sh"

[[ "$READY_TIMEOUT" =~ ^[0-9]+$ ]] || { echo "Error: ready-timeout must be a whole number of seconds" >&2; exit 1; }
mkdir -p "$CONTROL_DIR"
rm -f "$CONTROL_DIR/ready"
# Output goes to a file: a background process holding the step's stdout would delay the step and later hit a closed pipe.
nohup "$server" "$CONTROL_DIR" "$NETWORK_NAME" "$PARKED_CONTAINER" "$CONFIG_CONTAINER" "$ALTERNATE_HOST" \
  > "$LOG_FILE" 2>&1 < /dev/null &
pid=$!

for ((i = 0; i < READY_TIMEOUT * 10; i++)); do
  if [[ -e "$CONTROL_DIR/ready" ]]; then
    echo "Discovery node server $pid serving $CONTROL_DIR, log $LOG_FILE"
    exit 0
  fi
  if ! kill -0 "$pid" 2> /dev/null; then
    cat "$LOG_FILE" >&2
    echo "Error: discovery node server exited during startup" >&2
    exit 1
  fi
  sleep 0.1
done

cat "$LOG_FILE" >&2
kill "$pid" 2> /dev/null || true
echo "Error: discovery node server not ready after ${READY_TIMEOUT}s" >&2
exit 1
