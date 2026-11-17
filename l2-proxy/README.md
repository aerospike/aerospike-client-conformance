# Pluggable discovery L2 through a proxy

`run.sh` runs one client's L2 rows on one machine with a container runtime. It puts a three node Aerospike cluster behind one Toxiproxy listener per node, all behind one endpoint hostname, with no route from the client to the node addresses, and injects faults on those listeners. A client supplies a manifest and the driver it already has for L5 ([DRIVER.md](../l5-aws/DRIVER.md)); `rows.py` starts the driver, applies the faults through `l2ctl` and decides every row from the driver's output, the same way for every client. Every container, network and image a run creates is removed on exit, including on failure.

## What L2 covers and what it cannot

L2 covers what a client does when the only addresses it can reach are a proxy's listeners: the port-map shapes keyed by node name and by advertised address, pass-through of alternate addresses with distinct and shared names, `strict` and `pass-through` on a translation miss, a node whose advertised address the client can reach, a node that joins mid-run, a port-map pre-filled for a range of addresses, and recovery with bounded command latency after a listener is closed, holds data silently, resets connections or delays replies, and after every listener closes at once. It also covers source port exhaustion at the proxy and records larger than the client network's MTU.

It cannot show:

- A real load balancer or PrivateLink: idle timeouts, cross-zone balancing, zone outages and private DNS are L5 ([l5-aws](../l5-aws/README.md)).
- TLS through the driver: the optional TLS flags of the driver contract are separate work, so every row uses the clear listeners. On `--edition ee` the topology has TLS for a client's own tests (below).
- Client internals: blocked threads, the error-rate circuit breaker, behaviour per event loop and pool checks on checkout are visible only to the client's own tests. The topology and the control commands they use are below.
- A path MTU black hole: both ends of the client network share its MTU, so the MTU row shows that large records cross a small MTU, not that a client survives a missing path MTU discovery.

## The topology

For each profile `run.sh` builds:

- A node network with the three nodes and no published ports. The nodes run `asd` from the server image with a mesh heartbeat, replication factor 2 and namespace `test` in memory, without security.
- Toxiproxy, started by aerospike/shared-workflows `setup-discovery-topology` at a pinned commit, on the node network and on the client network, where it answers to the endpoint hostname `as-endpoint.test`. It has one listener per node, `<prefix>-node-N` on port base plus N.
- A client network created as internal, so a container on it reaches Toxiproxy and nothing else: no node address and no internet. The `client-has-no-route-to-node-addresses` row checks this before any driver starts.
- A DNS server, `<prefix>-dns`, on the client network, started by aerospike/shared-workflows `setup-dns-server` at the same commit: zone `as-endpoint.test` with an A record at Toxiproxy's client network address and one `_aerospike._tcp` SRV record per clear listener port, for a client's own DNS seed tests.
- A control container with curl on the node network, through which `l2ctl` and the action call Toxiproxy's API, since the host cannot always route to container addresses (Docker Desktop, a Podman machine).

Container names, network names and ports derive from `--prefix` (default `l2-proxy-<port base>`) and `--port-base` (default 4900): Toxiproxy's API on the port base, the endpoint listeners from the port base plus 1, listeners for nodes started mid-run from the port base plus 10, the TLS listeners on `--edition ee` from the port base plus 50, and the range listeners from the port base plus 100. The port-map-range subnet is `10.213.<port base / 100 mod 256>.0/24`. Before creating anything, the run checks that no container or network with the prefix exists, that the 200 ports from the port base are free on the host and not published by a container, and that no network uses the subnet. Two runs with different port bases run side by side.

## What a client supplies

The client's L5 manifest works unchanged. `run.sh` sources it in an Amazon Linux 2023 container on the host's architecture, as root with `sudo` installed, from the repository root:

| Variable    | Runs                                                                                                                                                                                                                                                       |
| ----------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `L5_SETUP`  | Once, to install the client's toolchain                                                                                                                                                                                                                    |
| `L5_BUILD`  | Once, after setup, to build the client and its driver                                                                                                                                                                                                      |
| `L5_DRIVER` | The driver, to [DRIVER.md](../l5-aws/DRIVER.md). `rows.py` runs it in a container from the built image, on the client network only, with the flags each row needs                                                                                          |
| `L2_RUN`    | Optional. Once per profile after the rows, in a container on the client network with the values file below as its environment; exit 0 passes. For a client's own L2 tests. On `port-range` it starts once Toxiproxy's source ports are free, or after 90 s |

