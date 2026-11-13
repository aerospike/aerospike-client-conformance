#!/usr/bin/env python3
#
# The L2 rows of one profile through a client's driver (l5-aws/DRIVER.md, contract version 5, 3, 2 or 1).
# run.sh runs it on the host once per profile, after it has built the topology, with L2_CTL naming
# l2ctl and L2_STATE the topology's state directory. The driver command is run.sh's wrapper, which
# starts the client's driver in a container on the client network. The driver's JSON lines are read
# with the driver code of l5-aws/rows.py; faults and readings go through $L2_CTL. Every interval a row
# asserts on is a difference of driver t values or a field the driver measured, except the re-seed
# bound, which also allows the wall time of the l2ctl call that ends the outage. Each check prints an
# L2-CHECK line with its measurement, bound and outcome, and each row ends with an L2-ROW line: pass,
# fail, or skip with the reason. A row leaves the topology as it found it.
#
# Exit status: 0 no row failed, 1 a row failed, 2 bad usage or environment, 3 a driver broke the
# contract.
#
# Usage: rows.py --profile PROFILE --driver "<driver command>" [--rows ROW,...] [--no-fault]
#        rows.py --plan --profile PROFILE

import argparse
import contextlib
import importlib.util
import os
import subprocess
import sys
import time

_spec = importlib.util.spec_from_file_location("l5_rows", os.path.join(os.path.dirname(os.path.abspath(__file__)),
	os.pardir, "l5-aws", "rows.py"))
l5 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(l5)
l5.TAG = "L2"

check, emit, say, fmt, describe, names = l5.check, l5.emit, l5.say, l5.fmt, l5.describe, l5.names
RowFailure, ContractError, UsageError = l5.RowFailure, l5.ContractError, l5.UsageError

CONTRACTS = (1, 2, 3, 5)
MESSAGE_FIELDS = dict(l5.MESSAGE_FIELDS, large=("step", "bytes", "keys", "written", "read", "mismatched", "failures",
	"firstFailure", "maxMs", "elapsedMs"))

PROFILES = ("default", "reachable", "pass-through-names", "pass-through-ports", "port-map-range", "port-range",
	"low-mtu", "idle-reset")
NAMESPACE = l5.NAMESPACE
PARTITIONS = l5.PARTITIONS
CLUSTER_SIZE = 3
TEND_INTERVAL_MS = l5.TEND_INTERVAL_MS
TIMEOUT_MS = l5.TIMEOUT_MS
ROUTING_WAIT_MS = l5.ROUTING_WAIT_MS
QUIET_WAIT_MS = l5.QUIET_WAIT_MS
REPLY_WAIT_MS = l5.REPLY_WAIT_MS
CTL_TIMEOUT_MS = 180000
NODE_WAIT_S = 120

MISS_WINDOW_MS = 20 * TEND_INTERVAL_MS
FAULT_WAIT_MS = 60000
FAULT_HOLD_MS = 5000
ATTEMPT_BOUND_MS = TIMEOUT_MS + 500
RECOVERY_MS = 60000
LATENCY_WINDOW_MS = 2000
RECOVERED_LATENCY_MS = TIMEOUT_MS
TOXIC_LATENCY_MS = 300
RESEED_BOUND_MS = 2 * TEND_INTERVAL_MS + TIMEOUT_MS
USABLE_TEND_INTERVALS = l5.USABLE_TEND_INTERVALS
LISTENER_WAIT_MS = 60000
LARGE_BYTES = 1 << 20
LARGE_KEYS = 8
LARGE_WAIT_MS = 120000
HEAVY_WORKERS = 64
LIGHT_WORKERS = 2
EXHAUSTED_QUIET_WAIT_MS = 180000
PORTS_FREE_WAIT_MS = 90000
PORTS_POLL_S = 5


def contract_problem(msg, contract=None):
	if isinstance(msg, dict) and msg.get("type") == "large" and contract != 5:
		return "large from a driver without contract 5"
	return l5.contract_problem(msg, contract, CONTRACTS, MESSAGE_FIELDS)


class Ctl:
	"""Runs l2ctl with a bound per call and parses the L2-* lines it prints."""

	def __init__(self, path):
		self.path = path

	def __call__(self, bound_ms, *args):
		command = "l2ctl " + " ".join(args)
		start = time.monotonic()
		proc = subprocess.Popen([self.path] + list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
			stderr=subprocess.PIPE, universal_newlines=True, start_new_session=True)
		try:
			out, err = proc.communicate(timeout=bound_ms / 1000)
			status = proc.returncode
		except subprocess.TimeoutExpired:
			l5.kill_group(proc)
			out, err = proc.communicate()
			status = None
		elapsed_ms = int((time.monotonic() - start) * 1000)
		lines = []
		for text in out.splitlines():
			if text.startswith("L2-"):
				say(text)
				lines.append(l5.Line(text))
		err = err.strip()
		emit("L2-CTL", command=command, exit=status, elapsedMs=elapsed_ms, boundMs=bound_ms)
		check("l2ctl-bound", status is not None, quiet=True, command=command, elapsedMs=elapsed_ms, boundMs=bound_ms)
		if status != 0 and err:
			say("L2-CTL-STDERR " + err.replace("\n", " "))
		result = l5.Result(command, status, lines, err)
		result.elapsed_ms = elapsed_ms
		return result

	def ok(self, *args, name=None, bound_ms=CTL_TIMEOUT_MS):
		result = self(bound_ms, *args)
		check(name or args[0], result.exit == 0, command=result.command, exit=result.exit,
			stderr=result.stderr.replace("\n", " ")[-500:])
		return result


def read_values(path):
	values = {}
	with open(path) as f:
		for line in f:
			line = line.rstrip("\n")
			if "=" in line and not line.startswith("#"):
				key, value = line.split("=", 1)
				values[key] = value
	return values


