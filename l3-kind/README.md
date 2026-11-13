# Pluggable discovery L3 on kind and AKO

`run.sh` runs one client's L3 rows on one machine with kind and a container runtime. For each profile it creates a kind cluster, installs the Aerospike Kubernetes Operator (AKO) and an AerospikeCluster through aerospike/shared-workflows `setup-kind-ako`, and runs the rows. A client supplies a manifest and the driver it already has for L5 ([DRIVER.md](../l5-aws/DRIVER.md)); `rows.py` starts the driver, makes every Kubernetes change through `l3ctl` and decides every row from kubectl, asinfo and the driver's output, the same way for every client. Every kind cluster, container and image a run creates is removed on exit, including on failure.

## What L3 covers and what it cannot

L3 covers what a client does against the alternate addresses AKO renders: one endpoint hostname shared by every pod with each pod's own Service NodePort, under `configuredIP` (a node label) and under `networkLoadBalancer` (the AerospikeCluster's address); one hostname per worker with the container hostPort; a pod added, removed, rescheduled, or recreated on a Kubernetes Node that was deleted and registered again, under a client seeded once; NodePorts kept and recreated across those changes, and a seed that only reaches the cluster through a NodePort no pod advertises; and TLS from a certificate that names the cluster's tls-name and not the hostname the client dials.

It cannot show:

- A real load balancer or PrivateLink: idle timeouts, cross-zone balancing, zone outages and private DNS are L5 ([l5-aws](../l5-aws/README.md)). Here the endpoint hostname resolves to one kind node, whose kube-proxy forwards each NodePort.
- Faults on the path: closed listeners, silent drops, resets and delays are L2 ([l2-proxy](../l2-proxy/README.md)).
- Client internals: blocked threads, retry counts other than the ones the driver reports, and pool behaviour are visible only to the client's own tests.
- A Kubernetes Node replaced by a new machine: kind keeps the node container, so a recreated Node keeps its address and only loses its UID and the labels set through the API.

## The cluster

| Profile       | kind nodes                                                                              | AerospikeCluster                                                                                       |
| ------------- | --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `default`     | One control-plane node, labelled with the endpoint hostname                             | 2 pods, `alternateAccess: configuredIP`, `multiPodPerHost: true`                                       |
| `per-node`    | A tainted control-plane node and 2 workers, worker N labelled `node-N.as-endpoint.test` | 2 pods, `configuredIP`, `multiPodPerHost: false`, one pod per worker                                   |
| `tls`         | As `default`                                                                            | As `default`, then TLS on port 4333 with tls-name `aerospike-tls`                                      |
| `nlb`         | One control-plane node, no label                                                        | 2 pods, `alternateAccess: networkLoadBalancer`, `loadBalancerAlternateAccessAddress: as-endpoint.test` |
| `nlb-per-pod` | A control-plane node and 3 workers, one spare                                           | As `nlb` with `multiPodPerHost: false`; every pod Service is set to `externalTrafficPolicy: Cluster`   |
| `nlb-tls`     | As `nlb`                                                                                | As `nlb`, then TLS with `loadBalancerTLSAlternateAccessAddress: as-endpoint-tls.test`                  |

`setup-kind-ako` runs at a pinned commit of aerospike/shared-workflows; `run.sh` downloads its files at that commit, or reads them from a local clone with `--shared-workflows`, and runs its `prepare` and `setup` steps without the step that writes the runner's `/etc/hosts`. It adds a CoreDNS entry that resolves the endpoint hostname to the first kind node, so the servers resolve it too. `per-node` labels the workers and adds their CoreDNS entries itself, then uses the action's functions to install AKO and the AerospikeCluster, since the action refuses one pod per host under `configuredIP`. The TLS profiles run `l3ctl enable-tls`, which generates a CA and a server certificate whose only SAN is `DNS:aerospike-tls`, mounts it from a secret and sets TLS access as alternate access is set; under `nlb-tls` it first adds a CoreDNS entry for the TLS hostname at the clear one's address.

The AerospikeCluster runs Aerospike Enterprise with security on. `run.sh` reads the admin password the action generates into the run's work directory, never prints it, and passes it to the driver as `AEROSPIKE_ADMIN_PASSWORD`; rows pass `--user admin`.

## How the driver reaches the cluster

The driver needs the endpoint hostnames to resolve to a kind node and every NodePort to be reachable. `run.sh` runs it in a container from the built client image with `--add-host` for each endpoint hostname, at the address CoreDNS has for it:

