#!/usr/bin/env python3
#
# One L5 row (W-1, W-2, W-3 or W-5) through a client's driver (DRIVER.md, contract version 1). run.sh
# runs it on the runner from the client checkout with the row environment of the README. It starts
# the driver with bash -c from the current directory, sends it commands, reads its JSON lines and
# makes every pass/fail call; it drives the cluster through $L5_CTL. Every interval it asserts on is
# a difference of driver t values or a field the driver measured. Each check prints an L5-CHECK line
# with its measurement, bound and outcome. The row leaves the cluster at the size it found it.
#
# Exit status: 0 every assertion holds, 1 one failed, 2 bad usage or environment, 3 a driver broke
# the contract.
#
# Usage: rows.py --row W-1|W-2|W-3|W-5 --driver "<L5_DRIVER>"

import argparse
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time

CONTRACT = 1
NAMESPACE = "test"
USER = "admin"
CLUSTER_SIZE = 3
PARTITIONS = 4096
TEND_INTERVAL_MS = 250
TIMEOUT_MS = 1000
READY_WAIT_MS = 60000
ROUTING_WAIT_MS = 60000
QUIET_TEND_INTERVALS = 5
QUIET_WAIT_MS = 60000
SERVE_WAIT_MS = 120000
REPLY_WAIT_MS = 10000
STALL_MARGIN_MS = 30000
QUIT_WAIT_MS = 60000
CTL_TIMEOUT_MS = 300000
CTL_MARGIN_MS = 120000
READY_TIMEOUT_S = 1200
HEALTHY_TIMEOUT_S = 600

SPOT_WAIT_MS = 25 * 60000
POD_REPLACED_MS = 20 * 60000
ROLLOUT_WAIT_MS = 95 * 60000
CLIENT_RECOVERY_MS = 60000

PENDING_WAIT_MS = 300000
PENDING_POLL_MS = 5000
ADD_NODE_WAIT_MS = 35 * 60000
SET_NODES_WAIT_MS = 35 * 60000
ADVERTISED_TIMEOUT_S = 900
LISTENER_WAIT_MS = 20 * 60000
SYNC_LISTENERS_WAIT_MS = 20 * 60000
PROBE_TIMEOUT_MS = 50
USABLE_TEND_INTERVALS = 2

OUTAGE_DURATION = "PT5M"
OUTAGE_DURATION_MS = 5 * 60000
OWNERSHIP_GAP_MS = 30000
COMMAND_BOUND_MS = OWNERSHIP_GAP_MS + 3 * TIMEOUT_MS
RUNNING_WAIT_MS = 120000
OUTAGE_WAIT_MS = OUTAGE_DURATION_MS + 10 * 60000
EXPERIMENT_WAIT_MS = 25 * 60000
STATUS_POLL_MS = 15000
RECOVERY_MS = 120000

REQUIRED_ENV = ("L5_CONTROL", "L5_ENDPOINT_NAME", "L5_SEED_PORT", "L5_CTL", "L5_KUBECONFIG",
	"AEROSPIKE_ADMIN_PASSWORD", "L5_NODEGROUPS", "L5_RUNNER_AZ", "L5_STACK", "AWS_REGION")

MESSAGE_FIELDS = {
	"ready": ("contract", "epochMs", "client", "clientVersion", "shape"),
	"snapshot": ("reason", "nodes", "succeeded", "failed", "maxLatencyMs", "lastError"),
	"log": ("level", "message"),
	"served": ("step", "partitions", "written", "read", "mismatched", "failures", "firstFailure", "elapsedMs"),
	"reset": (),
	"watch": ("address", "node", "servingAt", "lastClosedAt", "usableAt", "usableMs", "ownershipChecked"),
	"error": ("message",),
}
NODE_FIELDS = ("name", "host", "port", "address", "active", "reachable")
UNOWNED_FIELDS = ("owners", "longestUnownedMs")
SNAPSHOT_NUMBERS = ("succeeded", "failed", "maxLatencyMs", "unowned", "longestUnownedMs", "clientErrors",
	"clientTimeouts")
QUIET_COUNTERS = ("failed", "clientErrors", "clientTimeouts")


class RowFailure(Exception):
	pass


class ContractError(Exception):
	pass


class UsageError(Exception):
	pass


_output = threading.Lock()


def say(text):
	with _output:
		print(text, flush=True)


def fmt(value):
	if isinstance(value, bool):
		return "true" if value else "false"
	if value is None:
		return "null"
	if isinstance(value, (list, dict)):
		return json.dumps(value, separators=(",", ":"), sort_keys=True)
	return str(value)


def emit(tag, **fields):
	say(" ".join([tag] + ["%s=%s" % (k, fmt(v)) for k, v in fields.items()]))


def check(name, ok, quiet=False, **fields):
	if not ok or not quiet:
		emit("L5-CHECK", check=name, **fields, outcome="pass" if ok else "fail")
	if not ok:
		raise RowFailure(name + ": " + " ".join("%s=%s" % (k, fmt(v)) for k, v in fields.items()))


def is_number(value):
	return isinstance(value, (int, float)) and not isinstance(value, bool)


