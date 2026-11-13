# Test environments

The provider, translator and TLS name matching integration tests need the same servers for every client: Aerospike clusters that advertise particular addresses, a DNS server, a stub EC2 endpoint and TLS servers with particular certificates. Each script here brings one environment up on the local container runtime, writes the values a test needs to a file and brings it down again. A client only configures its client from those values.

| Environment    | What it is                                                                                                             |
| -------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `dns`          | Two node cluster on two host addresses, a standalone node and a CoreDNS server for zone `dns.test`                     |
| `aws-tags`     | Two node cluster on two host addresses advertising private addresses, and a stub EC2 endpoint returning both nodes     |
| `port-map`     | Two node cluster advertising only container addresses, node B created but not started                                  |
| `collision`    | A control cluster and a cluster whose two nodes advertise the same host and port                                       |
| `yaml`         | A pass-through cluster and a port-map cluster, each with a client YAML config file                                     |
| `pass-through` | Two node cluster, each node advertising its own published host port as alternate access address                        |
| `on-miss`      | The same cluster as `pass-through`, for translation miss cases                                                         |
| `alt-missing`  | A cluster where node B has no alternate access address, and a control cluster where both nodes have one                |
| `tls-matching` | A TLS server per certificate name matching case, and a two node TLS cluster reached through one TCP forwarder per node |

Each script's header says exactly what its environment is.

## Running one locally

```sh
export AEROSPIKE_IMAGE=aerospike/aerospike-server-enterprise:8.1
export FEATURES_CONF_PATH=$HOME/features.conf
values="$(envs/dns.sh up)"
set -a; . "$values"; set +a
# run the client's tests, which read NODE_A_HOST, DNS_SERVER and so on from the environment
envs/dns.sh down
```

`up` starts everything, waits until each cluster is stable, writes the values file and prints its path as the only line on stdout; progress goes to stderr. If `up` fails it removes what it created. `down` removes every container, network and host process the environment created and empties its work directory. A second `up` of the same environment fails until `down` has run.

Every script takes `up`, `down` and `info <node> <info command>`, which runs `asinfo -v <info command>` on that node, for example `envs/collision.sh info collision-b service-clear-alt`. Some take more verbs, listed below. A verb reads the same options as `up`, so run it with the values file loaded or with the same options set.

Requirements: `docker` or `podman`, `python3`, and for `tls-matching` also `openssl` and `yq`.

