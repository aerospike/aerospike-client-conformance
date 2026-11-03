#!/usr/bin/env bats

SERVER="$BATS_TEST_DIRNAME/../discovery-node-server.sh"

setup() {
  STUB="$BATS_TEST_TMPDIR/stub"
  CONTROL="$BATS_TEST_TMPDIR/control"
  mkdir -p "$STUB/bin" "$STUB/written" "$CONTROL"
  export STUB
  cat > "$STUB/template.conf" << 'EOF'
network {
    service {
        address any
        port 3000
    }
}
EOF
  cat > "$STUB/inspect.json" << 'EOF'
[{"Config": {"Image": "aerospike/aerospike-server:8.1",
             "Entrypoint": ["/usr/bin/as-tini-static"],
             "Cmd": ["asd", "--config-file", "/etc/aerospike-custom/discovery-as-4.conf"]},
  "Mounts": [{"Type": "volume", "Name": "discovery-conf", "Destination": "/etc/aerospike-custom", "RW": true},
             {"Type": "bind", "Source": "/tmp/features.conf", "Destination": "/etc/features.conf", "RW": false}],
  "HostConfig": {"ExtraHosts": ["alt.example:10.0.0.9"]}}]
EOF
  cat > "$STUB/bin/docker" << 'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB/calls"
case "$1" in
  inspect)
    if [[ "$2" == -f && "$3" == *State* ]]; then
      cat "$STUB/state-$4" 2> /dev/null || { echo "Error: No such object: $4" >&2; exit 1; }
    elif [[ "$2" == -f ]]; then
      echo "172.30.66.9"
    elif [[ "$2" == discovery-as-4 ]]; then
      cat "$STUB/inspect.json"
    else
      echo "Error: No such object: $2" >&2
      echo "[]"
      exit 1
    fi
    ;;
  cp)
    if [[ "$2" == *:* ]]; then
      cp "$STUB/template.conf" "$3"
    else
      cp "$2" "$STUB/written/${3##*/}"
    fi
    ;;
  logs) echo "log of $2" ;;
  exec)
    [[ -e "$STUB/resolves" ]] && { echo "10.0.0.1  $5"; exit 0; }
    exit 2
    ;;
  run)
    if [[ "$2" == --rm ]]; then
      printf '%s\n' "$@" > "$STUB/lookup-args"
      exit 2
    fi
    [[ ! -e "$STUB/fail-run" ]] || { echo "run failed" >&2; exit 1; }
    printf '%s\n' "$@" > "$STUB/run-args"
    ;;
  rm) ;;
esac
EOF
  chmod +x "$STUB/bin/docker"
  PATH="$STUB/bin:$PATH"
}

teardown() {
  [[ -z "${pid:-}" ]] || kill "$pid" 2> /dev/null || true
}

serve() {
  "$SERVER" "$CONTROL" discovery-aerospike-net "${1:-discovery-as-4}" discovery-as-1 "${2:-}" > "$BATS_TEST_TMPDIR/log" 2>&1 &
  pid=$!
  for _ in $(seq 50); do
    [[ -e "$CONTROL/ready" ]] && return 0
    sleep 0.1
  done
  return 1
}

request() {
  printf '%s\n' "$2" > "$CONTROL/$1.tmp"
  mv "$CONTROL/$1.tmp" "$CONTROL/$1.request"
  for _ in $(seq 50); do
    if [[ -e "$CONTROL/$1.ack" ]]; then
      cat "$CONTROL/$1.ack"
      return 0
    fi
    sleep 0.1
  done
  return 1
}

@test "exits during startup when the parked container cannot be inspected" {
  run "$SERVER" "$CONTROL" discovery-aerospike-net discovery-as-9 discovery-as-1
  [ "$status" -eq 1 ]
  [[ "$output" == *"cannot inspect parked container discovery-as-9"* ]]
  [ ! -e "$CONTROL/ready" ]
}