def contract_problem(msg):
	if not isinstance(msg, dict):
		return "not a JSON object"
	kind = msg.get("type")
	if kind not in MESSAGE_FIELDS:
		return "unknown type %r" % (kind,)
	if not is_number(msg.get("t")):
		return "%s without a numeric t" % kind
	missing = [f for f in MESSAGE_FIELDS[kind] if f not in msg]
	if kind == "snapshot":
		if not isinstance(msg.get("nodes"), list):
			return "snapshot nodes is not a list"
		for node in msg["nodes"]:
			if not isinstance(node, dict):
				return "snapshot node is not an object"
			missing += ["nodes[].%s" % f for f in NODE_FIELDS if f not in node]
			if "port" in node and not is_number(node["port"]):
				return "snapshot node port is not a number"
		if "unowned" in msg:
			missing += [f for f in UNOWNED_FIELDS if f not in msg]
		bad = [f for f in SNAPSHOT_NUMBERS if f in msg and not is_number(msg[f])]
		if bad:
			return "snapshot %s not a number" % ",".join(bad)
	if missing:
		return "%s without %s" % (kind, ",".join(missing))
	if kind == "ready" and msg["contract"] != CONTRACT:
		return "ready with contract %r, harness speaks %d" % (msg["contract"], CONTRACT)
	return None


class Line:
	"""One L5-* line: its tag and its key=value fields."""

	def __init__(self, text):
		tokens = text.split()
		self.tag = tokens[0]
		self.fields = dict(t.split("=", 1) for t in tokens[1:] if "=" in t)
		self.text = text

	def __getitem__(self, key):
		if key not in self.fields:
			raise RowFailure("no %s in %s" % (key, self.text))
		return self.fields[key]


class Result:
	def __init__(self, command, exit_status, lines, stderr):
		self.command = command
		self.exit = exit_status
		self.lines = lines
		self.stderr = stderr

	def tagged(self, tag):
		return [line for line in self.lines if line.tag == tag]

	def first(self, tag):
		lines = self.tagged(tag)
		check("%s-output" % self.command.split()[1], bool(lines), quiet=True, command=self.command, expected=tag)
		return lines[0]


class Ctl:
	"""Runs l5ctl with a bound per call and parses the L5-* lines it prints."""

	def __init__(self, path):
		self.path = path

	def __call__(self, bound_ms, *args):
		command = "l5ctl " + " ".join(args)
		start = time.monotonic()
		proc = subprocess.Popen([self.path] + list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
			stderr=subprocess.PIPE, universal_newlines=True, start_new_session=True)
		try:
			out, err = proc.communicate(timeout=bound_ms / 1000)
			status = proc.returncode
		except subprocess.TimeoutExpired:
			kill_group(proc)
			out, err = proc.communicate()
			status = None
		elapsed_ms = int((time.monotonic() - start) * 1000)
		lines = []
		for text in out.splitlines():
			if text.startswith("L5-"):
				say(text)
				lines.append(Line(text))
		err = err.strip()
		emit("L5-CTL", command=command, exit=status, elapsedMs=elapsed_ms, boundMs=bound_ms)
		check("l5ctl-bound", status is not None, quiet=True, command=command, elapsedMs=elapsed_ms, boundMs=bound_ms)
		if status != 0 and err:
			say("L5-CTL-STDERR " + err.replace("\n", " "))
		return Result(command, status, lines, err)

	def require(self, result, name=None):
		check(name or result.command.split()[1], result.exit == 0, command=result.command, exit=result.exit,
			stderr=result.stderr.replace("\n", " ")[-500:])
		return result

	def ok(self, bound_ms, *args, name=None):
		return self.require(self(bound_ms, *args), name)


def kill_group(proc):
	try:
		os.killpg(proc.pid, signal.SIGKILL)
	except OSError:
		pass


