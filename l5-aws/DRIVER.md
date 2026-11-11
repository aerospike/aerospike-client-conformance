# L5 driver contract

Each client runs L5 through a driver: one executable in the client's repository that builds a client, applies a fixed load, and reports what the client sees as JSON lines. The driver measures and reports; it asserts nothing. The harness drives the disruptions through `l5ctl`, reads the driver's output and makes every pass/fail decision, the same way for every client.

This document is for the engineer writing a client's driver, and is what the harness may rely on. Operating the harness is in [README.md](README.md).

Contract version: **5**.

Changed from 3: a `--port-map` key may be an advertised `HOST:PORT` as well as a node name, and the `large` command writes and reads records of a given size. Version 4 is reserved for the optional TLS flags being added in a separate change, which is not merged yet; version 5 does not include them.

Changed from 2 to 3: each failed attempt of the load writes a `failure` with the `t` its attempt started, so the harness can tell an attempt sent before a routing change from one sent after it.

Changed from 1 to 2: `watch` probes every address `HOST` resolves to, and `servingAt` is the start of the first of 3 consecutive rounds in which every address connected, where in 1 it was the start of the first attempt that connected to the first address. The `watch` reply adds `addresses`.

Status: the harness side is [`rows.py`](rows.py), which `run.sh` runs for each row when the manifest sets `L5_DRIVER`. A manifest without `L5_DRIVER` still runs its rows through `L5_RUN`, as described in the README. The L2 harness, [`l2-proxy/rows.py`](../l2-proxy/rows.py), runs the same driver against a local proxy topology.

A harness reads the version from `ready` and skips, with the reason, every row that needs a later version than the driver reports; a version 1 to 3 driver still runs every other row. `rows.py` here reads versions 1 to 3 and `l2-proxy/rows.py` reads 1, 2, 3 and 5.

## Packaging

The client's manifest (see the README) sets:

| Variable    | Runs                                                                                                                                                |
| ----------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `L5_SETUP`  | Once, to install the client's toolchain                                                                                                             |
| `L5_BUILD`  | Once, after setup, to build the client and its driver                                                                                               |
| `L5_DRIVER` | The driver command. The harness appends the flags below and runs it with `bash -c` from the repository root, as many times per row as the row needs |

The runner is Amazon Linux 2023 on x86_64. The driver gets the harness environment (README), of which it needs only `AEROSPIKE_ADMIN_PASSWORD`. L2 runs the same commands in an Amazon Linux 2023 container, as described in [its README](../l2-proxy/README.md).

## Invocation

```text
$L5_DRIVER --seed HOST:PORT --shape pass-through|port-map [--port-map NODE=HOST:PORT,...] [--on-miss strict|pass-through]
           [--user USER] --tend-interval MS --timeout MS --namespace NS --set SET [--workers N] [--keys N]
```

| Flag                   | Meaning                                                                                                |
| ---------------------- | ------------------------------------------------------------------------------------------------------ |
| `--seed`               | The only seed host                                                                                     |
| `--shape pass-through` | Use the services-alternate peer list, with the pass-through translator                                 |
| `--shape port-map`     | Use the standard peer list, with the port-map translator built from `--port-map`                       |
| `--port-map`           | Node name or advertised `HOST:PORT` to `host:port`, comma separated. Only with `--shape port-map`      |
| `--on-miss`            | Translator miss mode. Default `strict`                                                                 |
| `--user`               | User name; the password is `AEROSPIKE_ADMIN_PASSWORD`. Without it, no authentication                   |
| `--tend-interval`      | Client tend interval in milliseconds                                                                   |
| `--timeout`            | Cluster connection timeout, and the socket and total timeout of every command attempt, in milliseconds |
| `--namespace`, `--set` | Where the load and `serve` write                                                                       |
| `--workers`            | Concurrent load workers. Default 4                                                                     |
| `--keys`               | Keys the load cycles over. Default 1000                                                                |

A `--port-map` key that contains `:` is an advertised `HOST:PORT` (version 5), matched against the endpoint as the node advertises it on the standard list; any other key is a node name. Pass every key to the client's port-map translator as given.

Map each flag to the client's own configuration of the same meaning. In Java, `pass-through` is `useServicesAlternate = true` with `PassThroughAddressTranslator.INSTANCE`, and `port-map` is `useServicesAlternate = false` with `new PortMapAddressTranslator(map)` and `translationOnMiss`.

Set the client's own command retries to zero. The driver retries, so the timing is the same in every client, whatever its retry and sleep settings do.

## Load

Start the load once the client is created. Each of the `--workers` workers runs one command at a time, forever, until `quit`:

