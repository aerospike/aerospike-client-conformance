#!/usr/bin/env bash
#
# Translation misses (translation on_miss): a two node cluster where each node advertises
# alternate-access-address ENV_HOST and its own published host port as alternate-access-port, one
# host name and a port per node, so with useServicesAlternate the peer each node advertises is
# reachable from the host only as advertised. Clients seed node A only.
#
# Values: NODE_A_*, NODE_B_*. Nodes: a, b. Ports: ENV_PORT_BASE+60 and +61.
#
# Required environment: AEROSPIKE_IMAGE; FEATURES_CONF_PATH for an Enterprise image
#
# Usage: envs/on-miss.sh up | down | info <node> <info command>

ENV_NAME=on-miss
PORT_OFFSET=60
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

env_up() {
	local port_b=$((PORT + 1))

	require_server
	require_ports "$ENV_HOST:$PORT" "$ENV_HOST:$port_b"
	cluster "" A1 "$PORT" "$PORT" B2 "$port_b" "$port_b"
	wait_cluster a 2
	wait_cluster b 2

	put_node NODE_A A1 "$ENV_HOST" "$PORT" "$ENV_HOST" "$PORT"
	put_node NODE_B B2 "$ENV_HOST" "$port_b" "$ENV_HOST" "$port_b"
}

env_main "$@"