- `--driver-network kind` (the default) puts the container on the kind network. This works everywhere: on a Linux host, and on macOS with a Podman machine, where the host cannot route to the kind network.
- `--driver-network host` puts it on the host network. On a Linux host, as on `ubuntu-latest`, the host reaches the kind node addresses directly, so the driver sees the cluster as a client outside it does.

Either way the driver dials the hostnames the pods advertise, and the rows accept a node held at an advertised hostname or at the address the container resolves it to.

## What a client supplies

The client's L5 manifest works unchanged. `run.sh` sources it in an Amazon Linux 2023 container on the host's architecture, as root with `sudo` installed, from the repository root:

| Variable    | Runs                                                                                                                                  |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `L5_SETUP`  | Once, to install the client's toolchain                                                                                               |
| `L5_BUILD`  | Once, after setup, to build the client and its driver                                                                                 |
| `L5_DRIVER` | The driver, to [DRIVER.md](../l5-aws/DRIVER.md). `rows.py` runs it in a container from the built image, with the flags each row needs |

`--cache <dir>` keeps the build's `/root/.m2`, `/root/.cache` and dnf cache between runs, and the driver containers mount the first two.

## Rows

Each driver is seeded at the lowest-ordinal pod's advertised address with `--shape pass-through`, tend interval 250 ms and timeout 1000 ms. Each check prints an `L3-CHECK` line with its measurement, bound and outcome; each row ends with an `L3-ROW` line: `pass`, `fail`, or `skip` with the reason. Every profile lists every row; a row that does not apply to the profile, or needs a later contract version than the driver's `ready` reports, is skipped with the reason. Nothing that was not run is reported as passed. A row leaves the cluster at the size it found it, and runs at 2 pods at least.

| Row                                                       | Runs on                         | Needs      | Checks                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| --------------------------------------------------------- | ------------------------------- | ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `every-pod-advertises-shared-endpoint-with-own-node-port` | every profile but per-node      | contract 1 | Every pod advertises the endpoint hostname with its own Service NodePort, the hostname from the node label under `configuredIP` and from `loadBalancerAlternateAccessAddress` under `networkLoadBalancer`; the driver holds one node per pod at that host and port, nothing unowned, and `serve` writes and reads every partition                                                                                                                                                                                                  |
| `each-pod-at-its-worker-hostname-and-host-port`           | `per-node`                      | contract 1 | Each worker carries its own hostname as its label and runs one pod, which advertises that hostname with its container hostPort; the driver holds each node there and serves every partition                                                                                                                                                                                                                                                                                                                                        |
| `load-balancer-address-without-node-label-or-host-port`   | `nlb-per-pod`                   | contract 1 | One pod per worker, no node carries the label, no container has a hostPort, each pod advertises the AerospikeCluster's address with its Service NodePort; the driver holds each node there and serves every partition                                                                                                                                                                                                                                                                                                              |
| `scale-up-new-node-usable-within-two-tend-intervals`      | `default`, `nlb`, `nlb-per-pod` | contract 1 | The running driver `watch`es the added pod's advertised address and node: usable within 2 x the tend interval of the later of the peers-generation bump the new node causes and its first answer to a login and info request from the driver's network; the existing nodes stay held, the driver never holds no node, and every node is held at its address and every partition served afterwards                                                                                                                                  |
| `scale-down-removed-node-leaves-without-retry-storm`      | `default`, `nlb`, `nlb-per-pod` | contract 1 | The removed pod's node leaves the snapshots with nothing unowned; once it is gone, `failed`, `clientErrors` and `clientTimeouts` stay flat for 5 tend intervals while `succeeded` grows, and every partition is served                                                                                                                                                                                                                                                                                                             |
| `node-ports-stable-across-reschedule-and-recreate`        | `default`, `nlb`, `nlb-per-pod` | contract 1 | A NodePort Service pinned to pod 0 on a free port: no pod advertises it, and a driver seeded only through it holds every node and serves. Pod 0 deleted and rescheduled keeps its Service UID and NodePort; the last pod removed loses its Service, which is recreated with a new UID and NodePort when the pod is added (its old NodePort held meanwhile); the pinned Service keeps its port and seeds every node after each change                                                                                               |
| `rescheduled-pod-keeps-address-and-node-port`             | `nlb`, `nlb-per-pod`            | contract 1 | The last pod deleted and recreated by its StatefulSet advertises the same address and Service NodePort; the running driver holds every node at its address, never holds no node, and serves every partition                                                                                                                                                                                                                                                                                                                        |
| `pod-on-recreated-node-advertises-cluster-address`        | `nlb-per-pod`                   | contract 1 | The highest-ordinal pod on a worker the endpoint does not resolve to: its Node is labelled through the API, every other Node is cordoned, the Node is deleted (never a control-plane one) and registered again by restarting kubelet in its kind node container. The Node has a new UID and none of the API labels, the replacement pod runs on it and advertises the AerospikeCluster's address with its Service NodePort, and the running driver holds every node and serves. Every cordoned Node is uncordoned, also on failure |
| `shared-certificate-names-tls-name-not-endpoint`          | `tls`, `nlb-tls`                | no driver  | The certificate the tls-name serves has `DNS:aerospike-tls` as its only dNSName SAN and no SAN naming an endpoint hostname; each pod advertises its TLS address with its own TLS Service NodePort: the endpoint hostname under `configuredIP`, `loadBalancerTLSAlternateAccessAddress` under `networkLoadBalancer`, which may differ from the clear address                                                                                                                                                                        |
| `tls-name-holds-every-node-endpoint-name-not-created`     | `tls`, `nlb-tls`                | contract 4 | A driver with `--tls-name aerospike-tls` and the CA, seeded at the TLS address, holds every node at its advertised TLS address and serves every partition; the same seed with the TLS endpoint hostname as tls-name exits 3, the client not created                                                                                                                                                                                                                                                                                |

