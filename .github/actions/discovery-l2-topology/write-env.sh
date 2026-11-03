#!/usr/bin/env bash
# Write the topology settings a client test reads as a docker run --env-file.
set -euo pipefail

file="${RUNNER_TEMP:-/tmp}/discovery-l2.env"
DISCOVERY_UNRESOLVED_HOSTNAME=""
DISCOVERY_VERBATIM_ACCESS_ADDRESS=""
if [[ "${L2_PROFILE:-}" == "unresolved-access" ]]; then
  DISCOVERY_UNRESOLVED_HOSTNAME="${ALTERNATE_HOST:?}"
  DISCOVERY_VERBATIM_ACCESS_ADDRESS=0
  if [[ "${VERBATIM_ACCESS_ADDRESS:-false}" == "true" ]]; then
    DISCOVERY_VERBATIM_ACCESS_ADDRESS=1
  fi
fi
export DISCOVERY_UNRESOLVED_HOSTNAME DISCOVERY_VERBATIM_ACCESS_ADDRESS
names=(
  L2_PROFILE
  DISCOVERY_SUBSET
  DISCOVERY_ENDPOINT_HOSTNAME
  DISCOVERY_ENDPOINT_PORT_BASE
  DISCOVERY_ENDPOINT_PORTS
  DISCOVERY_TLS_ENDPOINT_PORTS
  DISCOVERY_PROXY_MAP
  TOXIPROXY_URL
  TOXIPROXY_HOST
  AEROSPIKE_CONTAINER_NAMES
  DISCOVERY_REACHABLE_NODE
  DISCOVERY_PASS_THROUGH_BRANCH
  DISCOVERY_PORT_MAP_RANGE
  DISCOVERY_PORT_MAP_PORT_BASE
  DISCOVERY_TLS_CERT_SHAPE
  DISCOVERY_CLIENT_NETWORK_MTU
  DISCOVERY_PROXY_PORT_RANGE
  DISCOVERY_IDLE_RESET
  DISCOVERY_UNRESOLVED_HOSTNAME
  DISCOVERY_VERBATIM_ACCESS_ADDRESS
)

{
  for name in "${names[@]}"; do
    value="${!name:-}"
    # docker --env-file takes the rest of the line literally and has no escapes.
    if [[ "$value" == *$'\n'* ]]; then
      echo "Error: $name contains a newline" >&2
      exit 1
    fi
    printf '%s=%s\n' "$name" "$value"
  done
  echo "TOXIPROXY_CONTROL_PORT=8474"
  if [[ "${CONTROL:-false}" == "true" ]]; then
    printf 'DISCOVERY_CONTROL_DIR=%s\n' "${CONTROL_DIR:?}"
  fi
} > "$file"

echo "env-file=$file" >> "${GITHUB_OUTPUT:-/dev/null}"
if [[ "${CONTROL:-false}" == "true" ]]; then
  echo "node-server-log=${NODE_SERVER_LOG:-}" >> "${GITHUB_OUTPUT:-/dev/null}"
fi
cat "$file"