| Option               | Default                            | Meaning                                                                                                                                                                               |
| -------------------- | ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AEROSPIKE_IMAGE`    | required                           | Server image. Community images work for every environment but `tls-matching`                                                                                                          |
| `FEATURES_CONF_PATH` | unset                              | Feature-key file, required for an Enterprise image. Mounted into each node, never printed                                                                                             |
| `CONTAINER_RUNTIME`  | `docker`                           | `docker` or `podman`                                                                                                                                                                  |
| `ENV_PREFIX`         | `conformance`                      | Prefix of every container, network and work directory: `<ENV_PREFIX>-<environment>-<node>`. Containers and networks also carry the label `conformance-env=<ENV_PREFIX>-<environment>` |
| `ENV_PORT_BASE`      | `4700`                             | First host port. Each environment uses its own block above it, so every environment can be up at once on one base                                                                     |
| `ENV_WORK_ROOT`      | `envs/work`                        | Directory holding each environment's configs, certificates, logs and values file                                                                                                      |
| `ENV_HOST_B`         | default route IPv4                 | Second host address, for `dns` and `aws-tags`                                                                                                                                         |
| `COREDNS_IMAGE`      | `docker.io/coredns/coredns:1.11.3` | DNS server image for `dns`                                                                                                                                                            |

Every host port is checked before anything starts, and `up` fails naming the ports in use. Two runs on one machine need different `ENV_PREFIX` values and port bases at least 110 apart. All ports are published on 127.0.0.1 only.

The `dns` and `aws-tags` clusters have node B on this host's outbound IPv4 address on the same port as node A on 127.0.0.1. Rootless podman cannot publish one port on two host addresses, so node B is published on 127.0.0.1 on another port and a TCP forwarder on the host (`forward.py`) listens on the second address. The forwarder runs with Docker too.

## Values

The values file has one `KEY=value` per line, no quoting, and can be sourced by a shell or appended to `$GITHUB_ENV`. Every file starts with:

| Key                                                                 | Value                                                                                  |
| ------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `ENV_NAME`                                                          | Environment name                                                                       |
| `ENV_CTL`                                                           | Path of the environment's script, to run its verbs                                     |
| `ENV_VALUES`                                                        | Path of this file                                                                      |
| `ENV_PREFIX`, `ENV_PORT_BASE`, `ENV_WORK_ROOT`, `CONTAINER_RUNTIME` | The options `up` ran with, so a verb run with this file loaded finds the environment   |
| `ENV_HOST`                                                          | `127.0.0.1`, the host every node is published on unless its own `_HOST` says otherwise |
| `ENV_NAMESPACE`                                                     | `test`, replication factor 1                                                           |

A node is five keys under a prefix such as `NODE_A` or `CONTROL_NODE_B`:

| Key                                                | Value                                                                                                                                                                                                                               |
| -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `<node>_NAME`                                      | Node ID, such as `A1`                                                                                                                                                                                                               |
| `<node>_HOST`, `<node>_PORT`                       | Where the node is reachable from this host                                                                                                                                                                                          |
| `<node>_ADVERTISED_HOST`, `<node>_ADVERTISED_PORT` | The access address the node advertises to clients: its alternate access address where it has one, otherwise its container address. Empty when the node has no alternate access address in an environment that sets one on its peers |

`CLIENT_CONFIG` is a client YAML config file for the environment where there is one.

### dns

Node A is on `ENV_HOST`, node B on the second host address, both on `ENV_PORT_BASE`, each advertising that address as its alternate access address. Node C is a cluster of its own on `ENV_PORT_BASE+2`. CoreDNS serves zone `dns.test` and reloads it every second:

| Name                         | Records                                                         |
| ---------------------------- | --------------------------------------------------------------- |
| `seeds.dns.test`             | A `NODE_A_HOST`, A `NODE_B_HOST`                                |
| `_aerospike._tcp.dns.test`   | SRV to `node-a.dns.test` and `node-b.dns.test` on `NODE_A_PORT` |
| `_change._tcp.dns.test`      | SRV to node-a and node-b; `set-change-srv` changes it           |
| `node-a`, `node-b`, `node-c` | A record of each node                                           |

Values: `NODE_A_*`, `NODE_B_*`, `NODE_C_*`, `DNS_SERVER` (`ip:port`, UDP and TCP), `DNS_ZONE_FILE`, `CLIENT_CONFIG` (provider `dns`, name `seeds.dns.test`, record type `a`, no static seed). Nodes: `a`, `b`, `c`.

Verbs: `stop-dns` and `start-dns` stop and start the DNS server. `set-change-srv <node>...` points `_change._tcp` at the given nodes (`node-a`, `node-b`, `node-c`; node C on `NODE_C_PORT`) and bumps the zone serial; `set-change-srv node-a node-b` restores it.

### aws-tags

Node A is on `ENV_HOST` and node B on the second host address, both on `ENV_PORT_BASE+10`, because the provider sets one port on every seed. They advertise 10.0.0.1 and 10.0.0.2 as alternate access addresses, which `CLIENT_CONFIG` maps to their reachable addresses with `ip_map`. The stub EC2 endpoint ([stub-ec2-server.py](../discovery/providers/stub-ec2-server.py)) runs on the host and returns both nodes as running instances tagged `aerospike-cluster=prod-a` whose private IP is the node's reachable address. It checks only that each request carries a well-formed SigV4 `Authorization` header for `AWS_ACCESS_KEY_ID`, `AWS_REGION` and service `ec2`, plus `X-Amz-Date`, and answers `AuthFailure` otherwise.

Values: `NODE_A_*`, `NODE_B_*` (`ADVERTISED_HOST` is the private address), `NODE_A_INSTANCE_ID`, `NODE_B_INSTANCE_ID`, `EC2_ENDPOINT`, `EC2_STUB_CONTROL`, `AWS_REGION`, and fixed fake `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `AWS_SESSION_TOKEN` so the provider's environment credential source runs end to end. `CLIENT_CONFIG` selects provider `aws-tags` with the stub endpoint. Nodes: `a`, `b`.

