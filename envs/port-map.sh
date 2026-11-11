#!/usr/bin/env bash
#
# port-map address translation: a two node cluster where each node advertises only its container
# address on port 3000, which the host cannot reach, and is published on its own host port. Node
# addresses are fixed so a port map can name node B before it exists. Node A is running; node B is
# created but not started until start-node-b. Clients seed node A only.
#
# Values: NODE_A_*, NODE_B_* (ADVERTISED_HOST and ADVERTISED_PORT are the container address). Nodes:
# a, b. Ports: ENV_PORT_BASE+20 and +21.
#
# Verbs: start-node-b starts node B and waits for the two node cluster; node-b-state prints running
# or stopped.
#
# Required environment: AEROSPIKE_IMAGE; FEATURES_CONF_PATH for an Enterprise image
#
# Usage: envs/port-map.sh up | down | start-node-b | node-b-state | info <node> <info command>

ENV_NAME=port-map
PORT_OFFSET=20
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

env_up() {
	local port_b=$((PORT + 1)) ip
	ip="$(subnet)"

	require_server
	require_ports "$ENV_HOST:$PORT" "$ENV_HOST:$port_b"
	fixed_cluster "" A1 "$PORT" B2 "$port_b" create
	wait_cluster a 1

	put_node NODE_A A1 "$ENV_HOST" "$PORT" "$ip.11" 3000
	put_node NODE_B B2 "$ENV_HOST" "$port_b" "$ip.12" 3000
}

verb_start-node-b() {
	require_up
	rt start "$NAME-b" >/dev/null
	wait_cluster b 2
	wait_cluster a 2
}

verb_node-b-state() {
	require_up
	if [ "$(rt inspect -f '{{.State.Running}}' "$NAME-b")" = "true" ]; then
		echo running
	else
		echo stopped
	fi
}

env_main "$@"