The driver needs no password: the cluster has no security, so rows pass no `--user`. The driver container has no internet, so `L5_BUILD` must download everything the driver needs. `--cache <dir>` keeps the build's `/root/.m2`, `/root/.cache` and dnf cache between runs, and the driver containers mount the first two.

## Profiles

| Profile              | Topology                                                                                                                                                                                |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `default`            | Every node advertises its private address only                                                                                                                                          |
| `reachable`          | The last node sets `access-address as-reachable.test` and its listener port; the others keep their private address and set that name and their listener port as their alternate address |
| `pass-through-names` | Node N sets `alternate-access-address node-N.pass-through.test` and its listener port                                                                                                   |
| `pass-through-ports` | Every node sets `alternate-access-address as-pass-through.test` and its own listener port. With `--unresolved-hostname` the name does not resolve in the servers                        |
| `port-map-range`     | The nodes sit on fixed addresses from `.11` in the profile's subnet, and Toxiproxy has a listener for every address `.11` to `.26` on the range base plus the last octet                |
| `port-range`         | Toxiproxy's `net.ipv4.ip_local_port_range` is `40000 40009`, which caps the connections it opens to each node                                                                           |
| `low-mtu`            | The client network's MTU is 1200 and the namespace sets `max-record-size 8M`                                                                                                            |
| `idle-reset`         | The default topology; the values file sets `DISCOVERY_IDLE_RESET=1` for a client's own idle reset tests                                                                                 |
| `cn-only`            | `--edition ee` only: the default topology with a certificate that carries the tls-name only as its CN                                                                                   |

Every name the nodes advertise resolves to Toxiproxy, in the servers (the server resolves access addresses at startup) and in the driver container. `--unresolved-hostname` needs a server that advertises access addresses verbatim; a server that resolves them at startup does not start, and the run reports the profile as not run with the node's log line.

## Rows

Each row runs its own driver, seeded at the first listener with tend interval 250 ms and timeout 1000 ms. A row that routes every node starts its driver once no node migrates a partition, and reads each node's namespace statistics through asinfo before and after its `serve`: no node may proxy a command to another node, which a client that sends a write anywhere but the partition's master would cause, and the nodes must serve every write and read themselves, each node some. Each check prints an `L2-CHECK` line with its measurement, bound and outcome; each row ends with an `L2-ROW` line: `pass`, `fail`, or `skip` with the reason. A row that needs a later contract version than the driver's `ready` reports is skipped and says which version it needs; a row that does not apply to the run is skipped with the reason. Nothing that was not run is reported as passed.

