#!/usr/bin/env bash
#
# L3 for one client, on one machine with kind and a container runtime (docker, or podman in rootful mode
# with KIND_EXPERIMENTAL_PROVIDER=podman). For each profile it creates a kind cluster and runs
# aerospike/shared-workflows setup-kind-ako's entrypoint at a pinned commit, which installs the Aerospike
# Kubernetes Operator and an AerospikeCluster whose pods advertise their alternate addresses; per-node
# builds what the action cannot (one pod per labelled worker), and the TLS profiles then serve TLS through
# l3ctl. It builds the client once with the manifest's L5_SETUP and L5_BUILD in an Amazon Linux 2023
# container, then runs rows.py for the profile, which starts the manifest's L5_DRIVER in a container that
# resolves the endpoint hostnames to kind nodes, makes every Kubernetes change through l3ctl and decides
# every row. Every kind cluster, container and image the run creates is removed on exit, including on
# failure, and the run fails if any is left.
#
# The driver container joins the kind network by default, which reaches every kind node on Linux and on a
# Podman machine alike; --driver-network host runs it on the host network instead, which on a Linux host
# reaches the kind node addresses directly. --plan prints each profile's cluster, AKO version and rows and
# creates nothing.
#
# Optional environment:
#   FEATURES_CONF_PATH          Enterprise feature-key file (required unless --plan); never printed
#   KIND_EXPERIMENTAL_PROVIDER  podman runs kind and every container on podman
#
# Usage: l3-kind/run.sh --client-repo <https url or local path> --commit <commit> --manifest <path>
#                       [--profile default,...|all] [--rows ROW,...] [--ako-version V] [--ako-helm-repo URL]
#                       [--cluster-chart-version V] [--driver-network kind|host] [--prefix <prefix>]
#                       [--shared-workflows <local clone>] [--cache <dir>] [--results <dir>] [--plan]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ALL_PROFILES="default per-node tls nlb nlb-per-pod nlb-tls"
SHARED_WORKFLOWS_COMMIT=3ad1bc4b8c4ca7e5b7661aab5cd9151def0e763a
ACTION_PATH=.github/actions/setup-kind-ako
ACTION_FILES="entrypoint.sh action.yaml deps/Chart.yaml templates/aerospike-cluster-values.yaml
templates/aerospike-cluster-tls-values.yaml templates/ingress-values.yaml templates/rbac.yaml"
BUILD_IMAGE=public.ecr.aws/amazonlinux/amazonlinux:2023
CLUSTER_SIZE=2
PER_NODE_DOMAIN=as-endpoint.test
TLS_ENDPOINT_HOSTNAME=as-endpoint-tls.test
KIND_NETWORK=kind

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
AKO_VERSION_OPTION="unset"
AKO_HELM_REPO_OPTION="unset"
CLUSTER_CHART_OPTION="unset"
DRIVER_NETWORK=kind
PREFIX=l3-kind
SHARED_WORKFLOWS_DIR=""
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
		--ako-version) AKO_VERSION_OPTION="${2:-}"; shift ;;
		--ako-helm-repo) AKO_HELM_REPO_OPTION="${2:-}"; shift ;;
		--cluster-chart-version) CLUSTER_CHART_OPTION="${2:-}"; shift ;;
		--driver-network) DRIVER_NETWORK="${2:-}"; shift ;;
		--prefix) PREFIX="${2:-}"; shift ;;
		--shared-workflows) SHARED_WORKFLOWS_DIR="${2:-}"; shift ;;
		--cache) CACHE="${2:-}"; shift ;;
		--results) RESULTS="${2:-}"; shift ;;
		--plan) PLAN=1 ;;
		*) usage ;;
	esac
	shift
done

[ "$PROFILES" != all ] || PROFILES="${ALL_PROFILES// /,}"
IFS=, read -r -a PROFILE_LIST <<<"$PROFILES"
for profile in "${PROFILE_LIST[@]}"; do
	[[ " $ALL_PROFILES " == *" $profile "* ]] || die "unknown profile $profile (profiles: $ALL_PROFILES, or all)"
