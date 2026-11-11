#!/usr/bin/env bash
#
# Translated address collisions: two independent two node clusters where every node advertises
# alternate-access-address ENV_HOST. In the control cluster each node advertises its own published
# host port as alternate-access-port. In the collision cluster node B advertises node A's published
# port, so with useServicesAlternate both nodes translate to the same host and port. Clients seed
# node A only.
#
# Values: CONTROL_NODE_A_*, CONTROL_NODE_B_*, COLLISION_NODE_A_*, COLLISION_NODE_B_*. Nodes:
# control-a, control-b, collision-a, collision-b. Ports: ENV_PORT_BASE+30 to +33.
#
# Required environment: AEROSPIKE_IMAGE; FEATURES_CONF_PATH for an Enterprise image
#
# Usage: envs/collision.sh up | down | info <node> <info command>

ENV_NAME=collision
PORT_OFFSET=30
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

env_up() {
	local control_b=$((PORT + 1)) collision_a=$((PORT + 2)) collision_b=$((PORT + 3)) c

	require_server
	require_ports "$ENV_HOST:$PORT" "$ENV_HOST:$control_b" "$ENV_HOST:$collision_a" "$ENV_HOST:$collision_b"
	cluster control A1 "$PORT" "$PORT" B2 "$control_b" "$control_b"
	cluster collision C3 "$collision_a" "$collision_a" D4 "$collision_b" "$collision_a"
	for c in control-a control-b collision-a collision-b; do
		wait_cluster "$c" 2
	done

	put_node CONTROL_NODE_A A1 "$ENV_HOST" "$PORT" "$ENV_HOST" "$PORT"
	put_node CONTROL_NODE_B B2 "$ENV_HOST" "$control_b" "$ENV_HOST" "$control_b"
	put_node COLLISION_NODE_A C3 "$ENV_HOST" "$collision_a" "$ENV_HOST" "$collision_a"
	put_node COLLISION_NODE_B D4 "$ENV_HOST" "$collision_b" "$ENV_HOST" "$collision_a"
}

env_main "$@"
