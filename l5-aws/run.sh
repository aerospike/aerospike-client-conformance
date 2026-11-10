#!/usr/bin/env bash
#
# One L5 run on AWS for one client, from the operator's machine. Creates stack.yaml as a uniquely
# named stack, installs AKO 4.6.0 and the AerospikeCluster from the runner with l5ctl, adds one NLB
# listener per pod NodePort and checks where the endpoint name resolves. On the runner, over EC2
# Instance Connect, it checks out the client, runs the manifest's setup and build commands and runs
# each row once: through rows.py with the manifest's driver (L5_DRIVER, DRIVER.md) when it sets one,
# otherwise the manifest's row command (L5_RUN) with L5_ROW set. Output goes to results/<stack name>/.
# The stack is always deleted (unless --keep), and the run fails if any resource tagged
# l5-aws-run=<stack name> is left.
#
# --control resolvable makes the endpoint name resolvable in the producer VPC too, for W-1 on a
# released server. --control unresolvable runs no rows and passes only if the server fails to start.
# --plan validates the arguments and prints what would be created, with no AWS call.
# --stack reuses a stack left by --keep instead of creating one; it is deleted at the end unless
# --keep is given again.
#
# Required environment:
#   AWS_PROFILE               operator credentials
#   AWS_REGION                region of the stack
#   AWS_PERMISSIONS_BOUNDARY  ARN of the permissions boundary set on every role
#   FEATURES_CONF_PATH        Enterprise feature-key file, copied to the runner and never printed
#
# Usage: l5-aws/run.sh --client-repo <https url> --commit <sha> --manifest <path in the client repo>
#                      --server-image <image> [--rows W-1,W-3,W-2,W-5] [--registry-auth <file>]
#                      [--control resolvable|unresolvable] [--stack <kept stack>] [--keep] [--plan]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_TAG=l5-aws-run
OPERATOR_TAG=l5-aws-operator
KUBECTL_VERSION=v1.33.0
HELM_VERSION=v3.16.4
ALL_ROWS="W-1 W-2 W-3 W-5"

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
SERVER_IMAGE=""
ROWS="W-1,W-3,W-2,W-5"
REGISTRY_AUTH=""
CONTROL=""
KEEP=0
EXISTING_STACK=""
PLAN=0
while [ $# -gt 0 ]; do
	case "$1" in
		--client-repo) CLIENT_REPO="${2:-}"; shift ;;
		--commit) COMMIT="${2:-}"; shift ;;
		--manifest) MANIFEST="${2:-}"; shift ;;
		--server-image) SERVER_IMAGE="${2:-}"; shift ;;
		--rows) ROWS="${2:-}"; shift ;;
		--registry-auth) REGISTRY_AUTH="${2:-}"; shift ;;
		--control) CONTROL="${2:-}"; shift ;;
		--stack) EXISTING_STACK="${2:-}"; shift ;;
		--keep) KEEP=1 ;;
		--plan) PLAN=1 ;;
		*) usage ;;
	esac
	shift
done
[[ "$SERVER_IMAGE" =~ ^[A-Za-z0-9._:/@-]+$ ]] || die "--server-image is required"
case "$CONTROL" in
	"" | resolvable | unresolvable) ;;
	*) die "--control must be resolvable or unresolvable" ;;
esac
# A kept stack already runs a cluster, so the control would see its pods, not a fresh start.
[ -z "$CONTROL" ] || [ -z "$EXISTING_STACK" ] || die "--control needs a new stack; it cannot run with --stack"
if [ "$CONTROL" = "unresolvable" ] || [ "$ROWS" = "none" ]; then
	ROW_LIST=()