| Profile              | Row                                                        | Needs      | Checks                                                                                                                                                                                                                                                                         |
| -------------------- | ---------------------------------------------------------- | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| every profile        | `client-has-no-route-to-node-addresses`                    | no driver  | From the client network, no node address connects and every endpoint port does                                                                                                                                                                                                 |
| every profile        | `port-map-by-node-name-routes-every-node`                  | contract 1 | Port-map keyed by node name: every node at its listener, every partition owned by a distinct node and served with no proxy hop                                                                                                                                                 |
| `default`            | `port-map-by-advertised-address-routes-every-node`         | contract 5 | Port-map keyed by each node's advertised standard address, the one it configures with no access address: every node at its listener and every partition served with no proxy hop                                                                                               |
| `default`            | `strict-miss-never-holds-the-unmapped-node`                | contract 1 | `strict` with one node unmapped: that node is never held and the others serve                                                                                                                                                                                                  |
| `default`            | `port-map-colliding-nodes-never-held`                      | contract 5 | Port-map keyed by advertised address with two nodes mapped to one node's listener, seeded at the third: neither colliding node is ever held, whether or not the client is created. Whether it keeps serving the seed node is not decided, so that check is reported as skipped |
| `default`            | `pass-through-miss-without-route-client-not-created`       | contract 1 | `pass-through` with only the seed mapped: the peers' advertised addresses, which the client has no route to, are used unchanged, so the client is not created and its error names each peer's address                                                                          |
| `default`            | `empty-alternate-list-client-not-created`                  | contract 1 | `pass-through` shape against nodes that advertise no alternate address: the client is not created, and its error names `use_services_alternate` in the client's spelling                                                                                                       |
| `default`            | `closed-listener-node-returns-with-bounded-latency`        | contract 1 | A node's listener closed: the client stops using the node, keeps holding every other node and still serves every partition the other nodes master, and once the listener is open again holds and serves every node within bounded command latency                              |
| `default`            | `silent-drop-attempts-time-out-within-bound-and-recover`   | contract 3 | A node's listener holds data in both directions with no reply and no close: every failed attempt fails within the timeout, and the node serves again once the fault is removed                                                                                                 |
| `default`            | `delayed-replies-below-timeout-serve-and-recover`          | contract 1 | A node's replies delayed 300 ms: commands slow by the delay and none fails, and latency is back within the bound once the delay is removed                                                                                                                                     |
| `default`            | `all-listeners-closed-client-reseeds-within-bound`         | contract 1 | Every listener closed until the client holds no node: once they are open again the client re-seeds within two tend intervals plus the timeout, then holds and serves every node                                                                                                |
| `reachable`          | `pass-through-miss-holds-reachable-node-at-its-address`    | contract 1 | `pass-through` with every node mapped but the one whose advertised address the client can reach: that node is held at its advertised address unchanged, the others at their listeners, and every partition is served with no proxy hop                                         |
| `reachable`          | `strict-miss-never-holds-the-reachable-node`               | contract 1 | `strict` with the same map: the unmapped reachable node is never held and the others serve                                                                                                                                                                                     |
| `reachable`          | `alternate-list-holds-only-nodes-with-alternate`           | contract 1 | `pass-through` shape: the nodes with an alternate address are held at it, and the node without one is never held                                                                                                                                                               |
| `reachable`          | `seed-without-alternate-client-not-created`                | contract 1 | `pass-through` shape seeded at the node with no alternate address: the client is not created, and its error names `use_services_alternate` in the client's spelling                                                                                                            |
| `pass-through-names` | `distinct-alternate-names-route-every-node`                | contract 1 | Each node advertises its own alternate name and listener port: pass-through routes every node, with no proxy hop                                                                                                                                                               |
| `pass-through-ports` | `shared-alternate-name-routes-every-node`                  | contract 1 | Every node advertises one alternate name and its own listener port: pass-through routes every node, with no proxy hop                                                                                                                                                          |
| `pass-through-ports` | `new-node-on-shared-name-usable-within-two-tend-intervals` | contract 1 | A node started on the shared alternate name with its own port is usable by the pass-through client within two tend intervals of its listener serving, with no client change, and held at that port                                                                             |
| `pass-through-ports` | `colliding-alternate-nodes-never-held`                     | contract 1 | Two nodes started advertising one alternate address and port, whose listener reaches one of them: the pass-through client never holds either, whether or not it is created. Whether it keeps serving the other nodes is not decided, so that check is reported as skipped      |
| `pass-through-ports` | `colliding-newcomer-never-held`                            | contract 1 | A node started advertising the alternate address and port of a held node. Always skipped: what the client must do then is not decided                                                                                                                                          |
| `pass-through-ports` | `shared-name-unresolved-in-servers-advertised-verbatim`    | no driver  | With `--unresolved-hostname`: the shared alternate name does not resolve where the servers run, and every node advertises it as configured. Skipped otherwise                                                                                                                  |
| `port-map-range`     | `range-port-map-routes-every-node`                         | contract 5 | A port-map pre-filled from a rule for every address of a range routes every node at its listener, with no proxy hop                                                                                                                                                            |
| `port-map-range`     | `new-node-in-range-usable-within-two-tend-intervals`       | contract 5 | A node started on a free address of the range joins through the pre-filled port-map within two tend intervals of its listener serving, with no client change, and is held at its listener                                                                                      |
| `port-map-range`     | `strict-never-holds-node-outside-range`                    | contract 5 | A node started on an address the pre-filled port-map does not cover is never held under `strict`, and the other nodes stay held                                                                                                                                                |
| `port-map-range`     | `pass-through-never-holds-unroutable-node-outside-range`   | contract 5 | The same node under `pass-through` is used at its advertised address, which the client has no route to, so it is never held, and the other nodes stay held                                                                                                                     |
| `port-range`         | `source-port-exhaustion-fails-within-bound-and-recovers`   | contract 3 | 64 concurrent commands against 10 free source ports: every failed attempt fails within the timeout, the client's error and timeout counts rise when the driver reports them, and once Toxiproxy holds none of those ports, at low load every node is held and served again     |
| `low-mtu`            | `large-records-over-low-mtu-within-timeout`                | contract 5 | Eight 1 MiB records are written and read back over the 1200 byte MTU, each command within the timeout                                                                                                                                                                          |
| `idle-reset`         | `reset-peer-attempts-fail-within-bound-and-recover`        | contract 3 | A node's listener resets every connection: every failed attempt fails within the timeout, and the node serves again once the fault is removed                                                                                                                                  |

