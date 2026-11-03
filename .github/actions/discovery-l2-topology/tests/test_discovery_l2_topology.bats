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

@test "the servers map every advertised hostname unless unresolvable-hostnames is true" {
  [ "$(setting reachable ce extra-hosts)" = as-reachable.test:127.0.0.1 ]
  [ "$(setting pass-through-names ce extra-hosts)" = node-1.pass-through.test:127.0.0.1,node-2.pass-through.test:127.0.0.1,node-3.pass-through.test:127.0.0.1 ]
  for profile in reachable pass-through-names pass-through-ports; do
    [ -n "$("$DIR/profile-settings.sh" "$profile" ce | sed -n 's/^extra-hosts=//p')" ]
    [ -z "$("$DIR/profile-settings.sh" "$profile" ce true | sed -n 's/^extra-hosts=//p')" ]
    [ "$("$DIR/profile-settings.sh" "$profile" ee true | grep -v '^extra-hosts=')" = "$("$DIR/profile-settings.sh" "$profile" ee | grep -v '^extra-hosts=')" ]
  done
}

@test "unresolvable-hostnames false keeps every profile's settings" {
  for profile in default reachable pass-through-names pass-through-ports port-map-range cn-only low-mtu port-range idle-reset; do
    for subset in ce ee; do
      [ "$("$DIR/profile-settings.sh" "$profile" "$subset" false)" = "$("$DIR/profile-settings.sh" "$profile" "$subset")" ]
    done
  done
}

@test "unresolved-access is the default topology with an unmapped alternate host for the node server" {
  for subset in ce ee; do
    [ "$(setting unresolved-access "$subset" alternate-host)" = as-unresolved.test ]
    [ "$(setting unresolved-access "$subset" extra-hosts)" = "" ]
    [ "$("$DIR/profile-settings.sh" unresolved-access "$subset" | grep -v '^alternate-host=')" = "$("$DIR/profile-settings.sh" default "$subset" | grep -v '^alternate-host=')" ]
  done
  [ "$("$DIR/profile-settings.sh" unresolved-access ce | cut -d= -f1)" = "$("$DIR/profile-settings.sh" default ce | cut -d= -f1)" ]
}

@test "verbatim-access-address true or false keeps every profile's settings" {
  for profile in default reachable pass-through-names pass-through-ports port-map-range cn-only low-mtu port-range idle-reset unresolved-access; do
    for subset in ce ee; do
      [ "$("$DIR/profile-settings.sh" "$profile" "$subset" false false)" = "$("$DIR/profile-settings.sh" "$profile" "$subset")" ]
      [ "$("$DIR/profile-settings.sh" "$profile" "$subset" false true)" = "$("$DIR/profile-settings.sh" "$profile" "$subset")" ]
    done
  done
}

@test "verbatim-access-address accepts only true or false" {
  for value in 1 0 yes TRUE ""; do
    run "$DIR/profile-settings.sh" unresolved-access ce false "$value"
    [ "$status" -eq 1 ]
    [[ "$output" == *"verbatim-access-address must be true or false (got: $value)"* ]]
  done
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
  run "$DIR/profile-settings.sh" reachable ce yes
  [ "$status" -eq 1 ]
  [[ "$output" == *"unresolvable-hostnames must be true or false"* ]]
}

fake_docker() {
  mkdir -p "$BATS_TEST_TMPDIR/bin"
  cat > "$BATS_TEST_TMPDIR/bin/docker" << 'EOF'
#!/usr/bin/env bash
# docker exec <container> getent hosts <hostname>: RESOLVES lists container/hostname pairs that resolve.
[[ " ${RESOLVES:-} " == *" $2/$5 "* ]] && { echo "127.0.0.1 $5"; exit 0; }
exit "${LOOKUP_STATUS:-2}"
EOF
  chmod +x "$BATS_TEST_TMPDIR/bin/docker"
  PATH="$BATS_TEST_TMPDIR/bin:$PATH"
}

@test "check-unresolvable passes when no hostname resolves in any node" {
  fake_docker
  run "$DIR/check-unresolvable.sh" discovery-as-1,discovery-as-2 a.test b.test
  [ "$status" -eq 0 ]
  [[ "$output" == *"b.test does not resolve in discovery-as-2"* ]]
}