- Worker `w` (from 0), iteration `i` (from 0) uses key `load-<k>` with `k = (w + i * workers) % keys`. An even `i` puts bin `v` with the integer `i`; an odd `i` gets the record.
- An attempt fails on any error the client returns or raises. Count it in `failed`, keep its message as `lastError`, write a `failure` (Output), sleep one tend interval, and issue the same command again. Repeat until it succeeds.
- A success counts in `succeeded`. Its latency is the time from its first attempt to its success. `maxLatencyMs` is the largest latency of the commands whose first attempt started at or after the last `reset`.

On a worker runtime without threads, run the workers as concurrent tasks; each still has one command in flight.

## Output

Stdout carries only JSON objects, one per line, each flushed as it is written. Everything else goes to stderr.

Every object has `type`, and `t`: milliseconds on a monotonic clock since the driver started, as a number. All intervals are differences of `t`, or are measured inside the driver on the same clock. The harness never times anything by when a line arrives.

### `ready`

Once, when the first load command has succeeded.

```json
{
	"type": "ready",
	"t": 812,
	"contract": 5,
	"epochMs": 1762189200123,
	"client": "java",
	"clientVersion": "9.2.0",
	"shape": "pass-through"
}
```

`epochMs` is the wall clock at `t`, for lining up with other logs only.

### `snapshot`

Every 100 ms, and immediately whenever the routing changes: a node is added or removed, a node's host, port or `active` changes, or `unowned` changes.

```json
{
	"type": "snapshot",
	"t": 1912,
	"reason": "heartbeat",
	"nodes": [
		{
			"name": "1A0",
			"host": "db.l5.aerospike.internal",
			"port": 30287,
			"address": "10.80.1.25",
			"active": true,
			"reachable": true
		}
	],
	"unowned": 0,
	"owners": { "1A0": 1365, "2A0": 1365, "3A0": 1366 },
	"longestUnownedMs": 0,
	"clientErrors": 0,
	"clientTimeouts": 0,
	"succeeded": 81234,
	"failed": 0,
	"maxLatencyMs": 42,
	"lastError": null
}
```

| Field                                              | Required                                | Meaning                                                                                                                                                                                                                                                                                 |
| -------------------------------------------------- | --------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `reason`                                           | yes                                     | `heartbeat` or `change`                                                                                                                                                                                                                                                                 |
| `nodes`                                            | yes                                     | Every node the client holds: its name; the host and port it connects to; `address`, the IP address that host resolved to for the client's connections to the node; whether the client considers it active; and `reachable` from the latest reachability check (`null` before the first) |
| `unowned`                                          | if the client exposes its partition map | Partitions of `--namespace` that are unowned (Definitions)                                                                                                                                                                                                                              |
| `owners`                                           | with `unowned`                          | Partitions whose master is each node                                                                                                                                                                                                                                                    |
| `longestUnownedMs`                                 | with `unowned`                          | Longest stretch since the last `reset` in which `unowned` was above zero, including a stretch still open                                                                                                                                                                                |
| `clientErrors`, `clientTimeouts`                   | if the client counts them               | The client's own error and timeout counts, summed over nodes                                                                                                                                                                                                                            |
| `succeeded`, `failed`, `maxLatencyMs`, `lastError` | yes                                     | From the load                                                                                                                                                                                                                                                                           |

Leave out a field the client cannot provide; never send a placeholder. The harness asserts on `unowned` only when it is present, and otherwise on `serve` and command latency.

### `failure`

Once for each failed attempt of the load, when it fails.

```json
{
	"type": "failure",
	"t": 19370,
	"startedAt": 19110,
	"message": "Node 2A0 10.80.1.25 30287: Timeout"
}
```

`startedAt` is the `t` at the start of that attempt, not of the command's first attempt. `message` is the error message, as kept in `lastError`. An attempt that fails before `ready` counts in `failed` but writes no `failure`, since `ready` is the first line and `t` never goes back.

### `log`

Each message the client logs at warning level or above.

```json
{ "type": "log", "t": 2210, "level": "WARN", "message": "..." }
```

### `served`, `reset`, `watch`, `large`, `error`

The replies to the commands below.

## Commands

Stdin carries one command per line. End of input means `quit`. An unknown or malformed command gets an `error` reply and is otherwise ignored.

### `serve STEP`

Write and read one key in every partition of `--namespace`. Take keys `STEP-0`, `STEP-1`, ... in order, keeping the first that falls in each partition (partition ID from the key digest, as the client computes it), until every partition has one. For each partition, put bin `v` with the string `STEP-<partition>`, then get it, one attempt each. Then reply:

```json
{
	"type": "served",
	"t": 5210,
	"step": "after",
	"partitions": 4096,
	"written": 4096,
	"read": 4096,
	"mismatched": 0,
	"failures": 0,
	"firstFailure": null,
	"elapsedMs": 3100
}
```

