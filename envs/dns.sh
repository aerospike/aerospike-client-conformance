#!/usr/bin/env bash
#
# DNS seed discovery: a two node cluster, a standalone third node and a CoreDNS server. Node A is
# reachable on ENV_HOST and node B on this host's outbound IPv4 address, both on the same port, and
# each advertises that address as alternate-access-address, so with useServicesAlternate both are
# reachable only as advertised. Node B is published on ENV_HOST:ENV_PORT_BASE+1 and reached on its
# address through a TCP forwarder on the host, because rootless podman cannot publish one port on
# two host addresses. Node C is a cluster of its own. CoreDNS serves zone dns.test from DNS_ZONE_FILE
# and reloads it every second. Clients get no static seed.
#
# Zone dns.test:
#   seeds            A NODE_A_HOST, A NODE_B_HOST
#   _aerospike._tcp  SRV to node-a and node-b on NODE_A_PORT
#   _change._tcp     SRV to node-a and node-b on NODE_A_PORT, changed by set-change-srv
#   node-a, node-b, node-c  A records of each node
#
# Values: NODE_A_*, NODE_B_*, NODE_C_*, DNS_SERVER (ip:port, UDP and TCP), DNS_ZONE_FILE,
# CLIENT_CONFIG (YAML selecting provider dns with names seeds.dns.test, record type a). Nodes: a, b,
# c. Ports: ENV_PORT_BASE to +3; node A and node B share ENV_PORT_BASE.
#
# Verbs: stop-dns and start-dns stop and start the DNS server; set-change-srv <node>... points
# _change._tcp at the given nodes (node-a, node-b, node-c) and bumps the zone serial.
#
# Required environment: AEROSPIKE_IMAGE; FEATURES_CONF_PATH for an Enterprise image
# Optional environment: ENV_HOST_B (node B's address), COREDNS_IMAGE (default docker.io/coredns/coredns:1.11.3)
#
# Usage: envs/dns.sh up | down | stop-dns | start-dns | set-change-srv <node>... | info <node> <info command>

ENV_NAME=dns
PORT_OFFSET=0
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

COREDNS_IMAGE="${COREDNS_IMAGE:-docker.io/coredns/coredns:1.11.3}"
ZONE="$WORK_DIR/coredns/dns.test.db"

write_zone() {
	local serial="$1" host_b="$2" port_c="$3" target
	shift 3

	{
		echo "\$ORIGIN dns.test."
		echo "@ 3600 IN SOA ns.dns.test. admin.dns.test. $serial 60 60 60 1"
		echo "@ 3600 IN NS ns.dns.test."
		echo "ns 1 IN A $ENV_HOST"
		echo "seeds 1 IN A $ENV_HOST"
		echo "seeds 1 IN A $host_b"
		echo "node-a 1 IN A $ENV_HOST"
		echo "node-b 1 IN A $host_b"
		echo "node-c 1 IN A $ENV_HOST"
		echo "_aerospike._tcp 1 IN SRV 10 10 $PORT node-a"
		echo "_aerospike._tcp 1 IN SRV 10 10 $PORT node-b"
		for target in "$@"; do
			if [ "$target" = node-c ]; then
				echo "_change._tcp 1 IN SRV 10 10 $port_c node-c"
			else
				echo "_change._tcp 1 IN SRV 10 10 $PORT $target"
			fi
		done
	} > "$ZONE.tmp"
	mv "$ZONE.tmp" "$ZONE"
}

wait_dns() {
	for _ in $(seq 1 30); do
		if [ "$(rt inspect -f '{{.State.Running}}' "$NAME-coredns" 2>/dev/null)" != "true" ]; then
			rt logs "$NAME-coredns" 2>&1 | tail -30 >&2
			die "container $NAME-coredns exited"
		fi
		if rt logs "$NAME-coredns" 2>&1 | grep -q "CoreDNS-"; then
			return 0
		fi
		sleep 1
	done
	die "container $NAME-coredns did not start after 30s"
}