The stub's control API, JSON over HTTP under `EC2_STUB_CONTROL`:

| Request          | Effect                                                                                                |
| ---------------- | ----------------------------------------------------------------------------------------------------- |
| `GET requests`   | Every EC2 request since the last reset: `params`, `authorization`, `authorization_valid`, `signature` |
| `POST reset`     | Back to both nodes as instances, mode `answer`, page size 1000, no delay, no recorded requests        |
| `POST instances` | Append instances: `[{"id": "i-...", "private_ip": "10.0.0.3"}]`                                       |
| `POST settings`  | Any of `{"mode": "answer" or "unauthorized" or "hang", "page_size": 2, "delay_ms": 700}`              |

`DescribeInstances` returns `page_size` instances per page with `NextToken`. Mode `unauthorized` answers `UnauthorizedOperation` (403) to a well signed request. Mode `hang` holds every request until the mode changes, or for 60 seconds, then closes it without a reply. Errors carry request ID `ea966190-f9aa-478e-9ede-example`. `python3 -m unittest scripts/test_stub_ec2_server.py` tests the stub.

### port-map

Each node advertises only its container address on port 3000, which the host cannot reach, and is published on its own host port. Node addresses are fixed, so a port map can name node B before it exists. Node B is created but not started.

Values: `NODE_A_*`, `NODE_B_*` (`ADVERTISED_HOST:ADVERTISED_PORT` is the container address). Nodes: `a`, `b`.

Verbs: `start-node-b` starts node B and waits for the two node cluster. `node-b-state` prints `running` or `stopped`.

### collision

Every node advertises alternate access address `ENV_HOST`. In the control cluster each node advertises its own published port. In the collision cluster node B advertises node A's published port, so both nodes translate to the same host and port.

