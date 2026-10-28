#!/usr/bin/env bash
#
# One aws-tags run against real AWS for one client, from the operator's machine. Creates stack.yaml
# as a uniquely named stack, waits for the three Aerospike nodes to form one cluster, stops the
# tagged extra instance and assumes the no-policy role. On the runner, over EC2 Instance Connect, it
# checks out the client, runs the manifest's setup and build commands and runs the client's probe in
# the latency, denied and join modes. It copies the probe output to results/<stack name>/ and checks
# it with check.py. The stack is always deleted, and the run fails if any resource tagged
# aws-tags-real-run=<stack name> is left.
#
# Required environment:
#   AWS_PROFILE               operator credentials
#   AWS_REGION                region of the stack
#   AWS_PERMISSIONS_BOUNDARY  ARN of the permissions boundary set on both roles
#
# Usage: aws-real/run.sh --client-repo <https url> --commit <sha> --manifest <path in the client repo>
#                        [--runs 20] [--warm 20] [--refreshes 30]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_TAG=aws-tags-real-run
DENIED_VARS=(AWS_REAL_DENIED_ACCESS_KEY_ID AWS_REAL_DENIED_SECRET_ACCESS_KEY AWS_REAL_DENIED_SESSION_TOKEN)
ACCESS_KEY_ID_PATTERN='\b(AKIA|ASIA)[A-Z0-9]{16}\b'
EXPECTED_NODES=3
NAMESPACE="test"

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
RUNS=20
WARM=20
REFRESHES=30
while [ $# -gt 0 ]; do
	case "$1" in
		--client-repo) CLIENT_REPO="${2:-}" ;;
		--commit) COMMIT="${2:-}" ;;
		--manifest) MANIFEST="${2:-}" ;;
		--runs) RUNS="${2:-}" ;;
		--warm) WARM="${2:-}" ;;
		--refreshes) REFRESHES="${2:-}" ;;
		*) usage ;;
	esac
	shift 2 || usage
