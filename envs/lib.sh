# shellcheck shell=bash
# Shared by the environment scripts in this directory, which source it after setting ENV_NAME
# (the environment's name) and PORT_OFFSET (the first port of its block above ENV_PORT_BASE), and
# defining env_up. See README.md for the options, the verbs and the values file.

set -euo pipefail

ENVS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTAINER_RUNTIME="${CONTAINER_RUNTIME:-docker}"
ENV_PREFIX="${ENV_PREFIX:-conformance}"
ENV_PORT_BASE="${ENV_PORT_BASE:-4700}"
ENV_WORK_ROOT="${ENV_WORK_ROOT:-$ENVS_DIR/work}"
ENV_HOST=127.0.0.1
NAMESPACE="test"
WAIT_SECONDS=90

die() {
	echo "ERROR: $*" >&2
	exit 2
}

log() {
	echo "$*" >&2
}

case "$CONTAINER_RUNTIME" in
	docker | podman) ;;
	*) die "CONTAINER_RUNTIME must be docker or podman" ;;
esac
[[ "$ENV_PREFIX" =~ ^[a-z0-9][a-z0-9.-]{0,39}$ ]] || die "ENV_PREFIX must be lowercase letters, digits, dots and dashes"
[[ "$ENV_PORT_BASE" =~ ^[0-9]+$ ]] && [ "$ENV_PORT_BASE" -ge 1024 ] && [ "$ENV_PORT_BASE" -le 65000 ] ||
	die "ENV_PORT_BASE must be a port from 1024 to 65000"

NAME="$ENV_PREFIX-$ENV_NAME"
LABEL="conformance-env=$NAME"
WORK_DIR="$ENV_WORK_ROOT/$NAME"
VALUES="$WORK_DIR/values.env"
PORT=$((ENV_PORT_BASE + PORT_OFFSET))
CTL="$ENVS_DIR/$(basename "${BASH_SOURCE[1]}")"

rt() {
	"$CONTAINER_RUNTIME" "$@"
}

require_runtime() {
	command -v "$CONTAINER_RUNTIME" >/dev/null 2>&1 || die "$CONTAINER_RUNTIME CLI not found"
	rt info >/dev/null 2>&1 || die "$CONTAINER_RUNTIME is not reachable"
	command -v python3 >/dev/null 2>&1 || die "python3 not found"
}

require_server() {
	[ -n "${AEROSPIKE_IMAGE:-}" ] || die "AEROSPIKE_IMAGE is not set"
	FEATURES=""
	if [ -n "${FEATURES_CONF_PATH:-}" ]; then
		[ -r "$FEATURES_CONF_PATH" ] || die "FEATURES_CONF_PATH '$FEATURES_CONF_PATH' is not readable"
		FEATURES="$(cd "$(dirname "$FEATURES_CONF_PATH")" && pwd)/$(basename "$FEATURES_CONF_PATH")"
	fi
	printf 'AEROSPIKE_IMAGE=%q\nFEATURES=%q\n' "$AEROSPIKE_IMAGE" "$FEATURES" > "$WORK_DIR/server.state"
}

# For a verb that starts a node: the image and feature-key file up used.
load_server() {
	# shellcheck disable=SC1091
	. "$WORK_DIR/server.state"
}

# The address of the interface holding the default route, for a node reachable on a second host address.
host_b() {
	local address="${ENV_HOST_B:-}"

	if [ -z "$address" ]; then
		address="$(python3 -c 'import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.connect(("192.0.2.1", 9))
print(s.getsockname()[0])' 2>/dev/null || true)"
	fi
	case "$address" in
		"" | 127.*) die "no non-loopback IPv4 address found for this host; set ENV_HOST_B" ;;
	esac
	echo "$address"
}

# require_ports <host:port[/udp]>...: fail unless every address is free to bind.
require_ports() {
	local busy
	busy="$(python3 - "$@" <<'EOF'
import socket, sys

busy = []
for spec in sys.argv[1:]:
    address, _, proto = spec.partition("/")
    host, _, port = address.rpartition(":")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if proto == "udp" else socket.SOCK_STREAM)
    try:
        s.bind((host, int(port)))
    except OSError:
        busy.append(spec)
    finally:
        s.close()
print(" ".join(busy))
EOF
)"
	[ -z "$busy" ] || die "in use: $busy; set ENV_PORT_BASE to a free range"
}

# A /24 for a network with fixed node addresses, derived from the environment's first port.
subnet() {
	echo "10.213.$(((PORT / 10) % 256))"
}

# put <key> <value>: add one line to the values file.
put() {
	echo "$1=$2" >> "$VALUES.tmp"
}

# put_node <key prefix> <name> <host> <port> [<advertised host> <advertised port>]
put_node() {
	put "${1}_NAME" "$2"
	put "${1}_HOST" "$3"
	put "${1}_PORT" "$4"
	put "${1}_ADVERTISED_HOST" "${5:-}"
	put "${1}_ADVERTISED_PORT" "${6:-}"
}

