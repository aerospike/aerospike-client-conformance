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
			*:subnet/*)
				state="$(aws ec2 describe-subnets --subnet-ids "$id" --query 'Subnets[].State' --output text 2>&1 || true)" ;;
			*:vpc-endpoint/*)
				state="$(aws ec2 describe-vpc-endpoints --vpc-endpoint-ids "$id" --query 'VpcEndpoints[].State' \
					--output text 2>&1 || true)" ;;
			*:vpc-endpoint-service/*)
				state="$(aws ec2 describe-vpc-endpoint-service-configurations --service-ids "$id" \
					--query 'ServiceConfigurations[].ServiceState' --output text 2>&1 || true)" ;;
			# A finished experiment is a record FIS keeps; it cannot be deleted and holds nothing.
			*:experiment/*)
				state="$(aws fis get-experiment --id "$id" --query 'experiment.state.status' --output text 2>&1 || true)"
				case "$state" in completed | stopped | failed) state=terminated ;; esac ;;
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

# EKS leaves these outside the stack: the VPC CNI's interfaces of a node Spot reclaimed, and the
# cluster security group when the cluster goes before its interfaces do. Either blocks the VPC delete.
delete_eks_leftovers() {
	local vpc eni sg
	for vpc in $(aws cloudformation describe-stack-resources --stack-name "$STACK" \
		--query "StackResources[?ResourceType=='AWS::EC2::VPC'].PhysicalResourceId" --output text); do
		for eni in $(aws ec2 describe-network-interfaces --filters "Name=vpc-id,Values=$vpc" Name=status,Values=available \
			"Name=description,Values=aws-K8S-*" --query 'NetworkInterfaces[].NetworkInterfaceId' --output text); do
			aws ec2 delete-network-interface --network-interface-id "$eni" && echo "L5-CLEANUP deleted interface $eni"
		done
		for sg in $(aws ec2 describe-security-groups --filters "Name=vpc-id,Values=$vpc" \
			"Name=group-name,Values=eks-cluster-sg-$STACK-*" --query 'SecurityGroups[].GroupId' --output text); do
			aws ec2 delete-security-group --group-id "$sg" >/dev/null && echo "L5-CLEANUP deleted security group $sg"
		done
	done
}

delete_stack() {
	aws cloudformation delete-stack --stack-name "$STACK"
	aws cloudformation wait stack-delete-complete --stack-name "$STACK"
}

rc=0
status="$(aws cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo GONE)"
echo "Stack $STACK: $status"
if [ "$status" != "GONE" ] && [ "$status" != "DELETE_COMPLETE" ]; then
	L5_STACK="$STACK" L5_ENV=/dev/null "$SCRIPT_DIR/l5ctl" cleanup || rc=1
	echo "Deleting stack $STACK"
	if delete_stack || { echo "Stack delete failed; removing what EKS left in its VPCs and retrying"; delete_eks_leftovers; delete_stack; }; then
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