else
	[[ "$CLIENT_REPO" =~ ^https://[A-Za-z0-9./_-]+$ ]] || die "--client-repo must be an https URL"
	[[ "$COMMIT" =~ ^[0-9a-f]{7,40}$ ]] || die "--commit must be a commit sha"
	[[ "$MANIFEST" =~ ^[A-Za-z0-9._/-]+$ && "$MANIFEST" != /* && "$MANIFEST" != *..* ]] ||
		die "--manifest must be a relative path inside the client repository"
	IFS=, read -r -a ROW_LIST <<<"$ROWS"
	for row in "${ROW_LIST[@]}"; do
		[[ " $ALL_ROWS " == *" $row "* ]] || die "unknown row $row (rows: $ALL_ROWS, or none)"
	done
fi
[ -z "$REGISTRY_AUTH" ] || [ -r "$REGISTRY_AUTH" ] || die "--registry-auth $REGISTRY_AUTH is not readable"
PRODUCER_RECORD=false
EXPECT_PRODUCER=no
if [ "$CONTROL" = "resolvable" ]; then
	PRODUCER_RECORD=true
	EXPECT_PRODUCER=yes
fi

for name in AWS_PROFILE AWS_REGION AWS_PERMISSIONS_BOUNDARY FEATURES_CONF_PATH; do
	[ -n "${!name:-}" ] || die "$name is not set"
done
[ -r "$FEATURES_CONF_PATH" ] || die "FEATURES_CONF_PATH is not readable"
for cmd in aws ssh ssh-keygen curl jq; do
	command -v "$cmd" >/dev/null 2>&1 || die "$cmd not found"
done
for f in stack.yaml l5ctl rows.py aerospike-cluster.yaml teardown.sh; do
	[ -f "$SCRIPT_DIR/$f" ] || die "$SCRIPT_DIR/$f is missing"
done
export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION" AWS_PAGER=""

STACK="${EXISTING_STACK:-l5-aws-$(date -u +%Y%m%d-%H%M%S)}"
[[ "$STACK" =~ ^l5-aws-[0-9]{8}-[0-9]{6}$ ]] || die "--stack must be l5-aws-YYYYmmdd-HHMMSS"
if [ "$PLAN" = "1" ]; then
	echo "Plan, no AWS call made:"
	echo "  stack          $STACK in $AWS_REGION, profile $AWS_PROFILE, template $SCRIPT_DIR/stack.yaml"
	echo "  creates        producer VPC (EKS $(sed -n '/^  KubernetesVersion:/,/Default:/s/^ *Default: *//p' "$SCRIPT_DIR/stack.yaml" | tr -d '"'), one system and three Spot node groups, internal NLB, endpoint service),"
	echo "                 consumer VPC (interface endpoint, private hosted zone, runner with an Elastic IP), IAM roles for EKS, nodes, FIS and the runner"
	echo "  boundary       $AWS_PERMISSIONS_BOUNDARY"
	echo "  producer zone  $PRODUCER_RECORD"
	echo "  server image   $SERVER_IMAGE${REGISTRY_AUTH:+ (pull secret from $REGISTRY_AUTH)}"
	if [ "$CONTROL" = "unresolvable" ]; then
		echo "  rows           none; passes only if the server fails to start"
	elif [ ${#ROW_LIST[@]} -eq 0 ]; then
		echo "  rows           none; stops after the cluster, listeners and name resolution are up"
	else
		echo "  client         $CLIENT_REPO $COMMIT $MANIFEST"
		echo "  rows           ${ROW_LIST[*]}"
		echo "  row command    rows.py with the manifest's L5_DRIVER when it sets one, otherwise its L5_RUN"
	fi
	echo "  after the run  $([ "$KEEP" = "1" ] && echo "stack kept; delete with teardown.sh" || echo "stack deleted, leftovers checked")"
	exit 0
fi

identity="$(aws sts get-caller-identity --query '[Account,Arn]' --output text)" ||
	die "aws sts get-caller-identity failed for profile $AWS_PROFILE"
read -r ACCOUNT CALLER <<<"$identity"
OPERATOR_IP="$(curl -sf -m 10 https://checkip.amazonaws.com | tr -d '[:space:]')" || die "cannot determine the operator's public IP"
[[ "$OPERATOR_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || die "unexpected public IP '$OPERATOR_IP'"
if [ ${#ROW_LIST[@]} -gt 0 ] && [[ "${CLIENT_REPO%.git}" =~ ^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$ ]]; then
	curl -sfI -m 20 "https://raw.githubusercontent.com/${BASH_REMATCH[1]}/$COMMIT/$MANIFEST" >/dev/null ||
		die "$MANIFEST not found at $COMMIT in $CLIENT_REPO; use --rows none for the environment only"
fi

OPERATOR="${CALLER##*/}"
live="$(aws cloudformation list-stacks --stack-status-filter CREATE_IN_PROGRESS CREATE_COMPLETE ROLLBACK_IN_PROGRESS \
	UPDATE_IN_PROGRESS UPDATE_COMPLETE_CLEANUP_IN_PROGRESS UPDATE_COMPLETE UPDATE_ROLLBACK_IN_PROGRESS \
	UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS UPDATE_ROLLBACK_COMPLETE \
	--query "StackSummaries[?starts_with(StackName,'l5-aws-')].[StackName,StackId,StackStatus,CreationTime]" --output text)" ||
	die "cannot list stacks in $AWS_REGION"
others=""
while read -r live_name live_id live_status live_created; do
	[ -n "$live_name" ] && [ "$live_name" != "$EXISTING_STACK" ] || continue
	owner="$(aws cloudformation describe-stacks --stack-name "$live_id" \
		--query "Stacks[0].Tags[?Key=='$OPERATOR_TAG'].Value | [0]" --output text 2>/dev/null)" || owner=""
	if [ -z "$owner" ] || [ "$owner" = "None" ]; then
		owner="$(aws cloudtrail lookup-events --lookup-attributes "AttributeKey=ResourceName,AttributeValue=$live_id" \
			--query "Events[?EventName=='CreateStack'].Username | [0]" --output text 2>/dev/null)" || owner=""
	fi
	[ -n "$owner" ] && [ "$owner" != "None" ] || owner=unknown
	live_created="${live_created%%.*}"
	live_created="${live_created%+00:00}Z"
	others+=$'\n'"  $live_name  $live_status  created $live_created  by $owner"
done <<<"$live"

short=""
check_capacity() {
	local label=$1 type=$2 service=$3 code=$4 count=$5 key=$6 needed limit in_use
	needed="$(grep -c "^    Type: AWS::EC2::$type\$" "$SCRIPT_DIR/stack.yaml")" || needed=0
	limit="$(aws service-quotas get-service-quota --service-code "$service" --quota-code "$code" \
		--query Quota.Value --output text 2>/dev/null)" ||
		limit="$(aws service-quotas get-aws-default-service-quota --service-code "$service" --quota-code "$code" \
			--query Quota.Value --output text 2>/dev/null)" ||
		die "cannot read the $label quota ($service $code) in $AWS_REGION, so the capacity check could not run"
	limit="${limit%%.*}"
	in_use="$(aws ec2 "$count" --query "length($key)" --output text)" ||
		die "cannot count $label in $AWS_REGION, so the capacity check could not run"
	[[ "$limit" =~ ^[0-9]+$ && "$in_use" =~ ^[0-9]+$ ]] ||
		die "unexpected $label quota '$limit' or count '$in_use', so the capacity check could not run"
	[ $((in_use + needed)) -le "$limit" ] || short+=$'\n'"  $label $in_use of $limit in use, this run needs $needed"
}
if [ -z "$EXISTING_STACK" ]; then
	check_capacity VPCs VPC vpc L-F678F1CE describe-vpcs Vpcs
	check_capacity "internet gateways" InternetGateway vpc L-A4707A72 describe-internet-gateways InternetGateways
	check_capacity "Elastic IPs" EIP ec2 L-0263D0A3 describe-addresses Addresses
	[ -z "$short" ] || die "not enough capacity in $AWS_REGION for another L5 run:$short"$'\n'"Live L5 stacks:${others:- none}"$'\n'"Wait for one to finish, ask its owner, raise the quota, or run in another AWS_REGION."
fi
[ -z "$others" ] || echo "Live L5 stacks in $AWS_REGION:$others"

RESULTS="$SCRIPT_DIR/results/$STACK"
WORK="$(mktemp -d)"
STACK_CREATED=0
mkdir -p "$RESULTS"
echo "Account: $ACCOUNT"
echo "Caller:  $CALLER"
echo "Region:  $AWS_REGION"
echo "Stack:   $STACK"
echo "Server:  $SERVER_IMAGE"
echo "Control: ${CONTROL:-none}"
echo "SSH from $OPERATOR_IP/32"

secret_scan() {
	local files=0 hits=0 f
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

cleanup() {
	local rc=$?
	trap - EXIT
	trap '' INT TERM
	set +e
	if [ "$STACK_CREATED" = "1" ]; then
		if [ "$KEEP" = "1" ]; then
			echo "Stack $STACK kept. Delete it with:"
			echo "  AWS_PROFILE=$AWS_PROFILE AWS_REGION=$AWS_REGION $SCRIPT_DIR/teardown.sh $STACK"
		else
			"$SCRIPT_DIR/teardown.sh" "$STACK" 2>&1 | tee "$RESULTS/cleanup.txt"
			if [ "${PIPESTATUS[0]}" -ne 0 ]; then
				rc=1
				echo "Teardown failed; the stack may still exist. Once the credentials work, run it again with:"
				echo "  AWS_PROFILE=$AWS_PROFILE AWS_REGION=$AWS_REGION $SCRIPT_DIR/teardown.sh $STACK"
			fi
		fi
	fi
	secret_scan || rc=1
	rm -rf "$WORK"
	[ $rc -eq 0 ] && echo "RUN PASS" || echo "RUN FAIL"
	exit $rc
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

STACK_CREATED=1
if [ -n "$EXISTING_STACK" ]; then
	status="$(aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].StackStatus" --output text)" ||
		die "stack $STACK not found"
	[ "$status" = "CREATE_COMPLETE" ] || die "stack $STACK is $status"
	echo "Reusing stack $STACK"
else
	aws cloudformation create-stack --stack-name "$STACK" --template-body "file://$SCRIPT_DIR/stack.yaml" \
		--capabilities CAPABILITY_IAM --tags "Key=$RUN_TAG,Value=$STACK" "Key=$OPERATOR_TAG,Value=$OPERATOR" \
		--parameters "ParameterKey=OperatorCidr,ParameterValue=$OPERATOR_IP/32" \
		"ParameterKey=PermissionsBoundaryArn,ParameterValue=$AWS_PERMISSIONS_BOUNDARY" \
		"ParameterKey=ProducerRecord,ParameterValue=$PRODUCER_RECORD" >/dev/null
	echo "Waiting for stack create (EKS takes 15 to 20 minutes)"
	if ! aws cloudformation wait stack-create-complete --stack-name "$STACK"; then
		# shellcheck disable=SC2016 # backticks are JMESPath literals
		aws cloudformation describe-stack-events --stack-name "$STACK" \
			--query 'StackEvents[?contains(ResourceStatus,`FAILED`)].[LogicalResourceId,ResourceStatusReason]' --output text >&2
		die "stack create failed"
	fi
fi

output() {
	aws cloudformation describe-stacks --stack-name "$STACK" \
		--query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
RUNNER_ID="$(output RunnerInstanceId)"
RUNNER_IP="$(output RunnerPublicIp)"
ENDPOINT_NAME="$(output EndpointName)"
[ "$(output ProducerRecord)" = "$PRODUCER_RECORD" ] ||
	die "stack $STACK has ProducerRecord=$(output ProducerRecord), this run needs $PRODUCER_RECORD"
{
	echo "L5_STACK=$STACK"
	echo "AWS_REGION=$AWS_REGION"
	echo "L5_CLUSTER=$(output ClusterName)"
	echo "L5_ENDPOINT_NAME=$ENDPOINT_NAME"
	echo "L5_PRODUCER_RECORD=$(output ProducerRecord)"
	echo "L5_NLB_ARN=$(output NlbArn)"
	echo "L5_PRODUCER_VPC=$(output ProducerVpcId)"
	echo "L5_NODEGROUPS=$(output AerospikeNodegroups)"
	echo "L5_PRODUCER_SUBNETS=$(output ProducerSubnets)"
	echo "L5_CONSUMER_SUBNETS=$(output ConsumerSubnets)"
	echo "L5_NODE_LT=$(output NodeLaunchTemplateId)"
	echo "L5_FIS_ROLE=$(output FisRoleArn)"
	echo "L5_RUNNER_AZ=$(output RunnerAz)"
} >"$RESULTS/env"
cat "$RESULTS/env"
echo "Runner:  $RUNNER_ID $RUNNER_IP"

ssh-keygen -q -t ed25519 -N '' -C "$STACK" -f "$WORK/key"
remote() {
	aws ec2-instance-connect send-ssh-public-key --instance-id "$RUNNER_ID" --instance-os-user ec2-user \
		--ssh-public-key "file://$WORK/key.pub" >/dev/null || return 1
	ssh -F /dev/null -i "$WORK/key" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 \
		-o ServerAliveInterval=30 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$WORK/known_hosts" \
		-o LogLevel=ERROR "ec2-user@$RUNNER_IP" "${REMOTE_COMMAND[@]}"
}

# Run a script read from stdin on the runner with these arguments, each quoted for the remote shell.
remote_script() {
	local quoted
	printf -v quoted '%q ' "$@"
	REMOTE_COMMAND=("bash -s -- $quoted")
}

# Write stdin to a file on the runner, readable by ec2-user only.
upload() {
	local quoted
	printf -v quoted '%q' "$1"
	REMOTE_COMMAND=("umask 077 && mkdir -p l5 && cat > $quoted")
	remote
}

echo "Waiting for the runner"
REMOTE_COMMAND=(test -f /var/tmp/l5-aws-ready)
for i in $(seq 1 60); do
	remote 2>/dev/null && break
	[ "$i" -lt 60 ] || die "runner not ready after 20 minutes"
	sleep 20
done

upload l5/l5ctl <"$SCRIPT_DIR/l5ctl"
upload l5/rows.py <"$SCRIPT_DIR/rows.py"
upload l5/aerospike-cluster.yaml <"$SCRIPT_DIR/aerospike-cluster.yaml"
upload l5/env <"$RESULTS/env"
upload l5/features.conf <"$FEATURES_CONF_PATH"
if [ -n "$REGISTRY_AUTH" ]; then
	upload l5/registry-auth.json <"$REGISTRY_AUTH"
fi

echo "Installing kubectl $KUBECTL_VERSION and helm $HELM_VERSION on the runner"
remote_script "$KUBECTL_VERSION" "$HELM_VERSION"
remote <<'EOF' >"$RESULTS/tools.log" 2>&1 || die "tool install failed; see tools.log"
set -euo pipefail
chmod 700 l5/l5ctl
cd "$(mktemp -d)"
curl -fsSLo kubectl "https://dl.k8s.io/release/$1/bin/linux/amd64/kubectl"
echo "$(curl -fsSL "https://dl.k8s.io/release/$1/bin/linux/amd64/kubectl.sha256")  kubectl" | sha256sum -c
sudo install -m 0755 kubectl /usr/local/bin/kubectl
curl -fsSLo helm.tgz "https://get.helm.sh/helm-$2-linux-amd64.tar.gz"
echo "$(curl -fsSL "https://get.helm.sh/helm-$2-linux-amd64.tar.gz.sha256sum" | awk '{print $1}')  helm.tgz" | sha256sum -c
tar xzf helm.tgz linux-amd64/helm
sudo install -m 0755 linux-amd64/helm /usr/local/bin/helm
kubectl version --client
helm version
aws --version
EOF

install_args=(--server-image "$SERVER_IMAGE" --features-conf l5/features.conf)
[ -z "$REGISTRY_AUTH" ] || install_args+=(--registry-auth l5/registry-auth.json)
[ "$CONTROL" != "unresolvable" ] || install_args+=(--expect-start-failure)
echo "Installing AKO and the AerospikeCluster"
remote_script "${install_args[@]}"
set +e
remote <<'EOF' 2>&1 | tee "$RESULTS/install.log"
l5/l5ctl install "$@"
rc=$?
rm -f l5/features.conf l5/registry-auth.json
exit $rc
EOF
install_rc=${PIPESTATUS[0]}
set -e

if [ "$CONTROL" = "unresolvable" ]; then
	if [ "$install_rc" -ne 0 ]; then
		grep -q '^ERROR: control run:' "$RESULTS/install.log" ||
			die "control run: install failed before the server was checked, no verdict; see install.log"
		die "control run: the server did not fail to start as expected; see install.log"
	fi
	grep '^L5-START-FAILURE' "$RESULTS/install.log"
	echo "CONTROL unresolvable PASS"
	exit 0
fi
[ "$install_rc" -eq 0 ] || die "install failed; see install.log"

echo "Adding listeners and checking name resolution"
remote_script "$EXPECT_PRODUCER"
remote <<'EOF' 2>&1 | tee "$RESULTS/setup.log" || die "environment setup failed; see setup.log"
set -euo pipefail
l5/l5ctl sync-listeners
l5/l5ctl wait-healthy
l5/l5ctl verify
l5/l5ctl resolve-check --expect-producer "$1"
l5/l5ctl status
EOF

if [ ${#ROW_LIST[@]} -eq 0 ]; then
	echo "Environment ready, no rows requested"
	exit 0
fi

echo "Checking out the client and running its setup and build on the runner"
remote_script "$CLIENT_REPO" "$COMMIT" "$MANIFEST"
set +e
remote <<'EOF' >"$RESULTS/build.log" 2>&1
set -euo pipefail
rm -rf client
git clone -q "$1" client
cd client
git checkout -q "$2"
echo "Commit: $(git rev-parse HEAD)"
[ -f "$3" ] || { echo "manifest $3 not found at $2"; exit 1; }
set -a
. "./$3"
set +a
for name in L5_SETUP L5_BUILD; do
	[ -n "${!name:-}" ] || { echo "$name is not set in $3"; exit 1; }
done
if [ -n "${L5_DRIVER:-}" ]; then
	echo "Rows: rows.py with L5_DRIVER"
elif [ -n "${L5_RUN:-}" ]; then
	echo "Rows: L5_RUN"
else
	echo "neither L5_DRIVER nor L5_RUN is set in $3"
	exit 1
fi
bash -c "$L5_SETUP" </dev/null
bash -c "$L5_BUILD" </dev/null
EOF
build_rc=$?
set -e
[ "$build_rc" -eq 0 ] || { tail -40 "$RESULTS/build.log" >&2; die "client setup or build failed; see build.log"; }
grep -E '^(Commit|Rows): ' "$RESULTS/build.log"

# Each row starts on a Completed cluster with a healthy listener per NodePort. rows.py with the
# manifest's L5_DRIVER, or else its L5_RUN, runs from the client checkout with the manifest and the
# environment loaded.
credentials_fail() {
	local err
	err="$(aws sts get-caller-identity --query Account --output text 2>&1 >/dev/null | sed '/^$/d' | paste -sd' ' -)" &&
		return 1
	echo "AWS credentials of profile $AWS_PROFILE stopped working ($err); run aws sso login, then rerun the row with --stack $STACK"
}

failed=0
for row in "${ROW_LIST[@]}"; do
	echo "Row $row"
	if reason="$(credentials_fail)"; then
		echo "ROW $row NOT RUN: $reason" | tee -a "$RESULTS/rows.txt"
		failed=1
		continue
	fi
	row_start="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
	remote_script "$MANIFEST" "$row" "${CONTROL:-none}"
	set +e
	remote <<'EOF' >"$RESULTS/$row.log" 2>&1
set -uo pipefail
l5/l5ctl wait-ready --timeout 1800 && l5/l5ctl sync-listeners --prune && l5/l5ctl wait-healthy || {
	echo "L5-ROW-SETUP failed"
	exit 3
}
set -a
. l5/env
set +a
export L5_ROW="$2" L5_CONTROL="$3" L5_CTL="$HOME/l5/l5ctl" L5_KUBECONFIG="$HOME/l5/kubeconfig"
export AEROSPIKE_ADMIN_PASSWORD="$(cat l5/admin-password)"
L5_SEED_PORT="$(KUBECONFIG="$L5_KUBECONFIG" kubectl -n aerospike get svc -o json | jq -r '[.items[]
	| select(.spec.type == "NodePort" and .spec.selector["statefulset.kubernetes.io/pod-name"] != null)
	| .spec.ports[0].nodePort] | sort | first')"
export L5_SEED_PORT
cd client
set -a
. "./$1"
set +a
echo "L5-ROW $L5_ROW seed=$L5_ENDPOINT_NAME:$L5_SEED_PORT control=$L5_CONTROL"
if [ -n "${L5_DRIVER:-}" ]; then
	python3 "$HOME/l5/rows.py" --row "$L5_ROW" --driver "$L5_DRIVER" </dev/null
else
	bash -c "$L5_RUN" </dev/null
fi
rc=$?
echo "L5-ROW $L5_ROW exit=$rc"
exit $rc
EOF
	row_rc=$?
	set -e
	REMOTE_COMMAND=(l5/l5ctl status)
	remote >"$RESULTS/$row-status.txt" 2>&1 || true
	REMOTE_COMMAND=(l5/l5ctl interruptions "$row_start")
	if remote >"$RESULTS/$row-interruptions.txt" 2>&1; then
		interrupted="$(grep -c '^L5-INTERRUPTED' "$RESULTS/$row-interruptions.txt" || true)"
	else
		interrupted=unknown
	fi
	if [ "$row_rc" -eq 0 ]; then
		echo "ROW $row PASS (unplanned Spot interruptions: $interrupted)" | tee -a "$RESULTS/rows.txt"
	else
		reason="$(credentials_fail)" || reason="$(grep -m1 '^L5-FAILED' "$RESULTS/$row.log" | cut -c1-200 || true)"
		line="ROW $row FAIL exit=$row_rc (see $row.log)${reason:+: $reason}"
		if [ "$interrupted" = unknown ]; then
			line+="; the Spot interruption check failed (see $row-interruptions.txt)"
		elif [ "$interrupted" -gt 0 ]; then
			line+="; $interrupted unplanned Spot interruption(s) during the row, rerun it (see $row-interruptions.txt)"
		fi
		echo "$line" | tee -a "$RESULTS/rows.txt"
		failed=1
	fi
done

echo "Results: $RESULTS"
[ "$failed" -eq 0 ] || die "one or more rows failed"