Values: `CONTROL_NODE_A_*`, `CONTROL_NODE_B_*`, `COLLISION_NODE_A_*`, `COLLISION_NODE_B_*` (`COLLISION_NODE_B_ADVERTISED_PORT` is node A's port). Nodes: `control-a`, `control-b`, `collision-a`, `collision-b`.

### yaml

Two clusters, each with a client YAML config file that is the client's only configuration. In the pass-through cluster each node advertises `ENV_HOST` and its own published port; the YAML sets `use_services_alternate: true` and `translation.provider: pass-through`. In the port-map cluster each node advertises only its container address; the YAML sets `translation.provider: port-map` and `on_miss: strict`, mapping node A by its advertised `host:port` and node B by node name.

Values: `PASS_THROUGH_NODE_A_*`, `PASS_THROUGH_NODE_B_*`, `PASS_THROUGH_CLIENT_CONFIG`, `PORT_MAP_NODE_A_*`, `PORT_MAP_NODE_B_*`, `PORT_MAP_CLIENT_CONFIG`. Nodes: `pass-through-a`, `pass-through-b`, `port-map-a`, `port-map-b`.

### pass-through and on-miss

Each node advertises alternate access address `ENV_HOST` and its own published port: one host name, a port per node. With `useServicesAlternate` the advertised address is the reachable one.

Values: `NODE_A_*`, `NODE_B_*`. Nodes: `a`, `b`.

### alt-missing

In the missing cluster node A advertises `ENV_HOST` and its own published port, and node B has no alternate access address. In the control cluster both nodes advertise one.

Values: `MISSING_NODE_A_*`, `MISSING_NODE_B_*` (`ADVERTISED_HOST` and `ADVERTISED_PORT` empty), `CONTROL_NODE_A_*`, `CONTROL_NODE_B_*`. Nodes: `missing-a`, `missing-b`, `control-a`, `control-b`.

### tls-matching

`up` writes a CA and a server certificate for every case with [generate-certs.sh](../tls/generate-certs.sh) `--server`, and starts the proxy cluster. A case's server runs only once `serve` starts it, so a run holds memory for the cases under test only. Each server's own tls-name is its certificate's subject CN.

| Key                                | Value                                                                                                        |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `TLS_CASES`                        | Comma separated case names                                                                                   |
| `TLS_<CASE>_PORT`                  | TLS port of the case's server on `ENV_HOST` (case name upper-cased)                                          |
| `TLS_<CASE>_NAME`                  | tlsName to connect with                                                                                      |
| `TLS_<CASE>_EXPECT`                | `accept` or `reject`                                                                                         |
| `TLS_<CASE>_WARNING`               | `true` when the legacy match deprecation warning must be logged                                              |
| `TLS_<CASE>_BOOTSTRAP`             | A tlsName the certificate accepts, to reach the server in a reject case                                      |
| `TLS_<CASE>_REVOKE`                | Hex serial of the server certificate to revoke, or empty                                                     |
| `TLS_<CASE>_CA`                    | CA file                                                                                                      |
| `PROXY_TLS_NAME`                   | tls-name of both proxy nodes, only a dNSName in their certificate, with a different CN and no iPAddress      |
| `PROXY_CA`                         | CA file of the proxy nodes                                                                                   |
| `PROXY_NODE_A_*`, `PROXY_NODE_B_*` | The nodes, `PORT` being the forwarder in front of each. Seed node A; node B and its tls-name come from peers |

Verbs: `serve <case>...` stops any running case server, starts the given cases' servers and waits until they are ready. Nodes: each case name, `proxy-a`, `proxy-b`.

## Ports

| Environment    | Ports above `ENV_PORT_BASE`                                            |
| -------------- | ---------------------------------------------------------------------- |
| `dns`          | +0 nodes A and B, +1 node B behind the forwarder, +2 node C, +3 DNS    |
| `aws-tags`     | +10 nodes A and B, +11 node B behind the forwarder, +12 stub EC2       |
| `port-map`     | +20, +21                                                               |
| `collision`    | +30 to +33                                                             |
| `yaml`         | +40 to +43                                                             |
| `pass-through` | +50, +51                                                               |
| `on-miss`      | +60, +61                                                               |
| `alt-missing`  | +70 to +73                                                             |
| `tls-matching` | +80 to +104 cases, +105 and +106 proxy nodes, +107 and +108 forwarders |

`port-map` and `yaml` put their fixed address clusters on `10.213.<n>.0/24`, with `n` from the environment's first port.

## From GitHub Actions

`.github/workflows/test-envs.yaml` is a reusable workflow. It checks out the calling repository and this one at `conformance-ref`, brings the environment up, appends the values file to `$GITHUB_ENV`, runs `command` with bash from the caller's checkout, and brings the environment down whatever happened, failing if anything with its prefix is left. The feature key comes from the `features-conf` secret, is written to a file only the runner user can read and is never printed.

```yaml
jobs:
  dns:
    uses: aerospike/aerospike-client-conformance/.github/workflows/test-envs.yaml@<sha>
    with:
      environment: dns
      conformance-ref: <sha>
      server-image: aerospike/aerospike-server-enterprise:8.1
      command: mvn -B test -Dtest=TestDnsDiscovery
    secrets:
      features-conf: ${{ secrets.AEROSPIKE_FEATURES_CONF }}
```

| Input             | Default        | Meaning                                                             |
| ----------------- | -------------- | ------------------------------------------------------------------- |
| `environment`     | required       | One of the environments above                                       |
| `command`         | required       | Run once the environment is up, with its values set                 |
| `server-image`    | required       | `AEROSPIKE_IMAGE`                                                   |
| `conformance-ref` | required       | Commit or tag of this repository, the one the workflow is called at |
| `port-base`       | `4700`         | `ENV_PORT_BASE`                                                     |
| `runs-on`         | `ubuntu-24.04` | Runner label                                                        |

The command runs in the workflow's own job on a fresh runner, so it installs any toolchain it needs itself. Verbs run as `"$ENV_CTL" <verb>`.
