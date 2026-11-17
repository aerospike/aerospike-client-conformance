#!/usr/bin/env bash
#
# Server certificate name matching: a TLS server per case, each with its own CA and a server
# certificate whose subject CN and subjectAltName are the case's, from tls/generate-certs.sh. up
# writes every case's certificate and values; serve starts the servers of the given cases and stops
# any other case's, so only the cases under test hold memory. Each server's own tls-name is the
# certificate CN, because the server refuses to start unless its tls-name is the CN or a dNSName.
# The proxy is a two node cluster, started by up, whose nodes both have tls-name
# PROXY_TLS_NAME, carried only as a dNSName with a non-matching CN and no iPAddress, reached only
# through one TCP forwarder per node on the host: clients seed node A and learn node B, and its
# tls-name, from peers.
#
# Values: TLS_CASES (comma separated case names); per case, with the name upper-cased:
#   TLS_<CASE>_PORT       TLS port of the case's server on ENV_HOST
#   TLS_<CASE>_NAME       tlsName to connect with
#   TLS_<CASE>_EXPECT     accept or reject
#   TLS_<CASE>_WARNING    true when the legacy-match deprecation warning must be logged
#   TLS_<CASE>_BOOTSTRAP  a tlsName the certificate accepts, to reach the server on a reject case
#   TLS_<CASE>_REVOKE     hex serial of the server certificate to revoke, or empty
#   TLS_<CASE>_CA         CA file
# and for the proxy: PROXY_TLS_NAME, PROXY_CA, PROXY_NODE_A_*, PROXY_NODE_B_* (PORT is the
# forwarder's). Nodes: each case name, proxy-a, proxy-b. Ports: ENV_PORT_BASE+80 to +108.
#
# Required environment: AEROSPIKE_IMAGE (Enterprise), FEATURES_CONF_PATH
#
# Verbs: serve <case>... starts the servers of those cases on their ports, after stopping any other
# case's server, and waits until they are ready.
#
# Usage: envs/tls-matching.sh up | down | serve <case>... | info <node> <info command>

ENV_NAME=tls-matching
PORT_OFFSET=80
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

GENERATE="$(cd "$ENVS_DIR/../tls" && pwd)/generate-certs.sh"

# name|tlsName|subject CN|subjectAltName (- for none) or shape:<shape in tls/name-matching.yaml>|expect|warning|bootstrap|revoke
# A reject case's certificate also carries its bootstrap tlsName as a dNSName, so the server is
# reached without the CN fallback (except cn_case_only).
CASES=(
	"dns_exact|node.example.com|server-cn|DNS:node.example.com|accept|false||no"
	"dns_case|node.example.com|server-cn|DNS:Node.Example.COM|accept|false||no"
	"ipv4_ip_san|127.0.0.1|server-cn|IP:127.0.0.1|accept|false||no"
	"ipv6_ip_san|2001:db8::1|server-cn|IP:2001:db8::1|accept|false||no"
	"ipv6_ip_san_equivalent|2001:0db8:0:0:0:0:0:1|server-cn|IP:2001:db8::1|accept|false||no"
	"ipv6_bracketed|[2001:db8::1]|server-cn|IP:2001:db8::1|accept|false||no"
	"ipv6_scoped|fe80::1%en0|server-cn|IP:fe80::1,DNS:server-cn|reject|false|server-cn|no"
	"ipv4_leading_zero|127.000.0.1|server-cn|IP:127.0.0.1,DNS:server-cn|reject|false|server-cn|no"
	"dns_only_as_ip|localhost|server-cn|IP:127.0.0.1,DNS:server-cn|reject|false|server-cn|no"
	"wildcard_one_label|a.example.com|server-cn|DNS:*.example.com|accept|false||no"
	"wildcard_case|A.Example.COM|server-cn|DNS:*.example.com|accept|false||no"
	"wildcard_bare_domain|example.com|server-cn|DNS:*.example.com,DNS:server-cn|reject|false|server-cn|no"
	"wildcard_two_labels|a.b.example.com|server-cn|DNS:*.example.com,DNS:server-cn|reject|false|server-cn|no"
	"wildcard_partial_label|ab.example.com|server-cn|DNS:a*.example.com,DNS:server-cn|reject|false|server-cn|no"
	"wildcard_double|a.b.example.com|server-cn|DNS:*.*.example.com,DNS:server-cn|reject|false|server-cn|no"
	"wildcard_inner_label|foo.a.example.com|server-cn|DNS:foo.*.example.com,DNS:server-cn|reject|false|server-cn|no"
	"ip_vs_wildcard|127.0.0.1|server-cn|DNS:*.0.0.1,DNS:server-cn|reject|false|server-cn|no"
	"cn_case_only|node.example.com|Node.Example.com|-|reject|false|Node.Example.com|no"
	"legacy_cn_only|node.example.com|node.example.com|-|accept|true||no"
	"legacy_cn_nonmatching_dns|node.example.com|node.example.com|DNS:other.example.com|accept|true||no"
	"legacy_ip_as_dns|127.0.0.1|server-cn|DNS:127.0.0.1|accept|true||no"
	"dns_san_nonmatching_cn|node.example.com|server-cn|DNS:node.example.com|accept|false||no"
	"ci_setup_aerospike_server|aerospike-tls|aerospike-tls|shape:ci-shape|accept|true||no"
	"ci_setup_aerospike_server_fixed|aerospike-tls|aerospike-tls|shape:ci-shape-fixed|accept|false||no"
	"revoked_serial|node.example.com|server-cn|DNS:node.example.com|reject|false||yes"
)
PROXY_TLS_NAME=cluster.example.com
PROXY_SAN=DNS:cluster.example.com