class Driver:
	"""A running driver: its messages in order, and the commands sent to it."""

	def __init__(self, command, flags, shape, outcome):
		self.shape = shape
		self.outcome = outcome
		self.command = command + " " + " ".join(shlex.quote(f) for f in flags)
		self.messages = []
		self.cond = threading.Condition()
		self.violation = None
		self.exited = False
		self.status = None
		self.quit_sent = False
		self.ready = None
		say("L5-DRIVER-START command=" + self.command)
		self.proc = subprocess.Popen(["bash", "-c", self.command], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
			stderr=subprocess.PIPE, universal_newlines=True, bufsize=1, start_new_session=True)
		self.threads = [threading.Thread(target=self._read, daemon=True),
			threading.Thread(target=self._copy_stderr, daemon=True)]
		for thread in self.threads:
			thread.start()

	def __enter__(self):
		try:
			self.await_ready()
		except BaseException:
			self._close_quietly()
			raise
		return self

	def __exit__(self, kind, error, trace):
		if kind is None:
			self.close()
		else:
			self._close_quietly()
		return False

	def _close_quietly(self):
		try:
			self.close()
		except (ContractError, RowFailure) as e:
			self.outcome.record(e)

	def _read(self):
		for raw in self.proc.stdout:
			raw = raw.rstrip("\n")
			try:
				msg = json.loads(raw)
				problem = contract_problem(msg)
			except ValueError:
				msg, problem = None, "non-JSON line on stdout: " + raw[:300]
			with self.cond:
				if problem is None and self.messages and msg["t"] < self.messages[-1]["t"]:
					problem = "t went back from %s to %s" % (self.messages[-1]["t"], msg["t"])
				if problem is None and msg["type"] == "ready" and msg["shape"] != self.shape:
					problem = "ready for shape %r, started with %r" % (msg["shape"], self.shape)
				if problem is None:
					msg["_index"] = len(self.messages)
					self.messages.append(msg)
				elif self.violation is None:
					self.violation = problem
				self.cond.notify_all()
			if problem is None and msg["type"] == "log":
				emit("CLIENT-LOG", t=msg["t"], level=msg["level"], message=msg["message"])
			elif problem is None and msg["type"] == "snapshot" and msg["reason"] == "change":
				emit("L5-SNAPSHOT", t=msg["t"], nodes=describe(msg["nodes"]), unowned=msg.get("unowned"),
					failed=msg["failed"], succeeded=msg["succeeded"])
			elif problem is None and msg["type"] == "error":
				emit("L5-DRIVER-ERROR", t=msg["t"], message=msg["message"])
		status = self.proc.wait()
		with self.cond:
			self.exited = True
			self.status = status
			self.cond.notify_all()

	def _copy_stderr(self):
		for raw in self.proc.stderr:
			say("DRIVER-STDERR " + raw.rstrip("\n"))

	def _raise_if_broken(self):
		if self.violation is not None:
			raise ContractError("driver broke the contract: " + self.violation)
		if self.exited and not self.quit_sent:
			if self.ready is None and self.status == 3:
				raise RowFailure("client-created: driver exited 3, the client could not be created")
			raise ContractError("driver exited with status %s before quit" % self.status)

	def mark(self):
		"""Where the next wait starts: the next message, and the latest t."""
		with self.cond:
			self._raise_if_broken()
			return len(self.messages), (self.messages[-1]["t"] if self.messages else None)

	def latest(self, kind="snapshot"):
		with self.cond:
			for msg in reversed(self.messages):
				if msg["type"] == kind:
					return msg
		return None

	def between(self, first, last, kind="snapshot"):
		with self.cond:
			return [m for m in self.messages[first:last + 1] if m["type"] == kind]

	def wait(self, pred, bound_ms, since=None):
		"""Pass each message from since (default: now) to pred until it returns a value. Returns the value
		and the driver-t milliseconds from the start, or None once a message's t is past bound_ms. Fails on a
		contract violation, or when the driver's t does not get past the bound within bound_ms plus
		STALL_MARGIN_MS."""
		index, start_t = since if since is not None else self.mark()
		deadline = time.monotonic() + (bound_ms + STALL_MARGIN_MS) / 1000
		while True:
			with self.cond:
				while index >= len(self.messages):
					self._raise_if_broken()
					remaining = deadline - time.monotonic()
					if remaining <= 0:
						raise ContractError("driver output stopped: t did not pass %s + %d ms within %d ms" %
							(start_t, bound_ms, bound_ms + STALL_MARGIN_MS))
					self.cond.wait(remaining)
				batch = self.messages[index:]
			for msg in batch:
				index += 1
				if start_t is None:
					start_t = msg["t"]
				if msg["t"] - start_t > bound_ms:
					return None, msg["t"] - start_t
				value = pred(msg)
				if value is not None:
					return value, msg["t"] - start_t

	def await_ready(self):
		ready, waited = self.wait(lambda m: m if m["type"] == "ready" else None, READY_WAIT_MS, since=(0, None))
		if ready is None:
			raise ContractError("no ready within %d ms of the driver's first t" % READY_WAIT_MS)
		self.ready = ready
		emit("L5-DRIVER-READY", t=ready["t"], client=ready["client"], clientVersion=ready["clientVersion"],
			shape=ready["shape"], contract=ready["contract"])

	def send(self, command):
		self._raise_if_broken()
		emit("L5-DRIVER-COMMAND", t=self.mark()[1], command=command)
		try:
			self.proc.stdin.write(command + "\n")
			self.proc.stdin.flush()
		except OSError as e:
			raise ContractError("driver stdin closed before quit: %s" % e)

	def reply(self, kind, bound_ms, since, match=lambda m: True):
		def pred(msg):
			if msg["type"] == "error":
				raise ContractError("driver answered with error: %s" % msg["message"])
			return msg if msg["type"] == kind and match(msg) else None
		return self.wait(pred, bound_ms, since)

	def request(self, command, kind, bound_ms, match=lambda m: True):
		since = self.mark()
		self.send(command)
		return self.reply(kind, bound_ms, since, match)

	def close(self):
		"""Send quit, kill the driver if it has not exited within QUIT_WAIT_MS, and fail on a non-zero exit."""
		with self.cond:
			before_quit = self.exited
			self.quit_sent = True
		if not before_quit:
			try:
				self.proc.stdin.write("quit\n")
				self.proc.stdin.close()
			except OSError:
				pass
			try:
				self.proc.wait(QUIT_WAIT_MS / 1000)
			except subprocess.TimeoutExpired:
				emit("L5-DRIVER-KILLED", reason="no exit within %d ms of quit" % QUIT_WAIT_MS)
				kill_group(self.proc)
				self.proc.wait()
		kill_group(self.proc)
		for thread in self.threads:
			thread.join(10)
		status = self.proc.returncode
		emit("L5-DRIVER-EXIT", status=status, afterQuit=not before_quit)
		if self.violation is not None:
			raise ContractError("driver broke the contract: " + self.violation)
		if before_quit and self.ready is None and status == 3:
			raise RowFailure("client-created: driver exited 3, the client could not be created")
		if status != 0:
			raise ContractError("driver exited with status %s %s" % (status, "before quit" if before_quit else "after quit"))