The fault rows (`fault=true` in `--plan`) show that their fault had an effect before they assert on it: the node stops being held, failures arrive, latency rises, or the client holds no node. `--no-fault` is the control run: every fault row runs without its fault, and `port-range` builds Toxiproxy with its default port range, so every fault row must fail. Bounds: a failed attempt within the timeout plus 500 ms, measured as the driver's `t` minus `startedAt`; recovery within 60 s; command latency within the timeout once recovered; a delayed command within the delay plus the timeout; a re-seed within two tend intervals plus the timeout, plus the time the `l2ctl` calls that open the listeners took; a new node usable within two tend intervals of its listener serving, from the driver's `watch`.

`python3 -m unittest l2-proxy/test_rows.py` tests `rows.py` offline against a fake driver and a stub `l2ctl`: a passing run per row, a run that breaks each assertion, and a run with an older driver for each row that needs a later contract.

## l2ctl

`l2ctl` reads the topology from `$L2_STATE/topology.env`. Each command prints `L2-*` lines and exits non-zero on failure; with no command it prints the list.

| Command                                                                                                                     | Use                                                                                                                            |
| --------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `nodes`                                                                                                                     | Per node, through asinfo: node name, address, listener and port, `service-clear-std`, `service-clear-alt`, peers, cluster size |
| `listeners`, `listener NAME on\|off`, `listener-add NAME PORT UPSTREAM [--off]`, `listener-remove NAME`                     | Toxiproxy listeners                                                                                                            |
| `toxic-add NAME TOXIC TYPE [--stream upstream\|downstream] [ATTRIBUTE=VALUE ...]`, `toxic-remove NAME TOXIC`, `toxics NAME` | Toxics: `reset_peer`, `timeout`, `latency`, `bandwidth` and the others Toxiproxy has, toxicity 1                               |
| `start-node [--name NAME] [--address IP] [--access HOST:PORT] [--alternate HOST:PORT] [--no-hosts]`, `stop-node CONTAINER`  | Nodes with the cluster's config and mesh seeds                                                                                 |
| `wait-cluster N`, `wait-advertised CONTAINER`                                                                               | Every node reports cluster size N with no pending migrations; every other node lists the container's node among its peers      |
| `lookup NAME`                                                                                                               | Whether NAME resolves in each node's container                                                                                 |
| `route-check`, `client-mtu`, `proxy-port-range`, `proxy-ports`                                                              | Checks from the client network and in Toxiproxy's network namespace                                                            |
| `stats NAMESPACE`                                                                                                           | Per node, through asinfo: the commands it proxied to another node, and the writes and reads it served for a client             |
| `drivers-stop`                                                                                                              | Removes driver containers left by a driver that was killed                                                                     |

## A client's own tests

Rows that assert on client internals stay in the client: the timeout fault on each event loop with no blocked thread, the error-rate circuit breaker, and idle connections reset in the pool, synchronously and on each event loop. They run against the same topology through `L2_RUN`, which gets the values file as its environment. The values file is `values.env` in the profile's results:

