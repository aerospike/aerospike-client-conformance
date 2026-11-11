#!/usr/bin/env bash
#
# L2 for one client, on one machine with a container runtime (docker, or rootless podman through its
# docker CLI). For each profile it builds the topology: three Aerospike nodes on a private node
# network with no published ports, Toxiproxy on that network and on a client network with one
# listener per node behind one endpoint hostname, as aerospike/shared-workflows
# setup-discovery-topology builds it, and a client network that is internal, so a container on it has
# no route to the node addresses. It builds the client once with the manifest's L5_SETUP and L5_BUILD
# in an Amazon Linux 2023 container, then runs rows.py for the profile, which starts the manifest's
# L5_DRIVER in a container on the client network only, applies the faults through l2ctl and decides
# every row. When the manifest sets L2_RUN, it then runs once per profile in that container with the
# topology's values file as its environment. Every container, network and image the run creates is
# removed on exit, including on failure, and the run fails if any is left.
#
# Container names, network names and ports derive from --prefix and --port-base, so runs with
# different values do not collide; the run checks that its names, ports and subnet are free first.
# --plan prints each profile's topology and rows and creates nothing. --no-fault is the control run:
# every fault row runs without its fault and must fail.
#
# Optional environment:
#   L2_PREFIX           default for --prefix (l2-proxy-<port base>)
#   L2_PORT_BASE        default for --port-base (4900)
#   FEATURES_CONF_PATH  Enterprise feature-key file, for --edition ee; mounted into the nodes, never printed
#   SHARED_WORKFLOWS_DIR  local aerospike/shared-workflows clone to read setup-discovery-topology from
#
# Usage: l2-proxy/run.sh --client-repo <https url or local path> --commit <commit> --manifest <path>
#                        [--profile default,...|all] [--rows ROW,...] [--edition ce|ee]
#                        [--server-image <image>] [--unresolved-hostname] [--no-fault]
#                        [--prefix <prefix>] [--port-base <port>] [--cache <dir>] [--results <dir>] [--plan]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ALL_PROFILES="default reachable pass-through-names pass-through-ports port-map-range port-range low-mtu idle-reset"
SHARED_WORKFLOWS_COMMIT=318e453bea7f3c068ead24057dab07312a7fe62e
ACTION_PATH=.github/actions/setup-discovery-topology
TOXIPROXY_IMAGE=ghcr.io/shopify/toxiproxy:2.12.0
CURL_IMAGE=curlimages/curl:8.11.1
BUILD_IMAGE=public.ecr.aws/amazonlinux/amazonlinux:2023
ENDPOINT_HOSTNAME=as-endpoint.test
REACHABLE_HOSTNAME=as-reachable.test
PASS_THROUGH_HOSTNAME=as-pass-through.test
PASS_THROUGH_DOMAIN=pass-through.test
NODES=3
RANGE_FIRST=11
RANGE_LAST=26
OUTSIDE_OCTET=40
CLIENT_NETWORK_MTU=1200
PROXY_PORT_RANGE="40000 40009"
PORT_SPAN=200

die() {
	echo "ERROR: $*" >&2
	exit 2
}

usage() {
	sed -n '/^# Usage:/,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
	exit 2
}

CLIENT_REPO=""
COMMIT=""
MANIFEST=""
PROFILES="default"
ROWS=""
EDITION=ce
SERVER_IMAGE=""
UNRESOLVED=0
NO_FAULT=0
PORT_BASE="${L2_PORT_BASE:-4900}"
PREFIX="${L2_PREFIX:-}"
CACHE=""
RESULTS=""
PLAN=0
while [ $# -gt 0 ]; do
	case "$1" in
		--client-repo) CLIENT_REPO="${2:-}"; shift ;;
		--commit) COMMIT="${2:-}"; shift ;;
		--manifest) MANIFEST="${2:-}"; shift ;;
		--profile) PROFILES="${2:-}"; shift ;;
		--rows) ROWS="${2:-}"; shift ;;
		--edition) EDITION="${2:-}"; shift ;;
		--server-image) SERVER_IMAGE="${2:-}"; shift ;;
		--unresolved-hostname) UNRESOLVED=1 ;;
		--no-fault) NO_FAULT=1 ;;
		--prefix) PREFIX="${2:-}"; shift ;;
		--port-base) PORT_BASE="${2:-}"; shift ;;
		--cache) CACHE="${2:-}"; shift ;;
		--results) RESULTS="${2:-}"; shift ;;
		--plan) PLAN=1 ;;
		*) usage ;;
	esac
	shift
done