class Env:
	def __init__(self, profile, driver, no_fault):
		missing = [name for name in ("L2_CTL", "L2_STATE") if not os.environ.get(name)]
		if missing:
			emit("L2-ENVIRONMENT", missing=",".join(missing))
			raise UsageError("environment missing " + ", ".join(missing))
		path = os.path.join(os.environ["L2_STATE"], "topology.env")
		if not os.path.isfile(path):
			raise UsageError("no topology at " + path)
		if not os.access(os.environ["L2_CTL"], os.X_OK):
			raise UsageError("L2_CTL %s is not executable" % os.environ["L2_CTL"])
		self.values = read_values(path)
		if self.values.get("L2_PROFILE") != profile:
			raise UsageError("the topology is profile %s, not %s" % (self.values.get("L2_PROFILE"), profile))
		self.profile = profile
		self.driver_command = driver
		self.no_fault = no_fault
		self.ctl = Ctl(os.environ["L2_CTL"])
		self.endpoint = self.values["DISCOVERY_ENDPOINT_HOSTNAME"]
		self.ports = [int(p) for p in self.values["DISCOVERY_ENDPOINT_PORTS"].split(",")]
		self.proxy_address = self.values.get("L2_PROXY_CLIENT_ADDRESS", "")
		self.contract = None
		self.outcome = None
		emit("L2-ENVIRONMENT", profile=profile, endpoint=self.endpoint, ports=self.ports, noFault=no_fault)

	def value(self, key):
		if not self.values.get(key):
			raise UsageError("topology value %s is not set" % key)
		return self.values[key]

	def address(self, port, host=None):
		return "%s:%d" % (host or self.endpoint, port)

	def topology(self, size=CLUSTER_SIZE):
		"""Each node as l2ctl reads it: container, node name, address, listener and port, and what it advertises."""
		lines = self.ctl.ok("nodes").tagged("L2-NODE")
		nodes = [{"container": line["container"], "node": line["node"], "address": line["address"],
			"listener": line["listener"], "port": int(line["port"] or 0), "std": line.fields.get("std", ""),
			"alt": line.fields.get("alt", ""), "state": line["state"]} for line in lines]
		down = [n["container"] for n in nodes if n["state"] != "up" or not n["node"] or n["port"] <= 0]
		check("topology", not down and len(nodes) == size, nodes=["%s=%s@%s" % (n["container"], n["node"], n["port"])
			for n in nodes], downOrWithoutListener=down, expected=size)
		return nodes

	def fault(self, *args):
		"""Apply or remove a fault through l2ctl, unless the run is a control run without faults."""
		if self.no_fault:
			emit("L2-FAULT", skipped=" ".join(args))
			return None
		return self.ctl.ok(*args)

	@contextlib.contextmanager
	def driver(self, set_name, shape="port-map", port_map=None, on_miss=None, seed_port=None, workers=None):
		drv = self.start(set_name, shape, port_map, on_miss, seed_port, workers)
		try:
			with drv:
				self.contract = drv.contract
				yield drv
		finally:
			self.stop_drivers()

	def start(self, set_name, shape="port-map", port_map=None, on_miss=None, seed_port=None, workers=None):
		flags = ["--seed", self.address(seed_port or self.ports[0]), "--shape", shape]
		if port_map is not None:
			flags += ["--port-map", ",".join("%s=%s" % kv for kv in sorted(port_map.items()))]
		if on_miss is not None:
			flags += ["--on-miss", on_miss]
		flags += ["--tend-interval", str(TEND_INTERVAL_MS), "--timeout", str(TIMEOUT_MS), "--namespace", NAMESPACE,
			"--set", set_name]
		if workers is not None:
			flags += ["--workers", str(workers)]
		return l5.Driver(self.driver_command, flags, shape, self.outcome, problem=contract_problem)

	def stop_drivers(self):
		self.ctl(CTL_TIMEOUT_MS, "drivers-stop")

	def driver_contract(self):
		"""The driver's contract version, from a driver started for it when no row has started one yet."""
		if self.contract is None:
			emit("L2-CONTRACT-PROBE", reason="no driver started yet in this profile")
			nodes = self.topology()
			with self.driver("l2-contract", port_map=port_map_by_name(self, nodes)):
				pass
		return self.contract

	def wait_cluster(self, size=CLUSTER_SIZE):
		self.ctl.ok("wait-cluster", str(size), "--timeout", str(NODE_WAIT_S), bound_ms=NODE_WAIT_S * 1000 + CTL_TIMEOUT_MS)


def port_map_by_name(env, nodes):
	return {n["node"]: env.address(n["port"]) for n in nodes}


def port_map_by_address(env, nodes):
	return {n["std"]: env.address(n["port"]) for n in nodes}


def held(snapshot, expected):
	"""A snapshot holding exactly the expected nodes, each active and reachable."""
	return (names(snapshot) == sorted(expected) and
		all(n["active"] is True and n["reachable"] is True for n in snapshot["nodes"]))


def await_held(drv, step, expected, bound_ms=ROUTING_WAIT_MS):
	snapshot, waited = drv.wait(lambda m: m if m["type"] == "snapshot" and held(m, expected) else None, bound_ms)
	last = snapshot or drv.latest()
	check("held", snapshot is not None, step=step, expected=sorted(expected), nodes=describe(last["nodes"]),
		unowned=last.get("unowned"), waitedMs=waited, boundMs=bound_ms)
	return snapshot


def assert_never_held(drv, step, node, window_ms=MISS_WINDOW_MS):
	"""Watch window_ms of driver t from now; fail if any snapshot holds node."""
	since = drv.mark()
	end, observed = drv.wait(lambda m: m if m["type"] == "snapshot" and m["t"] - since[1] >= window_ms else None,
		window_ms + ROUTING_WAIT_MS, since=since)
	check("window", end is not None, step=step, observedMs=observed, windowMs=window_ms)
	holding = [s["t"] for s in drv.between(since[0], end["_index"]) if node in names(s)]
	check("never-held", not holding, step=step, node=node, heldAt=holding[:5], windowMs=window_ms,
		unowned=end.get("unowned"))
	return end