| Variable                                                     | Value                                                                                                                                  |
| ------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------- |
| `DISCOVERY_SUBSET`                                           | `ce` or `ee`                                                                                                                           |
| `DISCOVERY_ENDPOINT_HOSTNAME`                                | `as-endpoint.test`                                                                                                                     |
| `DISCOVERY_ENDPOINT_PORT_BASE`, `DISCOVERY_ENDPOINT_PORTS`   | The first listener port, and every listener port, comma separated                                                                      |
| `DISCOVERY_PROXY_MAP`                                        | `<listener>:<port>`, comma separated; a listener is named after its node's container                                                   |
| `TOXIPROXY_URL`, `TOXIPROXY_HOST`, `TOXIPROXY_CONTROL_PORT`  | Toxiproxy's API, reachable from the client network                                                                                     |
| `AEROSPIKE_CONTAINER_NAMES`                                  | The node containers, comma separated                                                                                                   |
| `DISCOVERY_REACHABLE_NODE`, `DISCOVERY_PASS_THROUGH_BRANCH`  | `reachable`: the reachable node's container. `pass-through-names` and `pass-through-ports`: `A` and `B`                                |
| `DISCOVERY_PORT_MAP_RANGE`, `DISCOVERY_PORT_MAP_PORT_BASE`   | `port-map-range`: the first and last address of the range, and the range listener base                                                 |
| `DISCOVERY_CLIENT_NETWORK_MTU`, `DISCOVERY_PROXY_PORT_RANGE` | `low-mtu` and `port-range`                                                                                                             |
| `DISCOVERY_IDLE_RESET`                                       | `1` on `idle-reset`                                                                                                                    |
| `DISCOVERY_TLS_ENDPOINT_PORTS`                               | `--edition ee`: every TLS listener port, comma separated; their listeners are `<listener>-tls` in `DISCOVERY_PROXY_MAP`. Empty on `ce` |
| `DISCOVERY_TLS_NAME`, `DISCOVERY_TLS_CERT_SHAPE`             | `--edition ee`: `aerospike-tls`, and `san-only` or, on `cn-only`, `cn-only`. Empty on `ce`                                             |
| `DISCOVERY_TLS_CA_FILE`                                      | `--edition ee`: `/l2-tls/ca.crt` in the `L2_RUN` container, the CA that signed the server certificate. Empty on `ce`                   |
| `DISCOVERY_DNS_SERVER`                                       | The DNS server's address on the client network (port 53)                                                                               |
| `DISCOVERY_CONTROL_DIR`                                      | `/l2-control`, where the test writes node requests (below)                                                                             |

On `--edition ee` every node also serves TLS on port 4333 with tls-name `aerospike-tls`, behind one more Toxiproxy listener per node. Each profile gets its own CA and server certificate from [`tls/generate-certs.sh`](../tls/generate-certs.sh): `san-only` carries the tls-name only as a dNSName SAN with a CN that is not the tls-name, so only SAN matching accepts it, and `cn-only` carries it only as the CN. The test trusts `DISCOVERY_TLS_CA_FILE`.

`L2_PROFILE` names the profile. The test controls the faults through Toxiproxy's API, the same calls `l2ctl` makes:

```text
curl -sf -X POST "$TOXIPROXY_URL/proxies/<listener>/toxics" -H 'Content-Type: application/json' \
  -d '{"name":"drop","type":"timeout","stream":"downstream","toxicity":1,"attributes":{"timeout":0}}'
curl -sf -X POST "$TOXIPROXY_URL/proxies/<listener>/toxics" -H 'Content-Type: application/json' \
  -d '{"name":"reset","type":"reset_peer","stream":"downstream","toxicity":1,"attributes":{"timeout":0}}'
curl -sf -X DELETE "$TOXIPROXY_URL/proxies/<listener>/toxics/<name>"
curl -sf -X POST "$TOXIPROXY_URL/proxies/<listener>" -H 'Content-Type: application/json' -d '{"enabled":false}'
```

A test removes every toxic it adds and enables every listener it disables before it ends.

### Node requests