class Outcome:
	def __init__(self):
		self.status = 0

	def record(self, error):
		status = 3 if isinstance(error, ContractError) else 1
		self.status = max(self.status, status)
		emit("L5-FAILED", kind="contract" if status == 3 else "assertion", reason=str(error))

	def guard(self, step, *args):
		try:
			step(*args)
		except (RowFailure, ContractError) as e:
			self.record(e)


class Env:
	def __init__(self, row, driver):
		missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
		if missing:
			emit("L5-ENVIRONMENT", missing=",".join(missing))
			raise UsageError("row environment missing " + ", ".join(missing))
		self.row = row
		self.driver_command = driver
		self.control = os.environ["L5_CONTROL"]
		self.endpoint = os.environ["L5_ENDPOINT_NAME"]
		self.runner_az = os.environ["L5_RUNNER_AZ"]
		if self.control not in ("none", "resolvable"):
			raise UsageError("L5_CONTROL must be none or resolvable, got " + self.control)
		seed_port = os.environ["L5_SEED_PORT"]
		if not seed_port.isdigit() or not 0 < int(seed_port) < 65536:
			raise UsageError("L5_SEED_PORT is not a port: " + seed_port)
		self.seed_port = int(seed_port)
		if not os.access(os.environ["L5_CTL"], os.X_OK):
			raise UsageError("L5_CTL %s is not executable" % os.environ["L5_CTL"])
		self.ctl = Ctl(os.environ["L5_CTL"])
		self.outcome = Outcome()
		emit("L5-ENVIRONMENT", row=row, control=self.control, endpoint=self.endpoint, seedPort=self.seed_port,
			runnerAz=self.runner_az)

	def address(self, port):
		return "%s:%d" % (self.endpoint, port)

	def wait_ready(self):
		self.ctl.ok(READY_TIMEOUT_S * 1000 + CTL_MARGIN_MS, "wait-ready", "--timeout", str(READY_TIMEOUT_S))

	def wait_healthy(self):
		self.ctl.ok(HEALTHY_TIMEOUT_S * 1000 + CTL_MARGIN_MS, "wait-healthy", "--timeout", str(HEALTHY_TIMEOUT_S))

	def pods(self):
		"""The AerospikeCluster's spec size, and each pod with its zone, node name and NodePort."""
		result = self.ctl.ok(CTL_TIMEOUT_MS, "pods")
		size = int(result.first("L5-CLUSTER")["size"])
		pods = [{"pod": line["pod"], "phase": line["phase"], "zone": line["zone"], "node": line["node"],
			"nodePort": int(line["nodePort"] or 0)} for line in result.tagged("L5-POD")]
		ports = [p["nodePort"] for p in pods]
		bad = [p["pod"] for p in pods if not p["node"] or p["nodePort"] <= 0]
		check("pod-endpoints", not bad and len(set(ports)) == len(ports), withoutNodeOrNodePort=bad, nodePorts=ports)
		return size, pods

	def start(self):
		self.wait_ready()
		self.wait_healthy()
		size, pods = self.pods()
		check("cluster-size", size == CLUSTER_SIZE and len(pods) == size, size=size, pods=len(pods),
			expected=CLUSTER_SIZE)
		return size

	def restore_size(self, size):
		now, pods = self.pods()
		if now != size or len(pods) != size:
			emit("L5-RESTORE", size=now, pods=len(pods), to=size)
			self.ctl.ok(CTL_TIMEOUT_MS, "scale", str(size))
			self.wait_ready()

	def driver(self, set_name, shape="pass-through", port_map=None):
		flags = ["--seed", self.address(self.seed_port), "--shape", shape]
		if port_map is not None:
			flags += ["--port-map", ",".join("%s=%s" % kv for kv in sorted(port_map.items())), "--on-miss", "strict"]
		flags += ["--user", USER, "--tend-interval", str(TEND_INTERVAL_MS), "--timeout", str(TIMEOUT_MS),
			"--namespace", NAMESPACE, "--set", set_name]
		return Driver(self.driver_command, flags, shape, self.outcome)


def describe(nodes):
	return ["%s@%s:%s/%s/active=%s/reachable=%s" % (n["name"], n["host"], n["port"], n["address"], fmt(n["active"]),
		fmt(n["reachable"])) for n in nodes]


def names(snapshot):
	return sorted(n["name"] for n in snapshot["nodes"])


def owners(snapshot):
	return sorted(k for k, v in snapshot.get("owners", {}).items() if v)