done
[[ "$CLIENT_REPO" =~ ^https://[A-Za-z0-9./_-]+$ ]] || die "--client-repo must be an https URL"
[[ "$COMMIT" =~ ^[0-9a-f]{7,40}$ ]] || die "--commit must be a commit sha"
[[ "$MANIFEST" =~ ^[A-Za-z0-9._/-]+$ && "$MANIFEST" != /* && "$MANIFEST" != *..* ]] ||
	die "--manifest must be a relative path inside the client repository"
for value in "$RUNS" "$WARM" "$REFRESHES"; do
	[[ "$value" =~ ^[1-9][0-9]*$ ]] || die "--runs, --warm and --refreshes must be positive integers"
done

for name in AWS_PROFILE AWS_REGION AWS_PERMISSIONS_BOUNDARY; do
	[ -n "${!name:-}" ] || die "$name is not set"
done
for cmd in aws ssh ssh-keygen curl jq perl python3; do
	command -v "$cmd" >/dev/null 2>&1 || die "$cmd not found"
done
export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION" AWS_PAGER=""

identity="$(aws sts get-caller-identity --query '[Account,Arn]' --output text)" ||
	die "aws sts get-caller-identity failed for profile $AWS_PROFILE"
read -r ACCOUNT CALLER <<<"$identity"
echo "Account: $ACCOUNT"
echo "Caller:  $CALLER"
echo "Region:  $AWS_REGION"
echo "Client:  $CLIENT_REPO $COMMIT $MANIFEST"

case "$CALLER" in
	arn:aws*:sts::*:assumed-role/*)
		role_name="${CALLER#*:assumed-role/}"
		role_name="${role_name%%/*}"
		CREATOR_ARN="$(aws iam get-role --role-name "$role_name" --query Role.Arn --output text)" ||
			die "cannot resolve the IAM ARN of role $role_name"
		;;
	arn:aws*:iam::*:role/* | arn:aws*:iam::*:user/*) CREATOR_ARN="$CALLER" ;;
	*) die "caller $CALLER is not a role or user" ;;
esac

OPERATOR_IP="$(curl -sf -m 10 https://checkip.amazonaws.com | tr -d '[:space:]')" || die "cannot determine the operator's public IP"
[[ "$OPERATOR_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || die "unexpected public IP '$OPERATOR_IP'"

STACK="aws-tags-real-$(date -u +%Y%m%d-%H%M%S)"
RESULTS="$SCRIPT_DIR/results/$STACK"
WORK="$(mktemp -d)"
STACK_CREATED=0
mkdir -p "$RESULTS"
echo "Stack:   $STACK"
echo "SSH from $OPERATOR_IP/32"

redact() {
	perl -pe 'BEGIN { $| = 1 } s/'"$ACCESS_KEY_ID_PATTERN"'/$1\[redacted\]/g'
}

secret_scan() {
	local values=() name creds files=0 hits=0 f
	for name in "${DENIED_VARS[@]}"; do
		[ -z "${!name:-}" ] || values+=("${!name}")
	done
	if creds="$(aws configure export-credentials --format process 2>/dev/null)"; then
		while IFS= read -r f; do
			[ -z "$f" ] || values+=("$f")
		done < <(jq -r '.AccessKeyId, .SecretAccessKey, (.SessionToken // empty)' <<<"$creds")
	fi
	creds=""
	while IFS= read -r -d '' f; do
		files=$((files + 1))
		if grep -qE "$ACCESS_KEY_ID_PATTERN" "$f" ||
			{ [ ${#values[@]} -gt 0 ] && grep -qF -f <(printf '%s\n' "${values[@]}") "$f"; }; then
			hits=$((hits + 1))
			echo "SECRET FOUND in $f" >&2
		fi
	done < <(find "$RESULTS" -type f -print0)
	values=()
	echo "Secret scan: $hits of $files result files contain an access key id, secret or session token"
	[ $hits -eq 0 ]
}

remaining_resources() {
	local arn id state
	# The tagging API lists deleted instances and volumes for a while; EC2 is authoritative for those.
	for arn in $(aws resourcegroupstaggingapi get-resources --tag-filters "Key=$RUN_TAG,Values=$STACK" \
		--query 'ResourceTagMappingList[].ResourceARN' --output text); do
		[ "$arn" != "None" ] || continue
		id="${arn##*/}"
		case "$arn" in
			*:instance/*)
				state="$(aws ec2 describe-instances --instance-ids "$id" --query 'Reservations[].Instances[].State.Name' \
					--output text 2>&1 || true)" ;;
			*:volume/*)
				state="$(aws ec2 describe-volumes --volume-ids "$id" --query 'Volumes[].State' --output text 2>&1 || true)" ;;
			*) state="" ;;
		esac
		case "$state" in
			terminated)
				echo "  ignored $arn (terminated)" >&2
				continue
				;;
			*NotFound*)
				echo "  ignored $arn (not found)" >&2
				continue
				;;
		esac
		echo "$arn"
	done
}

cleanup() {
	local rc=$? left="" i
	trap - EXIT
	trap '' INT TERM
	set +e
	if [ "$STACK_CREATED" = "1" ]; then
		echo "Deleting stack $STACK"
		aws cloudformation delete-stack --stack-name "$STACK"
		if aws cloudformation wait stack-delete-complete --stack-name "$STACK"; then
			echo "Stack $STACK deleted"
		else
			echo "ERROR: stack $STACK delete did not complete" >&2
			# shellcheck disable=SC2016 # backticks are JMESPath literals
			aws cloudformation describe-stack-events --stack-name "$STACK" --max-items 20 \
				--query 'StackEvents[?contains(ResourceStatus,`FAILED`)].[LogicalResourceId,ResourceStatusReason]' --output text
			rc=1
		fi
		for i in $(seq 1 20); do
			left="$(remaining_resources 2>"$WORK/ignored")"
			[ -n "$left" ] || break
			sleep 30
		done
		sort -u "$WORK/ignored"
		{
			echo "aws resourcegroupstaggingapi get-resources --tag-filters Key=$RUN_TAG,Values=$STACK"
			aws resourcegroupstaggingapi get-resources --tag-filters "Key=$RUN_TAG,Values=$STACK" --output json
		} >"$RESULTS/cleanup.txt"
		if [ -n "$left" ]; then
			echo "ERROR: resources tagged $RUN_TAG=$STACK remain:" | tee -a "$RESULTS/cleanup.txt" >&2
			echo "$left" | tee -a "$RESULTS/cleanup.txt" >&2
			rc=1
		else
			echo "No resource tagged $RUN_TAG=$STACK remains" | tee -a "$RESULTS/cleanup.txt"
		fi
	fi
	secret_scan || rc=1
	unset "${DENIED_VARS[@]}"
	rm -rf "$WORK"
	[ $rc -eq 0 ] && echo "RUN PASS" || echo "RUN FAIL"
	exit $rc
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

STACK_CREATED=1
aws cloudformation create-stack --stack-name "$STACK" --template-body "file://$SCRIPT_DIR/stack.yaml" \
	--capabilities CAPABILITY_IAM --tags "Key=$RUN_TAG,Value=$STACK" \
	--parameters "ParameterKey=OperatorCidr,ParameterValue=$OPERATOR_IP/32" \
	"ParameterKey=PermissionsBoundaryArn,ParameterValue=$AWS_PERMISSIONS_BOUNDARY" \
	"ParameterKey=CreatorArn,ParameterValue=$CREATOR_ARN" >/dev/null
echo "Waiting for stack create"
if ! aws cloudformation wait stack-create-complete --stack-name "$STACK"; then
	# shellcheck disable=SC2016 # backticks are JMESPath literals
	aws cloudformation describe-stack-events --stack-name "$STACK" \
		--query 'StackEvents[?contains(ResourceStatus,`FAILED`)].[LogicalResourceId,ResourceStatusReason]' --output text >&2
	die "stack create failed"
fi

output() {
	aws cloudformation describe-stacks --stack-name "$STACK" \
		--query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
NODE_IPS="$(output NodeIps)"
UNTAGGED_ID="$(output UntaggedInstanceId)"
STOPPED_ID="$(output StoppedInstanceId)"
RUNNER_ID="$(output RunnerInstanceId)"
RUNNER_IP="$(output RunnerPublicIp)"
RUNNER_ROLE="$(output RunnerRoleName)"
DENIED_ROLE_ARN="$(output DeniedRoleArn)"
echo "Nodes:   $NODE_IPS"
echo "Runner:  $RUNNER_ID $RUNNER_IP"

{
	for role in "$RUNNER_ROLE" "${DENIED_ROLE_ARN##*/}"; do
		echo "== role $role"
		aws iam get-role --role-name "$role" --query 'Role.PermissionsBoundary' --output json
		echo "attached managed policies:"
		aws iam list-attached-role-policies --role-name "$role" --query 'AttachedPolicies' --output json
		for policy in $(aws iam list-role-policies --role-name "$role" --query 'PolicyNames' --output text); do
			echo "inline policy $policy:"
			aws iam get-role-policy --role-name "$role" --policy-name "$policy" --query 'PolicyDocument' --output json
		done
	done
	echo "== runner $RUNNER_ID metadata options"
	aws ec2 describe-instances --instance-ids "$RUNNER_ID" \
		--query 'Reservations[0].Instances[0].MetadataOptions.{HttpTokens:HttpTokens,HttpEndpoint:HttpEndpoint}' --output json
} | tee "$RESULTS/stack.txt"

ssh-keygen -q -t ed25519 -N '' -C "$STACK" -f "$WORK/key"
remote() {
	aws ec2-instance-connect send-ssh-public-key --instance-id "$RUNNER_ID" --instance-os-user ec2-user \
		--ssh-public-key "file://$WORK/key.pub" >/dev/null || return 1
	ssh -F /dev/null -i "$WORK/key" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 \
		-o ServerAliveInterval=30 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$WORK/known_hosts" \
		-o LogLevel=ERROR "$@" "ec2-user@$RUNNER_IP" "${REMOTE_COMMAND[@]}"
}

# Run a script read from stdin on the runner with these arguments, each quoted for the remote shell.
remote_script() {
	local quoted
	printf -v quoted '%q ' "$@"
	REMOTE_COMMAND=("bash -s -- $quoted")
}

echo "Waiting for the runner"
REMOTE_COMMAND=(test -f /var/tmp/aws-tags-real-ready)
for i in $(seq 1 60); do
	remote 2>/dev/null && break
	[ "$i" -lt 60 ] || die "runner not ready after 20 minutes"
	sleep 20
done

echo "Waiting for one cluster of three nodes"
IFS=, read -r -a NODES <<<"$NODE_IPS"
REMOTE_COMMAND=(python3 - "${NODES[@]}")
for i in $(seq 1 40); do
	if remote <<'PY'; then break; fi
import socket, struct, sys

def info(host, command):
    with socket.create_connection((host, 3000), timeout=3) as s:
        body = (command + "\n").encode()
        s.sendall(struct.pack(">Q", (2 << 56) | (1 << 48) | len(body)) + body)
        data = b""
        while len(data) < 8 or len(data) < 8 + (struct.unpack(">Q", data[:8])[0] & 0xFFFFFFFFFFFF):
            chunk = s.recv(65536)
            if not chunk:
                raise OSError("connection closed")
            data += chunk
        return data[8:].decode()

sizes = {}
for host in sys.argv[1:]:
    try:
        stats = info(host, "statistics").split("\t", 1)[-1].strip()
        sizes[host] = dict(kv.split("=", 1) for kv in stats.split(";") if "=" in kv).get("cluster_size")
    except OSError as e:
        sizes[host] = str(e)
print("cluster_size " + " ".join(host + "=" + str(size) for host, size in sizes.items()))
sys.exit(0 if all(size == str(len(sys.argv) - 1) for size in sizes.values()) else 1)
PY
	[ "$i" -lt 40 ] || die "the nodes did not form one cluster in 10 minutes"
	sleep 15
done

echo "Stopping $STOPPED_ID"
aws ec2 stop-instances --instance-ids "$STOPPED_ID" >/dev/null
aws ec2 wait instance-stopped --instance-ids "$STOPPED_ID"
EXCLUDED="$(aws ec2 describe-instances --instance-ids "$UNTAGGED_ID" "$STOPPED_ID" \
	--query 'Reservations[].Instances[].PrivateIpAddress' --output text | tr -s '[:space:]' ',' | sed 's/,$//')"
# shellcheck disable=SC2016 # backticks are JMESPath literals
aws ec2 describe-instances --instance-ids "$UNTAGGED_ID" "$STOPPED_ID" \
	--query 'Reservations[].Instances[].[InstanceId,PrivateIpAddress,State.Name,join(`,`,Tags[].Key)]' --output text |
	tee -a "$RESULTS/stack.txt"
echo "Excluded: $EXCLUDED"

echo "Checking out the client and running its setup and build on the runner"
remote_script "$CLIENT_REPO" "$COMMIT" "$MANIFEST"
set +e
remote <<'EOF' 2>&1 | redact >"$RESULTS/build.log"
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
for name in AWS_REAL_SETUP AWS_REAL_BUILD AWS_REAL_PROBE; do
	[ -n "${!name:-}" ] || { echo "$name is not set in $3"; exit 1; }
done
bash -c "$AWS_REAL_SETUP" </dev/null
bash -c "$AWS_REAL_BUILD" </dev/null
EOF
build_rc=${PIPESTATUS[0]}
set -e
[ "$build_rc" -eq 0 ] || { tail -40 "$RESULTS/build.log" >&2; die "client setup or build failed; see build.log"; }
grep '^Commit: ' "$RESULTS/build.log"

# The probe runs from the client checkout with the manifest loaded. Outside the denied mode the
# instance profile must be its only credential source.
# shellcheck disable=SC2016 # expanded on the runner, not here
PROBE_PREAMBLE='set -uo pipefail
cd client
set -a
. "./$1"
set +a
probe() {
	bash -c "$AWS_REAL_PROBE \"\$@\"" probe "$@" </dev/null
}
only_instance_profile() {
	local name token role
	for name in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_PROFILE AWS_SHARED_CREDENTIALS_FILE \
		AWS_WEB_IDENTITY_TOKEN_FILE AWS_ROLE_ARN AWS_CONTAINER_CREDENTIALS_RELATIVE_URI AWS_CONTAINER_CREDENTIALS_FULL_URI; do
		[ -z "${!name:-}" ] || { echo "$name is set; the instance profile must be the only credential source" >&2; exit 1; }
	done
	[ ! -e "$HOME/.aws/credentials" ] || { echo "$HOME/.aws/credentials exists" >&2; exit 1; }
	token="$(curl -sf -m 2 -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60" || true)"
	[ -n "$token" ] || { echo "IMDSv2 is not reachable" >&2; exit 1; }
	role="$(curl -sf -m 2 -H "X-aws-ec2-metadata-token: $token" http://169.254.169.254/latest/meta-data/iam/security-credentials/ || true)"
	[ -n "$role" ] || { echo "the runner has no instance profile role" >&2; exit 1; }
	echo "Instance profile role: $role" >&2
}
export AWS_REAL_REGION="$2" AWS_REAL_TAG="$3"
'