done
[[ "$PREFIX" =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]] || die "--prefix must be lower-case letters, digits and dashes, at most 31"
[[ "$DRIVER_NETWORK" =~ ^(kind|host)$ ]] || die "--driver-network must be kind or host"
[ "$AKO_VERSION_OPTION" = unset ] || [[ "$AKO_VERSION_OPTION" =~ ^(latest|[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?)?$ ]] ||
	die "--ako-version must be latest or a chart version"
[ "$AKO_HELM_REPO_OPTION" = unset ] || [[ "$AKO_HELM_REPO_OPTION" =~ ^(https://[A-Za-z0-9./_~-]+)?$ ]] ||
	die "--ako-helm-repo must be an https URL"
[ "$CLUSTER_CHART_OPTION" = unset ] || [[ "$CLUSTER_CHART_OPTION" =~ ^([0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?)?$ ]] ||
	die "--cluster-chart-version must be a chart version"
if [ "$PLAN" = 0 ]; then
	[[ "$CLIENT_REPO" =~ ^https://[A-Za-z0-9./_-]+$ ]] || [ -d "$CLIENT_REPO" ] ||
		die "--client-repo must be an https URL or a local repository"
	[[ "$COMMIT" =~ ^[A-Za-z0-9._/-]+$ ]] || die "--commit is required"
	[[ "$MANIFEST" =~ ^[A-Za-z0-9._/-]+$ && "$MANIFEST" != /* && "$MANIFEST" != *..* ]] ||
		die "--manifest must be a relative path inside the client repository"
fi

DOCKER=docker
[ "${KIND_EXPERIMENTAL_PROVIDER:-}" != podman ] || DOCKER=podman
BUILD_CONTAINER="$PREFIX-build"
CLIENT_IMAGE="localhost/$PREFIX-client:latest"

# Read one file of the action at the pinned commit to stdout.
action_file() {
	if [ -n "$SHARED_WORKFLOWS_DIR" ]; then
		git -C "$SHARED_WORKFLOWS_DIR" show "$SHARED_WORKFLOWS_COMMIT:$ACTION_PATH/$1"
	else
		curl -sfL "https://raw.githubusercontent.com/aerospike/shared-workflows/$SHARED_WORKFLOWS_COMMIT/$ACTION_PATH/$1"
	fi
}

# The AKO chart, Helm repository and cluster chart a profile installs. On networkLoadBalancer profiles the
# defaults are the one AKO build with networkLoadBalancer alternate access, which has no cluster chart of its
# own; elsewhere they are empty, which takes the action's pins.
ako_settings() {
	case "$1" in
		nlb*)
			AKO_VERSION=4.8.0-dev1
			AKO_HELM_REPO=https://aerospike.github.io/aerospike-kubernetes-operator
			CLUSTER_CHART_VERSION=4.7.0
			;;
		*)
			AKO_VERSION=""
			AKO_HELM_REPO=""
			CLUSTER_CHART_VERSION=""
			;;
	esac
	[ "$AKO_VERSION_OPTION" = unset ] || AKO_VERSION=$AKO_VERSION_OPTION
	[ "$AKO_HELM_REPO_OPTION" = unset ] || AKO_HELM_REPO=$AKO_HELM_REPO_OPTION
	[ "$CLUSTER_CHART_OPTION" = unset ] || CLUSTER_CHART_VERSION=$CLUSTER_CHART_OPTION
}

# The AKO version as the action resolves it: empty takes deps/Chart.yaml, latest is resolved at setup.
resolved_ako() {
	local pinned='.dependencies[] | select(.name == "aerospike-kubernetes-operator")' chart version repo
	if [ -n "$AKO_VERSION" ]; then
		echo "$AKO_VERSION from ${AKO_HELM_REPO:-the repository setup-kind-ako pins}"
	elif chart="$(action_file deps/Chart.yaml 2>/dev/null)" && [ -n "$chart" ]; then
		version="$(yq "$pinned | .version" <<<"$chart")"
		repo="$(yq "$pinned | .repository" <<<"$chart")"
		echo "$version from ${AKO_HELM_REPO:-$repo} (pinned in setup-kind-ako deps/Chart.yaml)"
	else
		echo "pinned in setup-kind-ako deps/Chart.yaml at $SHARED_WORKFLOWS_COMMIT (not readable here)"
	fi
}

profile_shape() {
	ALTERNATE_ACCESS=configuredIP
	MULTI_POD_PER_HOST=true
	TLS=0
	case "$1" in
		tls) TLS=1 ;;
		nlb) ALTERNATE_ACCESS=networkLoadBalancer ;;
		nlb-per-pod) ALTERNATE_ACCESS=networkLoadBalancer MULTI_POD_PER_HOST=false ;;
		nlb-tls) ALTERNATE_ACCESS=networkLoadBalancer TLS=1 ;;
		per-node) MULTI_POD_PER_HOST=false ;;
	esac
}

describe_profile() {
	local profile=$1 nodes
	profile_shape "$profile"
	ako_settings "$profile"
	case "$profile" in
		per-node) nodes="control-plane (tainted) and $CLUSTER_SIZE workers, worker N labelled node-N.$PER_NODE_DOMAIN" ;;
		nlb-per-pod) nodes="control-plane and $((CLUSTER_SIZE + 1)) workers, one spare" ;;
		*) nodes="one control-plane node, every pod on it" ;;
	esac
	echo "Profile $profile:"
	echo "  kind cluster   $PREFIX-$profile: $nodes"
	echo "  ako            $(resolved_ako), cluster chart ${CLUSTER_CHART_VERSION:-(the AKO version)}"
	echo "  cluster        $CLUSTER_SIZE pods, alternateAccess $ALTERNATE_ACCESS, multiPodPerHost $MULTI_POD_PER_HOST$([ "$TLS" = 1 ] && echo ", TLS with tls-name aerospike-tls")"
	case "$profile" in
		nlb-tls) echo "  endpoints      as-endpoint.test (clear), $TLS_ENDPOINT_HOSTNAME (TLS)" ;;
		per-node) echo "  endpoints      node-1.$PER_NODE_DOMAIN .. node-$CLUSTER_SIZE.$PER_NODE_DOMAIN" ;;
		*) echo "  endpoints      as-endpoint.test" ;;
	esac
	echo "  driver         $PREFIX-driver-<pid> from $CLIENT_IMAGE on the $([ "$DRIVER_NETWORK" = kind ] && echo "$KIND_NETWORK network" || echo "host network")"
	python3 "$SCRIPT_DIR/rows.py" --plan --profile "$profile" | sed "s/^L3-PLAN-ROW profile=[^ ]* row=/  row            /"
}

if [ "$PLAN" = 1 ]; then
	echo "Plan, nothing created:"
	echo "  client         ${CLIENT_REPO:-(--client-repo)} ${COMMIT:-(--commit)} ${MANIFEST:-(--manifest)}, built in $BUILD_IMAGE as $CLIENT_IMAGE"
	echo "  action         setup-kind-ako at shared-workflows $SHARED_WORKFLOWS_COMMIT"
	echo "  rows           ${ROWS:-every row of each profile}"
	for profile in "${PROFILE_LIST[@]}"; do
		describe_profile "$profile"
	done
	exit 0
fi

((BASH_VERSINFO[0] >= 4)) || die "bash 4 or later is required by setup-kind-ako"
for cmd in "$DOCKER" kind kubectl helm yq jq openssl git python3 curl tar; do
	command -v "$cmd" >/dev/null 2>&1 || die "$cmd not found"
done
"$DOCKER" info >/dev/null 2>&1 || die "$DOCKER is not reachable"
[ -n "${FEATURES_CONF_PATH:-}" ] || die "FEATURES_CONF_PATH is required: the AerospikeCluster runs Enterprise"
[ -r "$FEATURES_CONF_PATH" ] || die "FEATURES_CONF_PATH is not readable"
for profile in "${PROFILE_LIST[@]}"; do
	! kind get clusters 2>/dev/null | grep -qx "$PREFIX-$profile" || die "kind cluster $PREFIX-$profile exists, another run may be active"
done
taken="$("$DOCKER" ps -a --format '{{.Names}}' | grep -E "^$PREFIX-" || true)"
[ -z "$taken" ] || die "containers named $PREFIX-* exist, another run may be active: $(echo "$taken" | paste -sd' ' -)"

RESULTS="${RESULTS:-$SCRIPT_DIR/results/$PREFIX-$(date -u +%Y%m%d-%H%M%S)}"
mkdir -p "$RESULTS"
RESULTS="$(cd "$RESULTS" && pwd -P)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/l3-kind.XXXXXX")"
WORK="$(cd "$WORK" && pwd -P)"
mkdir -p "$WORK/action/deps" "$WORK/action/templates"
KIND_NETWORK_EXISTED=0
"$DOCKER" network inspect "$KIND_NETWORK" >/dev/null 2>&1 && KIND_NETWORK_EXISTED=1

secret_scan() {
	local files=0 hits=0 f patterns="$WORK/patterns"
	awk 'length($0) >= 24' "$FEATURES_CONF_PATH" >"$patterns"
	cat "$WORK"/*/admin-password >>"$patterns" 2>/dev/null || true
	while IFS= read -r -d '' f; do
		files=$((files + 1))
		if [ -s "$patterns" ] && grep -qF -f "$patterns" "$f"; then
			hits=$((hits + 1))
			echo "SECRET CONTENT FOUND in $f" >&2
		fi
	done < <(find "$RESULTS" -type f -print0)
	echo "Secret scan: $hits of $files result files contain feature-key or admin password content"
	[ $hits -eq 0 ]
}

remove_cluster() {
	local c
	for c in $("$DOCKER" ps -aq --filter "name=^$PREFIX-driver-" 2>/dev/null); do
		"$DOCKER" rm -f "$c" >/dev/null 2>&1 || true
	done
	kind delete cluster --name "$1" >/dev/null 2>&1 || true
}

cleanup() {
	local rc=$? left profile
	trap - EXIT
	trap '' INT TERM
	set +e
	for profile in "${PROFILE_LIST[@]}"; do
		remove_cluster "$PREFIX-$profile"
	done
	for c in $("$DOCKER" ps -aq --filter "name=^$PREFIX-" 2>/dev/null); do
		"$DOCKER" rm -f "$c" >/dev/null 2>&1
	done
	"$DOCKER" image rm -f "$CLIENT_IMAGE" >/dev/null 2>&1
	if [ "$KIND_NETWORK_EXISTED" = 0 ] && ! kind get clusters 2>/dev/null | grep -qv '^No kind clusters'; then
		"$DOCKER" network rm "$KIND_NETWORK" >/dev/null 2>&1
	fi
	if [ -d "$WORK/client" ] && ! rm -rf "$WORK/client" 2>/dev/null; then
		"$DOCKER" run --rm --user 0 -v "$WORK:/work" --entrypoint rm "$BUILD_IMAGE" -rf /work/client >/dev/null 2>&1
	fi
	secret_scan || rc=1
	rm -rf "$WORK"
	left="$(kind get clusters 2>/dev/null | grep -E "^$PREFIX-"; "$DOCKER" ps -a --format '{{.Names}}' | grep -E "^$PREFIX-";
		"$DOCKER" images --format '{{.Repository}}' | grep -E "/$PREFIX-client$")"
	if [ -n "$left" ]; then
		echo "Left after cleanup: $(echo "$left" | paste -sd' ' -)" | tee "$RESULTS/cleanup.txt"
		rc=1
	else
		echo "Left after cleanup: none (no kind cluster, container or image named $PREFIX-*)" | tee "$RESULTS/cleanup.txt"
	fi
	{
		echo "kind get clusters:"
		kind get clusters 2>&1
		echo "$DOCKER ps -a:"
		"$DOCKER" ps -a --format '{{.Names}} {{.Image}} {{.Status}}' 2>&1
	} >>"$RESULTS/cleanup.txt"
	echo "Results: $RESULTS"
	[ $rc -eq 0 ] && echo "RUN PASS" || echo "RUN FAIL"
	exit $rc
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for f in $ACTION_FILES; do
	action_file "$f" >"$WORK/action/$f" && [ -s "$WORK/action/$f" ] ||
		die "cannot read $ACTION_PATH/$f at $SHARED_WORKFLOWS_COMMIT"
done
# The action adds the endpoint hostname to the runner's /etc/hosts with sudo; the driver container gets it
# through --add-host instead.
sed '/| sudo tee -a \/etc\/hosts/d' "$WORK/action/entrypoint.sh" >"$WORK/action/entrypoint-local.sh"
! grep -nE 'sudo|/etc/hosts' "$WORK/action/entrypoint-local.sh" ||
	die "entrypoint.sh at $SHARED_WORKFLOWS_COMMIT still writes /etc/hosts after removing its hosts step"
grep -q '^cmd_setup()' "$WORK/action/entrypoint-local.sh" || die "entrypoint.sh at $SHARED_WORKFLOWS_COMMIT has no cmd_setup"
KIND_NODE_IMAGE="$(yq '.inputs."kind-node-image".default' "$WORK/action/action.yaml")"
SERVER_IMAGE="$(yq '.inputs."server-image".default' "$WORK/action/action.yaml")"
[[ "$KIND_NODE_IMAGE" =~ ^[A-Za-z0-9._:/@-]+$ && "$SERVER_IMAGE" =~ ^[A-Za-z0-9._:/-]+$ ]] ||
	die "no kind-node-image or server-image default in action.yaml"

echo "Prefix:  $PREFIX"
echo "Action:  setup-kind-ako at $SHARED_WORKFLOWS_COMMIT, $KIND_NODE_IMAGE, $SERVER_IMAGE"
echo "Client:  $CLIENT_REPO $COMMIT $MANIFEST"
echo "Driver:  on the $DRIVER_NETWORK network ($DOCKER)"
# The server image also carries the probe l3ctl runs on the driver's network.
for image in "$BUILD_IMAGE" "$SERVER_IMAGE"; do
	"$DOCKER" image inspect "$image" >/dev/null 2>&1 || "$DOCKER" pull -q "$image" >/dev/null || die "cannot pull $image"
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

echo "Building the client in $BUILD_IMAGE (build.log)"
CACHE_MOUNTS=()
if [ -n "$CACHE" ]; then
	mkdir -p "$CACHE/m2" "$CACHE/cache" "$CACHE/dnf"
	CACHE="$(cd "$CACHE" && pwd -P)"
	CACHE_MOUNTS=(-v "$CACHE/m2:/root/.m2" -v "$CACHE/cache:/root/.cache")
fi
build_args=(--name "$BUILD_CONTAINER" -v "$WORK/client:/client" -w /client "${CACHE_MOUNTS[@]+"${CACHE_MOUNTS[@]}"}")
[ -z "$CACHE" ] || build_args+=(-v "$CACHE/dnf:/var/cache/dnf")
# shellcheck disable=SC2016 # expanded in the build container
if ! "$DOCKER" run "${build_args[@]}" "$BUILD_IMAGE" bash -c 'set -e
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
"$DOCKER" commit "$BUILD_CONTAINER" "$CLIENT_IMAGE" >/dev/null 2>&1 || die "cannot save the built client image"
"$DOCKER" rm -f "$BUILD_CONTAINER" >/dev/null

output() {
	sed -n "s/^$1=//p" "$2" | head -1
}

# One pod per kind worker, each worker labelled with its own hostname and CoreDNS resolving it there, which
# the action refuses under configuredIP; the operator and the AerospikeCluster come from its functions.
setup_per_node() {
	(
		# shellcheck source=/dev/null
		source "$WORK/action/entrypoint-local.sh"
		# The pods publish hostPorts and have no per-pod Service to patch.
		# shellcheck disable=SC2329 # called by the action's deploy_cluster
		serve_node_ports_on_every_node() { :; }
		local workers i name ip hosts_block corefile
		mkdir -p "$WORK_DIR"
		mapfile -t workers < <(kubectl get nodes -l '!node-role.kubernetes.io/control-plane' \
			-o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | sort)
		((${#workers[@]} == CLUSTER_SIZE)) || error "expected $CLUSTER_SIZE kind workers, found ${workers[*]}"
		hosts_block="    hosts {"$'\n'
		for i in "${!workers[@]}"; do
			name="node-$((i + 1)).$PER_NODE_DOMAIN"
			ip="$(kubectl get node "${workers[$i]}" -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')"
			kubectl label node "${workers[$i]}" "${ALTERNATE_ACCESS_LABEL}=${name}" --overwrite
			hosts_block+="        $ip $name"$'\n'
		done
		hosts_block+="        fallthrough"$'\n'"    }"
		corefile="$(kubectl -n kube-system get cm coredns -o jsonpath='{.data.Corefile}')"
		BLOCK="$hosts_block" awk '
			{ print }
			! done && /^\.:53[[:space:]]*\{[[:space:]]*$/ { print ENVIRON["BLOCK"]; done = 1 }
			END { exit done ? 0 : 1 }
		' <<<"$corefile" >"$WORK_DIR/Corefile" || error "Corefile has no '.:53 {' server block"
		kubectl -n kube-system create configmap coredns --from-file="Corefile=$WORK_DIR/Corefile" \
			--dry-run=client -o yaml | kubectl apply -f -
		kubectl -n kube-system rollout restart deploy/coredns
		kubectl -n kube-system rollout status deploy/coredns --timeout=180s
		apply_pinned_defaults
		run_group "Install cert-manager and AKO" install_ako
		run_group "Apply AerospikeCluster" deploy_cluster
	)
}

# Create the profile's kind cluster and AerospikeCluster into $STATE. Fails the profile on any error.
build_cluster() {
	local profile=$1 kind_args=() kind_config password corefile names=() name address hosts="" crs ns cr endpoint status resolved
	local tls_name="" ca_file="" tls_endpoint="" tls_args=()
	profile_shape "$profile"
	ako_settings "$profile"
	export KUBECONFIG="$STATE/kubeconfig" RUNNER_TEMP="$STATE"
	export HELM_CONFIG_HOME="$STATE/helm/config" HELM_CACHE_HOME="$STATE/helm/cache" HELM_DATA_HOME="$STATE/helm/data"
	export AKO_HELM_REPO AKO_VERSION ALTERNATE_ACCESS CLUSTER_CHART_VERSION MULTI_POD_PER_HOST KIND_NODE_IMAGE SERVER_IMAGE
	export CLUSTER_SIZE ENABLE_TLS=false
	[ "$profile" != per-node ] || MULTI_POD_PER_HOST=true
	FEATURES_CONTENT="$(cat "$FEATURES_CONF_PATH")" ADMIN_PASSWORD="" GITHUB_OUTPUT="$STATE/prepare.out" \
		bash "$WORK/action/entrypoint-local.sh" prepare >/dev/null
	[ "$profile" != per-node ] || MULTI_POD_PER_HOST=false
	password="$(output admin-password "$STATE/prepare.out")"
	(umask 077 && printf '%s' "$password" >"$STATE/admin-password")
	kind_config="$(output kind-config "$STATE/prepare.out")"
	rm -f "$STATE/prepare.out"
	if [ "$profile" = per-node ]; then
		kind_config="$STATE/kind-config.yaml"
		{
			printf 'kind: Cluster\napiVersion: kind.x-k8s.io/v1alpha4\nnodes:\n  - role: control-plane\n'
			for _ in $(seq 1 "$CLUSTER_SIZE"); do
				printf '  - role: worker\n'
			done
		} >"$kind_config"
	fi
	[ -z "$kind_config" ] || kind_args=(--config "$kind_config")

	echo "Creating kind cluster $CLUSTER ($KIND_NODE_IMAGE)"
	kind create cluster --name "$CLUSTER" --image "$KIND_NODE_IMAGE" "${kind_args[@]+"${kind_args[@]}"}" --wait 180s \
		--kubeconfig "$KUBECONFIG"
	echo "Setting up AKO and the AerospikeCluster (setup.log)"
	if [ "$profile" = per-node ]; then
		# Not in an && or || list, so set -e holds inside the subshell.
		set +e
		(
			set -e
			FEATURES_CONTENT="$(cat "$FEATURES_CONF_PATH")" ADMIN_PASSWORD="$password" setup_per_node
		) 2>&1 | sed '/::add-mask::/d' >"$RES/setup.log"
		status=("${PIPESTATUS[@]}")
		set -e
		[ "${status[0]}" = 0 ] || { tail -30 "$RES/setup.log" >&2; return 1; }
		endpoint="node-1.$PER_NODE_DOMAIN"
		for i in $(seq 1 "$CLUSTER_SIZE"); do
			names+=("node-$i.$PER_NODE_DOMAIN")
		done
	else
		FEATURES_CONTENT="$(cat "$FEATURES_CONF_PATH")" ADMIN_PASSWORD="$password" GITHUB_OUTPUT="$STATE/setup.out" \
			bash "$WORK/action/entrypoint-local.sh" setup 2>&1 | sed '/::add-mask::/d' >"$RES/setup.log" ||
			{ tail -30 "$RES/setup.log" >&2; return 1; }
		endpoint="$(output endpoint-hostname "$STATE/setup.out")"
		names+=("$endpoint")
	fi
	[ -n "$endpoint" ] || { echo "setup produced no endpoint hostname" >&2; return 1; }
	resolved="$(sed -n 's/^AKO chart version: //p' "$RES/setup.log" | head -1)"
	echo "AKO version: ${resolved:-unknown}, helm repo ${AKO_HELM_REPO:-$(yq '.dependencies[] | select(.name == "aerospike-kubernetes-operator") | .repository' "$WORK/action/deps/Chart.yaml")}, cluster chart ${CLUSTER_CHART_VERSION:-$resolved}" |
		tee "$RES/ako.txt"
	crs="$(kubectl get aerospikecluster -A -o jsonpath='{range .items[*]}{.metadata.namespace} {.metadata.name}{"\n"}{end}')"
	[ "$(grep -c . <<<"$crs")" = 1 ] || { echo "expected one AerospikeCluster, found: $crs" >&2; return 1; }
	read -r ns cr <<<"$crs"
	write_topology "$profile" "$endpoint" "$ns" "$cr" "" "" "" ""

	if [ "$TLS" = 1 ]; then
		echo "Enabling TLS (tls.log)"
		[ "$profile" != nlb-tls ] || tls_args=(--tls-endpoint "$TLS_ENDPOINT_HOSTNAME")
		"$L3_CTL" enable-tls "${tls_args[@]+"${tls_args[@]}"}" >"$RES/tls.log" 2>&1 || { tail -20 "$RES/tls.log" >&2; return 1; }
		tls_name="$(sed -n 's/^L3-TLS .*tlsName=\([^ ]*\).*/\1/p' "$RES/tls.log")"
		ca_file="$(sed -n 's/^L3-TLS .*caFile=\([^ ]*\).*/\1/p' "$RES/tls.log")"
		tls_endpoint="$(sed -n 's/^L3-TLS .*tlsEndpoint=\([^ ]*\).*/\1/p' "$RES/tls.log")"
		tls_endpoint="${tls_endpoint:-$endpoint}"
		[ "$tls_endpoint" = "$endpoint" ] || names+=("$tls_endpoint")
	fi

	# The driver resolves each endpoint hostname as the servers do, to the kind node CoreDNS has for it.
	corefile="$(kubectl -n kube-system get cm coredns -o jsonpath='{.data.Corefile}')"
	for name in "${names[@]}"; do
		address="$(awk -v name="$name" 'NF == 2 && $2 == name { print $1; exit }' <<<"$corefile")"
		[ -n "$address" ] || { echo "no CoreDNS hosts entry for $name" >&2; return 1; }
		hosts+="${hosts:+,}$name:$address"
		echo "Driver resolves $name to $address"
	done
	write_topology "$profile" "$endpoint" "$ns" "$cr" "$tls_endpoint" "$tls_name" "$ca_file" "$hosts"
}

write_topology() {
	{
		echo "L3_PROFILE=$1"
		echo "L3_CLUSTER=$PREFIX"
		echo "L3_KUBECONFIG=$STATE/kubeconfig"
		echo "L3_DOCKER=$DOCKER"
		echo "L3_ENDPOINT_HOSTNAME=$2"
		echo "L3_NAMESPACE=$3"
		echo "L3_CR=$4"
		echo "L3_TLS_ENDPOINT_HOSTNAME=$5"
		echo "L3_TLS_NAME=$6"
		echo "L3_TLS_CA_FILE=$7"
		echo "L3_CLIENT_HOSTS=$8"
		echo "L3_DRIVER_NETWORK=$([ "$DRIVER_NETWORK" = kind ] && echo "$KIND_NETWORK" || echo host)"
		echo "L3_PROBE_IMAGE=$SERVER_IMAGE"
	} >"$STATE/topology.env"
}

# The driver wrapper rows.py starts: the manifest's L5_DRIVER in a container from the built image that
# resolves the endpoint hostnames to kind nodes and reads the TLS CA file at the path rows.py passes.
write_driver() {
	local hosts=() entry entries network=$KIND_NETWORK
	[ "$DRIVER_NETWORK" = kind ] || network=host
	IFS=, read -r -a entries <<<"$(sed -n 's/^L3_CLIENT_HOSTS=//p' "$STATE/topology.env")"
	for entry in "${entries[@]+"${entries[@]}"}"; do
		hosts+=(--add-host "$entry")
	done
	mkdir -p "$STATE/tls"
	{
		echo '#!/usr/bin/env bash'
		# shellcheck disable=SC2016 # expanded when the wrapper runs
		printf 'AEROSPIKE_ADMIN_PASSWORD="$(cat %q)"\nexport AEROSPIKE_ADMIN_PASSWORD\n' "$STATE/admin-password"
		printf 'exec %q run --rm -i --name "%s-driver-$$" --network %q -e AEROSPIKE_ADMIN_PASSWORD' "$DOCKER" "$PREFIX" "$network"
		[ ${#hosts[@]} -eq 0 ] || printf ' %q' "${hosts[@]}"
		[ ${#CACHE_MOUNTS[@]} -eq 0 ] || printf ' %q' "${CACHE_MOUNTS[@]}"
		printf ' -v %q -v %q -w /client %q' "$STATE/tls:$STATE/tls:ro" "$WORK/client:/client" "$CLIENT_IMAGE"
		# shellcheck disable=SC2016 # expanded in the driver container
		printf ' bash -c %q %q "$@"\n' 'set -a; . "./$0"; set +a; eval "$L5_DRIVER \"\$@\""' "$MANIFEST"
	} >"$STATE/driver"
	chmod +x "$STATE/driver"
}

export L3_CTL="$SCRIPT_DIR/l3ctl"
failed=0
: >"$RESULTS/rows.txt"
for PROFILE in "${PROFILE_LIST[@]}"; do
	echo "Profile $PROFILE"
	CLUSTER="$PREFIX-$PROFILE"
	STATE="$WORK/$PROFILE"
	RES="$RESULTS/$PROFILE"
	mkdir -p "$STATE" "$RES"
	export L3_STATE="$STATE"
	set +e
	(set -e; build_cluster "$PROFILE") 2>&1 | tee "$RES/build-cluster.log"
	cluster_rc=${PIPESTATUS[0]}
	set -e
	if [ "$cluster_rc" -ne 0 ]; then
		echo "PROFILE $PROFILE NOT RUN: the cluster could not be built (see $PROFILE/build-cluster.log and setup.log)" |
			tee -a "$RESULTS/rows.txt"
		KUBECONFIG="$STATE/kubeconfig" kubectl get pods -A -o wide >"$RES/pods.txt" 2>&1 || true
		failed=1
		remove_cluster "$CLUSTER"
		continue
	fi
	export KUBECONFIG="$STATE/kubeconfig"
	write_driver
	"$L3_CTL" status >"$RES/status-before.txt" 2>&1 || true
	row_args=(--profile "$PROFILE" --driver "$STATE/driver")
	[ -z "$ROWS" ] || row_args+=(--rows "$ROWS")
	echo "Running the rows (rows.log)"
	set +e
	python3 "$SCRIPT_DIR/rows.py" "${row_args[@]}" </dev/null >"$RES/rows.log" 2>&1
	rows_rc=$?
	set -e
	grep -E '^L3-ROW ' "$RES/rows.log" | sed 's/^L3-ROW /ROW /' | tee -a "$RESULTS/rows.txt" || true
	if [ "$rows_rc" -ne 0 ]; then
		failed=1
		grep -m1 '^ERROR' "$RES/rows.log" | sed "s/^/PROFILE $PROFILE /" | tee -a "$RESULTS/rows.txt" || true
	fi
	"$L3_CTL" status >"$RES/status-after.txt" 2>&1 || true
	kubectl -n ako logs deploy/aerospike-kubernetes-operator -c manager --tail=400 >"$RES/ako-manager.log" 2>&1 || true
	remove_cluster "$CLUSTER"
done

[ "$failed" -eq 0 ] || die "one or more rows or profiles failed"