def assert_nodes_at(env, step, snapshot, expected):
	"""Each held node at its expected host and port. expected maps a node name to (host, port); a host also
	matches the Toxiproxy address every topology name resolves to on the client network."""
	wrong = []
	for node in snapshot["nodes"]:
		host, port = expected.get(node["name"], (None, None))
		emit("L2-NODE-AT", step=step, node=node["name"], at="%s:%s" % (node["host"], node["port"]), address=node["address"],
			expected=host and "%s:%s" % (host, port))
		if host is None or node["port"] != port or node["host"] not in (host, env.proxy_address):
			wrong.append("%s@%s:%s" % (node["name"], node["host"], node["port"]))
	check("node-address", not wrong, step=step, wrong=wrong, expected={k: "%s:%s" % v for k, v in sorted(expected.items())},
		proxyAddress=env.proxy_address)


def serve_held(drv, step, snapshot):
	"""serve with nodes missing from the client: every partition a held node masters is read back."""
	served, waited = drv.request("serve " + step, "served", l5.SERVE_WAIT_MS, lambda m: m["step"] == step)
	check("served-reply", served is not None, step=step, waitedMs=waited, boundMs=l5.SERVE_WAIT_MS)
	owned = sum(snapshot.get("owners", {}).values())
	check("serve-held", served["mismatched"] == 0 and served["read"] >= owned, step=step, read=served["read"],
		written=served["written"],
		failures=served["failures"], mismatched=served["mismatched"], ownedByHeld=owned, firstFailure=served["firstFailure"])


def expect_not_created(env, step, named=(), **flags):
	"""The driver exits 3 before ready: the client could not be created, and its error names each of named."""
	drv = env.start("l2-" + step, **flags)
	try:
		try:
			drv.await_ready()
			created = True
		except RowFailure as e:
			if not str(e).startswith("client-created"):
				raise
			created = False
		check("client-not-created", not created, step=step, status=drv.status,
			ready=drv.ready and {k: drv.ready[k] for k in ("client", "contract")})
		errors = [m["message"] for m in drv.between(0, len(drv.messages), "error")]
		unnamed = [name for name in named if not any(name in message for message in errors)]
		check("error-names", not unnamed, step=step, unnamed=unnamed, errors=errors)
	finally:
		try:
			drv.close()
		except RowFailure as e:
			if not str(e).startswith("client-created"):
				raise
		env.stop_drivers()


def recovered(env, drv, step, nodes):
	"""After a fault is removed: every node routed at its listener, every partition served, the load quiet, and
	the commands started after that within RECOVERED_LATENCY_MS."""
	snapshot = l5.await_routing(drv, step, [n["node"] for n in nodes], RECOVERY_MS)
	assert_nodes_at(env, step, snapshot, {n["node"]: (env.endpoint, n["port"]) for n in nodes})
	l5.serve(drv, step)
	l5.await_quiet(drv, step, QUIET_WAIT_MS)
	reset, _ = drv.request("reset", "reset", REPLY_WAIT_MS)
	check("reset-reply", reset is not None, boundMs=REPLY_WAIT_MS)
	end, _ = drv.wait(lambda m: m if m["type"] == "snapshot" and m["t"] - reset["t"] >= LATENCY_WINDOW_MS else None,
		LATENCY_WINDOW_MS + REPLY_WAIT_MS, since=(reset["_index"] + 1, reset["t"]))
	check("latency-window", end is not None, step=step, windowMs=LATENCY_WINDOW_MS)
	check("recovered-latency", end["maxLatencyMs"] <= RECOVERED_LATENCY_MS, step=step, maxLatencyMs=end["maxLatencyMs"],
		boundMs=RECOVERED_LATENCY_MS, windowMs=LATENCY_WINDOW_MS, failed=end["failed"])


def failures_after(drv, since, hold_ms, step):
	"""Hold hold_ms of driver t from since and return the failures whose attempt started at or after since."""
	end, observed = drv.wait(lambda m: m if m["type"] == "snapshot" and m["t"] - since[1] >= hold_ms else None,
		hold_ms + FAULT_WAIT_MS, since=since)
	check("fault-window", end is not None, step=step, observedMs=observed, holdMs=hold_ms)
	return [f for f in drv.between(since[0], end["_index"], "failure") if f["startedAt"] >= since[1]], end


def assert_bounded_failures(step, failures, end, since_t):
	slowest = max(failures, key=lambda f: f["t"] - f["startedAt"]) if failures else None
	emit("L2-FAILURES", step=step, count=len(failures), firstAfterMs=failures and failures[0]["t"] - since_t,
		slowestMs=slowest and slowest["t"] - slowest["startedAt"], message=slowest and slowest["message"],
		succeeded=end["succeeded"], failed=end["failed"])
	check("fault-observed", bool(failures), step=step, failures=len(failures), holdMs=FAULT_HOLD_MS,
		lastError=end["lastError"])
	check("attempt-bound", slowest["t"] - slowest["startedAt"] <= ATTEMPT_BOUND_MS, step=step,
		slowestMs=slowest["t"] - slowest["startedAt"], boundMs=ATTEMPT_BOUND_MS, message=slowest["message"])


def row_no_route(env):
	"""From the client network, no node address connects and every endpoint port does."""
	result = env.ctl.ok("route-check")
	routes = result.tagged("L2-ROUTE")
	check("routes-read", len(routes) >= 2 * CLUSTER_SIZE, routes=len(routes))


def row_port_map_by_name(env):
	"""Port-map keyed by node name: every node at its listener, every partition owned by a distinct node and
	served."""
	nodes = env.topology()
	with env.driver("l2-port-map-name", port_map=port_map_by_name(env, nodes)) as drv:
		snapshot = l5.await_routing(drv, "port-map-name", [n["node"] for n in nodes], ROUTING_WAIT_MS)
		assert_nodes_at(env, "port-map-name", snapshot, {n["node"]: (env.endpoint, n["port"]) for n in nodes})
		l5.serve(drv, "port-map-name")