# write_conf <file> <cluster name> <node id> <mesh seed or ""> [<alternate address> <alternate port> [<tls name> <cert dir>]]
write_conf() {
	local file="$1" cluster="$2" node_id="$3" mesh_seed="$4" alt_address="${5:-}" alt_port="${6:-}"
	local tls_name="${7:-}" cert_dir="${8:-}"

	{
		echo "service {"
		[ -z "$FEATURES" ] || echo "    feature-key-file /etc/aerospike/features.conf"
		echo "    cluster-name $cluster"
		echo "    proto-fd-max 15000"
		echo "    node-id $node_id"
		echo "}"
		echo
		echo "logging {"
		echo "    console {"
		echo "        context any info"
		echo "    }"
		echo "}"
		echo
		echo "network {"
		if [ -n "$tls_name" ]; then
			echo "    tls $tls_name {"
			echo "        cert-file /etc/aerospike/env/$cert_dir/server.crt"
			echo "        key-file /etc/aerospike/env/$cert_dir/server.key"
			echo "        ca-file /etc/aerospike/env/$cert_dir/ca.crt"
			echo "    }"
		fi
		echo "    service {"
		echo "        address any"
		echo "        port 3000"
		if [ -n "$alt_address" ]; then
			echo "        alternate-access-address $alt_address"
			echo "        alternate-access-port $alt_port"
		fi
		if [ -n "$tls_name" ]; then
			echo "        tls-port 4333"
			echo "        tls-authenticate-client false"
			echo "        tls-name $tls_name"
		fi
		echo "    }"
		echo "    heartbeat {"
		echo "        mode mesh"
		echo "        port 3002"
		[ -z "$mesh_seed" ] || echo "        mesh-seed-address-port $mesh_seed 3002"
		echo "    }"
		echo "    fabric {"
		echo "        port 3001"
		echo "    }"
		echo "}"
		echo
		echo "namespace $NAMESPACE {"
		echo "    replication-factor 1"
		echo "    storage-engine memory {"
		echo "        data-size 512M"
		echo "    }"
		echo "}"
	} > "$WORK_DIR/$file"
}

# network <suffix or ""> [<subnet /24 prefix>]: create a network and print its name.
network() {
	local name="$NAME${1:+-$1}"
	local args=(--label "$LABEL")

	[ -z "${2:-}" ] || args+=(--subnet "$2.0/24")
	rt network create "${args[@]}" "$name" >/dev/null || die "cannot create network $name${2:+ on $2.0/24}"
	echo "$name"
}

# node <run|create> <suffix> <network> <publish> <conf file> [<ip>]
node() {
	local action="$1" container="$NAME-$2" net="$3" publish="$4" conf="$5" ip="${6:-}"
	local args=(--name "$container" --label "$LABEL" --network "$net" -p "$publish" --ulimit nofile=15000
		-v "$WORK_DIR:/etc/aerospike/env:ro")

	[ "$action" = create ] || args=(-d "${args[@]}")
	[ -z "$ip" ] || args+=(--ip "$ip")
	[ -z "$FEATURES" ] || args+=(-v "$FEATURES:/etc/aerospike/features.conf:ro")
	rt "$action" "${args[@]}" --entrypoint asd "$AEROSPIKE_IMAGE" --foreground \
		--config-file "/etc/aerospike/env/$conf" >/dev/null
}

# cluster <suffix or ""> <node a> <port a> <alternate port a> <node b> <port b> <alternate port b>: two
# node cluster in containers [<suffix>-]a and [<suffix>-]b, published on ENV_HOST. A node with an
# alternate port advertises alternate-access-address ENV_HOST and that port; with "" it has none.
cluster() {
	local s="${1:+$1-}" cluster="${1:-$ENV_NAME}" net

	write_conf "${s}a.conf" "$cluster" "$2" "$NAME-${s}b" "${4:+$ENV_HOST}" "$4"
	write_conf "${s}b.conf" "$cluster" "$5" "$NAME-${s}a" "${7:+$ENV_HOST}" "$7"
	log "Starting ${1:-cluster}: $2 on $ENV_HOST:$3, $5 on $ENV_HOST:$6 ($AEROSPIKE_IMAGE)"
	net="$(network "$1")"
	node run "${s}a" "$net" "$ENV_HOST:$3:3000" "${s}a.conf"
	node run "${s}b" "$net" "$ENV_HOST:$6:3000" "${s}b.conf"
}

# fixed_cluster <suffix or ""> <node a> <port a> <node b> <port b> <run|create>: two node cluster on
# a network with subnet(), node A at .11 and node B at .12, each advertising only that address on
# port 3000, which the host cannot reach. Node B is started with run or only created with create.
fixed_cluster() {
	local s="${1:+$1-}" cluster="${1:-$ENV_NAME}" ip net
	ip="$(subnet)"

	write_conf "${s}a.conf" "$cluster" "$2" "$ip.12"
	write_conf "${s}b.conf" "$cluster" "$4" "$ip.11"
	log "Starting ${1:-cluster}: $2 ($ip.11:3000) on $ENV_HOST:$3, $4 ($ip.12:3000) on $ENV_HOST:$5 ($AEROSPIKE_IMAGE)"
	net="$(network "$1" "$ip")"
	node run "${s}a" "$net" "$ENV_HOST:$3:3000" "${s}a.conf" "$ip.11"
	node "$6" "${s}b" "$net" "$ENV_HOST:$5:3000" "${s}b.conf" "$ip.12"
}