`read` counts gets that returned the value written; `mismatched` counts gets that returned a record with another value or none.

### `reset`

Restart `maxLatencyMs` and `longestUnownedMs` from now, and reply `{"type":"reset","t":...}`.

### `watch HOST:PORT NODE`

Measure how soon the client uses a node once its listener serves:

1. Every 10 ms, resolve `HOST` and open a TCP connection to every address it resolves to, at `PORT`, each with a 50 ms connect timeout, concurrently, and close them. A round whose connects have not all finished before the next 10 ms tick delays that tick; rounds never overlap. `servingAt` is the `t` at the start of the first of 3 consecutive rounds in which every address connected; `lastClosedAt` is the `t` at the start of the last round in which any address did not connect. Every address is required because the client may connect to any of them, so the measurement starts only when none refuses.
2. From `servingAt`, every 10 ms, check whether `NODE` is usable (Definitions). `usableAt` is the `t` of the first check where it is.
3. Reply when `NODE` is usable, or 60000 ms after `servingAt`:

```json
{
	"type": "watch",
	"t": 90412,
	"address": "db.l5.aerospike.internal:31000",
	"node": "4A0",
	"servingAt": 90150,
	"lastClosedAt": 90140,
	"addresses": [
		{ "address": "10.80.1.25", "firstConnectedAt": 90100 },
		{ "address": "10.80.2.31", "firstConnectedAt": 90130 },
		{ "address": "10.80.3.17", "firstConnectedAt": 90150 }
	],
	"usableAt": 90400,
	"usableMs": 250,
	"ownershipChecked": true
}
```

`addresses` has one object per address `HOST` resolved to, in resolution order: `address`, the IP address, and `firstConnectedAt`, the `t` at the start of the first round in which it connected, or `null` if none did.

`usableAt` and `usableMs` are `null` if the node did not become usable. `ownershipChecked` is false when the client does not expose its partition map. Probe and check in the driver, with no harness round trip; this is the one measurement whose bound, twice the tend interval, is too tight to rebuild from snapshots.

One `watch` at a time; a second gets an `error` reply.

### `large STEP BYTES KEYS`

Version 5. Write and read `KEYS` keys, `STEP-0` to `STEP-<KEYS-1>`, each with bin `v` a string of exactly `BYTES` characters that starts with the key's name, one put then one get per key, one attempt each. Then reply:

```json
{
	"type": "large",
	"t": 7310,
	"step": "mtu",
	"bytes": 1048576,
	"keys": 8,
	"written": 8,
	"read": 8,
	"mismatched": 0,
	"failures": 0,
	"firstFailure": null,
	"maxMs": 41,
	"elapsedMs": 610
}
```

`read` and `mismatched` count as for `serve`. `maxMs` is the longest single put or get.

### `quit`

Stop the load: let each worker finish its current attempt, and give up after 30 s. Close the client and exit 0.

### `error`

```json
{ "type": "error", "t": 3001, "message": "unknown command: serv x" }
```

## Definitions

- **Reachable**: an info request for `node` to that node answers with the node's name within 1000 ms. Check every node every 500 ms on a thread or task of its own, so a slow check never delays the 10 ms routing poll.
- **Unowned**: a partition whose master in the client's map is missing, not active, or not the node the client holds under that name.
- **Usable**: the client holds the node, it is active, and, when the client exposes its partition map, no partition is unowned.

## Timing

- Poll the client's routing (nodes, their host, port and `active`, and the partition map) every 10 ms, and send a `change` snapshot as soon as it differs.
- Heartbeat snapshots every 100 ms, whether or not anything changed.
- Every `t` is read from one monotonic clock in the driver process.
- Nothing a command does may stop the load, the routing poll or the snapshots.

## Exit status

| Status | When                                                                        |
| ------ | --------------------------------------------------------------------------- |
| 0      | After `quit` or end of input                                                |
| 2      | Bad flags; the reason on stderr                                             |
| 3      | The client could not be created; an `error` line, then the reason on stderr |

## Checking a driver locally

Against any single Aerospike server on `127.0.0.1:3000`, with its node name from `asinfo -v node`:

```text
printf 'serve smoke\nwatch 127.0.0.1:3000 <node>\nquit\n' | (sleep 5; cat) | \
  $L5_DRIVER --seed 127.0.0.1:3000 --shape port-map --port-map <node>=127.0.0.1:3000 \
  --user admin --tend-interval 250 --timeout 1000 --namespace test --set smoke
```

Expect `ready`; snapshots about every 100 ms with one node, active and reachable; `served` with 4096 written and read and no failures; `watch` with a `usableMs` near 0; and exit status 0. Leave `--user` out against a server without security.
