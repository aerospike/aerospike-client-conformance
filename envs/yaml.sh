#!/usr/bin/env bash
#
# Discovery and translation configured from the YAML config file only: two two node clusters and a
# YAML file for each. Clients seed node A only.
#
# - pass-through: each node advertises alternate-access-address ENV_HOST and its own published host
#   port. The YAML sets use_services_alternate: true and translation.provider: pass-through.
# - port-map: each node advertises only its container address on port 3000, which the host cannot
#   reach. The YAML sets translation.provider: port-map and on_miss: strict, mapping node A by its
#   advertised host:port and node B by node name to their published host ports.
#
# Values: PASS_THROUGH_NODE_A_*, PASS_THROUGH_NODE_B_*, PASS_THROUGH_CLIENT_CONFIG, PORT_MAP_NODE_A_*,
# PORT_MAP_NODE_B_*, PORT_MAP_CLIENT_CONFIG. Nodes: pass-through-a, pass-through-b, port-map-a,
# port-map-b. Ports: ENV_PORT_BASE+40 to +43.
#
# Required environment: AEROSPIKE_IMAGE; FEATURES_CONF_PATH for an Enterprise image
#
# Usage: envs/yaml.sh up | down | info <node> <info command>

ENV_NAME=yaml
PORT_OFFSET=40
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

env_up() {
	local pt_b=$((PORT + 1)) pm_a=$((PORT + 2)) pm_b=$((PORT + 3)) ip c
	ip="$(subnet)"

	require_server
	require_ports "$ENV_HOST:$PORT" "$ENV_HOST:$pt_b" "$ENV_HOST:$pm_a" "$ENV_HOST:$pm_b"
	cluster pass-through A1 "$PORT" "$PORT" B2 "$pt_b" "$pt_b"
	fixed_cluster port-map A1 "$pm_a" B2 "$pm_b" run
	for c in pass-through-a pass-through-b port-map-a port-map-b; do
		wait_cluster "$c" 2
	done

	cat > "$WORK_DIR/pass-through.yaml" <<YAML
version: 1.2.0
static:
  client:
    use_services_alternate: true
    translation:
      provider: pass-through
dynamic:
  client:
    tend_interval: 250
YAML
	cat > "$WORK_DIR/port-map.yaml" <<YAML
version: 1.2.0
static:
  client:
    translation:
      provider: port-map
      on_miss: strict
      port_map:
        "$ip.11:3000": "$ENV_HOST:$pm_a"
        "B2": "$ENV_HOST:$pm_b"
dynamic:
  client:
    tend_interval: 250
YAML

	put_node PASS_THROUGH_NODE_A A1 "$ENV_HOST" "$PORT" "$ENV_HOST" "$PORT"
	put_node PASS_THROUGH_NODE_B B2 "$ENV_HOST" "$pt_b" "$ENV_HOST" "$pt_b"
	put PASS_THROUGH_CLIENT_CONFIG "$WORK_DIR/pass-through.yaml"
	put_node PORT_MAP_NODE_A A1 "$ENV_HOST" "$pm_a" "$ip.11" 3000
	put_node PORT_MAP_NODE_B B2 "$ENV_HOST" "$pm_b" "$ip.12" 3000
	put PORT_MAP_CLIENT_CONFIG "$WORK_DIR/port-map.yaml"
}

env_main "$@"