def row_port_map_by_address(env):
	"""Port-map keyed by each node's advertised standard address, the one it configures with no access
	address: every node at its listener and every partition served."""
	nodes = env.topology()
	bad = [n["container"] + "=" + n["std"] for n in nodes if not n["std"].endswith(":3000") or n["std"].count(":") != 1]
	check("advertised-standard", not bad, unexpected=bad)
	with env.driver("l2-port-map-address", port_map=port_map_by_address(env, nodes), on_miss="strict") as drv:
		snapshot = l5.await_routing(drv, "port-map-address", [n["node"] for n in nodes], ROUTING_WAIT_MS)
		assert_nodes_at(env, "port-map-address", snapshot, {n["node"]: (env.endpoint, n["port"]) for n in nodes})
		l5.serve(drv, "port-map-address")


def subset_row(env, step, mapped, missed, on_miss):
	"""A port-map for some nodes only: the mapped nodes are held at their listeners, the missed one is never
	held, and the partitions the held nodes master are served."""
	with env.driver("l2-" + step, port_map=port_map_by_name(env, mapped), on_miss=on_miss) as drv:
		snapshot = await_held(drv, step, [n["node"] for n in mapped])
		assert_nodes_at(env, step, snapshot, {n["node"]: (env.endpoint, n["port"]) for n in mapped})
		end = assert_never_held(drv, step, missed["node"])
		serve_held(drv, step, end)


def row_strict_miss(env):
	"""on_miss strict with one node unmapped: that node is never held and the others serve."""
	nodes = env.topology()
	subset_row(env, "strict-miss", nodes[:-1], nodes[-1], "strict")


def row_unmapped_peers(env):
	"""on_miss pass-through with only the seed mapped: the peers' advertised addresses, which the client has no
	route to, are used unchanged, so the client is not created and its error names each peer's address."""
	nodes = env.topology()
	seed = [n for n in nodes if n["port"] == env.ports[0]]
	check("seed-node", len(seed) == 1, seedPort=env.ports[0])
	peers = [n["address"] for n in nodes if n is not seed[0]]
	expect_not_created(env, "unmapped-peers", named=peers, port_map=port_map_by_name(env, seed), on_miss="pass-through")


def row_empty_alternate(env):
	"""pass-through shape against nodes that advertise no alternate address: the client is not created."""
	nodes = env.topology()
	with_alt = [n["container"] + "=" + n["alt"] for n in nodes if n["alt"]]
	check("alternate-empty", not with_alt, withAlternate=with_alt)
	expect_not_created(env, "empty-alternate", shape="pass-through")


def fault_row(env, step, victims, apply, remove, observe):
	"""One port-map driver: route, apply the fault, observe it, remove it, then recover."""
	nodes = env.topology()
	with env.driver("l2-" + step, port_map=port_map_by_name(env, nodes)) as drv:
		l5.await_routing(drv, step + "-before", [n["node"] for n in nodes], ROUTING_WAIT_MS)
		applied = False
		try:
			since = drv.mark()
			emit("L2-FAULT", step=step, victims=[v["container"] for v in victims], t=since[1])
			applied = True
			for args in apply:
				env.fault(*args)
			observe(drv, since)
		finally:
			if applied:
				for args in remove:
					env.outcome.guard(env.fault, *args)
		recovered(env, drv, step, nodes)


def victim(env):
	return [n for n in env.topology() if n["port"] != env.ports[0]][0]


def row_listener_closed(env):
	"""A node's listener closed: the client stops using the node, and once the listener is open again holds and
	serves every node within bounded command latency."""
	node = victim(env)

	def observe(drv, since):
		def gone(m):
			if m["type"] != "snapshot":
				return None
			at = [n for n in m["nodes"] if n["name"] == node["node"]]
			return m if not at or at[0]["active"] is not True or at[0]["reachable"] is not True else None
		snapshot, waited = drv.wait(gone, FAULT_WAIT_MS, since=since)
		check("fault-observed", snapshot is not None, node=node["node"], waitedMs=waited, boundMs=FAULT_WAIT_MS,
			nodes=describe((snapshot or drv.latest())["nodes"]))

	fault_row(env, "listener-closed", [node], [("listener", node["listener"], "off")],
		[("listener", node["listener"], "on")], observe)


def toxic_row(env, step, toxics):
	node = victim(env)

	def observe(drv, since):
		failures, end = failures_after(drv, since, FAULT_HOLD_MS, step)
		assert_bounded_failures(step, failures, end, since[1])

	apply = [("toxic-add", node["listener"], name, kind, "--stream", stream) + tuple(attrs) for name, kind, stream, attrs in
		toxics]
	remove = [("toxic-remove", node["listener"], name) for name, _, _, _ in toxics]
	fault_row(env, step, [node], apply, remove, observe)


def row_silent_drop(env):
	"""A node's listener holds data in both directions with no reply and no close: every failed attempt fails
	within the timeout, and the node serves again once the fault is removed."""
	toxic_row(env, "silent-drop", [("drop-down", "timeout", "downstream", ["timeout=0"]),
		("drop-up", "timeout", "upstream", ["timeout=0"])])


def row_reset_peer(env):
	"""A node's listener resets every connection: every failed attempt fails within the timeout, and the node
	serves again once the fault is removed."""
	toxic_row(env, "reset-peer", [("reset", "reset_peer", "downstream", ["timeout=0"])])