env_up() {
	local port_b=$((PORT + 1)) port_c=$((PORT + 2)) dns_port=$((PORT + 3)) host_b net

	require_server
	host_b="$(host_b)"
	require_ports "$ENV_HOST:$PORT" "$host_b:$PORT" "$ENV_HOST:$port_b" "$ENV_HOST:$port_c" \
		"$ENV_HOST:$dns_port" "$ENV_HOST:$dns_port/udp"
	write_conf a.conf "$ENV_NAME" A1 "$NAME-b" "$ENV_HOST" "$PORT"
	write_conf b.conf "$ENV_NAME" B2 "$NAME-a" "$host_b" "$PORT"
	write_conf c.conf "$ENV_NAME-c" C3 "" "$ENV_HOST" "$port_c"

	mkdir -p "$WORK_DIR/coredns"
	cat > "$WORK_DIR/coredns/Corefile" <<EOF
dns.test:53 {
    file /etc/coredns/dns.test.db {
        reload 1s
    }
    errors
}
EOF
	write_zone 1 "$host_b" "$port_c" node-a node-b
	cat > "$WORK_DIR/client.yaml" <<EOF
version: 1.2.0
static:
  client:
    use_services_alternate: true
    discovery:
      provider: dns
      execution_mode: thread
      refresh_interval_ms: 1000
      dns:
        names:
          - seeds.dns.test
        record_type: a
        port: $PORT
        timeout_ms: 80
        servers:
          - $ENV_HOST:$dns_port
EOF

	log "Starting A1 on $ENV_HOST:$PORT, B2 on $host_b:$PORT through $ENV_HOST:$port_b, C3 on $ENV_HOST:$port_c ($AEROSPIKE_IMAGE)"
	log "Starting DNS server on $ENV_HOST:$dns_port ($COREDNS_IMAGE)"
	net="$(network "")"
	node run a "$net" "$ENV_HOST:$PORT:3000" a.conf
	node run b "$net" "$ENV_HOST:$port_b:3000" b.conf
	node run c "$(network c)" "$ENV_HOST:$port_c:3000" c.conf
	rt run -d --name "$NAME-coredns" --label "$LABEL" -p "$ENV_HOST:$dns_port:53/udp" -p "$ENV_HOST:$dns_port:53/tcp" \
		-v "$WORK_DIR/coredns:/etc/coredns:ro" "$COREDNS_IMAGE" -conf /etc/coredns/Corefile >/dev/null
	wait_cluster a 2
	wait_cluster b 2
	wait_cluster c 1
	wait_dns
	forward node-b "$host_b" "$PORT" "$port_b"

	put_node NODE_A A1 "$ENV_HOST" "$PORT" "$ENV_HOST" "$PORT"
	put_node NODE_B B2 "$host_b" "$PORT" "$host_b" "$PORT"
	put_node NODE_C C3 "$ENV_HOST" "$port_c" "$ENV_HOST" "$port_c"
	put DNS_SERVER "$ENV_HOST:$dns_port"
	put DNS_ZONE_FILE "$ZONE"
	put CLIENT_CONFIG "$WORK_DIR/client.yaml"
}

verb_stop-dns() {
	require_up
	rt stop -t 1 "$NAME-coredns" >/dev/null
}

verb_start-dns() {
	require_up
	rt start "$NAME-coredns" >/dev/null
}

verb_set-change-srv() {
	local serial target

	require_up
	[ $# -gt 0 ] || die "usage: set-change-srv <node>..."
	for target in "$@"; do
		case "$target" in
			node-a | node-b | node-c) ;;
			*) die "unknown node '$target'; use node-a, node-b or node-c" ;;
		esac
	done
	serial="$(awk '$4 == "SOA" { print $7 + 1 }' "$ZONE")"
	# shellcheck disable=SC1090
	. "$VALUES"
	write_zone "$serial" "$NODE_B_HOST" "$NODE_C_PORT" "$@"
}

env_main "$@"