def routed(snapshot, expected):
	nodes = snapshot["nodes"]
	return (names(snapshot) == sorted(expected) and
		all(n["active"] is True and n["reachable"] is True for n in nodes) and
		snapshot.get("unowned", 0) == 0 and
		("owners" not in snapshot or owners(snapshot) == sorted(expected)))


def resolve(name):
	try:
		return sorted({info[4][0] for info in socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)})
	except OSError:
		return []


def tcp_connects(host, port, timeout_ms):
	try:
		with socket.create_connection((host, port), timeout=timeout_ms / 1000):
			return True
	except OSError:
		return False


def await_routing(drv, step, expected, bound_ms):
	"""A snapshot holding one node per expected name, each active and reachable, with every partition
	owned when the driver reports ownership."""
	snapshot, waited = drv.wait(lambda m: m if m["type"] == "snapshot" and routed(m, expected) else None, bound_ms)
	last = snapshot or drv.latest()
	check("routing", snapshot is not None, step=step, expected=sorted(expected), nodes=describe(last["nodes"]),
		unowned=last.get("unowned"), owners=last.get("owners"), waitedMs=waited, boundMs=bound_ms)
	return snapshot


def assert_nodes_at_endpoint(env, step, snapshot, pods):
	"""Each node at its pod's NodePort, on the endpoint name or an address it resolves to here."""
	resolved = resolve(env.endpoint)
	by_node = {p["node"]: p for p in pods}
	wrong_port, wrong_host = [], []
	for node in snapshot["nodes"]:
		pod = by_node.get(node["name"])
		emit("L5-NODE", step=step, node=node["name"], pod=pod and pod["pod"], host="%s:%s" % (node["host"], node["port"]),
			address=node["address"], expected=pod and env.address(pod["nodePort"]), endpointResolvesTo=resolved)
		if pod is None or node["port"] != pod["nodePort"]:
			wrong_port.append("%s:%s" % (node["name"], node["port"]))
		if node["host"] != env.endpoint and node["host"] not in resolved:
			wrong_host.append("%s:%s" % (node["name"], node["host"]))
	check("node-port", not wrong_port, step=step, wrong=wrong_port,
		expected={p["node"]: p["nodePort"] for p in pods})
	check("node-host", not wrong_host, step=step, wrong=wrong_host, endpoint=env.endpoint, resolved=resolved)


def serve(drv, step):
	served, waited = drv.request("serve " + step, "served", SERVE_WAIT_MS, lambda m: m["step"] == step)
	fields = {k: served[k] for k in ("partitions", "written", "read", "mismatched", "failures", "firstFailure",
		"elapsedMs")} if served else {}
	check("serve", served is not None and served["partitions"] == PARTITIONS and served["written"] == PARTITIONS and
		served["read"] == PARTITIONS and served["failures"] == 0 and served["mismatched"] == 0,
		step=step, expected=PARTITIONS, **fields, waitedMs=waited, boundMs=SERVE_WAIT_MS)


def await_quiet(drv, step, bound_ms):
	"""QUIET_TEND_INTERVALS tend intervals of driver t in which failed, clientErrors and clientTimeouts do
	not change and succeeded grows."""
	window_ms = QUIET_TEND_INTERVALS * TEND_INTERVAL_MS
	base = [None]

	def quiet(msg):
		if msg["type"] != "snapshot":
			return None
		if base[0] is None or [msg.get(k) for k in QUIET_COUNTERS] != [base[0].get(k) for k in QUIET_COUNTERS]:
			base[0] = msg
			return None
		if msg["t"] - base[0]["t"] >= window_ms and msg["succeeded"] > base[0]["succeeded"]:
			return msg
		return None

	end, waited = drv.wait(quiet, bound_ms)
	last = end or drv.latest()
	check("quiet", end is not None, step=step, windowMs=window_ms, boundMs=bound_ms, waitedMs=waited,
		failed=last["failed"], clientErrors=last.get("clientErrors"), clientTimeouts=last.get("clientTimeouts"),
		succeeded=last["succeeded"], lastError=last["lastError"])
	return end


def row_w1(env):
	"""Both address shapes, each with a fresh driver seeded at the endpoint name and the seed NodePort."""
	found = env.start()
	try:
		_, pods = env.pods()
		nodes = [p["node"] for p in pods]
		for shape in ("pass-through", "port-map"):
			port_map = {p["node"]: env.address(p["nodePort"]) for p in pods} if shape == "port-map" else None
			emit("L5-SHAPE", shape=shape, seed=env.address(env.seed_port), pods=[p["pod"] for p in pods],
				portMap=port_map)
			with env.driver("l5-shapes", shape, port_map) as drv:
				snapshot = await_routing(drv, shape, nodes, ROUTING_WAIT_MS)
				assert_nodes_at_endpoint(env, shape, snapshot, pods)
				serve(drv, shape)
		env.ctl.ok(CTL_TIMEOUT_MS, "verify")
		expect = "yes" if env.control == "resolvable" else "no"
		env.ctl.ok(CTL_TIMEOUT_MS, "resolve-check", "--expect-producer", expect)
	finally:
		env.outcome.guard(env.restore_size, found)


