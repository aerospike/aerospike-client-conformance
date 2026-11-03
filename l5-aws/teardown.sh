#!/usr/bin/env bash
#
# Delete one L5 stack and everything l5ctl created for it: FIS experiments and templates, NLB
# listeners and target groups first, then the stack. Fails if any resource tagged
# l5-aws-run=<stack name> is left. run.sh calls it; run it by hand for a stack kept with --keep or
# left by an interrupted run.
#
# Required environment: AWS_PROFILE, AWS_REGION
#
# Usage: l5-aws/teardown.sh <stack name>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_TAG=l5-aws-run

die() {
	echo "ERROR: $*" >&2
	exit 2
}

STACK="${1:-}"
[[ "$STACK" =~ ^l5-aws-[0-9]{8}-[0-9]{6}$ ]] || die "usage: teardown.sh l5-aws-YYYYmmdd-HHMMSS"
for name in AWS_PROFILE AWS_REGION; do
	[ -n "${!name:-}" ] || die "$name is not set"
done
export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION" AWS_PAGER=""

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

rc=0
status="$(aws cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo GONE)"
echo "Stack $STACK: $status"
if [ "$status" != "GONE" ] && [ "$status" != "DELETE_COMPLETE" ]; then
	L5_STACK="$STACK" L5_ENV=/dev/null "$SCRIPT_DIR/l5ctl" cleanup || rc=1
	echo "Deleting stack $STACK"
	aws cloudformation delete-stack --stack-name "$STACK"
	if aws cloudformation wait stack-delete-complete --stack-name "$STACK"; then
		echo "Stack $STACK deleted"
	else
		echo "ERROR: stack $STACK delete did not complete" >&2
		# shellcheck disable=SC2016 # backticks are JMESPath literals
		aws cloudformation describe-stack-events --stack-name "$STACK" --max-items 30 \
			--query 'StackEvents[?contains(ResourceStatus,`FAILED`)].[LogicalResourceId,ResourceStatusReason]' --output text
		rc=1
	fi
fi

left=""
ignored="$(mktemp)"
for i in $(seq 1 20); do
	left="$(remaining_resources 2>"$ignored")"
	[ -n "$left" ] || break
	[ "$i" -lt 20 ] && sleep 30
done
sort -u "$ignored"
rm -f "$ignored"
echo "aws resourcegroupstaggingapi get-resources --tag-filters Key=$RUN_TAG,Values=$STACK"
if [ -n "$left" ]; then
	echo "ERROR: resources tagged $RUN_TAG=$STACK remain:" >&2
	echo "$left" >&2
	rc=1
else
	echo "No resource tagged $RUN_TAG=$STACK remains"
fi
exit $rc