@test "exits during startup when the parked container has no index" {
  run "$SERVER" "$CONTROL" discovery-aerospike-net discovery-as discovery-as-1
  [ "$status" -eq 1 ]
  [[ "$output" == *"does not end in -<index>"* ]]
}

@test "start clones the parked node as the next index and acks its name and address" {
  serve
  run request a1 "start"
  [ "$status" -eq 0 ]
  [ "$output" = $'container=discovery-as-5\naddress=172.30.66.9' ]
  [ ! -e "$CONTROL/a1.request" ]
  [ -e "$STUB/written/discovery-as-5.conf" ]
  run cat "$STUB/run-args"
  [[ "$output" == *$'--name\ndiscovery-as-5\n'* ]]
  [[ "$output" == *$'--network\ndiscovery-aerospike-net\n'* ]]
  [[ "$output" == *$'-v\ndiscovery-conf:/etc/aerospike-custom\n'* ]]
  [[ "$output" == *$'-v\n/tmp/features.conf:/etc/features.conf:ro\n'* ]]
  [[ "$output" == *$'--add-host\nalt.example:10.0.0.9\n'* ]]
  [[ "$output" == *$'--entrypoint\n/usr/bin/as-tini-static\n'* ]]
  [[ "$output" == *$'aerospike/aerospike-server:8.1\nasd\n--config-file\n/etc/aerospike-custom/discovery-as-5.conf' ]]
}

@test "a second start takes the next index" {
  serve
  request b1 "start"
  run request b2 "start"
  [[ "$output" == container=discovery-as-6* ]]
}

@test "start address= passes a fixed IP" {
  serve
  request c1 "start address=172.30.66.6"
  run cat "$STUB/run-args"
  [[ "$output" == *$'--ip\n172.30.66.6\n'* ]]
}

@test "start address= rejects anything but an IPv4 address" {
  serve
  run request c2 "start address=node-6"
  [ "$output" = "error=address=node-6 is not an IPv4 address" ]
  [ ! -e "$STUB/run-args" ]
}

@test "start alternate-port= adds the alternate access lines after the service port" {
  serve discovery-as-4 alt.example
  request d1 "start alternate-port=4000"
  run cat "$STUB/written/discovery-as-5.conf"
  [[ "$output" == *$'        port 3000\n        alternate-access-address alt.example\n        alternate-access-port 4000\n    }'* ]]
}

@test "start alternate-port= advertises whichever alternate host the server was given" {
  serve discovery-as-4 as-unresolved.test
  request d3 "start alternate-port=4001"
  run cat "$STUB/written/discovery-as-5.conf"
  [[ "$output" == *$'        alternate-access-address as-unresolved.test\n        alternate-access-port 4001\n'* ]]
}

@test "start alternate-port= needs an alternate host" {
  serve
  run request d2 "start alternate-port=4000"
  [ "$output" = "error=alternate-port=4000 needs a port and an alternate host" ]
}

@test "start rejects an unknown option" {
  serve
  run request e1 "start replicas=2"
  [ "$output" = "error=unknown start option replicas=2" ]
}

@test "a failed docker run answers with its error" {
  serve
  touch "$STUB/fail-run"
  run request e2 "start"
  [ "$output" = "error=run failed " ]
}

@test "stop removes a node this server started" {
  serve
  request f1 "start"
  run request f2 "stop discovery-as-5"
  [ "$output" = "removed=discovery-as-5" ]
  grep -qx "rm -f discovery-as-5" "$STUB/calls"
}

@test "stop refuses a node this server did not start" {
  serve
  run request f3 "stop discovery-as-1"
  [ "$output" = "error=cannot remove discovery-as-1" ]
  ! grep -q "^rm " "$STUB/calls"
}