The cluster-changing rows run on `default`, `nlb` and `nlb-per-pod`; the TLS profiles run the endpoint and TLS rows. A driver at contract 3 or earlier has no TLS flags, so its TLS driver row is skipped and says so.

The scale-up clock starts at the later of two times: the peers-generation bump on an existing pod that announces the new node, and the new node's first answer, the first time a login with the driver's user followed by a `node` info request succeeds at the node's advertised address and Service NodePort, from the driver's network, through the same path the client uses. It does not start at the listener serving, as L5 does, or at the pod turning Ready: the server accepts connections and turns Ready before it can answer a login, and for a while after it refuses one (the client sees `Connection failed`), which no client can shorten. `l3ctl join-watch` polls each every 10 ms in one long-lived process: in the existing pod, a perl program logs in once and asks for `peers-generation` on that connection, and on the driver's network a perl program in a container from the server image connects, logs in and asks for `node` until the node answers. It also polls the new pod's Ready condition through one `kubectl proxy` connection. Each reading is stamped with the wall clock when it arrives, and the gap between the first changed reading and the one before it is the poll interval achieved, which must be 20 ms or less for the bump and the answer, or the row fails. The clock stops at `usableAt` from the driver's `watch`. `rows.py` lines the wall clock up with driver `t` from the `reset` round trip that took least, and reports that offset and half the round trip as its error. A node usable before the start point is a measurement error and fails the row. The row's `L3-SCALE-UP-TIMES` line gives the bump, first answer, Ready, serving and usable times in driver `t`, the start, the usable time from the start and from serving, the gap from Ready to the first answer, the poll intervals, and the probe's failed attempts by reason.

`python3 -m unittest l3-kind/test_rows.py` tests `rows.py` offline against a fake driver and a stub `l3ctl`: a passing run per row, a run that breaks each row, and a contract 3 driver on the TLS driver row.

## l3ctl

`l3ctl` reads the cluster from `$L3_STATE/topology.env`. Each command prints `L3-*` lines and exits non-zero on failure; with no command it prints the list.