certificate() {
	local dir="$1" cn="$2" san="$3"

	if [ "${san#shape:}" != "$san" ]; then
		"$GENERATE" --server "$dir" --shape "${san#shape:}" >/dev/null
	else
		"$GENERATE" --server "$dir" "$cn" "$san" >/dev/null
	fi
}

env_up() {
	local i port name tls_name cn san expect warning bootstrap revoke key serial net names=()
	local proxy_a=$((PORT + 25)) proxy_b=$((PORT + 26)) forward_a=$((PORT + 27)) forward_b=$((PORT + 28))

	require_server
	[ -n "$FEATURES" ] || die "FEATURES_CONF_PATH is not set; TLS needs an Enterprise image and its feature key"
	command -v openssl >/dev/null 2>&1 || die "openssl not found"
	require_ports "$ENV_HOST:$proxy_a" "$ENV_HOST:$proxy_b" "$ENV_HOST:$forward_a" "$ENV_HOST:$forward_b"

	for i in "${!CASES[@]}"; do
		IFS='|' read -r name tls_name cn san expect warning bootstrap revoke <<< "${CASES[$i]}"
		certificate "$WORK_DIR/$name" "$cn" "$san"
		write_conf "$name/aerospike.conf" "$ENV_NAME" A1 "" "" "" "$cn" "$name"
		serial=""
		[ "$revoke" = no ] || serial="$(openssl x509 -in "$WORK_DIR/$name/server.crt" -noout -serial | cut -d= -f2)"
		key="TLS_$(echo "$name" | tr '[:lower:]' '[:upper:]')"
		put "${key}_PORT" "$((PORT + i))"
		put "${key}_NAME" "$tls_name"
		put "${key}_EXPECT" "$expect"
		put "${key}_WARNING" "$warning"
		put "${key}_BOOTSTRAP" "$bootstrap"
		put "${key}_REVOKE" "$serial"
		put "${key}_CA" "$WORK_DIR/$name/ca.crt"
		names+=("$name")
	done
	put TLS_CASES "$(IFS=,; echo "${names[*]}")"
	network "" >/dev/null

	certificate "$WORK_DIR/proxy" server-cn "$PROXY_SAN"
	write_conf proxy/a.conf "$ENV_NAME-proxy" A1 "$NAME-proxy-b" "" "" "$PROXY_TLS_NAME" proxy
	write_conf proxy/b.conf "$ENV_NAME-proxy" B2 "$NAME-proxy-a" "" "" "$PROXY_TLS_NAME" proxy
	log "Starting proxy: A1 through $ENV_HOST:$forward_a, B2 through $ENV_HOST:$forward_b, tls-name $PROXY_TLS_NAME ($AEROSPIKE_IMAGE)"
	net="$(network proxy)"
	node run proxy-a "$net" "$ENV_HOST:$proxy_a:4333" proxy/a.conf
	node run proxy-b "$net" "$ENV_HOST:$proxy_b:4333" proxy/b.conf
	wait_cluster proxy-a 2
	wait_cluster proxy-b 2
	forward proxy-a "$ENV_HOST" "$forward_a" "$proxy_a"
	forward proxy-b "$ENV_HOST" "$forward_b" "$proxy_b"
	put PROXY_TLS_NAME "$PROXY_TLS_NAME"
	put PROXY_CA "$WORK_DIR/proxy/ca.crt"
	put_node PROXY_NODE_A A1 "$ENV_HOST" "$forward_a"
	put_node PROXY_NODE_B B2 "$ENV_HOST" "$forward_b"
}

verb_serve() {
	local want c i found running port starting=() ports=()

	require_up
	load_server
	[ $# -gt 0 ] || die "usage: serve <case>..."
	for want in "$@"; do
		found=0
		for i in "${!CASES[@]}"; do
			if [ "${CASES[$i]%%|*}" = "$want" ]; then
				starting+=("$want")
				ports+=("$ENV_HOST:$((PORT + i))")
				found=1
			fi
		done
		[ "$found" = 1 ] || die "unknown case '$want'"
	done

	for running in $(rt ps -a --filter "label=$LABEL" --format '{{.Names}}'); do
		case "$running" in
			"$NAME"-proxy-*) ;;
			*) rt rm -f "$running" >/dev/null ;;
		esac
	done
	require_ports "${ports[@]}"
	for i in "${!starting[@]}"; do
		c="${starting[$i]}"
		port="${ports[$i]#*:}"
		log "Starting $c on $ENV_HOST:$port"
		node run "$c" "$NAME" "$ENV_HOST:$port:4333" "$c/aerospike.conf"
	done
	for c in "${starting[@]}"; do
		wait_cluster "$c" 1
	done
}

env_main "$@"
