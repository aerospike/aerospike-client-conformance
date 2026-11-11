#!/bin/sh
# Regenerate certs/<shape>.pem for every shape in name-matching.yaml. Self-signed, valid for 100 years.
# Private keys are written to a temporary directory and deleted; only certificates are kept.
#
# generate-certs.sh --server <dir> (<cn> <san> | --shape <shape>) instead writes a certificate for a
# test server to <dir>: ca.crt, server.crt signed by it, and server.key. <san> is an openssl
# subjectAltName value such as DNS:a.example.com,IP:127.0.0.1, or - for none. The CA key is deleted.
set -eu

here=$(cd "$(dirname "$0")" && pwd)

require_yq() {
	if ! command -v yq >/dev/null 2>&1; then
		echo "generate-certs.sh: yq (https://github.com/mikefarah/yq) is required" >&2
		exit 1
	fi
}

shape() {
	require_yq
	yq -r ".shapes | to_entries[] | select(.key == \"$1\") | [.key, .value.subjectCN, (.value.san | join(\",\"))] | join(\"|\")" \
		"$here/name-matching.yaml"
}

server() {
	dir=$1 cn=$2 san=$3
	mkdir -p "$dir"
	openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 36500 \
		-keyout "$dir/ca.key" -subj "/CN=Aerospike-Server-Test-CA" -addext "keyUsage=critical,keyCertSign,cRLSign" \
		-out "$dir/ca.crt" 2>/dev/null
	openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
		-keyout "$dir/server.key" -subj "/CN=$cn" -out "$dir/server.csr" 2>/dev/null
	printf 'basicConstraints = CA:FALSE\nsubjectKeyIdentifier = hash\nauthorityKeyIdentifier = keyid\n' > "$dir/server.ext"
	[ "$san" = - ] || printf 'subjectAltName = %s\n' "$san" >> "$dir/server.ext"
	openssl x509 -req -in "$dir/server.csr" -CA "$dir/ca.crt" -CAkey "$dir/ca.key" -CAcreateserial -days 36500 \
		-extfile "$dir/server.ext" -out "$dir/server.crt" 2>/dev/null
	rm -f "$dir/ca.key" "$dir/ca.srl" "$dir/server.csr" "$dir/server.ext"
	chmod 644 "$dir/server.key"
	echo "$dir/server.crt"
}

if [ "${1:-}" = --server ]; then
	[ $# -eq 4 ] || { echo "usage: generate-certs.sh --server <dir> (<cn> <san> | --shape <shape>)" >&2; exit 2; }
	if [ "$3" = --shape ]; then
		line=$(shape "$4")
		[ -n "$line" ] || { echo "generate-certs.sh: no shape '$4' in name-matching.yaml" >&2; exit 2; }
		cn=${line#*|}
		san=${cn#*|}
		server "$2" "${cn%%|*}" "${san:--}"
	else
		server "$2" "$3" "$4"
	fi
	exit 0
fi

require_yq
cd "$here"
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