def row_latency(env):
	"""A node's replies delayed below the timeout: commands slow by the delay and none fails, and latency is back
	within the bound once the delay is removed."""
	node = victim(env)

	def observe(drv, since):
		reset, _ = drv.request("reset", "reset", REPLY_WAIT_MS)
		check("reset-reply", reset is not None, boundMs=REPLY_WAIT_MS)
		end, _ = drv.wait(lambda m: m if m["type"] == "snapshot" and m["t"] - reset["t"] >= FAULT_HOLD_MS else None,
			FAULT_HOLD_MS + FAULT_WAIT_MS, since=(reset["_index"] + 1, reset["t"]))
		check("fault-window", end is not None, holdMs=FAULT_HOLD_MS)
		at_reset = drv.between(0, reset["_index"])[-1]
		check("fault-observed", end["maxLatencyMs"] >= TOXIC_LATENCY_MS, maxLatencyMs=end["maxLatencyMs"],
			latencyMs=TOXIC_LATENCY_MS, holdMs=FAULT_HOLD_MS)
		check("delayed-latency", end["maxLatencyMs"] <= TOXIC_LATENCY_MS + TIMEOUT_MS, maxLatencyMs=end["maxLatencyMs"],
			boundMs=TOXIC_LATENCY_MS + TIMEOUT_MS)
		check("delayed-no-failures", end["failed"] == at_reset["failed"], failedAtReset=at_reset["failed"],
			failed=end["failed"], lastError=end["lastError"])

	fault_row(env, "latency", [node], [("toxic-add", node["listener"], "delay", "latency", "--stream", "downstream",
		"latency=%d" % TOXIC_LATENCY_MS)], [("toxic-remove", node["listener"], "delay")], observe)


def row_reseed(env):
	"""Every listener closed until the client holds no node: once they are open again the client re-seeds within
	two tend intervals plus the timeout, then holds and serves every node."""
	nodes = env.topology()
	step = "reseed"
	with env.driver("l2-reseed", port_map=port_map_by_name(env, nodes)) as drv:
		l5.await_routing(drv, step + "-before", [n["node"] for n in nodes], ROUTING_WAIT_MS)
		closed = False
		try:
			closed = True
			for n in nodes:
				env.fault("listener", n["listener"], "off")
			empty, waited = drv.wait(lambda m: m if m["type"] == "snapshot" and not m["nodes"] else None, FAULT_WAIT_MS)
			check("fault-observed", empty is not None, waitedMs=waited, boundMs=FAULT_WAIT_MS,
				nodes=describe((empty or drv.latest())["nodes"]))
			before = drv.mark()
			elapsed = 0
			for n in nodes:
				result = env.fault("listener", n["listener"], "on")
				elapsed += result.elapsed_ms if result else 0
			closed = False
			seeded, waited = drv.wait(lambda m: m if m["type"] == "snapshot" and m["nodes"] else None, RECOVERY_MS,
				since=before)
			check("reseed", seeded is not None and waited <= RESEED_BOUND_MS + elapsed, reseedMs=waited,
				boundMs=RESEED_BOUND_MS, listenerCallsMs=elapsed, nodes=describe((seeded or drv.latest())["nodes"]))
		finally:
			if closed:
				for n in nodes:
					env.outcome.guard(env.fault, "listener", n["listener"], "on")
		recovered(env, drv, step, nodes)


def reachable_split(env):
	nodes = env.topology()
	container = env.value("DISCOVERY_REACHABLE_NODE")
	reachable = [n for n in nodes if n["container"] == container]
	check("reachable-node", len(reachable) == 1, container=container)
	return [n for n in nodes if n is not reachable[0]], reachable[0]


def row_reachable_passes_through(env):
	"""on_miss pass-through with every node mapped but the one whose advertised address the client can reach:
	that node is held at its advertised address unchanged, the others at their listeners."""
	mapped, reachable = reachable_split(env)
	host = env.value("L2_ACCESS_HOSTNAME")
	check("advertised-reachable", reachable["std"] == env.address(reachable["port"], host), container=reachable["container"],
		std=reachable["std"], expected=env.address(reachable["port"], host))
	with env.driver("l2-reachable", port_map=port_map_by_name(env, mapped), on_miss="pass-through") as drv:
		everyone = mapped + [reachable]
		snapshot = l5.await_routing(drv, "reachable", [n["node"] for n in everyone], ROUTING_WAIT_MS)
		expected = {n["node"]: (env.endpoint, n["port"]) for n in mapped}
		expected[reachable["node"]] = (host, reachable["port"])
		assert_nodes_at(env, "reachable", snapshot, expected)
		l5.serve(drv, "reachable")


def row_reachable_strict(env):
	"""on_miss strict with the same map: the unmapped reachable node is never held and the others serve."""
	mapped, reachable = reachable_split(env)
	subset_row(env, "reachable-strict", mapped, reachable, "strict")


def row_alternate_with_alternate(env):
	"""pass-through shape: the nodes with an alternate address are held at it, and the node without one is
	never held."""
	with_alt, reachable = reachable_split(env)
	host = env.value("L2_ACCESS_HOSTNAME")
	check("alternate-advertised", all(n["alt"] == env.address(n["port"], host) for n in with_alt) and not reachable["alt"],
		alternates={n["container"]: n["alt"] for n in with_alt + [reachable]})
	with env.driver("l2-alternate", shape="pass-through") as drv:
		snapshot = await_held(drv, "alternate", [n["node"] for n in with_alt])
		assert_nodes_at(env, "alternate", snapshot, {n["node"]: (host, n["port"]) for n in with_alt})
		end = assert_never_held(drv, "alternate", reachable["node"])
		serve_held(drv, "alternate", end)


def row_seed_without_alternate(env):
	"""pass-through shape seeded at the node with no alternate address: the client is not created."""
	_, reachable = reachable_split(env)
	expect_not_created(env, "seed-without-alternate", shape="pass-through", seed_port=reachable["port"])


def pass_through_row(env, step):
	"""pass-through shape: every node at the alternate address it advertises, every partition owned by a
	distinct node and served."""
	nodes = env.topology()
	expected = {}
	for n in nodes:
		host, _, port = n["alt"].rpartition(":")
		expected[n["node"]] = (host, int(port or 0))
	wrong = [n["container"] + "=" + n["alt"] for n in nodes if expected[n["node"]][1] != n["port"] or
		not expected[n["node"]][0]]
	check("alternate-advertised", not wrong, wrong=wrong)
	with env.driver("l2-" + step, shape="pass-through") as drv:
		snapshot = l5.await_routing(drv, step, [n["node"] for n in nodes], ROUTING_WAIT_MS)
		assert_nodes_at(env, step, snapshot, expected)
		l5.serve(drv, step)