# wait_cluster <suffix> <size>: wait until the node sees a stable cluster of that size.
wait_cluster() {
	local container="$NAME-$1" size="$2"

	for _ in $(seq 1 "$WAIT_SECONDS"); do
		if [ "$(rt inspect -f '{{.State.Running}}' "$container" 2>/dev/null)" != "true" ]; then
			rt logs "$container" 2>&1 | tail -30 >&2
			die "container $container exited"
		fi
		if rt exec "$container" asinfo -v "cluster-stable:size=$size;ignore-migrations=true" 2>/dev/null |
			grep -Eq '^[0-9A-F]+$'; then
			return 0
		fi
		sleep 1
	done
	rt logs "$container" 2>&1 | tail -30 >&2
	die "container $container did not reach a stable cluster of $size after ${WAIT_SECONDS}s"
}

# background <name> <command>...: start a host process that outlives this script.
background() {
	local name="$1"
	shift
	nohup "$@" > "$WORK_DIR/$name.log" 2>&1 < /dev/null &
	echo $! > "$WORK_DIR/$name.pid"
	sleep 1
	kill -0 "$(cat "$WORK_DIR/$name.pid")" 2>/dev/null || {
		cat "$WORK_DIR/$name.log" >&2
		die "$name did not start"
	}
}

# forward <name> <listen host> <listen port> <target port>: TCP forwarder to 127.0.0.1:<target port>.
forward() {
	background "forward-$1" python3 "$ENVS_DIR/forward.py" "$2" "$3" 127.0.0.1 "$4"
	log "$(cat "$WORK_DIR/forward-$1.log")"
}

# The directory itself is kept: a podman machine can go on serving a removed directory to a new
# container mounting a new one at the same path.
clear_work_dir() {
	[ ! -d "$WORK_DIR" ] || find "$WORK_DIR" -mindepth 1 -delete
}

env_down() {
	local pid_file pid ids

	for pid_file in "$WORK_DIR"/*.pid; do
		[ -e "$pid_file" ] || continue
		pid="$(cat "$pid_file")"
		if ps -p "$pid" -o command= 2>/dev/null | grep -Eq "$ENVS_DIR/forward.py|stub-ec2-server.py"; then
			kill "$pid" 2>/dev/null || true
		fi
		rm -f "$pid_file"
	done
	ids="$(rt ps -aq --filter "label=$LABEL")"
	# shellcheck disable=SC2086
	[ -z "$ids" ] || rt rm -f $ids >/dev/null
	ids="$(rt network ls -q --filter "label=$LABEL")"
	# shellcheck disable=SC2086
	[ -z "$ids" ] || rt network rm $ids >/dev/null
	clear_work_dir
	log "$NAME is down"
}

up_failed() {
	local rc=$?
	if [ "$rc" -ne 0 ]; then
		log "up failed; removing what it created"
		env_down
	fi
	exit "$rc"
}

env_start() {
	require_runtime
	[ ! -e "$VALUES" ] || die "$NAME is already up ($VALUES); run down first"
	[ -z "$(rt ps -aq --filter "label=$LABEL")" ] || die "containers labelled $LABEL exist; run down first"
	trap up_failed EXIT
	trap 'exit 130' INT TERM
	mkdir -p "$WORK_DIR"
	clear_work_dir
	{
		echo "ENV_NAME=$ENV_NAME"
		echo "ENV_CTL=$CTL"
		echo "ENV_VALUES=$VALUES"
		echo "ENV_PREFIX=$ENV_PREFIX"
		echo "ENV_PORT_BASE=$ENV_PORT_BASE"
		echo "ENV_WORK_ROOT=$ENV_WORK_ROOT"
		echo "CONTAINER_RUNTIME=$CONTAINER_RUNTIME"
		echo "ENV_HOST=$ENV_HOST"
		echo "ENV_NAMESPACE=$NAMESPACE"
	} > "$VALUES.tmp"
	env_up "$@"
	mv "$VALUES.tmp" "$VALUES"
	trap - EXIT
	echo "$VALUES"
}

require_up() {
	[ -e "$VALUES" ] || die "$NAME is not up"
}

# info <node> <command>: run an info command on a node, e.g. info a service-clear-alt
verb_info() {
	[ $# -eq 2 ] || die "usage: info <node> <info command>"
	require_up
	rt exec "$NAME-$1" asinfo -v "$2"
}

env_main() {
	local verb="${1:-}"
	[ $# -eq 0 ] || shift

	case "$verb" in
		up) env_start "$@" ;;
		down)
			require_runtime
			env_down
			;;
		"" | -h | --help) sed -n '/^# Usage:/,/^$/p' "$CTL" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
		*)
			declare -F "verb_$verb" >/dev/null || die "unknown verb '$verb'"
			"verb_$verb" "$@"
			;;
	esac
}
