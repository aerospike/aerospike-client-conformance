# discovery-node-server

Starts a background server that adds and removes Aerospike nodes while a discovery test runs, so a test can scale the cluster out or in mid-test. Every client uses the same server and the same file format; each client only needs a small helper in its tests that writes a request and reads the ack.

For starting or stopping a node between steps rather than inside a test, use [`aerospike-node-control`](https://github.com/aerospike/shared-workflows/tree/main/.github/actions/aerospike-node-control) from shared-workflows.

## Usage

Create the cluster with [`setup-aerospike-server`](https://github.com/aerospike/shared-workflows/tree/main/.github/actions/setup-aerospike-server) and leave at least one node parked with `start-nodes`. Start this server before the step that runs the tests, and pass `control-dir` to the tests.

```yaml
- uses: aerospike/shared-workflows/.github/actions/setup-aerospike-server@<sha>
  id: aerospike
  with:
    num-nodes: "4"
    start-nodes: "3"
    container-name-prefix: discovery-as
    network-subnet: 172.30.66.0/24

- uses: aerospike/aerospike-client-conformance/.github/actions/discovery-node-server@<sha>
  id: node-server
  with:
    control-dir: ${{ github.workspace }}/target/discovery-control
    network-name: ${{ steps.aerospike.outputs.network-name }}
    parked-container: discovery-as-4
    config-container: discovery-as-1

- name: Run the discovery tests
  env:
    DISCOVERY_CONTROL_DIR: ${{ github.workspace }}/target/discovery-control
  run: <client test command>

- name: Show the node server log
  if: ${{ always() }}
  run: cat "${{ steps.node-server.outputs.log-file }}"
```

The server runs until the job ends; GitHub stops it then. When the tests run in a container, mount `control-dir` into it.

## Inputs

| Name               | Required | Default | Description                                                                                                    |
| ------------------ | -------- | ------- | -------------------------------------------------------------------------------------------------------------- |
| `control-dir`      | Yes      |         | Directory the tests write requests to; created if missing                                                      |
| `network-name`     | Yes      |         | Docker network of the nodes                                                                                    |
| `parked-container` | Yes      |         | A created but not started node, `<prefix>-<index>`. Starts clone it as `<prefix>-<index + 1>`, `+ 2` and so on |
| `config-container` | Yes      |         | A running node that mounts the config volume                                                                   |
| `alternate-host`   | No       | `""`    | Hostname that `alternate-port=<port>` advertises as `alternate-access-address`                                 |
| `ready-timeout`    | No       | `30`    | Seconds to wait for the server to be ready                                                                     |

## Outputs

| Name       | Description                                            |
| ---------- | ------------------------------------------------------ |
| `log-file` | The server's log, one line per node started or removed |

## Requests

A test picks a unique id, writes one line to `<id>.tmp` in `control-dir` and renames it to `<id>.request`, so the server never reads a partial request. The server deletes the request and answers with `<id>.ack`, written the same way. The ack holds one `key=value` per line; the test reads it, deletes it, and treats an `error` key as a failure.

| Request                                    | Ack on success                        |
| ------------------------------------------ | ------------------------------------- |
| `start`                                    | `container=<name>` and `address=<ip>` |
| `start alternate-port=<port>`              | the same                              |
| `start address=<ip>`                       | the same                              |
| `start alternate-port=<port> address=<ip>` | the same                              |
| `stop <name>`                              | `removed=<name>`                      |

- `start` runs a new node cloned from `parked-container`: same image, command, mounts and extra hosts, and its config copied under the new name.
- `alternate-port=<port>` adds `alternate-access-address <alternate-host>` and `alternate-access-port <port>` to the new node's service section. It needs `alternate-host`.
- `address=<ip>` gives the new node that IP. The network needs a fixed subnet (`network-subnet`).
- `stop <name>` removes a node this server started; it refuses any other container.

Any failure answers `error=<message>` instead.

A Java helper, for example:

```java
Path dir = Paths.get(System.getenv("DISCOVERY_CONTROL_DIR"));
String id = UUID.randomUUID().toString();
Files.write(dir.resolve(id + ".tmp"), "start address=172.30.66.6\n".getBytes(StandardCharsets.UTF_8));
Files.move(dir.resolve(id + ".tmp"), dir.resolve(id + ".request"), StandardCopyOption.ATOMIC_MOVE);
// wait for <id>.ack, read key=value lines, delete it
```

## Tests

`tests/` runs the server against a stub `docker`, so it needs only `bats` and `jq`:

```bash
bats .github/actions/discovery-node-server/tests/
```