| Command                                                                                                           | Use                                                                                                                                                                                                              |
| ----------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pods`                                                                                                            | The AerospikeCluster, and per pod: Kubernetes node, IP, Ready, UID, Service NodePorts and UID, container hostPort, and through asinfo in the pod its node name, `service-clear-alt` and `service-tls-alt`        |
| `nodes`                                                                                                           | Per Kubernetes Node: UID, InternalIP, role, labels, unschedulable, Ready                                                                                                                                         |
| `resize N`, `scale N`, `join-watch POD OBSERVER --resize N --host HOST`, `wait-pod POD`, `wait-completed`         | Size changes, the wall-clock times of an added pod's peers-generation bump, first answer and Ready condition, and waits for a new pod's Service NodePort and node-id or for the AerospikeCluster to be Completed |
| `delete-pod POD`, `wait-ready POD [--old-uid UID]`                                                                | Delete a pod, and wait for its replacement                                                                                                                                                                       |
| `service NAME [PORT]`, `service-create NAME POD NODEPORT`, `service-delete NAME`, `node-port-range`, `node-ports` | Service NodePorts                                                                                                                                                                                                |
| `label-node NODE KEY VALUE`, `cordon-others NODE`, `uncordon NODE...`                                             | Node labels and scheduling                                                                                                                                                                                       |
| `delete-node NODE`, `reregister NODE`                                                                             | Delete a worker's Node, never a control-plane one, and register it again by restarting kubelet in its kind node container, which kind does not do itself                                                         |
| `enable-tls [--tls-endpoint HOST]`, `certificate`                                                                 | Serve TLS, and read the certificate the tls-name serves                                                                                                                                                          |
| `drivers-stop`, `status`                                                                                          | Remove driver containers left by a killed driver; print the cluster                                                                                                                                              |

## Running it locally

With docker, or podman in rootful mode with `KIND_EXPERIMENTAL_PROVIDER=podman`, and kind, kubectl, helm, yq, jq, openssl, git, curl, bash 4 or later and Python 3.9 or later:

```text
FEATURES_CONF_PATH=/path/to/features.conf l3-kind/run.sh --client-repo /path/to/aerospike-client-java \
  --commit origin/pluggable-discovery --manifest test/discovery/l5-aws.manifest --profile default --cache ~/.cache/l3-kind
```

- `--client-repo` is an https URL or a local repository; a local repository is read with `git archive` and never written.
- `--profile` takes a comma-separated list or `all`; the client is built once and each profile gets its own kind cluster, deleted before the next.
- `--rows` runs the named rows of each profile.
- `--ako-version`, `--ako-helm-repo` and `--cluster-chart-version` are passed to the action. On the `configuredIP` profiles they default to empty, which installs the AKO version and Helm repository pinned in the action's `deps/Chart.yaml`, and the cluster chart of the same version. `networkLoadBalancer` needs an AKO build with it, which is today only `4.8.0-dev1`, published only at `https://aerospike.github.io/aerospike-kubernetes-operator` and with no cluster chart of its own, so the `nlb` profiles default to `4.8.0-dev1`, that repository and cluster chart `4.7.0`. `latest` takes the newest release in the repository. The run prints the resolved AKO version and writes it to the profile's `ako.txt`.
- `--driver-network kind|host` is described above; `--prefix` (default `l3-kind`) names the kind clusters `<prefix>-<profile>` and the containers `<prefix>-*`.
- Run one profile at a time on a machine. Separate prefixes let runs proceed in parallel, but kind clusters sharing one machine slow each other enough to fail the scale-up row: `default` measured 808 ms with three clusters running and 238 ms alone, against 500 ms.
- `--shared-workflows <dir>` reads the action from a local aerospike/shared-workflows clone at the pinned commit instead of downloading it.
- `--plan` prints each profile's cluster, AKO version and rows and creates nothing.
- `FEATURES_CONF_PATH` is passed to the action, never printed, and checked absent from every result file, as is the admin password.
- On a Podman machine the paths the containers mount (the work directory and cache) must be under a directory the machine shares, such as `/Users` or `/private`; `run.sh` resolves them to their real paths. Kind on podman needs the machine in rootful mode.

The run refuses to start when a kind cluster or container with its names exists. Results go to `l3-kind/results/<prefix>-<time>/` unless `--results` says otherwise: `client.txt`, `build.log`, `rows.txt` with one line per row, and per profile `build-cluster.log`, `setup.log`, `ako.txt`, `tls.log` on the TLS profiles, `status-before.txt`, `status-after.txt`, `rows.log` and `ako-manager.log`. `cleanup.txt` lists anything with the prefix left after the run, followed by `kind get clusters` and the container list; the run fails if anything is left. A profile whose cluster could not be built is reported as `NOT RUN`.

## From a workflow

`.github/workflows/l3-kind.yaml` runs `run.sh` on `ubuntu-latest` and uploads the results as an artifact. The feature key comes from the `FEATURES_CONF` secret and is written to a file only the job reads:

```yaml
jobs:
  l3:
    uses: aerospike/aerospike-client-conformance/.github/workflows/l3-kind.yaml@<sha>
    with:
      client-repository: aerospike/aerospike-client-java
      commit: ${{ github.sha }}
      manifest: test/discovery/l5-aws.manifest
      profile: default,nlb,nlb-per-pod
    secrets:
      FEATURES_CONF: ${{ secrets.FEATURES_CONF }}
```

`rows`, `ako-version`, `ako-helm-repo`, `cluster-chart-version` and `conformance-ref` are optional. Each profile takes about 5 minutes to build and 5 to 15 minutes of rows, so a caller may run profiles as separate jobs.