@test "check-unresolvable fails with the hostname and container that resolves" {
  fake_docker
  RESOLVES=discovery-as-2/b.test run "$DIR/check-unresolvable.sh" discovery-as-1,discovery-as-2 a.test b.test
  [ "$status" -eq 1 ]
  [[ "$output" == *"b.test resolves in discovery-as-2: 127.0.0.1 b.test"* ]]
}

@test "check-unresolvable fails when the lookup cannot run" {
  fake_docker
  LOOKUP_STATUS=125 run "$DIR/check-unresolvable.sh" discovery-as-1 a.test
  [ "$status" -eq 1 ]
  [[ "$output" == *"Cannot look up a.test in discovery-as-1 (exit 125)"* ]]
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

@test "write-env sets the unresolved hostname and verbatim flag only for unresolved-access" {
  export RUNNER_TEMP="$BATS_TEST_TMPDIR" GITHUB_OUTPUT="$BATS_TEST_TMPDIR/out"
  env_file="$BATS_TEST_TMPDIR/discovery-l2.env"
  L2_PROFILE=unresolved-access ALTERNATE_HOST=as-unresolved.test VERBATIM_ACCESS_ADDRESS=false run "$DIR/write-env.sh"
  [ "$status" -eq 0 ]
  grep -qx "DISCOVERY_UNRESOLVED_HOSTNAME=as-unresolved.test" "$env_file"
  grep -qx "DISCOVERY_VERBATIM_ACCESS_ADDRESS=0" "$env_file"

  L2_PROFILE=unresolved-access ALTERNATE_HOST=as-unresolved.test VERBATIM_ACCESS_ADDRESS=true run "$DIR/write-env.sh"
  grep -qx "DISCOVERY_VERBATIM_ACCESS_ADDRESS=1" "$env_file"

  L2_PROFILE=unresolved-access ALTERNATE_HOST=as-unresolved.test run "$DIR/write-env.sh"
  grep -qx "DISCOVERY_VERBATIM_ACCESS_ADDRESS=0" "$env_file"

  for profile in default pass-through-ports; do
    L2_PROFILE=$profile ALTERNATE_HOST=as-pass-through.test VERBATIM_ACCESS_ADDRESS=true \
      DISCOVERY_UNRESOLVED_HOSTNAME=leaked DISCOVERY_VERBATIM_ACCESS_ADDRESS=1 run "$DIR/write-env.sh"
    [ "$status" -eq 0 ]
    grep -qx "DISCOVERY_UNRESOLVED_HOSTNAME=" "$env_file"
    grep -qx "DISCOVERY_VERBATIM_ACCESS_ADDRESS=" "$env_file"
  done
}

@test "apply-profile unresolved-access passes when the hostname resolves in no running node" {
  fake_docker
  L2_PROFILE=unresolved-access AEROSPIKE_CONTAINER_NAMES=discovery-as-1,discovery-as-2,discovery-as-3 \
    DISCOVERY_PROXY_MAP=discovery-as-1:3000 ALTERNATE_HOST=as-unresolved.test VERBATIM_ACCESS_ADDRESS=false \
    run "$DIR/apply-profile.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"advertises access addresses verbatim: false"* ]]
  [[ "$output" == *"as-unresolved.test does not resolve in discovery-as-3"* ]]
}

@test "apply-profile unresolved-access fails when the hostname resolves in a running node" {
  fake_docker
  RESOLVES=discovery-as-2/as-unresolved.test L2_PROFILE=unresolved-access \
    AEROSPIKE_CONTAINER_NAMES=discovery-as-1,discovery-as-2,discovery-as-3 DISCOVERY_PROXY_MAP=discovery-as-1:3000 \
    ALTERNATE_HOST=as-unresolved.test VERBATIM_ACCESS_ADDRESS=true run "$DIR/apply-profile.sh"
  [ "$status" -eq 1 ]
  [[ "$output" == *"as-unresolved.test resolves in discovery-as-2: 127.0.0.1 as-unresolved.test"* ]]
}

@test "write-env refuses a value with a newline" {
  export RUNNER_TEMP="$BATS_TEST_TMPDIR"
  DISCOVERY_PROXY_MAP=$'a\nb' run "$DIR/write-env.sh"
  [ "$status" -eq 1 ]
  [[ "$output" == *"DISCOVERY_PROXY_MAP contains a newline"* ]]
}
