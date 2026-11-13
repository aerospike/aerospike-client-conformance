#!/usr/bin/env bash
#
# Nodes without an alternate access address (useServicesAlternate): two independent two node
# clusters. In the missing cluster node A advertises alternate-access-address ENV_HOST and its own
# published host port, and node B has no alternate access address. In the control cluster both
# nodes advertise ENV_HOST and their own published host port.
#
# Values: MISSING_NODE_A_*, MISSING_NODE_B_* (ADVERTISED_HOST and ADVERTISED_PORT empty),
# CONTROL_NODE_A_*, CONTROL_NODE_B_*. Nodes: missing-a, missing-b, control-a, control-b. Ports:
# ENV_PORT_BASE+70 to +73.
#
# Required environment: AEROSPIKE_IMAGE; FEATURES_CONF_PATH for an Enterprise image
#
# Usage: envs/alt-missing.sh up | down | info <node> <info command>

ENV_NAME=alt-missing
PORT_OFFSET=70
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

env_up() {
	local missing_b=$((PORT + 1)) control_a=$((PORT + 2)) control_b=$((PORT + 3)) c

	require_server
	require_ports "$ENV_HOST:$PORT" "$ENV_HOST:$missing_b" "$ENV_HOST:$control_a" "$ENV_HOST:$control_b"
	cluster missing A1 "$PORT" "$PORT" B2 "$missing_b" ""
	cluster control C3 "$control_a" "$control_a" D4 "$control_b" "$control_b"
	for c in missing-a missing-b control-a control-b; do
		wait_cluster "$c" 2
	done

	put_node MISSING_NODE_A A1 "$ENV_HOST" "$PORT" "$ENV_HOST" "$PORT"
	put_node MISSING_NODE_B B2 "$ENV_HOST" "$missing_b"
	put_node CONTROL_NODE_A C3 "$ENV_HOST" "$control_a" "$ENV_HOST" "$control_a"
	put_node CONTROL_NODE_B D4 "$ENV_HOST" "$control_b" "$ENV_HOST" "$control_b"
}

env_main "$@"