def row_distinct_names(env):
	"""Each node advertises its own alternate name and listener port: pass-through routes every node."""
	nodes = env.topology()
	check("distinct-names", len({n["alt"].rpartition(":")[0] for n in nodes}) == len(nodes),
		alternates=[n["alt"] for n in nodes])
	pass_through_row(env, "distinct-names")


def row_shared_name(env):
	"""Every node advertises one alternate name and its own listener port: pass-through routes every node."""
	nodes = env.topology()
	check("shared-name", len({n["alt"].rpartition(":")[0] for n in nodes}) == 1, alternates=[n["alt"] for n in nodes])
	pass_through_row(env, "shared-name")


def new_node_row(env, step, drv_args, start_args, address, listener_off, listener_on, restore):
	"""A node advertised before its listener serves is usable within two tend intervals of the listener
	serving, then every node is routed and served."""
	nodes = env.topology()
	state = {"container": None, "listener": False}
	try:
		with env.driver("l2-" + step, **drv_args) as drv:
			l5.await_routing(drv, step + "-before", [n["node"] for n in nodes], ROUTING_WAIT_MS)
			state["listener"] = True
			listener_off()
			started = env.ctl.ok("start-node", *start_args).first("L2-STARTED")
			state["container"] = started["container"]
			advertised = env.ctl.ok("wait-advertised", started["container"], "--timeout", str(NODE_WAIT_S),
				bound_ms=NODE_WAIT_S * 1000 + CTL_TIMEOUT_MS).first("L2-ADVERTISED")
			node = advertised["node"]
			watch_mark = drv.mark()
			drv.send("watch %s %s" % (address, node))
			sync_t = drv.mark()[1]
			listener_on(started["container"])
			reply, waited = drv.reply("watch", LISTENER_WAIT_MS, (watch_mark[0], sync_t))
			check("watch-reply", reply is not None, address=address, node=node, waitedMs=waited, boundMs=LISTENER_WAIT_MS)
			emit("L2-NODE-USABLE", node=node, advertised=address, servingAt=reply["servingAt"],
				lastClosedAt=reply["lastClosedAt"], usableAt=reply["usableAt"], usableMs=reply["usableMs"],
				ownershipChecked=reply["ownershipChecked"], tendInterval=TEND_INTERVAL_MS)
			check("watch-usable", reply["usableAt"] is not None and reply["usableMs"] is not None, node=node,
				servingAt=reply["servingAt"], usableAt=reply["usableAt"])
			budget = USABLE_TEND_INTERVALS * TEND_INTERVAL_MS
			check("usable-ms", reply["usableMs"] <= budget, node=node, usableMs=reply["usableMs"], boundMs=budget)
			l5.await_routing(drv, step, [n["node"] for n in nodes] + [node], ROUTING_WAIT_MS)
			l5.serve(drv, step)
	finally:
		if state["container"]:
			env.outcome.guard(env.ctl.ok, "stop-node", state["container"])
		if state["listener"]:
			env.outcome.guard(restore, state["container"])
		if state["container"]:
			env.outcome.guard(env.wait_cluster)


def row_new_node_shared_name(env):
	"""A node started on the shared alternate name with its own port is usable by the pass-through client
	within two tend intervals of its listener serving, with no client change."""
	host = env.value("L2_ACCESS_HOSTNAME")
	port = int(env.value("L2_EXTRA_PORT_BASE"))

	def restore(container):
		if container and container in [line["name"] for line in env.ctl.ok("listeners").tagged("L2-LISTENER")]:
			env.ctl.ok("listener-remove", container)

	new_node_row(env, "new-node-shared-name", {"shape": "pass-through"}, ("--alternate", "%s:%d" % (host, port)),
		"%s:%d" % (host, port), lambda: None,
		lambda container: env.ctl.ok("listener-add", container, str(port), container + ":3000"), restore)


def range_map(env):
	subnet = env.value("L2_SUBNET")
	first, last = (int(a.rpartition(".")[2]) for a in env.value("DISCOVERY_PORT_MAP_RANGE").split("-"))
	base = int(env.value("DISCOVERY_PORT_MAP_PORT_BASE"))
	return {"%s.%d:3000" % (subnet, o): env.address(base + o) for o in range(first, last + 1)}, subnet, first, last, base


def row_range_routes(env):
	"""A port-map pre-filled from a rule for every address of a range routes every node at its listener."""
	port_map, subnet, first, last, base = range_map(env)
	nodes = env.topology()
	outside = [n["address"] for n in nodes if n["std"] not in port_map]
	check("nodes-in-range", not outside, outside=outside, range="%s.%d-%d" % (subnet, first, last))
	with env.driver("l2-range", port_map=port_map, on_miss="strict") as drv:
		snapshot = l5.await_routing(drv, "range", [n["node"] for n in nodes], ROUTING_WAIT_MS)
		assert_nodes_at(env, "range", snapshot, {n["node"]: (env.endpoint, base + int(n["address"].rpartition(".")[2]))
			for n in nodes})
		l5.serve(drv, "range")


def row_new_node_in_range(env):
	"""A node started on a free address of the range joins through the pre-filled port-map within two tend
	intervals of its listener serving, with no client change."""
	port_map, subnet, first, last, base = range_map(env)
	used = {int(n["address"].rpartition(".")[2]) for n in env.topology()}
	octet = [o for o in range(first, last + 1) if o not in used][0]
	listener = "range-%s.%d" % (subnet, octet)

	new_node_row(env, "new-node-in-range", {"port_map": port_map, "on_miss": "strict"},
		("--address", "%s.%d" % (subnet, octet)), env.address(base + octet), lambda: env.ctl.ok("listener", listener, "off"),
		lambda container: env.ctl.ok("listener", listener, "on"), lambda container: env.ctl.ok("listener", listener, "on"))