def recover(env, drv, step, replaced, bound_ms, *command):
	start_t = drv.mark()[1]
	failed_before = drv.latest()["failed"]
	result = env.ctl.ok(bound_ms, *command, name=step)
	lines = {line["pod"]: line for line in result.tagged("L5-REPLACED")}
	missing = [pod for pod in replaced if pod not in lines or
		lines[pod].fields.get("newInstance") in ("", "-", lines[pod].fields.get("oldInstance"))]
	check("replaced", not missing, step=step, pods=replaced, notReplaced=missing)
	env.wait_ready()
	env.wait_healthy()
	ready_t = drv.mark()[1]
	_, pods = env.pods()
	snapshot = await_routing(drv, step, [p["node"] for p in pods], CLIENT_RECOVERY_MS)
	env.ctl.ok(CTL_TIMEOUT_MS, "verify")
	assert_nodes_at_endpoint(env, step, snapshot, pods)
	end = await_quiet(drv, step, QUIET_WAIT_MS)
	emit("L5-RECOVERY", step=step, clusterReadyMs=ready_t - start_t, clientRoutedMs=snapshot["t"] - start_t,
		quietMs=end["t"] - start_t, failedCommands=end["failed"] - failed_before, succeededCommands=end["succeeded"],
		clientRecoveryBoundMs=CLIENT_RECOVERY_MS, quietBoundMs=QUIET_WAIT_MS, lastError=end["lastError"])


def row_w2(env):
	"""One pass-through driver through a Spot interruption of the first pod's instance, then a rollout of
	every node."""
	found = env.start()
	try:
		_, pods = env.pods()
		with env.driver("l5-replace") as drv:
			await_routing(drv, "start", [p["node"] for p in pods], ROUTING_WAIT_MS)
			victim = pods[0]["pod"]
			recover(env, drv, "spot-interrupt", [victim], SPOT_WAIT_MS + POD_REPLACED_MS, "spot-interrupt", victim,
				"--wait")
			_, pods = env.pods()
			recover(env, drv, "rollout", [p["pod"] for p in pods], ROLLOUT_WAIT_MS, "rollout", "all", "--wait")
	finally:
		env.outcome.guard(env.restore_size, found)


def await_pending(env, drv, existing_pods):
	next_poll = [None]

	def poll(msg):
		if next_poll[0] is not None and msg["t"] < next_poll[0]:
			return None
		next_poll[0] = msg["t"] + PENDING_POLL_MS
		lines = env.ctl.ok(CTL_TIMEOUT_MS, "pending").tagged("L5-PENDING")
		return lines[0] if lines else None

	pending, waited = drv.wait(poll, PENDING_WAIT_MS)
	check("pending", pending is not None, waitedMs=waited, boundMs=PENDING_WAIT_MS, pollMs=PENDING_POLL_MS)
	check("pending-pod", pending["pod"] not in existing_pods and pending["zone"] not in ("", "unknown"),
		pod=pending["pod"], zone=pending["zone"], existing=sorted(existing_pods))
	return pending["pod"], pending["zone"]