[[ "$PORT_BASE" =~ ^[1-9][0-9]*$ ]] && [ "$PORT_BASE" -ge 1024 ] && [ $((PORT_BASE + PORT_SPAN)) -le 65535 ] ||
	die "--port-base must be a port from 1024 to $((65535 - PORT_SPAN))"
PREFIX="${PREFIX:-l2-proxy-$PORT_BASE}"
[[ "$PREFIX" =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]] || die "--prefix must be lower-case letters, digits and dashes, at most 31"
[ "$PROFILES" != all ] || PROFILES="${ALL_PROFILES// /,}"
IFS=, read -r -a PROFILE_LIST <<<"$PROFILES"
for profile in "${PROFILE_LIST[@]}"; do
	[[ " $ALL_PROFILES " == *" $profile "* ]] || die "unknown profile $profile (profiles: $ALL_PROFILES, or all)"
done
[ "$UNRESOLVED" = 0 ] || [[ ",$PROFILES," == *",pass-through-ports,"* ]] ||
	die "--unresolved-hostname applies to the pass-through-ports profile only"
case "$EDITION" in
	ce) SERVER_IMAGE="${SERVER_IMAGE:-aerospike/aerospike-server:latest}" ;;
	ee) SERVER_IMAGE="${SERVER_IMAGE:-aerospike/aerospike-server-enterprise:latest}" ;;
	*) die "--edition must be ce or ee" ;;