While `L2_RUN` runs, `node-server` runs on the host and serves node requests in the file format of [`discovery-node-server`](../.github/actions/discovery-node-server/README.md#requests), so a test that starts nodes there works unchanged. Its directory is mounted at `DISCOVERY_CONTROL_DIR`. The test writes one line to `<id>.tmp`, renames it to `<id>.request`, and reads `key=value` lines from `<id>.ack`; an `error` key is a failure.

| Request                                        | Ack on success                                                         |
| ---------------------------------------------- | ---------------------------------------------------------------------- |
| `start [alternate-port=<port>] [address=<ip>]` | `container=<name>` and `address=<ip>`                                  |
| `stop <name>`                                  | `removed=<name>`                                                       |
| `status <name>`                                | `running`, `exit-code`, `log`, `lookup-in`, `lookup-exit` and `lookup` |

- `start` runs a node with the cluster's config and mesh seeds through `l2ctl start-node`, named after the topology's nodes: `<prefix>-node-4`, then the next free number. It has no listener; the test adds one through Toxiproxy's API, upstream `<name>:3000`.
- `alternate-port=<port>` advertises the profile's alternate hostname with that port: `as-pass-through.test` on `pass-through-ports`, which resolves in the servers unless the run has `--unresolved-hostname`. Other profiles have no alternate hostname and answer with an error.
- `address=<ip>` gives the node that address; only `port-map-range` has a fixed subnet.
- `stop <name>` removes a node this server started and refuses any other.
- `status <name>` reports on a node of the topology: `running=true` or `false`, its `exit-code`, and `log`, the node's log at a path the test can read. On a profile with an alternate hostname it also looks that name up with `getent hosts`, in the node while it runs and otherwise in a new container from the server image on the node network: `lookup-in=container` or `image`, `lookup-exit` (`2` means not found) and `lookup`, its output on one line.

The server stops when `L2_RUN` ends, and removes every node it started, so the profile's cleanup finds none.

## Running it locally

With docker, or rootless podman through its docker CLI (`podman-docker`, or the docker CLI pointed at the podman socket), bash 4 or later, git, jq, curl and Python 3.9 or later:

```text
l2-proxy/run.sh --client-repo /path/to/aerospike-client-java --commit origin/pluggable-discovery \
  --manifest test/discovery/l5-aws.manifest --profile all --cache ~/.cache/l2-proxy
```

- `--client-repo` is an https URL or a local repository; a local repository is read with `git archive` and never written.
- `--profile` takes a comma-separated list or `all`; the client is built once.
- `--rows` runs the named rows of each profile that has them.
- `--edition ee` runs Aerospike Enterprise with `FEATURES_CONF_PATH`, which is mounted into the nodes, never printed, and checked absent from every result file, and needs openssl for the certificates. `all` includes `cn-only` only with `--edition ee`. `--server-image` picks another image.
- `--plan` prints each profile's topology and rows and creates nothing.
- On a Podman machine the paths the containers mount (the work directory, results and cache) must be under a directory the machine shares, such as `/Users` or `/private`; `run.sh` resolves them to their real paths.

Results go to `l2-proxy/results/<prefix>-<time>/` unless `--results` says otherwise: `client.txt`, `build.log`, `rows.txt` with one line per row, and per profile `build-topology.log`, `topology.log`, `dns.log`, `values.env`, `status-before.txt`, `status-after.txt`, `rows.log`, each node's and Toxiproxy's log, and `l2-run.log` and `node-server.log` when the manifest sets `L2_RUN`, with the log of every node the node server removed. `cleanup.txt` lists anything with the prefix left after the run; the run fails if it is not empty. A profile whose topology could not be built is reported as `NOT RUN`.

## From a workflow

`.github/workflows/l2-proxy.yaml` runs `run.sh` on `ubuntu-latest` and uploads the results as an artifact. The feature key comes from the `FEATURES_CONF` secret and is written to a file only the job reads:

```yaml
jobs:
  l2:
    uses: aerospike/aerospike-client-conformance/.github/workflows/l2-proxy.yaml@<sha>
    with:
      client-repository: aerospike/aerospike-client-java
      commit: ${{ github.sha }}
      manifest: test/discovery/l5-aws.manifest
      profile: all
    secrets:
      FEATURES_CONF: ${{ secrets.FEATURES_CONF }}
```

`edition` defaults to `ee`; `rows`, `server-image` and `conformance-ref` are optional. `unresolved-hostname: true` passes `--unresolved-hostname`, so `shared-name-unresolved-in-servers-advertised-verbatim` runs instead of skipping; `profile` must then include `pass-through-ports` and `server-image` must advertise access addresses verbatim. The results artifact is `l2-proxy-results-<profile>-<run id>-<attempt>`, so a caller can call the workflow once per profile in one run.