def row_outside_range_strict(env):
	"""A node started on an address the pre-filled port-map does not cover is never held under on_miss strict,
	and the other nodes stay held."""
	port_map, subnet, first, last, base = range_map(env)
	nodes = env.topology()
	outside = "%s.%d" % (subnet, int(env.value("L2_OUTSIDE_OCTET")))
	state = {"container": None}
	try:
		with env.driver("l2-outside-range", port_map=port_map, on_miss="strict") as drv:
			l5.await_routing(drv, "outside-range-before", [n["node"] for n in nodes], ROUTING_WAIT_MS)
			started = env.ctl.ok("start-node", "--address", outside).first("L2-STARTED")
			state["container"] = started["container"]
			node = env.ctl.ok("wait-advertised", started["container"], "--timeout", str(NODE_WAIT_S),
				bound_ms=NODE_WAIT_S * 1000 + CTL_TIMEOUT_MS).first("L2-ADVERTISED")["node"]
			end = assert_never_held(drv, "outside-range", node, MISS_WINDOW_MS)
			check("others-held", sorted(names(end)) == sorted(n["node"] for n in nodes), nodes=describe(end["nodes"]),
				expected=sorted(n["node"] for n in nodes))
	finally:
		if state["container"]:
			env.outcome.guard(env.ctl.ok, "stop-node", state["container"])
			env.outcome.guard(env.wait_cluster)


def row_unresolved_shared_name(env):
	"""The shared alternate name does not resolve where the servers run, and every node advertises it as
	configured."""
	host = env.value("L2_ACCESS_HOSTNAME")
	lookups = env.ctl.ok("lookup", host).tagged("L2-LOOKUP")
	resolving = [line["container"] for line in lookups if line["resolves"] != "false"]
	check("unresolved-in-servers", lookups and not resolving, hostname=host, resolvesIn=resolving)
	nodes = env.topology()
	wrong = [n["container"] + "=" + n["alt"] for n in nodes if n["alt"] != env.address(n["port"], host)]
	check("advertised-verbatim", not wrong, wrong=wrong, hostname=host)


def row_source_ports(env):
	"""More concurrent commands than Toxiproxy has source ports for: every failed attempt fails within the
	timeout, and at low load every node is held and served again."""
	first, last = env.value("DISCOVERY_PROXY_PORT_RANGE").split()
	reading = env.ctl.ok("proxy-port-range").first("L2-PORT-RANGE")
	check("proxy-port-range", (reading["first"], reading["last"]) == (first, last), first=reading["first"],
		last=reading["last"], expected="%s-%s" % (first, last))
	nodes = env.topology()
	with env.driver("l2-source-ports", port_map=port_map_by_name(env, nodes), workers=HEAVY_WORKERS) as drv:
		since = drv.mark()
		failures, end = failures_after(drv, since, FAULT_HOLD_MS, "source-ports")
		assert_bounded_failures("source-ports", failures, end, since[1])
	await_proxy_ports_free(env, "low-load")
	with env.driver("l2-source-ports-low", port_map=port_map_by_name(env, nodes), workers=LIGHT_WORKERS) as drv:
		snapshot = l5.await_routing(drv, "low-load", [n["node"] for n in nodes], RECOVERY_MS)
		assert_nodes_at(env, "low-load", snapshot, {n["node"]: (env.endpoint, n["port"]) for n in nodes})
		l5.await_quiet(drv, "low-load", EXHAUSTED_QUIET_WAIT_MS)
		l5.serve(drv, "low-load")


def await_proxy_ports_free(env, step):
	"""Wait until Toxiproxy holds no socket on its source ports: the exhausted run's sockets stay in
	TIME_WAIT for a minute after it closes them, and until then a new client cannot connect at all."""
	bound_ms = PORTS_FREE_WAIT_MS
	start = time.monotonic()
	while True:
		reading = env.ctl.ok("proxy-ports").first("L2-PROXY-PORTS")
		waited = int((time.monotonic() - start) * 1000)
		if reading["used"] == "0" or waited >= bound_ms:
			break
		time.sleep(PORTS_POLL_S)
	check("proxy-ports-free", reading["used"] == "0", step=step, used=reading["used"], timeWait=reading["timeWait"],
		waitedMs=waited, boundMs=bound_ms)


def row_large_record(env):
	"""Records larger than the client network's MTU are written and read back, each command within the
	timeout."""
	mtu = env.value("DISCOVERY_CLIENT_NETWORK_MTU")
	reading = env.ctl.ok("client-mtu").first("L2-MTU")
	check("client-mtu", reading["mtu"] == mtu, mtu=reading["mtu"], expected=mtu)
	nodes = env.topology()
	with env.driver("l2-large", port_map=port_map_by_name(env, nodes)) as drv:
		l5.await_routing(drv, "large", [n["node"] for n in nodes], ROUTING_WAIT_MS)
		reply, waited = drv.request("large mtu %d %d" % (LARGE_BYTES, LARGE_KEYS), "large", LARGE_WAIT_MS,
			lambda m: m["step"] == "mtu")
		fields = {k: reply[k] for k in MESSAGE_FIELDS["large"] if k != "step"} if reply else {}
		check("large", reply is not None and reply["bytes"] == LARGE_BYTES and reply["keys"] == LARGE_KEYS and
			reply["written"] == LARGE_KEYS and reply["read"] == LARGE_KEYS and reply["failures"] == 0 and
			reply["mismatched"] == 0, expectedBytes=LARGE_BYTES, expectedKeys=LARGE_KEYS, **fields, waitedMs=waited,
			boundMs=LARGE_WAIT_MS)
		check("large-ms", reply["maxMs"] <= TIMEOUT_MS, maxMs=reply["maxMs"], boundMs=TIMEOUT_MS)


