#!/bin/sh
# Regenerate certs/<shape>.pem for every shape in name-matching.yaml. Self-signed, valid for 100 years.
# Private keys are written to a temporary directory and deleted; only certificates are kept.
set -eu

cd "$(dirname "$0")"

if ! command -v yq >/dev/null 2>&1; then
	echo "generate-certs.sh: yq (https://github.com/mikefarah/yq) is required" >&2
	exit 1
fi

out=certs
rm -rf "$out"
mkdir -p "$out"
keys=$(mktemp -d)
trap 'rm -rf "$keys"' EXIT

yq -r '.shapes | to_entries[] | [.key, .value.subjectCN, (.value.san | join(","))] | join("|")' name-matching.yaml \
	> "$keys/shapes"

while IFS='|' read -r name cn san; do
	if [ -n "$san" ]; then
		openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 36500 \
			-keyout "$keys/$name.key" -subj "/CN=$cn" -addext "subjectAltName=$san" \
			-out "$out/$name.pem" 2>/dev/null
	else
		openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 36500 \
			-keyout "$keys/$name.key" -subj "/CN=$cn" \
			-out "$out/$name.pem" 2>/dev/null
	fi
	echo "$out/$name.pem"
done < "$keys/shapes"