run_probe() {
	local mode="$1"
	shift
	set +e
	remote "$@" 2> >(redact >"$RESULTS/$mode.log") | redact >"$RESULTS/$mode.jsonl"
	local rc=${PIPESTATUS[0]}
	set -e
	wait
	[ "$rc" -eq 0 ] || echo "WARNING: $mode probe session exited with $rc; see $mode.log" >&2
}

TAG="aerospike-cluster=$STACK"

echo "Probe: $RUNS latency runs of $WARM warm refreshes"
remote_script "$MANIFEST" "$AWS_REGION" "$TAG" "$RUNS" "$WARM"
run_probe latency <<EOF
$PROBE_PREAMBLE
only_instance_profile
for i in \$(seq 1 "\$4"); do
	AWS_REAL_WARM="\$5" probe latency || echo "latency run \$i: probe exited with \$?" >&2
done
EOF

echo "Assuming $DENIED_ROLE_ARN"
for i in $(seq 1 12); do
	if creds="$(aws sts assume-role --role-arn "$DENIED_ROLE_ARN" --role-session-name aws-tags-denied \
		--query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text 2>/dev/null)"; then
		break
	fi
	[ "$i" -lt 12 ] || die "cannot assume $DENIED_ROLE_ARN"
	sleep 10
done
read -r AWS_REAL_DENIED_ACCESS_KEY_ID AWS_REAL_DENIED_SECRET_ACCESS_KEY AWS_REAL_DENIED_SESSION_TOKEN <<<"$creds"
creds=""
export AWS_REAL_DENIED_ACCESS_KEY_ID AWS_REAL_DENIED_SECRET_ACCESS_KEY AWS_REAL_DENIED_SESSION_TOKEN