def row_w3(env):
	"""A pod Pending for want of a node, then advertised at a NodePort with no listener, then served once
	its listener is added, under one pass-through driver."""
	found = env.start()
	state = {"scaled": False, "zone": None, "nodes": None}
	try:
		_, pods = env.pods()
		with env.driver("l5-scale-out") as drv:
			start = await_routing(drv, "start", [p["node"] for p in pods], ROUTING_WAIT_MS)
			existing = names(start)
			scale_mark = drv.mark()
			failed_at_scale = drv.latest()["failed"]
			state["scaled"] = True
			env.ctl.ok(CTL_TIMEOUT_MS, "scale", str(found + 1))
			pod, zone = await_pending(env, drv, {p["pod"] for p in pods})
			end = await_quiet(drv, "pending", QUIET_WAIT_MS)
			check("pending-failed", end["failed"] == failed_at_scale, pod=pod, failedAtScale=failed_at_scale,
				failed=end["failed"], lastError=end["lastError"])
			changed = [{"t": s["t"], "nodes": names(s)} for s in drv.between(scale_mark[0], end["_index"])
				if names(s) != existing]
			check("pending-nodes", not changed, pod=pod, nodesAtScale=existing, changed=changed[:5])

			added = env.ctl(ADD_NODE_WAIT_MS, "add-node", zone, "--wait")
			if added.tagged("L5-NODES"):
				state["zone"] = zone
				state["nodes"] = int(added.first("L5-NODES")["desired"]) - 1
			env.ctl.require(added)
			added.first("L5-NODES")

			advertised = env.ctl.ok(ADVERTISED_TIMEOUT_S * 1000 + CTL_MARGIN_MS, "wait-advertised", pod, "--timeout",
				str(ADVERTISED_TIMEOUT_S)).first("L5-ADVERTISED")
			node = advertised["node"]
			node_port = int(advertised["nodePort"])
			address = env.address(node_port)
			serving = tcp_connects(env.endpoint, node_port, PROBE_TIMEOUT_MS)
			check("advertised-before-listener", not serving, pod=pod, node=node, address=address, serving=serving,
				probeTimeoutMs=PROBE_TIMEOUT_MS)

			watch_mark = drv.mark()
			before = drv.latest()
			drv.send("watch %s %s" % (address, node))
			window_ms = QUIET_TEND_INTERVALS * TEND_INTERVAL_MS
			window, observed = drv.wait(lambda m: m if m["type"] == "snapshot" and m["t"] - watch_mark[1] >= window_ms
				else None, window_ms + ROUTING_WAIT_MS, since=watch_mark)
			check("unreachable-window", window is not None, observedMs=observed, windowMs=window_ms)
			stuck = sorted({n["name"] for s in drv.between(watch_mark[0], window["_index"]) for n in s["nodes"]
				if n["active"] is True and n["reachable"] is False})
			token = ":%d" % node_port
			warnings = [w for w in drv.between(scale_mark[0], window["_index"], "log") if token in w["message"]]
			emit("L5-UNREACHABLE-PEER", pod=pod, node=node, advertised=address, observedMs=observed, windowMs=window_ms,
				nodes=names(window), heldNewNode=node in names(window), stuckNodes=stuck, warnings=len(warnings),
				failed="%s->%s" % (before["failed"], window["failed"]),
				succeeded="%s->%s" % (before["succeeded"], window["succeeded"]))
			for warning in warnings:
				emit("L5-PEER-WARNING", t=warning["t"], level=warning["level"], message=warning["message"])
			check("unreachable-peer", not stuck, pod=pod, stuckNodes=stuck, windowMs=window_ms)
			check("window-failed", window["failed"] == before["failed"], pod=pod, failedBefore=before["failed"],
				failed=window["failed"], lastError=window["lastError"])
			check("window-succeeded", window["succeeded"] > before["succeeded"], pod=pod,
				succeededBefore=before["succeeded"], succeeded=window["succeeded"])

			sync_t = drv.mark()[1]
			env.ctl.ok(SYNC_LISTENERS_WAIT_MS, "sync-listeners")
			env.wait_healthy()
			reply, waited = drv.reply("watch", LISTENER_WAIT_MS, (watch_mark[0], sync_t))
			check("watch-reply", reply is not None, address=address, node=node, waitedMs=waited,
				boundMs=LISTENER_WAIT_MS)
			emit("L5-NODE-USABLE", pod=pod, node=node, advertised=address, servingAt=reply["servingAt"],
				lastClosedAt=reply["lastClosedAt"], usableAt=reply["usableAt"], usableMs=reply["usableMs"],
				ownershipChecked=reply["ownershipChecked"], tendInterval=TEND_INTERVAL_MS)
			check("watch-usable", reply["usableAt"] is not None and reply["usableMs"] is not None, node=node,
				servingAt=reply["servingAt"], usableAt=reply["usableAt"])
			budget_ms = USABLE_TEND_INTERVALS * TEND_INTERVAL_MS
			check("usable-ms", reply["usableMs"] <= budget_ms, node=node, usableMs=reply["usableMs"], boundMs=budget_ms,
				ownershipChecked=reply["ownershipChecked"])

			await_routing(drv, "served", existing + [node], ROUTING_WAIT_MS)
			serve(drv, "served")
	finally:
		restore_w3(env, found, state)


def restore_w3(env, found, state):
	"""Scale back, set the node group back to its starting size and remove the added listener."""
	def scale_back():
		env.ctl.ok(CTL_TIMEOUT_MS, "scale", str(found))
		env.wait_ready()

	def nodes_back():
		env.ctl.ok(SET_NODES_WAIT_MS, "set-nodes", state["zone"], str(state["nodes"]), "--wait")
		env.wait_ready()

	if state["scaled"]:
		env.outcome.guard(scale_back)
	if state["zone"] is not None:
		env.outcome.guard(nodes_back)
	if state["scaled"]:
		env.outcome.guard(env.ctl.ok, SYNC_LISTENERS_WAIT_MS, "sync-listeners", "--prune")
	env.outcome.guard(env.restore_size, found)


def log_resolved(env, when, snapshot):
	emit("L5-RESOLVED", when=when, t=snapshot["t"], name=env.endpoint, addresses=resolve(env.endpoint) or "unresolved",
		clientHosts=["%s=%s:%s/%s" % (n["name"], n["host"], n["port"], n["address"]) for n in snapshot["nodes"]])