esac
[[ "$SERVER_IMAGE" =~ ^[A-Za-z0-9._:/@-]+$ ]] || die "--server-image is not an image reference"
[[ "$CLIENT_REPO" =~ ^https://[A-Za-z0-9./_-]+$ ]] || [ -d "$CLIENT_REPO" ] ||
	die "--client-repo must be an https URL or a local repository"
[[ "$COMMIT" =~ ^[A-Za-z0-9._/-]+$ ]] || die "--commit is required"
[[ "$MANIFEST" =~ ^[A-Za-z0-9._/-]+$ && "$MANIFEST" != /* && "$MANIFEST" != *..* ]] ||
	die "--manifest must be a relative path inside the client repository"

CONTROL_PORT=$PORT_BASE
LISTEN_PORT_BASE=$((PORT_BASE + 1))
EXTRA_PORT_BASE=$((PORT_BASE + 10))
RANGE_PORT_BASE=$((PORT_BASE + 100))
SUBNET="10.213.$((PORT_BASE / 100 % 256))"
NODE_NETWORK="$PREFIX-nodes"
CLIENT_NETWORK="$PREFIX-client"
PROXY="$PREFIX-toxiproxy"
CTL_CONTAINER="$PREFIX-ctl"
BUILD_CONTAINER="$PREFIX-build"
CLIENT_IMAGE="localhost/$PREFIX-client:latest"

describe_profile() {
	local profile=$1
	echo "Profile $profile:"
	echo "  networks       $NODE_NETWORK (nodes, no published ports$([ "$profile" = port-map-range ] && echo ", subnet $SUBNET.0/24")),"
	echo "                 $CLIENT_NETWORK (internal$([ "$profile" = low-mtu ] && echo ", MTU $CLIENT_NETWORK_MTU"))"
	echo "  nodes          $PREFIX-node-1..$NODES, $SERVER_IMAGE ($EDITION)$([ "$profile" = port-map-range ] && echo ", on $SUBNET.$RANGE_FIRST upwards")"
	echo "  toxiproxy      $PROXY on both networks, API port $CONTROL_PORT, endpoint $ENDPOINT_HOSTNAME ports $LISTEN_PORT_BASE-$((LISTEN_PORT_BASE + NODES - 1))$([ "$profile" = port-range ] && echo ", net.ipv4.ip_local_port_range $PROXY_PORT_RANGE")"
	case "$profile" in
		reachable) echo "  advertises     node $NODES: access-address $REACHABLE_HOSTNAME and its listener port; the others: private address, alternate $REACHABLE_HOSTNAME and their listener port" ;;
		pass-through-names) echo "  advertises     node N: alternate node-N.$PASS_THROUGH_DOMAIN and its listener port" ;;
		pass-through-ports) echo "  advertises     every node: alternate $PASS_THROUGH_HOSTNAME and its listener port; $([ "$UNRESOLVED" = 1 ] && echo "the name does not resolve in the servers" || echo "the name resolves to Toxiproxy in the servers")" ;;
		port-map-range) echo "  listeners      $SUBNET.$RANGE_FIRST-$RANGE_LAST on $RANGE_PORT_BASE plus the last octet" ;;
		low-mtu) echo "  namespace      max-record-size 8M" ;;
		*) echo "  advertises     private address only" ;;
	esac
	echo "  control        $CTL_CONTAINER (curl on $NODE_NETWORK); new-node listeners from port $EXTRA_PORT_BASE"
	echo "  driver         $PREFIX-driver-<pid> from $CLIENT_IMAGE on $CLIENT_NETWORK only"
	python3 "$SCRIPT_DIR/rows.py" --plan --profile "$profile" | sed 's/^L2-PLAN-ROW profile=[^ ]* /  row            /'
}

if [ "$PLAN" = 1 ]; then
	echo "Plan, nothing created:"
	echo "  client         $CLIENT_REPO $COMMIT $MANIFEST, built in $BUILD_IMAGE as $CLIENT_IMAGE"
	echo "  prefix         $PREFIX, ports $PORT_BASE-$((PORT_BASE + PORT_SPAN - 1))"
	echo "  rows           ${ROWS:-every row of each profile}$([ "$NO_FAULT" = 1 ] && echo "; control run, fault rows without their fault")"
	for profile in "${PROFILE_LIST[@]}"; do
		describe_profile "$profile"
	done
	exit 0
fi

((BASH_VERSINFO[0] >= 4)) || die "bash 4 or later is required by setup-discovery-topology"
for cmd in docker git jq python3 curl tar; do
	command -v "$cmd" >/dev/null 2>&1 || die "$cmd not found"
done
docker info >/dev/null 2>&1 || die "the container runtime is not reachable through docker"
if [ "$EDITION" = ee ]; then
	[ -n "${FEATURES_CONF_PATH:-}" ] || die "--edition ee needs FEATURES_CONF_PATH"
	[ -r "$FEATURES_CONF_PATH" ] || die "FEATURES_CONF_PATH is not readable"
fi

taken="$(docker ps -a --format '{{.Names}}' | grep -E "^$PREFIX-" || true)$(docker network ls --format '{{.Name}}' | grep -E "^$PREFIX-" || true)"
[ -z "$taken" ] || die "containers or networks named $PREFIX-* exist, another run may be active: $(echo "$taken" | paste -sd' ' -)"
busy="$(python3 - "$PORT_BASE" "$PORT_SPAN" <<'EOF'
import socket, sys
base, span = int(sys.argv[1]), int(sys.argv[2])
busy = []
for port in range(base, base + span):
	with socket.socket() as s:
		try:
			s.bind(("127.0.0.1", port))
		except OSError:
			busy.append(str(port))
print(",".join(busy))
EOF
)"
published="$(docker ps --format '{{.Ports}}' | grep -oE ':[0-9]+->' | tr -d ':>-' | sort -u |
	awk -v lo="$PORT_BASE" -v hi="$((PORT_BASE + PORT_SPAN - 1))" '$1 >= lo && $1 <= hi' | paste -sd, -)" || published=""
[ -z "$busy$published" ] || die "ports $PORT_BASE-$((PORT_BASE + PORT_SPAN - 1)) are not free (bound: ${busy:-none}; published by a container: ${published:-none}); use another --port-base"
for network in $(docker network ls -q); do
	if docker network inspect "$network" -f '{{range .IPAM.Config}}{{.Subnet}} {{end}}{{range .Subnets}}{{.Subnet}} {{end}}' 2>/dev/null |
		grep -qE "(^| )$SUBNET\."; then
		die "subnet $SUBNET.0/24 is in use by network $network; use another --port-base"
	fi
done

RESULTS="${RESULTS:-$SCRIPT_DIR/results/$PREFIX-$(date -u +%Y%m%d-%H%M%S)}"
mkdir -p "$RESULTS"
RESULTS="$(cd "$RESULTS" && pwd -P)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/l2-proxy.XXXXXX")"
WORK="$(cd "$WORK" && pwd -P)"
mkdir -p "$WORK/bin" "$WORK/action"

secret_scan() {
	local files=0 hits=0 f
	[ "$EDITION" = ee ] || return 0
	while IFS= read -r -d '' f; do
		files=$((files + 1))
		if grep -qF -f <(awk 'length($0) >= 24' "$FEATURES_CONF_PATH") "$f"; then
			hits=$((hits + 1))
			echo "FEATURE KEY CONTENT FOUND in $f" >&2
		fi
	done < <(find "$RESULTS" -type f -print0)
	echo "Secret scan: $hits of $files result files contain feature-key content"
	[ $hits -eq 0 ]
}

remove_topology() {
	local c n
	for c in $(docker ps -aq --filter "name=^$PREFIX-" 2>/dev/null); do
		docker rm -f "$c" >/dev/null 2>&1 || true
	done
	for n in "$CLIENT_NETWORK" "$NODE_NETWORK"; do
		docker network rm "$n" >/dev/null 2>&1 || true
	done
}

cleanup() {
	local rc=$? left
	trap - EXIT
	trap '' INT TERM
	set +e
	remove_topology
	docker image rm -f "$CLIENT_IMAGE" >/dev/null 2>&1
	if [ -d "$WORK/client" ] && ! rm -rf "$WORK/client" 2>/dev/null; then
		docker run --rm --user 0 -v "$WORK:/work" --entrypoint rm "$CURL_IMAGE" -rf /work/client >/dev/null 2>&1
	fi
	rm -rf "$WORK"
	left="$(docker ps -a --format '{{.Names}}' | grep -E "^$PREFIX-"; docker network ls --format '{{.Name}}' | grep -E "^$PREFIX-";
		docker images --format '{{.Repository}}' | grep -E "/$PREFIX-client$")"
	if [ -n "$left" ]; then
		echo "Left after cleanup: $(echo "$left" | paste -sd' ' -)" | tee "$RESULTS/cleanup.txt"
		rc=1
	else
		echo "Left after cleanup: none (no container, network or image named $PREFIX-*)" | tee "$RESULTS/cleanup.txt"
	fi
	secret_scan || rc=1
	echo "Results: $RESULTS"
	[ $rc -eq 0 ] && echo "RUN PASS" || echo "RUN FAIL"
	exit $rc
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for f in entrypoint.sh proxy_map.sh; do
	if [ -n "${SHARED_WORKFLOWS_DIR:-}" ]; then
		git -C "$SHARED_WORKFLOWS_DIR" show "$SHARED_WORKFLOWS_COMMIT:$ACTION_PATH/$f" >"$WORK/action/$f" ||
			die "$f not found at $SHARED_WORKFLOWS_COMMIT in $SHARED_WORKFLOWS_DIR"
	else
		curl -sfL "https://raw.githubusercontent.com/aerospike/shared-workflows/$SHARED_WORKFLOWS_COMMIT/$ACTION_PATH/$f" \
			-o "$WORK/action/$f" || die "cannot download $ACTION_PATH/$f at $SHARED_WORKFLOWS_COMMIT"
	fi
done
# setup-discovery-topology calls Toxiproxy's API with curl at its node network address.
cat >"$WORK/bin/curl" <<EOF
#!/usr/bin/env bash
exec docker exec -i "$CTL_CONTAINER" curl "\$@"
EOF
chmod +x "$WORK/bin/curl"

echo "Prefix:  $PREFIX, ports $PORT_BASE-$((PORT_BASE + PORT_SPAN - 1))"
echo "Server:  $SERVER_IMAGE ($EDITION)"
echo "Client:  $CLIENT_REPO $COMMIT $MANIFEST"
for image in "$SERVER_IMAGE" "$TOXIPROXY_IMAGE" "$CURL_IMAGE" "$BUILD_IMAGE"; do
	docker image inspect "$image" >/dev/null 2>&1 || docker pull -q "$image" >/dev/null || die "cannot pull $image"
done

echo "Checking out the client"
mkdir -p "$WORK/client"
if [ -d "$CLIENT_REPO" ]; then
	sha="$(git -C "$CLIENT_REPO" rev-parse --verify "$COMMIT^{commit}")" || die "$COMMIT is not a commit in $CLIENT_REPO"
	git -C "$CLIENT_REPO" archive --format=tar "$sha" | tar -x -C "$WORK/client" || die "cannot export $sha"
else
	git clone -q "$CLIENT_REPO" "$WORK/client" || die "cannot clone $CLIENT_REPO"
	git -C "$WORK/client" checkout -q "$COMMIT" || die "cannot check out $COMMIT"
	sha="$(git -C "$WORK/client" rev-parse HEAD)"
fi
echo "Commit:  $sha" | tee "$RESULTS/client.txt"
[ -f "$WORK/client/$MANIFEST" ] || die "manifest $MANIFEST not found at $sha"
manifest_value() {
	# shellcheck disable=SC1090 # the client's manifest
	(set -a && . "$WORK/client/$MANIFEST" && printf '%s' "${!1:-}")
}
for name in L5_SETUP L5_BUILD L5_DRIVER; do
	[ -n "$(manifest_value "$name")" ] || die "$name is not set in $MANIFEST"
done
L2_RUN="$(manifest_value L2_RUN)"

echo "Building the client in $BUILD_IMAGE (build.log)"
# The cache holds what the build downloads, so the driver containers mount it too.
CACHE_MOUNTS=()
if [ -n "$CACHE" ]; then
	mkdir -p "$CACHE/m2" "$CACHE/cache" "$CACHE/dnf"
	CACHE="$(cd "$CACHE" && pwd -P)"
	CACHE_MOUNTS=(-v "$CACHE/m2:/root/.m2" -v "$CACHE/cache:/root/.cache")
fi
build_args=(--name "$BUILD_CONTAINER" -v "$WORK/client:/client" -w /client "${CACHE_MOUNTS[@]+"${CACHE_MOUNTS[@]}"}")
[ -z "$CACHE" ] || build_args+=(-v "$CACHE/dnf:/var/cache/dnf")
# shellcheck disable=SC2016 # expanded in the build container
if ! docker run "${build_args[@]}" "$BUILD_IMAGE" bash -c 'set -e
echo keepcache=1 >>/etc/dnf/dnf.conf
dnf install -y -q sudo tar gzip findutils which git >/dev/null
set -a
. "./$0"
set +a
bash -c "$L5_SETUP" </dev/null
bash -c "$L5_BUILD" </dev/null' "$MANIFEST" >"$RESULTS/build.log" 2>&1; then
	tail -40 "$RESULTS/build.log" >&2
	die "client setup or build failed; see build.log"
fi
docker commit "$BUILD_CONTAINER" "$CLIENT_IMAGE" >/dev/null || die "cannot save the built client image"
docker rm -f "$BUILD_CONTAINER" >/dev/null

render_base_conf() {
	local profile=$1 conf=$2 feature_key="" max_record="" mesh_seeds="" i
	[ "$EDITION" != ee ] || feature_key="feature-key-file /etc/aerospike/features.conf"
	[ "$profile" != low-mtu ] || max_record="max-record-size 8M"
	for i in $(seq 1 "$NODES"); do
		mesh_seeds+="        mesh-seed-address-port $PREFIX-node-$i 3002"$'\n'
	done
	cat >"$conf" <<EOF
service {
    $feature_key
    cluster-name l2
}

logging {
    console {
        context any info
    }
}

network {
    service {
        address any
        port 3000
    }
    heartbeat {
        mode mesh
        address any
        port 3002
        interval 150
        timeout 10
$mesh_seeds    }
    fabric {
        address any
        port 3001
    }
}

namespace test {
    replication-factor 2
    $max_record
    storage-engine memory {
        data-size 1G
    }
}
EOF
}

container_address() {
	docker inspect -f "{{(index .NetworkSettings.Networks \"$2\").IPAddress}}" "$1"
}

# Write the topology the rows and l2ctl read, and the values file a client's own tests read.
write_topology() {
	local state=$1
	{
		echo "L2_PREFIX=$PREFIX"
		echo "L2_PROFILE=$PROFILE"
		echo "L2_NODE_NETWORK=$NODE_NETWORK"
		echo "L2_CLIENT_NETWORK=$CLIENT_NETWORK"
		echo "L2_CTL_CONTAINER=$CTL_CONTAINER"
		echo "L2_SERVER_IMAGE=$SERVER_IMAGE"
		echo "L2_CURL_IMAGE=$CURL_IMAGE"
		echo "L2_NODE_HOSTS=${NODE_HOSTS:-}"
		echo "L2_CLIENT_HOSTS=${CLIENT_HOSTS:-}"
		echo "L2_PROXY_CLIENT_ADDRESS=${PROXY_CLIENT_ADDRESS:-}"
		echo "L2_ACCESS_HOSTNAME=${ACCESS_HOSTNAME:-}"
		echo "L2_EXTRA_PORT_BASE=$EXTRA_PORT_BASE"
		echo "L2_SUBNET=$SUBNET"
		echo "L2_OUTSIDE_OCTET=$OUTSIDE_OCTET"
		echo "L2_UNRESOLVED=$([ "$PROFILE" = pass-through-ports ] && echo "$UNRESOLVED" || echo 0)"
		cat "$state/values.env"
	} >"$state/topology.env"
}

write_values() {
	local state=$1
	{
		echo "DISCOVERY_SUBSET=$EDITION"
		echo "DISCOVERY_ENDPOINT_HOSTNAME=$(output endpoint-hostname)"
		echo "DISCOVERY_ENDPOINT_PORT_BASE=$(output listen-port-base)"
		echo "DISCOVERY_ENDPOINT_PORTS=$(output endpoint-ports)"
		echo "DISCOVERY_TLS_ENDPOINT_PORTS="
		echo "DISCOVERY_PROXY_MAP=$(output proxy-name-port-map)"
		echo "TOXIPROXY_URL=$(output toxiproxy-url)"
		echo "TOXIPROXY_HOST=$(output toxiproxy-container-name)"
		echo "TOXIPROXY_CONTROL_PORT=$CONTROL_PORT"
		echo "AEROSPIKE_CONTAINER_NAMES=$CONTAINER_NAMES"
		echo "DISCOVERY_REACHABLE_NODE=${REACHABLE_NODE:-}"
		echo "DISCOVERY_PASS_THROUGH_BRANCH=${PASS_THROUGH_BRANCH:-}"
		echo "DISCOVERY_PORT_MAP_RANGE=${PORT_MAP_RANGE:-}"
		echo "DISCOVERY_PORT_MAP_PORT_BASE=${PORT_MAP_PORT_BASE:-}"
		echo "DISCOVERY_CLIENT_NETWORK_MTU=${MTU:-}"
		echo "DISCOVERY_PROXY_PORT_RANGE=${PORT_RANGE:-}"
		echo "DISCOVERY_IDLE_RESET=${IDLE_RESET:-}"
	} >"$state/values.env"
}

# Build one profile's topology into $STATE. Fails the profile on any error.
build_topology() {
	local profile=$1 network_args=() client_args=(--internal) i container port octet hostnames=() proxy_server_address
	local index entry proxies sysctls=""

	MTU=""
	PORT_RANGE=""
	IDLE_RESET=""
	REACHABLE_NODE=""
	PASS_THROUGH_BRANCH=""
	PORT_MAP_RANGE=""
	PORT_MAP_PORT_BASE=""
	NODE_HOSTS=""
	CLIENT_HOSTS=""
	ACCESS_HOSTNAME=""
	case "$profile" in
		port-map-range) network_args=(--subnet "$SUBNET.0/24" --ip-range "$SUBNET.128/25") ;;
		low-mtu)
			MTU=$CLIENT_NETWORK_MTU
			client_args+=(--opt "com.docker.network.driver.mtu=$MTU")
			;;
		port-range)
			PORT_RANGE="$PROXY_PORT_RANGE"
			# The control run leaves Toxiproxy's port range as it is.
			[ "$NO_FAULT" = 1 ] || sysctls="net.ipv4.ip_local_port_range=$PORT_RANGE"
			;;
		idle-reset) IDLE_RESET=1 ;;
	esac
	docker network create "${network_args[@]+"${network_args[@]}"}" "$NODE_NETWORK" >/dev/null
	docker network create "${client_args[@]}" "$CLIENT_NETWORK" >/dev/null
	docker run -d --name "$CTL_CONTAINER" --network "$NODE_NETWORK" --entrypoint sleep "$CURL_IMAGE" infinity >/dev/null

	mkdir -p "$STATE/conf"
	render_base_conf "$profile" "$STATE/conf/aerospike.conf"
	if [ "$EDITION" = ee ]; then
		cp "$FEATURES_CONF_PATH" "$STATE/features.conf"
		chmod 644 "$STATE/features.conf"
	fi
	CONTAINER_NAMES=""
	for i in $(seq 1 "$NODES"); do
		CONTAINER_NAMES+="${CONTAINER_NAMES:+,}$PREFIX-node-$i"
	done
	: >"$STATE/topology.out"
	output() {
		grep -E "^$1=" "$STATE/topology.out" | head -1 | cut -d= -f2-
	}
	write_values "$STATE"
	{
		echo "TOXIPROXY_URL=http://$PROXY:$CONTROL_PORT"
		echo "TOXIPROXY_HOST=$PROXY"
	} >>"$STATE/values.env"
	write_topology "$STATE"

	echo "Starting $NODES nodes on $NODE_NETWORK"
	for i in $(seq 1 "$NODES"); do
		if [ "$profile" = port-map-range ]; then
			"$L2_CTL" start-node --name "$PREFIX-node-$i" --address "$SUBNET.$((RANGE_FIRST + i - 1))"
		else
			"$L2_CTL" start-node --name "$PREFIX-node-$i"
		fi
	done
	"$L2_CTL" wait-cluster "$NODES" --timeout 120

	echo "Setting up the topology with setup-discovery-topology (shared-workflows $SHARED_WORKFLOWS_COMMIT)"
	PATH="$WORK/bin:$PATH" \
		CONTAINER_NAMES="$CONTAINER_NAMES" \
		AEROSPIKE_NETWORK_NAME="$NODE_NETWORK" \
		CLIENT_NETWORK_NAME="$CLIENT_NETWORK" \
		PROXY_CONTAINER_NAME="$PROXY" \
		ENDPOINT_HOSTNAME="$ENDPOINT_HOSTNAME" \
		LISTEN_PORT_BASE="$LISTEN_PORT_BASE" \
		TOXIPROXY_IMAGE="$TOXIPROXY_IMAGE" \
		TOXIPROXY_CONTROL_PORT="$CONTROL_PORT" \
		TLS_LISTEN_PORT_BASE="" \
		CLIENT_NETWORK_MTU="" \
		PROXY_SYSCTLS="$sysctls" \
		GITHUB_OUTPUT="$STATE/topology.out" \
		bash "$WORK/action/entrypoint.sh" >"$RES/topology.log" 2>&1 || { tail -30 "$RES/topology.log" >&2; return 1; }
	PROXY_CLIENT_ADDRESS="$(container_address "$PROXY" "$CLIENT_NETWORK")"
	proxy_server_address="$(container_address "$PROXY" "$NODE_NETWORK")"
	[ -n "$PROXY_CLIENT_ADDRESS" ] && [ -n "$proxy_server_address" ] || { echo "no Toxiproxy address" >&2; return 1; }
	IFS=, read -r -a proxies <<<"$(output proxy-name-port-map)"

	case "$profile" in
		reachable | pass-through-names | pass-through-ports)
			case "$profile" in
				reachable)
					REACHABLE_NODE="$PREFIX-node-$NODES"
					ACCESS_HOSTNAME=$REACHABLE_HOSTNAME
					hostnames=("$REACHABLE_HOSTNAME")
					;;
				pass-through-names) PASS_THROUGH_BRANCH=A ;;
				pass-through-ports)
					PASS_THROUGH_BRANCH=B
					ACCESS_HOSTNAME=$PASS_THROUGH_HOSTNAME
					hostnames=("$PASS_THROUGH_HOSTNAME")
					;;
			esac
			[ "$profile" != pass-through-names ] || for i in $(seq 1 "$NODES"); do
				hostnames+=("node-$i.$PASS_THROUGH_DOMAIN")
			done
			for entry in "${hostnames[@]}"; do
				[ "$profile" = pass-through-ports ] && [ "$UNRESOLVED" = 1 ] ||
					NODE_HOSTS+="${NODE_HOSTS:+,}$entry:$proxy_server_address"
				CLIENT_HOSTS+="${CLIENT_HOSTS:+,}$entry:$PROXY_CLIENT_ADDRESS"
			done
			write_values "$STATE"
			write_topology "$STATE"
			echo "Recreating the nodes with their access addresses"
			index=0
			for entry in "${proxies[@]}"; do
				container="${entry%:*}"
				port="${entry##*:}"
				index=$((index + 1))
				"$L2_CTL" stop-node "$container" >/dev/null
				case "$profile" in
					reachable)
						if [ "$container" = "$REACHABLE_NODE" ]; then
							"$L2_CTL" start-node --name "$container" --access "$REACHABLE_HOSTNAME:$port"
						else
							"$L2_CTL" start-node --name "$container" --alternate "$REACHABLE_HOSTNAME:$port"
						fi
						;;
					pass-through-names) "$L2_CTL" start-node --name "$container" --alternate "node-$index.$PASS_THROUGH_DOMAIN:$port" ;;
					pass-through-ports) "$L2_CTL" start-node --name "$container" --alternate "$PASS_THROUGH_HOSTNAME:$port" ;;
				esac
			done
			if ! "$L2_CTL" wait-cluster "$NODES" --timeout 120; then
				for entry in "${proxies[@]}"; do
					[ "$(docker inspect -f '{{.State.Running}}' "${entry%:*}" 2>/dev/null)" = true ] ||
						echo "Node ${entry%:*} exited: $(docker logs "${entry%:*}" 2>&1 | grep -m1 -E 'CRITICAL|Invalid access address')"
				done
				[ "$UNRESOLVED" = 0 ] || echo "The server resolves its access addresses at startup and does not start when they do not resolve; --unresolved-hostname needs a server that advertises them verbatim"
				return 1
			fi
			;;
		port-map-range)
			PORT_MAP_RANGE="$SUBNET.$RANGE_FIRST-$SUBNET.$RANGE_LAST"
			PORT_MAP_PORT_BASE=$RANGE_PORT_BASE
			for octet in $(seq "$RANGE_FIRST" "$RANGE_LAST"); do
				"$L2_CTL" listener-add "range-$SUBNET.$octet" "$((RANGE_PORT_BASE + octet))" "$SUBNET.$octet:3000" >/dev/null
			done
			;;
	esac
	write_values "$STATE"
	write_topology "$STATE"
	cp "$STATE/values.env" "$RES/values.env"
}

# The driver wrapper rows.py starts: the manifest's L5_DRIVER in a container on the client network only.
write_driver() {
	local hosts=() entry
	IFS=, read -r -a entries <<<"${CLIENT_HOSTS:-}"
	for entry in "${entries[@]+"${entries[@]}"}"; do
		hosts+=(--add-host "$entry")
	done
	{
		echo '#!/usr/bin/env bash'
		printf 'exec docker run --rm -i --name "%s-driver-$$" --network %q' "$PREFIX" "$CLIENT_NETWORK"
		[ ${#hosts[@]} -eq 0 ] || printf ' %q' "${hosts[@]}"
		[ ${#CACHE_MOUNTS[@]} -eq 0 ] || printf ' %q' "${CACHE_MOUNTS[@]}"
		printf ' -v %q -w /client %q' "$WORK/client:/client" "$CLIENT_IMAGE"
		# shellcheck disable=SC2016 # expanded in the driver container
		printf ' bash -c %q %q "$@"\n' 'set -a; . "./$0"; set +a; eval "$L5_DRIVER \"\$@\""' "$MANIFEST"
	} >"$STATE/driver"
	chmod +x "$STATE/driver"
}

export L2_CTL="$SCRIPT_DIR/l2ctl"
failed=0
: >"$RESULTS/rows.txt"
for PROFILE in "${PROFILE_LIST[@]}"; do
	echo "Profile $PROFILE"
	STATE="$WORK/$PROFILE"
	RES="$RESULTS/$PROFILE"
	mkdir -p "$STATE" "$RES"
	export L2_STATE="$STATE"
	set +e
	(set -e; build_topology "$PROFILE") >"$RES/build-topology.log" 2>&1
	topology_rc=$?
	set -e
	if [ "$topology_rc" -ne 0 ]; then
		tail -20 "$RES/build-topology.log" >&2
		echo "PROFILE $PROFILE NOT RUN: the topology could not be built (see $PROFILE/build-topology.log)" | tee -a "$RESULTS/rows.txt"
		docker ps -a --filter "name=^$PREFIX-node-" --format '{{.Names}}' | while read -r c; do
			docker logs "$c" >"$RES/$c.log" 2>&1 || true
		done
		failed=1
		remove_topology
		continue
	fi
	# Values set in the subshell are read back from the topology file.
	CLIENT_HOSTS="$(sed -n 's/^L2_CLIENT_HOSTS=//p' "$STATE/topology.env")"
	write_driver
	"$L2_CTL" status >"$RES/status-before.txt" 2>&1 || true
	row_args=(--profile "$PROFILE" --driver "$STATE/driver")
	[ -z "$ROWS" ] || row_args+=(--rows "$ROWS")
	[ "$NO_FAULT" = 0 ] || row_args+=(--no-fault)
	echo "Running the rows (rows.log)"
	set +e
	python3 "$SCRIPT_DIR/rows.py" "${row_args[@]}" </dev/null >"$RES/rows.log" 2>&1
	rows_rc=$?
	set -e
	grep -E '^L2-ROW ' "$RES/rows.log" | sed 's/^L2-ROW /ROW /' | tee -a "$RESULTS/rows.txt" || true
	if [ "$rows_rc" -ne 0 ]; then
		failed=1
		grep -m1 '^ERROR' "$RES/rows.log" | sed "s/^/PROFILE $PROFILE /" | tee -a "$RESULTS/rows.txt" || true
	fi
	if [ -n "$L2_RUN" ]; then
		echo "Running L2_RUN (l2-run.log)"
		IFS=, read -r -a entries <<<"${CLIENT_HOSTS:-}"
		run_hosts=()
		for entry in "${entries[@]+"${entries[@]}"}"; do
			run_hosts+=(--add-host "$entry")
		done
		set +e
		# shellcheck disable=SC2016 # expanded in the client container
		docker run --rm --name "$PREFIX-driver-run" --network "$CLIENT_NETWORK" "${run_hosts[@]+"${run_hosts[@]}"}" \
			"${CACHE_MOUNTS[@]+"${CACHE_MOUNTS[@]}"}" \
			--env-file "$STATE/values.env" -e L2_PROFILE="$PROFILE" -v "$WORK/client:/client" -w /client "$CLIENT_IMAGE" \
			bash -c 'set -a; . "./$0"; set +a; bash -c "$L2_RUN"' "$MANIFEST" </dev/null >"$RES/l2-run.log" 2>&1
		run_rc=$?
		set -e
		if [ "$run_rc" -eq 0 ]; then
			echo "L2_RUN $PROFILE PASS" | tee -a "$RESULTS/rows.txt"
		else
			echo "L2_RUN $PROFILE FAIL exit=$run_rc (see $PROFILE/l2-run.log)" | tee -a "$RESULTS/rows.txt"
			failed=1
		fi
	fi
	"$L2_CTL" status >"$RES/status-after.txt" 2>&1 || true
	for c in $(docker ps -a --filter "name=^$PREFIX-node-" --format '{{.Names}}'); do
		docker logs "$c" >"$RES/$c.log" 2>&1 || true
	done
	docker logs "$PROXY" >"$RES/toxiproxy.log" 2>&1 || true
	remove_topology
done

[ "$failed" -eq 0 ] || die "one or more rows or profiles failed"
