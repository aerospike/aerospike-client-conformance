#!/usr/bin/env bats

DIR="$BATS_TEST_DIRNAME/.."

setting() {
  "$DIR/profile-settings.sh" "$1" "$2" | sed -n "s/^$3=//p"
}

@test "default adds a parked node and the node server" {
  [ "$(setting default ce num-nodes)" = 4 ]
  [ "$(setting default ce control)" = true ]
  [ "$(setting default ce network-subnet)" = "" ]
}

@test "every profile prints the same keys" {
  expected="$("$DIR/profile-settings.sh" default ce | cut -d= -f1)"
  for profile in reachable pass-through-names pass-through-ports port-map-range cn-only low-mtu port-range idle-reset; do
    [ "$("$DIR/profile-settings.sh" "$profile" ce | cut -d= -f1)" = "$expected" ]
  done
}

@test "port-map-range fixes the subnet and starts the node server" {
  [ "$(setting port-map-range ce network-subnet)" = 172.30.66.0/24 ]
  [ "$(setting port-map-range ce control)" = true ]
}

@test "pass-through-ports advertises an alternate host the node server reuses" {
  [ "$(setting pass-through-ports ce alternate-host)" = as-pass-through.test ]
  [ "$(setting pass-through-ports ce extra-hosts)" = as-pass-through.test:127.0.0.1 ]
}

@test "port-range sets the proxy source port range" {
  [ "$(setting port-range ce proxy-port-range)" = "40000 40009" ]
  [ "$(setting port-range ce proxy-sysctls)" = "net.ipv4.ip_local_port_range=40000 40009" ]
}

@test "ee outside cn-only uses a SAN-only certificate whose CN is not the tls-name" {
  [ "$(setting default ee tls-cert-cn)" = not-the-tls-name ]
  [ "$(setting default ee cert-shape)" = san-only ]
  [ "$(setting default ce tls-cert-cn)" = aerospike-tls ]
}

@test "cn-only keeps the CN and drops the tls-name SAN" {
  [ "$(setting cn-only ee include-tls-name-san)" = false ]
  [ "$(setting cn-only ee tls-cert-cn)" = aerospike-tls ]
  [ "$(setting cn-only ee cert-shape)" = cn-only ]
}

@test "an unknown profile or subset fails" {
  run "$DIR/profile-settings.sh" nope ce
  [ "$status" -eq 1 ]
  [[ "$output" == *"unknown profile nope"* ]]
  run "$DIR/profile-settings.sh" default xx
  [ "$status" -eq 1 ]
  [[ "$output" == *"subset must be ce or ee"* ]]
}

@test "write-env writes every setting and the control dir only when the node server runs" {
  export RUNNER_TEMP="$BATS_TEST_TMPDIR" GITHUB_OUTPUT="$BATS_TEST_TMPDIR/out"
  export L2_PROFILE=port-map-range DISCOVERY_SUBSET=ce DISCOVERY_PORT_MAP_RANGE=172.30.66.2-172.30.66.17
  CONTROL=false run "$DIR/write-env.sh"
  [ "$status" -eq 0 ]
  env_file="$BATS_TEST_TMPDIR/discovery-l2.env"
  grep -qx "L2_PROFILE=port-map-range" "$env_file"
  grep -qx "DISCOVERY_PORT_MAP_RANGE=172.30.66.2-172.30.66.17" "$env_file"
  grep -qx "DISCOVERY_REACHABLE_NODE=" "$env_file"
  grep -qx "TOXIPROXY_CONTROL_PORT=8474" "$env_file"
  ! grep -q "^DISCOVERY_CONTROL_DIR=" "$env_file"
  grep -qx "env-file=$env_file" "$GITHUB_OUTPUT"

  CONTROL=true CONTROL_DIR=/work/.discovery-control NODE_SERVER_LOG=/tmp/log run "$DIR/write-env.sh"
  grep -qx "DISCOVERY_CONTROL_DIR=/work/.discovery-control" "$env_file"
  grep -qx "node-server-log=/tmp/log" "$GITHUB_OUTPUT"
}

@test "write-env refuses a value with a newline" {
  export RUNNER_TEMP="$BATS_TEST_TMPDIR"
  DISCOVERY_PROXY_MAP=$'a\nb' run "$DIR/write-env.sh"
  [ "$status" -eq 1 ]
  [[ "$output" == *"DISCOVERY_PROXY_MAP contains a newline"* ]]
}