def row_w5(env):
	"""Five minutes without traffic in or out of the zone of the endpoint address most of the driver's
	nodes use, under one pass-through driver."""
	found = env.start()
	state = {"experiment": None, "ended": False}
	try:
		_, pods = env.pods()
		with env.driver("l5-az-outage") as drv:
			start = await_routing(drv, "start", [p["node"] for p in pods], ROUTING_WAIT_MS)
			log_resolved(env, "before", start)
			by_address = {}
			for node in start["nodes"]:
				by_address[node["address"]] = by_address.get(node["address"], 0) + 1
			address = max(sorted(by_address), key=by_address.get)
			zone = env.ctl.ok(CTL_TIMEOUT_MS, "endpoint-zone", address).first("L5-ENDPOINT-ZONE")["zone"]
			rack_zones = sorted({p["zone"] for p in pods})
			check("outage-zone", zone in rack_zones, zone=zone, address=address, nodesByAddress=by_address,
				rackZones=rack_zones, runnerAz=env.runner_az)

			started = env.ctl(CTL_TIMEOUT_MS + RUNNING_WAIT_MS, "az-outage", zone, "--duration", OUTAGE_DURATION,
				"--wait-running")
			fis = [line for line in started.tagged("L5-FIS") if "experiment" in line.fields]
			if fis:
				state["experiment"] = fis[0]["experiment"]
			env.ctl.require(started)
			started.first("L5-FIS")
			experiment = state["experiment"]

			reset, _ = drv.request("reset", "reset", REPLY_WAIT_MS)
			check("reset-reply", reset is not None, boundMs=REPLY_WAIT_MS)
			running_t = reset["t"]
			seen = {"status": "running", "next": running_t + STATUS_POLL_MS, "halfway": False, "unowned": False,
				"longestUnownedMs": 0, "maxLatencyMs": 0, "stretch": None}

			def during(msg):
				if msg["type"] != "snapshot":
					return None
				if "unowned" in msg:
					seen["unowned"] = True
					seen["longestUnownedMs"] = max(seen["longestUnownedMs"], msg["longestUnownedMs"])
					if msg["unowned"] and seen["stretch"] is None:
						seen["stretch"] = msg["t"]
						emit("L5-OUTAGE-UNOWNED", zone=zone, afterRunningMs=msg["t"] - running_t, unowned=msg["unowned"],
							nodes=describe(msg["nodes"]))
					elif not msg["unowned"] and seen["stretch"] is not None:
						emit("L5-OUTAGE-OWNED", zone=zone, afterRunningMs=msg["t"] - running_t,
							unownedMs=msg["t"] - seen["stretch"], owners=msg["owners"])
						seen["stretch"] = None
				seen["maxLatencyMs"] = max(seen["maxLatencyMs"], msg["maxLatencyMs"])
				if not seen["halfway"] and msg["t"] - running_t >= OUTAGE_DURATION_MS // 2:
					seen["halfway"] = True
					log_resolved(env, "during", msg)
				if msg["t"] >= seen["next"]:
					seen["next"] = msg["t"] + STATUS_POLL_MS
					seen["status"] = env.ctl.ok(CTL_TIMEOUT_MS, "experiment", experiment).first("L5-FIS")["status"]
					if seen["status"] != "running":
						return msg
				return None

			end, observed = drv.wait(during, OUTAGE_WAIT_MS, since=(reset["_index"] + 1, running_t))
			last = end or drv.latest()
			emit("L5-OUTAGE", zone=zone, experiment=experiment, status=seen["status"], observedMs=observed,
				durationMs=OUTAGE_DURATION_MS, unownedReported=seen["unowned"], longestUnownedMs=seen["longestUnownedMs"],
				maxLatencyMs=seen["maxLatencyMs"], failed=last["failed"], succeeded=last["succeeded"],
				lastError=last["lastError"])
			check("outage-ended", end is not None, experiment=experiment, status=seen["status"], observedMs=observed,
				boundMs=OUTAGE_WAIT_MS, pollMs=STATUS_POLL_MS)
			check("longest-unowned", not seen["unowned"] or seen["longestUnownedMs"] <= OWNERSHIP_GAP_MS,
				unownedReported=seen["unowned"], longestUnownedMs=seen["longestUnownedMs"], boundMs=OWNERSHIP_GAP_MS)
			check("max-latency", seen["maxLatencyMs"] <= COMMAND_BOUND_MS, maxLatencyMs=seen["maxLatencyMs"],
				boundMs=COMMAND_BOUND_MS)

			env.ctl.ok(EXPERIMENT_WAIT_MS, "experiment", experiment, "--wait", name="experiment-wait")
			state["ended"] = True
			env.wait_ready()
			env.wait_healthy()
			_, pods = env.pods()
			after = await_routing(drv, "after", [p["node"] for p in pods], RECOVERY_MS)
			log_resolved(env, "after", after)
			serve(drv, "after")
	finally:
		restore_w5(env, found, state)


def restore_w5(env, found, state):
	"""Wait for the outage to end and the cluster to be ready."""
	def outage_over():
		env.ctl.ok(EXPERIMENT_WAIT_MS, "experiment", state["experiment"], "--wait", name="experiment-wait")
		env.wait_ready()

	if state["experiment"] is not None and not state["ended"]:
		env.outcome.guard(outage_over)
	env.outcome.guard(env.restore_size, found)


ROWS = {"W-1": row_w1, "W-2": row_w2, "W-3": row_w3, "W-5": row_w5}


def main(argv=None):
	parser = argparse.ArgumentParser(description="Run one L5 row through a client's driver (DRIVER.md).")
	parser.add_argument("--row", required=True, choices=sorted(ROWS))
	parser.add_argument("--driver", required=True, help="the manifest's L5_DRIVER")
	try:
		args = parser.parse_args(argv)
	except SystemExit as e:
		return 2 if e.code else 0
	try:
		env = Env(args.row, args.driver)
	except UsageError as e:
		say("ERROR: %s" % e)
		return 2
	try:
		ROWS[args.row](env)
	except (RowFailure, ContractError) as e:
		env.outcome.record(e)
	emit("L5-ROW-RESULT", row=args.row, exit=env.outcome.status)
	return env.outcome.status


if __name__ == "__main__":
	sys.exit(main())
