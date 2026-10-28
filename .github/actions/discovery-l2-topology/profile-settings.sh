#!/usr/bin/env bash
# Print the settings of an L2 profile as key=value lines.
set -euo pipefail

profile="${1:?profile}"
subset="${2:?subset}"

num_nodes=3
extra_hosts=""
subnet=""
control=false
alternate_host=""
tls_name_san=true
cert_cn=aerospike-tls
cert_shape=""
client_mtu=""
port_range=""
proxy_sysctls=""
idle_reset=""

case "$subset" in
  ce | ee) ;;
  *)
    echo "Error: subset must be ce or ee (got: $subset)" >&2
    exit 1
    ;;
esac

case "$profile" in
  default)
    num_nodes=4
    control=true
    ;;
  reachable)
    extra_hosts=as-reachable.test:127.0.0.1
    ;;
  pass-through-names)
    extra_hosts=node-1.pass-through.test:127.0.0.1,node-2.pass-through.test:127.0.0.1,node-3.pass-through.test:127.0.0.1
    ;;
  pass-through-ports)
    num_nodes=4
    control=true
    alternate_host=as-pass-through.test
    extra_hosts=as-pass-through.test:127.0.0.1
    ;;
  port-map-range)
    num_nodes=4
    control=true
    subnet=172.30.66.0/24
    ;;
  cn-only)
    tls_name_san=false
    cert_shape=cn-only
    ;;
  low-mtu)
    client_mtu=1200
    ;;
  port-range)
    port_range="40000 40009"
    proxy_sysctls="net.ipv4.ip_local_port_range=${port_range}"
    ;;
  idle-reset)
    idle_reset=1
    ;;
  *)
    echo "Error: unknown profile $profile" >&2
    exit 1
    ;;
esac

# Outside cn-only the CN must not match the tls-name, so only SAN matching can pass.
if [[ "$subset" == "ee" && "$profile" != "cn-only" ]]; then
  cert_cn=not-the-tls-name
  cert_shape=san-only
fi

cat << EOF
num-nodes=${num_nodes}
extra-hosts=${extra_hosts}
network-subnet=${subnet}
control=${control}
alternate-host=${alternate_host}
include-tls-name-san=${tls_name_san}
tls-cert-cn=${cert_cn}
cert-shape=${cert_shape}
client-network-mtu=${client_mtu}
proxy-port-range=${port_range}
proxy-sysctls=${proxy_sysctls}
idle-reset=${idle_reset}
EOF
