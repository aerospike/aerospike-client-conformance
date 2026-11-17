#!/usr/bin/env bash
#
# AWS EC2 tag seed discovery against a stub EC2 endpoint: a two node cluster and
# discovery/providers/stub-ec2-server.py on the host. Node A is reachable on ENV_HOST and node B on
# this host's outbound IPv4 address, both on the same port, because the provider sets one port on
# every seed. Node B is published on ENV_HOST:ENV_PORT_BASE+11 and reached on its address through a
# TCP forwarder on the host, because rootless podman cannot publish one port on two host addresses.
# Each node advertises a private address (10.0.0.1, 10.0.0.2) as alternate-access-address, which
# the client maps to the node's reachable address with an IP map. The stub returns both nodes as
# running instances tagged aerospike-cluster=prod-a whose private IP is the node's reachable address,
# and POST EC2_STUB_CONTROL/reset restores that. Clients get no static seed.
#
# Credentials are fixed fake values in the values file; the stub checks only that each request
# carries a well-formed SigV4 Authorization header for AWS_ACCESS_KEY_ID, AWS_REGION and service ec2.
#
# Values: NODE_A_*, NODE_B_* (ADVERTISED_HOST is the private address), NODE_A_INSTANCE_ID,
# NODE_B_INSTANCE_ID, EC2_ENDPOINT, EC2_STUB_CONTROL (control API base URL), AWS_REGION,
# AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN, CLIENT_CONFIG (YAML selecting
# provider aws-tags with the stub endpoint). Nodes: a, b. Ports: ENV_PORT_BASE+10 to +12; node A and
# node B share ENV_PORT_BASE+10.
#
# Required environment: AEROSPIKE_IMAGE; FEATURES_CONF_PATH for an Enterprise image
# Optional environment: ENV_HOST_B (node B's address)
#
# Usage: envs/aws-tags.sh up | down | info <node> <info command>

ENV_NAME=aws-tags
PORT_OFFSET=10
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

STUB="$(cd "$ENVS_DIR/../discovery/providers" && pwd)/stub-ec2-server.py"
REGION=us-east-1
ACCESS_KEY=AKIAIOSFODNN7EXAMPLE
PRIVATE_A=10.0.0.1
PRIVATE_B=10.0.0.2
INSTANCE_A="i-0a1b2c3d4e5f60001"
INSTANCE_B="i-0a1b2c3d4e5f60002"

env_up() {
	local port_b=$((PORT + 1)) stub_port=$((PORT + 2)) host_b net

	require_server
	host_b="$(host_b)"
	require_ports "$ENV_HOST:$PORT" "$host_b:$PORT" "$ENV_HOST:$port_b" "$ENV_HOST:$stub_port"
	write_conf a.conf "$ENV_NAME" A1 "$NAME-b" "$PRIVATE_A" "$PORT"
	write_conf b.conf "$ENV_NAME" B2 "$NAME-a" "$PRIVATE_B" "$PORT"
	cat > "$WORK_DIR/client.yaml" <<EOF
version: 1.2.0
static:
  client:
    use_services_alternate: true
    ip_map:
      "$PRIVATE_A": "$ENV_HOST"
      "$PRIVATE_B": "$host_b"
    discovery:
      provider: aws-tags
      execution_mode: thread
      refresh_interval_ms: 1000
      refresh_timeout_ms: 50
      aws:
        region: $REGION
        filters:
          - name: tag:aerospike-cluster
            values:
              - prod-a
        address_field: private_ip
        port: $PORT
        timeout_ms: 50
        init_timeout_ms: 5000
        endpoint: http://$ENV_HOST:$stub_port
EOF

	log "Starting A1 on $ENV_HOST:$PORT, B2 on $host_b:$PORT through $ENV_HOST:$port_b ($AEROSPIKE_IMAGE)"
	net="$(network "")"
	node run a "$net" "$ENV_HOST:$PORT:3000" a.conf
	node run b "$net" "$ENV_HOST:$port_b:3000" b.conf
	wait_cluster a 2
	wait_cluster b 2
	forward node-b "$host_b" "$PORT" "$port_b"
	log "Starting stub EC2 endpoint on http://$ENV_HOST:$stub_port, region $REGION, access key $ACCESS_KEY"
	background stub-ec2 python3 "$STUB" --host "$ENV_HOST" --port "$stub_port" --access-key "$ACCESS_KEY" \
		--region "$REGION" --instance "$INSTANCE_A=$ENV_HOST" --instance "$INSTANCE_B=$host_b"

	put_node NODE_A A1 "$ENV_HOST" "$PORT" "$PRIVATE_A" "$PORT"
	put_node NODE_B B2 "$host_b" "$PORT" "$PRIVATE_B" "$PORT"
	put NODE_A_INSTANCE_ID "$INSTANCE_A"
	put NODE_B_INSTANCE_ID "$INSTANCE_B"
	put EC2_ENDPOINT "http://$ENV_HOST:$stub_port"
	put EC2_STUB_CONTROL "http://$ENV_HOST:$stub_port/stub"
	put AWS_REGION "$REGION"
	put AWS_ACCESS_KEY_ID "$ACCESS_KEY"
	put AWS_SECRET_ACCESS_KEY wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
	put AWS_SESSION_TOKEN FwoGZXIvYXdzEXAMPLEFAKESESSIONTOKENFORSTUBTESTS
	put CLIENT_CONFIG "$WORK_DIR/client.yaml"
}

env_main "$@"