echo "Probe: denied"
remote_script "$MANIFEST" "$AWS_REGION" "$TAG"
run_probe denied -o SendEnv=AWS_REAL_DENIED_ACCESS_KEY_ID -o SendEnv=AWS_REAL_DENIED_SECRET_ACCESS_KEY \
	-o SendEnv=AWS_REAL_DENIED_SESSION_TOKEN <<EOF
$PROBE_PREAMBLE
AWS_ACCESS_KEY_ID="\$AWS_REAL_DENIED_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="\$AWS_REAL_DENIED_SECRET_ACCESS_KEY" \
	AWS_SESSION_TOKEN="\$AWS_REAL_DENIED_SESSION_TOKEN" probe denied
EOF
export -n "${DENIED_VARS[@]}"

echo "Probe: join, $REFRESHES tend refreshes"
remote_script "$MANIFEST" "$AWS_REGION" "$TAG" "$EXPECTED_NODES" "$NAMESPACE" "$REFRESHES"
run_probe join <<EOF
$PROBE_PREAMBLE
only_instance_profile
AWS_REAL_EXPECTED_NODES="\$4" AWS_REAL_NAMESPACE="\$5" AWS_REAL_REFRESHES="\$6" probe join
EOF

echo "Results: $RESULTS"
set +e
AWS_REAL_DENIED_SECRET_ACCESS_KEY="$AWS_REAL_DENIED_SECRET_ACCESS_KEY" \
	AWS_REAL_DENIED_SESSION_TOKEN="$AWS_REAL_DENIED_SESSION_TOKEN" \
	python3 "$SCRIPT_DIR/check.py" --latency "$RESULTS/latency.jsonl" --denied "$RESULTS/denied.jsonl" \
	--join "$RESULTS/join.jsonl" --runs "$RUNS" --expected-nodes "$EXPECTED_NODES" --excluded "$EXCLUDED" |
	tee "$RESULTS/check.txt"
check_rc=${PIPESTATUS[0]}
set -e
[ "$check_rc" -eq 0 ] || die "check.py failed"