@test "status of a running node acks its state, log and the lookup in the container" {
  serve discovery-as-4 as-unresolved.test
  request h1 "start alternate-port=4000"
  echo "true 0" > "$STUB/state-discovery-as-5"
  run request h2 "status discovery-as-5"
  [ "$output" = "$(printf 'running=true\nexit-code=0\nlog=%s\nlookup-in=container\nlookup-exit=2\nlookup= ' "$CONTROL/discovery-as-5.log")" ]
  [ "$(cat "$CONTROL/discovery-as-5.log")" = "log of discovery-as-5" ]
  grep -qx "exec discovery-as-5 getent hosts as-unresolved.test" "$STUB/calls"
  [ ! -e "$STUB/lookup-args" ]
}

@test "status of an exited node looks the hostname up in a new container from the same image and network" {
  serve discovery-as-4 as-unresolved.test
  request h3 "start alternate-port=4000"
  echo "false 1" > "$STUB/state-discovery-as-5"
  run request h4 "status discovery-as-5"
  [ "$output" = "$(printf 'running=false\nexit-code=1\nlog=%s\nlookup-in=image\nlookup-exit=2\nlookup= ' "$CONTROL/discovery-as-5.log")" ]
  run cat "$STUB/lookup-args"
  [[ "$output" == $'run\n--rm\n--name\ndiscovery-as-lookup-h4\n--network\ndiscovery-aerospike-net\n--add-host\nalt.example:10.0.0.9\n--entrypoint\ngetent\naerospike/aerospike-server:8.1\nhosts\nas-unresolved.test' ]]
  ! grep -q "^exec " "$STUB/calls"
}

@test "status reports what the hostname resolves to" {
  serve discovery-as-4 as-unresolved.test
  echo "true 0" > "$STUB/state-discovery-as-1"
  touch "$STUB/resolves"
  run request h5 "status discovery-as-1"
  [[ "$output" == *$'\nlookup-exit=0\nlookup=10.0.0.1  as-unresolved.test ' ]]
}

@test "status without an alternate host acks no lookup" {
  serve
  echo "true 0" > "$STUB/state-discovery-as-1"
  run request h6 "status discovery-as-1"
  [ "$output" = "$(printf 'running=true\nexit-code=0\nlog=%s\nlookup-in=\nlookup-exit=\nlookup= ' "$CONTROL/discovery-as-1.log")" ]
  ! grep -q "^exec " "$STUB/calls"
}

@test "status refuses a container it cannot inspect or outside the node prefix" {
  serve
  run request h7 "status discovery-as-7"
  [ "$output" = "error=cannot inspect discovery-as-7 Error: No such object: discovery-as-7 " ]
  echo "true 0" > "$STUB/state-other-1"
  run request h8 "status other-1"
  [ "$output" = "error=cannot inspect other-1 " ]
  ! grep -q "State.* other-1" "$STUB/calls"
}

@test "an unknown request answers with an error" {
  serve
  run request g1 "restart discovery-as-5"
  [ "$output" = "error=unknown request: restart discovery-as-5" ]
}

@test "start-server.sh returns once the server is ready" {
  export CONTROL_DIR="$CONTROL" NETWORK_NAME=discovery-aerospike-net PARKED_CONTAINER=discovery-as-4 \
    CONFIG_CONTAINER=discovery-as-1 LOG_FILE="$BATS_TEST_TMPDIR/server.log"
  run "$BATS_TEST_DIRNAME/../start-server.sh"
  [ "$status" -eq 0 ]
  [ -e "$CONTROL/ready" ]
  pkill -f "discovery-node-server.sh $CONTROL" || true
}

@test "start-server.sh fails with the log when the server exits during startup" {
  export CONTROL_DIR="$CONTROL" NETWORK_NAME=discovery-aerospike-net PARKED_CONTAINER=discovery-as-9 \
    CONFIG_CONTAINER=discovery-as-1 LOG_FILE="$BATS_TEST_TMPDIR/server.log"
  run "$BATS_TEST_DIRNAME/../start-server.sh"
  [ "$status" -eq 1 ]
  [[ "$output" == *"cannot inspect parked container discovery-as-9"* ]]
  [[ "$output" == *"exited during startup"* ]]
}