class Row:
	def __init__(self, name, run, contract=1, fault=False, applies=lambda env: None):
		self.name = name
		self.run = run
		self.contract = contract
		self.fault = fault
		self.applies = applies
		self.doc = " ".join(run.__doc__.split())


ROUTE = Row("client-has-no-route-to-node-addresses", row_no_route, contract=0)
BY_NAME = Row("port-map-by-node-name-routes-every-node", row_port_map_by_name)

ROWS = {
	"default": [ROUTE, BY_NAME,
		Row("port-map-by-advertised-address-routes-every-node", row_port_map_by_address, contract=5),
		Row("strict-miss-never-holds-the-unmapped-node", row_strict_miss),
		Row("pass-through-miss-without-route-client-not-created", row_unmapped_peers),
		Row("empty-alternate-list-client-not-created", row_empty_alternate),
		Row("closed-listener-node-returns-with-bounded-latency", row_listener_closed, fault=True),
		Row("silent-drop-attempts-time-out-within-bound-and-recover", row_silent_drop, contract=3, fault=True),
		Row("delayed-replies-below-timeout-serve-and-recover", row_latency, fault=True),
		Row("all-listeners-closed-client-reseeds-within-bound", row_reseed, fault=True)],
	"reachable": [ROUTE, BY_NAME,
		Row("pass-through-miss-holds-reachable-node-at-its-address", row_reachable_passes_through),
		Row("strict-miss-never-holds-the-reachable-node", row_reachable_strict),
		Row("alternate-list-holds-only-nodes-with-alternate", row_alternate_with_alternate),
		Row("seed-without-alternate-client-not-created", row_seed_without_alternate)],
	"pass-through-names": [ROUTE, BY_NAME,
		Row("distinct-alternate-names-route-every-node", row_distinct_names)],
	"pass-through-ports": [ROUTE, BY_NAME,
		Row("shared-alternate-name-routes-every-node", row_shared_name),
		Row("new-node-on-shared-name-usable-within-two-tend-intervals", row_new_node_shared_name),
		Row("shared-name-unresolved-in-servers-advertised-verbatim", row_unresolved_shared_name, contract=0,
			applies=lambda env: None if env.values.get("L2_UNRESOLVED") == "1" else
			"the topology maps the shared name in the servers; run with --unresolved-hostname")],
	"port-map-range": [ROUTE, BY_NAME,
		Row("range-port-map-routes-every-node", row_range_routes, contract=5),
		Row("new-node-in-range-usable-within-two-tend-intervals", row_new_node_in_range, contract=5),
		Row("strict-never-holds-node-outside-range", row_outside_range_strict, contract=5)],
	"port-range": [ROUTE, BY_NAME,
		Row("source-port-exhaustion-fails-within-bound-and-recovers", row_source_ports, contract=3, fault=True)],
	"low-mtu": [ROUTE, BY_NAME,
		Row("large-records-over-low-mtu-within-timeout", row_large_record, contract=5)],
	"idle-reset": [ROUTE, BY_NAME,
		Row("reset-peer-attempts-fail-within-bound-and-recover", row_reset_peer, contract=3, fault=True)],
}


def plan(profile):
	for row in ROWS[profile]:
		needs = "no driver" if row.contract == 0 else "contract %d" % row.contract
		say("L2-PLAN-ROW profile=%s row=%s needs=%s fault=%s: %s" % (profile, row.name, needs, fmt(row.fault), row.doc))


def run_row(env, row):
	env.outcome = l5.Outcome()
	reason = row.applies(env) or ""
	result = "skip" if reason else None
	if result is None and row.contract > 1:
		try:
			contract = env.driver_contract()
		except (RowFailure, ContractError) as e:
			env.outcome.record(e)
			result = "fail"
			reason = "no contract version: %s" % e
		else:
			if contract < row.contract:
				result = "skip"
				reason = "the driver speaks contract %s; this row needs %d" % (contract, row.contract)
	if result is None:
		emit("L2-ROW-START", profile=env.profile, row=row.name)
		try:
			row.run(env)
		except (RowFailure, ContractError) as e:
			env.outcome.record(e)
		result = "pass" if env.outcome.status == 0 else "fail"
	if result == "fail" and not reason:
		reason = "see the L2-FAILED lines above"
	say("L2-ROW profile=%s row=%s result=%s%s" % (env.profile, row.name, result, " reason=" + reason if reason else ""))
	return result, env.outcome.status


def main(argv=None):
	parser = argparse.ArgumentParser(description="Run the L2 rows of one profile through a client's driver.")
	parser.add_argument("--profile", required=True, choices=PROFILES)
	parser.add_argument("--driver", help="command that starts the client's driver on the client network")
	parser.add_argument("--rows", help="comma-separated rows of the profile to run (default: all)")
	parser.add_argument("--no-fault", action="store_true", help="control run: fault rows run without their fault")
	parser.add_argument("--plan", action="store_true", help="list the profile's rows and exit")
	try:
		args = parser.parse_args(argv)
	except SystemExit as e:
		return 2 if e.code else 0
	if args.plan:
		plan(args.profile)
		return 0
	rows = ROWS[args.profile]
	if args.rows:
		wanted = args.rows.split(",")
		unknown = [r for r in wanted if r not in [row.name for profile in ROWS.values() for row in profile]]
		if unknown:
			say("ERROR: unknown rows: %s" % ",".join(unknown))
			return 2
		rows = [row for row in rows if row.name in wanted]
	if not args.driver:
		say("ERROR: --driver is required")
		return 2
	try:
		env = Env(args.profile, args.driver, args.no_fault)
	except UsageError as e:
		say("ERROR: %s" % e)
		return 2
	status = 0
	results = []
	for row in rows:
		result, row_status = run_row(env, row)
		results.append(result)
		status = max(status, row_status, 1 if result == "fail" else 0)
	emit("L2-PROFILE-RESULT", profile=args.profile, passed=results.count("pass"), failed=results.count("fail"),
		skipped=results.count("skip"), exit=status)
	return status


if __name__ == "__main__":
	sys.exit(main())
