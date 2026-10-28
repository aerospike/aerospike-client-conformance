# discovery-l2-topology

Builds the L2 discovery topology for one profile, the same for every client. It starts an Aerospike cluster with [`setup-aerospike-server`](https://github.com/aerospike/shared-workflows/tree/main/.github/actions/setup-aerospike-server), puts it behind Toxiproxy on a separate client network with [`setup-discovery-topology`](https://github.com/aerospike/shared-workflows/tree/main/.github/actions/setup-discovery-topology), applies the profile, and starts [`discovery-node-server`](../discovery-node-server) when the profile adds nodes mid-test.

The client keeps only what is specific to it: its runner image and cache, the tests each profile runs, the command that runs them, and publishing the results.

## Usage

```yaml
jobs:
  l2:
    strategy:
      fail-fast: false
      matrix:
        profile:
          [
            default,
            reachable,
            pass-through-names,
            pass-through-ports,
            port-map-range,
            low-mtu,
            port-range,
            idle-reset,
          ]
    runs-on: ubuntu-24.04
    permissions:
      id-token: write
      contents: read
    steps:
      - uses: actions/checkout@<sha>

      - uses: aerospike/aerospike-client-conformance/.github/actions/discovery-l2-topology@<sha>
        id: l2
        with:
          profile: ${{ matrix.profile }}
          subset: ce
          server-edition: community
          server-container-repo: database-docker-virtual/aerospike-server
          server-tag: latest
          oidc-provider: ${{ secrets.JFROG_OIDC_PROVIDER }}
          oidc-audience: ${{ secrets.JFROG_OIDC_AUDIENCE }}

      - name: Run the L2 tests
        env:
          CLIENT_NETWORK: ${{ steps.l2.outputs.client-network }}
          ENV_FILE: ${{ steps.l2.outputs.env-file }}
          CLIENT_HOSTS: ${{ steps.l2.outputs.client-hosts }}
        run: |
          args=(--rm --network "$CLIENT_NETWORK" --env-file "$ENV_FILE" -v "$GITHUB_WORKSPACE:$GITHUB_WORKSPACE" -w "$GITHUB_WORKSPACE")
          IFS=, read -r -a hosts <<< "$CLIENT_HOSTS"
          for host in "${hosts[@]}"; do args+=(--add-host "$host"); done
          docker run "${args[@]}" <client image> <client test command>
```

For `ee`, pass `enable-tls: "true"`, a `tls-listen-port-base`, `features-content` and an enterprise image, and mount `tls-cert-dir` so the client trusts `ca.crt`.

The tests must run on `client-network`: they reach the nodes only through the proxy. Profiles that add nodes write requests to `control-dir`, which defaults to a directory in the workspace, so a workspace mount is enough.

## Profiles

| Profile              | Topology                                                                                                                                       |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `default`            | 3 running nodes and 1 parked; node server running                                                                                              |
| `reachable`          | The last node advertises `as-reachable.test` as its access address, the others as alternate                                                    |
| `pass-through-names` | Each node advertises `node-<n>.pass-through.test` as alternate, one name per node                                                              |
| `pass-through-ports` | Every node advertises `as-pass-through.test` with its own port; 1 parked node; node server                                                     |
| `port-map-range`     | Subnet `172.30.66.0/24`, one listener per address `.2` to `.17` on `31000 + last octet`; Toxiproxy moved to `.250`; 1 parked node; node server |
| `cn-only`            | `ee` certificate with the tls-name only as CN                                                                                                  |
| `low-mtu`            | Client network MTU 1200; namespace `max-record-size 8M`                                                                                        |
| `port-range`         | Proxy source ports limited to `40000` to `40009`                                                                                               |
| `idle-reset`         | Sets `DISCOVERY_IDLE_RESET=1` for the tests                                                                                                    |

In `ee`, every profile except `cn-only` uses a SAN-only certificate whose CN is not the tls-name, so only SAN matching can pass.

## Inputs

| Name                    | Required | Default                                      | Description                                  |
| ----------------------- | -------- | -------------------------------------------- | -------------------------------------------- |
| `profile`               | No       | `default`                                    | One of the profiles above                    |
| `subset`                | Yes      |                                              | `ce` or `ee`                                 |
| `server-edition`        | Yes      |                                              | `community` or `enterprise`                  |
| `server-container-repo` | Yes      |                                              | Aerospike image repository path              |
| `server-tag`            | Yes      |                                              | Aerospike image tag                          |
| `enable-tls`            | No       | `"false"`                                    | Server TLS and TLS proxy listeners           |
| `tls-listen-port-base`  | No       | `""`                                         | First TLS proxy listen port; empty skips TLS |
| `features-content`      | No       | `""`                                         | Enterprise feature-key file contents         |
| `oidc-provider`         | Yes      |                                              | JFrog OIDC provider                          |
| `oidc-audience`         | Yes      |                                              | JFrog OIDC audience                          |
| `control-dir`           | No       | `${{ github.workspace }}/.discovery-control` | Node request directory                       |

## Outputs

| Name              | Description                                                         |
| ----------------- | ------------------------------------------------------------------- |
| `client-network`  | Docker network to run the client on                                 |
| `env-file`        | Settings for `docker run --env-file` (see below)                    |
| `client-hosts`    | Comma-separated `host:ip` entries for `--add-host`; empty when none |
| `tls-cert-dir`    | Directory with `ca.crt` when TLS is enabled                         |
| `node-server-log` | Node server log; empty when the profile adds no nodes               |

## Client environment

`env-file` sets every variable below; an empty value means the profile does not use it.

| Variable                        | Content                                                  |
| ------------------------------- | -------------------------------------------------------- |
| `L2_PROFILE`                    | The profile                                              |
| `DISCOVERY_SUBSET`              | `ce` or `ee`                                             |
| `DISCOVERY_ENDPOINT_HOSTNAME`   | Proxy hostname the client seeds through                  |
| `DISCOVERY_ENDPOINT_PORT_BASE`  | First proxy listen port                                  |
| `DISCOVERY_ENDPOINT_PORTS`      | Comma-separated proxy listen ports, one per running node |
| `DISCOVERY_TLS_ENDPOINT_PORTS`  | Comma-separated TLS proxy listen ports                   |
| `DISCOVERY_PROXY_MAP`           | `container:port` per running node                        |
| `TOXIPROXY_URL`                 | Toxiproxy control API                                    |
| `TOXIPROXY_HOST`                | Toxiproxy container name                                 |
| `TOXIPROXY_CONTROL_PORT`        | `8474`                                                   |
| `AEROSPIKE_CONTAINER_NAMES`     | Comma-separated running node containers                  |
| `DISCOVERY_REACHABLE_NODE`      | `reachable`: the node that advertises the reachable name |
| `DISCOVERY_PASS_THROUGH_BRANCH` | `A` for pass-through-names, `B` for pass-through-ports   |
| `DISCOVERY_PORT_MAP_RANGE`      | `port-map-range`: first and last address of the range    |
| `DISCOVERY_PORT_MAP_PORT_BASE`  | `port-map-range`: `31000`                                |
| `DISCOVERY_TLS_CERT_SHAPE`      | `san-only` or `cn-only` in `ee`                          |
| `DISCOVERY_CLIENT_NETWORK_MTU`  | `low-mtu`: `1200`                                        |
| `DISCOVERY_PROXY_PORT_RANGE`    | `port-range`: `40000 40009`                              |
| `DISCOVERY_IDLE_RESET`          | `idle-reset`: `1`                                        |
| `DISCOVERY_CONTROL_DIR`         | Only for profiles with the node server: `control-dir`    |

## Tests

```bash
bats .github/actions/discovery-l2-topology/tests/
```

The tests cover the profile settings and the env file. The profile steps themselves need Docker and a cluster, so they run in a client's L2 workflow.
