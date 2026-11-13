#!/usr/bin/env python3
#
# The L3 rows of one profile through a client's driver (l5-aws/DRIVER.md, contract version 1 to 5).
# run.sh runs it on the host once per profile, after it has built the kind cluster, with L3_CTL naming
# l3ctl and L3_STATE the cluster's state directory. The driver command is run.sh's wrapper, which starts
# the client's driver in a container that resolves the endpoint hostnames to kind nodes. The driver's
# JSON lines are read with the driver code of l5-aws/rows.py; every Kubernetes change and reading goes
# through $L3_CTL. Every interval a row asserts on is a difference of driver t values or a field the
# driver measured. Each check prints an L3-CHECK line with its measurement, bound and outcome, and each
# row ends with an L3-ROW line: pass, fail, or skip with the reason. A row leaves the cluster at the size
# it found it.
#
# Exit status: 0 no row failed, 1 a row failed, 2 bad usage or environment, 3 a driver broke the
# contract.
#
# Usage: rows.py --profile PROFILE --driver "<driver command>" [--rows ROW,...]
#        rows.py --plan --profile PROFILE

import argparse
import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import time

_spec = importlib.util.spec_from_file_location("l5_rows", os.path.join(os.path.dirname(os.path.abspath(__file__)),
	os.pardir, "l5-aws", "rows.py"))
l5 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(l5)
l5.TAG = "L3"

check, emit, say, fmt, describe, names = l5.check, l5.emit, l5.say, l5.fmt, l5.describe, l5.names
RowFailure, ContractError, UsageError = l5.RowFailure, l5.ContractError, l5.UsageError

CONTRACTS = (1, 2, 3, 4, 5)
TLS_CONTRACT = 4

CONFIGURED_IP = "configuredIP"
NETWORK_LOAD_BALANCER = "networkLoadBalancer"
PROFILES = {
	"default": {"access": CONFIGURED_IP, "multiPodPerHost": True, "tls": False},
	"per-node": {"access": CONFIGURED_IP, "multiPodPerHost": False, "tls": False},
	"tls": {"access": CONFIGURED_IP, "multiPodPerHost": True, "tls": True},
	"nlb": {"access": NETWORK_LOAD_BALANCER, "multiPodPerHost": True, "tls": False},
	"nlb-per-pod": {"access": NETWORK_LOAD_BALANCER, "multiPodPerHost": False, "tls": False},
	"nlb-tls": {"access": NETWORK_LOAD_BALANCER, "multiPodPerHost": True, "tls": True},
}
NAMESPACE = l5.NAMESPACE
USER = l5.USER
TEND_INTERVAL_MS = l5.TEND_INTERVAL_MS
TIMEOUT_MS = l5.TIMEOUT_MS
ROUTING_WAIT_MS = 120000
REPLY_WAIT_MS = l5.REPLY_WAIT_MS
CTL_TIMEOUT_MS = 180000
SCALE_TIMEOUT_S = 600
POD_TIMEOUT_S = 600
NODE_TIMEOUT_S = 300
MIN_PODS = 2

USABLE_BOUND_MS = l5.USABLE_TEND_INTERVALS * TEND_INTERVAL_MS
POLL_BOUND_MS = 20
LISTENER_WAIT_MS = 300000
QUIET_WINDOW_MS = l5.QUIET_TEND_INTERVALS * TEND_INTERVAL_MS
ALTERNATE_ACCESS_LABEL = "aerospike.com/configured-alternate-access-address"
NODE_MARK = "l3-kind-mark"
NODE_MARK_VALUE = "set-through-api"


def contract_problem(msg, contract=None):
	return l5.contract_problem(msg, contract, CONTRACTS, l5.MESSAGE_FIELDS)


class Ctl:
	"""Runs l3ctl with a bound per call and parses the L3-* lines it prints."""

	def __init__(self, path):
		self.path = path

	def __call__(self, bound_ms, *args):
		return self.finish(self.start(*args), bound_ms)

	def start(self, *args):
		"""Start l3ctl without waiting for it; finish collects it."""
		proc = subprocess.Popen([self.path] + list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
			stderr=subprocess.PIPE, universal_newlines=True, start_new_session=True)
		return proc, "l3ctl " + " ".join(args), time.monotonic()

	def finish(self, started, bound_ms):
		proc, command, start = started
		remaining = max(0, bound_ms / 1000 - (time.monotonic() - start))
		try:
			out, err = proc.communicate(timeout=remaining)
			status = proc.returncode
		except subprocess.TimeoutExpired:
			l5.kill_group(proc)
			out, err = proc.communicate()
			status = None
		elapsed_ms = int((time.monotonic() - start) * 1000)
		lines = []
		for text in out.splitlines():
			if text.startswith("L3-"):
				say(text)
				lines.append(l5.Line(text))
		err = err.strip()
		emit("L3-CTL", command=command, exit=status, elapsedMs=elapsed_ms, boundMs=bound_ms)
		check("l3ctl-bound", status is not None, quiet=True, command=command, elapsedMs=elapsed_ms, boundMs=bound_ms)
		if status != 0 and err:
			say("L3-CTL-STDERR " + err.replace("\n", " "))
		return l5.Result(command, status, lines, err)

	def require(self, result, name=None):
		check(name or result.command.split()[1], result.exit == 0, command=result.command, exit=result.exit,
			stderr=result.stderr.replace("\n", " ")[-500:])
		return result

	def ok(self, *args, name=None, bound_ms=CTL_TIMEOUT_MS):
		return self.require(self(bound_ms, *args), name or args[0])


def read_values(path):
	values = {}
	with open(path) as f:
		for line in f:
			line = line.rstrip("\n")
			if "=" in line and not line.startswith("#"):
				key, value = line.split("=", 1)
				values[key] = value
	return values


def split_address(address):
	host, _, port = address.rpartition(":")
	return host, int(port) if port.isdigit() else 0


def ordinal(pod):
	return int(pod["pod"].rpartition("-")[2])


class Env:
	def __init__(self, profile, driver):
		missing = [name for name in ("L3_CTL", "L3_STATE") if not os.environ.get(name)]
		if missing:
			emit("L3-ENVIRONMENT", missing=",".join(missing))
			raise UsageError("environment missing " + ", ".join(missing))
		path = os.path.join(os.environ["L3_STATE"], "topology.env")
		if not os.path.isfile(path):
			raise UsageError("no cluster at " + path)
		if not os.access(os.environ["L3_CTL"], os.X_OK):
			raise UsageError("L3_CTL %s is not executable" % os.environ["L3_CTL"])
		self.values = read_values(path)
		if self.values.get("L3_PROFILE") != profile:
			raise UsageError("the cluster is profile %s, not %s" % (self.values.get("L3_PROFILE"), profile))
		self.profile = profile
		self.shape = PROFILES[profile]
		self.driver_command = driver
		self.ctl = Ctl(os.environ["L3_CTL"])
		self.endpoint = self.values["L3_ENDPOINT_HOSTNAME"]
		self.tls_endpoint = self.values.get("L3_TLS_ENDPOINT_HOSTNAME", "")
		self.tls_name = self.values.get("L3_TLS_NAME", "")
		self.tls_ca_file = self.values.get("L3_TLS_CA_FILE", "")
		self.hosts = dict(entry.rsplit(":", 1) for entry in self.values.get("L3_CLIENT_HOSTS", "").split(",") if entry)
		self.contract = None
		self.outcome = None
		emit("L3-ENVIRONMENT", profile=profile, endpoint=self.endpoint, tlsEndpoint=self.tls_endpoint, hosts=self.hosts)

	def cluster(self, size=None):
		"""The AerospikeCluster and each of its pods as l3ctl reads them, every pod Running, Ready and
		answering asinfo."""
		result = self.ctl.ok("pods")
		line = result.first("L3-CLUSTER")
		cluster = dict(line.fields)
		cluster["size"] = int(cluster["size"] or 0)
		pods = []
		for line in result.tagged("L3-POD"):
			pod = dict(line.fields)
			for key in ("nodePort", "tlsNodePort", "hostPort"):
				pod[key] = int(pod.get(key) or 0)
			pods.append(pod)
		pods.sort(key=ordinal)
		size = cluster["size"] if size is None else size
		down = [p["pod"] for p in pods if p["phase"] != "Running" or p["ready"] != "True" or not p["node"]]
		check("pods", not down and len(pods) == size == cluster["size"], pods=["%s=%s@%s/%d" % (p["pod"], p["node"],
			p["worker"], p["nodePort"]) for p in pods], notReady=down, size=cluster["size"], expected=size)
		return cluster, pods

	def nodes(self):
		lines = self.ctl.ok("nodes").tagged("L3-NODE")
		nodes = {}
		for line in lines:
			node = dict(line.fields)
			node["labels"] = json.loads(node["labels"] or "{}")
			nodes[node["name"]] = node
		return nodes

	def scale(self, size):
		self.ctl.ok("scale", str(size), "--timeout", str(SCALE_TIMEOUT_S), bound_ms=SCALE_TIMEOUT_S * 1000 + CTL_TIMEOUT_MS)

	def wait_completed(self):
		self.ctl.ok("wait-completed", "--timeout", str(SCALE_TIMEOUT_S), bound_ms=SCALE_TIMEOUT_S * 1000 + CTL_TIMEOUT_MS)

	def at_least(self, size):
		"""The cluster at no fewer than size pods; returns the size to restore."""
		found, _ = self.cluster()
		if found["size"] < size:
			self.scale(size)
		return found["size"]

	def restore(self, size):
		cluster = dict(self.ctl.ok("pods").first("L3-CLUSTER").fields)
		if int(cluster["size"] or 0) != size:
			self.scale(size)

	def host_matches(self, host, expected):
		return host == expected or host == self.hosts.get(expected)

	@contextlib.contextmanager
	def driver(self, set_name, seed, tls_name=None):
		drv = self.start(set_name, seed, tls_name)
		try:
			with drv:
				self.contract = drv.contract
				yield drv
		finally:
			self.stop_drivers()

	def start(self, set_name, seed, tls_name=None):
		flags = ["--seed", seed, "--shape", "pass-through", "--user", USER, "--tend-interval", str(TEND_INTERVAL_MS),
			"--timeout", str(TIMEOUT_MS), "--namespace", NAMESPACE, "--set", set_name]
		if tls_name is not None:
			flags += ["--tls-name", tls_name, "--tls-ca-file", self.tls_ca_file]
		return l5.Driver(self.driver_command, flags, "pass-through", self.outcome, problem=contract_problem)

	def stop_drivers(self):
		self.ctl(CTL_TIMEOUT_MS, "drivers-stop")

	def driver_contract(self):
		"""The driver's contract version, from a driver started for it when no row has started one yet."""
		if self.contract is None:
			emit("L3-CONTRACT-PROBE", reason="no driver started yet in this profile")
			_, pods = self.cluster()
			with self.driver("l3-contract", pods[0]["clearAlt"]):
				pass
		return self.contract


def advertised(step, pods, field, expected):
	"""Each pod advertises expected[pod] as field, read through asinfo in the pod."""
	wrong = ["%s=%s" % (p["pod"], p[field]) for p in pods if p[field] != expected[p["pod"]]]
	check("advertised-" + field, not wrong, step=step, wrong=wrong, expected=expected)


def assert_nodes_at(env, step, snapshot, expected):
	"""Each held node at its expected host and port. expected maps a node name to (host, port); a host also
	matches the address the driver container resolves it to."""
	wrong = []
	for node in snapshot["nodes"]:
		host, port = expected.get(node["name"], (None, None))
		emit("L3-NODE-AT", step=step, node=node["name"], at="%s:%s" % (node["host"], node["port"]), address=node["address"],
			expected=host and "%s:%s" % (host, port))
		if host is None or node["port"] != port or not env.host_matches(node["host"], host):
			wrong.append("%s@%s:%s" % (node["name"], node["host"], node["port"]))
	check("node-address", not wrong, step=step, wrong=wrong, expected={k: "%s:%s" % v for k, v in sorted(expected.items())},
		hosts=env.hosts)


def at_advertised(pods, field="clearAlt"):
	return {p["node"]: split_address(p[field]) for p in pods}


def routes_every_pod(env, step, pods, expected, seed=None):
	"""A driver seeded once holds one node per pod at its expected address, nothing unowned, and serve writes
	and reads every partition."""
	with env.driver("l3-" + step, seed or pods[0]["clearAlt"]) as drv:
		snapshot = l5.await_routing(drv, step, [p["node"] for p in pods], ROUTING_WAIT_MS)
		assert_nodes_at(env, step, snapshot, expected)
		l5.serve(drv, step)


def never_empty(drv, step, since):
	"""The driver held a node in every snapshot since: it was never left to re-seed."""
	empty = [s["t"] for s in drv.between(since[0], drv.mark()[0]) if not s["nodes"]]
	check("never-empty", not empty, step=step, emptyAt=empty[:5])


def kept(drv, step, since, held):
	"""Every node in held stayed in every snapshot since."""
	dropped = [(s["t"], sorted(set(held) - set(names(s)))) for s in drv.between(since[0], drv.mark()[0])
		if not set(held) <= set(names(s))]
	check("nodes-kept", not dropped, step=step, nodes=sorted(held), droppedAt=dropped[:5])


def shared_host(env, cluster, pods):
	"""The host every pod advertises: the node label under configuredIP, loadBalancerAlternateAccessAddress
	under networkLoadBalancer. Either way it is the endpoint hostname."""
	if cluster["alternateAccess"] == NETWORK_LOAD_BALANCER:
		check("load-balancer-address", cluster["lbAddress"] == env.endpoint, lbAddress=cluster["lbAddress"],
			endpoint=env.endpoint)
		return cluster["lbAddress"]
	nodes = env.nodes()
	labels = {p["pod"]: nodes.get(p["worker"], {}).get("labels", {}).get(ALTERNATE_ACCESS_LABEL, "") for p in pods}
	check("node-label", set(labels.values()) == {env.endpoint}, labels=labels, endpoint=env.endpoint)
	return env.endpoint


def shared_expected(env, step):
	cluster, pods = env.cluster()
	host = shared_host(env, cluster, pods)
	missing = [p["pod"] for p in pods if p["nodePort"] <= 0]
	check("service-node-ports", not missing and len({p["nodePort"] for p in pods}) == len(pods), step=step,
		withoutNodePort=missing, nodePorts={p["pod"]: p["nodePort"] for p in pods})
	advertised(step, pods, "clearAlt", {p["pod"]: "%s:%d" % (host, p["nodePort"]) for p in pods})
	return cluster, pods, host


def row_shared_endpoint(env):
	"""Every pod advertises the endpoint hostname with its own Service NodePort (from the node label under
	configuredIP, from loadBalancerAlternateAccessAddress under networkLoadBalancer); the driver holds one
	node per pod at that host and port, nothing is unowned, and serve writes and reads every partition."""
	size = env.at_least(MIN_PODS)
	try:
		_, pods, host = shared_expected(env, "shared-endpoint")
		routes_every_pod(env, "shared-endpoint", pods, {p["node"]: (host, p["nodePort"]) for p in pods})
	finally:
		env.restore(size)


def row_per_node(env):
	"""Each kind worker carries its own hostname as its node label and runs one pod: each pod advertises its
	worker's hostname with its container hostPort, and the driver holds each node there and serves every
	partition."""
	_, pods = env.cluster()
	nodes = env.nodes()
	labels = {p["pod"]: nodes.get(p["worker"], {}).get("labels", {}).get(ALTERNATE_ACCESS_LABEL, "") for p in pods}
	check("one-pod-per-worker", len({p["worker"] for p in pods}) == len(pods), workers={p["pod"]: p["worker"] for p in pods})
	check("worker-labels", all(labels.values()) and len(set(labels.values())) == len(pods), labels=labels)
	check("host-ports", all(p["hostPort"] > 0 for p in pods), hostPorts={p["pod"]: p["hostPort"] for p in pods})
	advertised("per-node", pods, "clearAlt", {p["pod"]: "%s:%d" % (labels[p["pod"]], p["hostPort"]) for p in pods})
	routes_every_pod(env, "per-node", pods, {p["node"]: (labels[p["pod"]], p["hostPort"]) for p in pods})


def row_load_balancer_per_host(env):
	"""networkLoadBalancer with one pod per host: no node carries the alternate access label, no container
	publishes a hostPort, each pod advertises the AerospikeCluster's address with its own Service NodePort,
	and the driver holds each node there and serves every partition."""
	cluster, pods = env.cluster()
	check("network-load-balancer", cluster["alternateAccess"] == NETWORK_LOAD_BALANCER and
		cluster["multiPodPerHost"] == "false", alternateAccess=cluster["alternateAccess"],
		multiPodPerHost=cluster["multiPodPerHost"])
	check("one-pod-per-worker", len({p["worker"] for p in pods}) == len(pods), workers={p["pod"]: p["worker"] for p in pods})
	labelled = {n: node["labels"][ALTERNATE_ACCESS_LABEL] for n, node in env.nodes().items()
		if ALTERNATE_ACCESS_LABEL in node["labels"]}
	check("no-node-label", not labelled, labelled=labelled)
	check("no-host-port", all(p["hostPort"] == 0 for p in pods), hostPorts={p["pod"]: p["hostPort"] for p in pods})
	address = cluster["lbAddress"]
	advertised("load-balancer", pods, "clearAlt", {p["pod"]: "%s:%d" % (address, p["nodePort"]) for p in pods})
	routes_every_pod(env, "load-balancer", pods, {p["node"]: (address, p["nodePort"]) for p in pods})


def wall_ms():
	return time.time() * 1000


def clock_offset(drv, rounds=3):
	"""Driver t minus the harness's wall clock in milliseconds, from the reset round trip that took least, and
	half that round trip as its error. Lines up the l3ctl readings, which carry the wall clock, with driver t."""
	best = None
	for _ in range(rounds):
		before = wall_ms()
		reply, _ = drv.request("reset", "reset", REPLY_WAIT_MS)
		after = wall_ms()
		check("reset-reply", reply is not None, boundMs=REPLY_WAIT_MS, quiet=True)
		if best is None or after - before < best[1] * 2:
			best = (reply["t"] - (before + after) / 2, (after - before) / 2)
	return best


def row_scale_up(env):
	"""The pod the operator adds is usable by the running driver within two tend intervals of the later of the
	peers-generation bump it causes and its first answer to a login and info request from the driver's network, at the endpoint hostname and its own Service
	NodePort, with no driver restart or reseed; every node is held and every partition served afterwards."""
	_, pods, host = shared_expected(env, "scale-up-before")
	size = len(pods)
	prefix = pods[-1]["pod"].rpartition("-")[0]
	new_pod = "%s-%d" % (prefix, ordinal(pods[-1]) + 1)
	join = None
	try:
		with env.driver("l3-scale-up", pods[0]["clearAlt"]) as drv:
			l5.await_routing(drv, "scale-up-before", [p["node"] for p in pods], ROUTING_WAIT_MS)
			since = drv.mark()
			offset, error = clock_offset(drv)
			join = env.ctl.start("join-watch", new_pod, pods[0]["pod"], "--resize", str(size + 1), "--host", host,
				"--timeout", str(POD_TIMEOUT_S))
			up = env.ctl.ok("wait-pod", new_pod, "--timeout", str(POD_TIMEOUT_S),
				bound_ms=POD_TIMEOUT_S * 1000 + CTL_TIMEOUT_MS).first("L3-POD-UP")
			node, address = up["node"], "%s:%s" % (host, up["nodePort"])
			watch_mark = drv.mark()
			reply, waited = drv.request("watch %s %s" % (address, node), "watch", LISTENER_WAIT_MS)
			check("watch-reply", reply is not None, address=address, node=node, readyAtWatch=up["ready"], waitedMs=waited,
				boundMs=LISTENER_WAIT_MS)
			joined = env.ctl.require(env.ctl.finish(join, POD_TIMEOUT_S * 1000 + CTL_TIMEOUT_MS)).first("L3-JOINED")
			join = None
			bump_at = round(float(joined["bumpEpochMs"]) + offset)
			answer_at = round(float(joined["answerEpochMs"]) + offset)
			ready_at = round(float(joined["readyEpochMs"]) + offset)
			start_at = max(bump_at, answer_at)
			usable_ms = reply["usableAt"] - start_at if reply["usableAt"] is not None else None
			times = dict(bumpAt=bump_at, answerAt=answer_at, readyAt=ready_at, servingAt=reply["servingAt"],
				usableAt=reply["usableAt"])
			emit("L3-SCALE-UP-TIMES", node=node, pod=new_pod, advertised=address, **times, startAt=start_at, usableMs=usable_ms,
				readyToAnswerMs=answer_at - ready_at, usableFromServingMs=reply["usableMs"], lastClosedAt=reply["lastClosedAt"],
				watchSentAt=watch_mark[1], generation=joined["generation"], answeredBy=joined["node"],
				bumpPollMs=joined["bumpPollMs"], peersMaxPollMs=joined["peersMaxPollMs"], answerPollMs=joined["answerPollMs"],
				answerMaxPollMs=joined["answerMaxPollMs"], answerAttempts=joined["answerAttempts"],
				answerFailures=joined["answerFailures"], readyPollMs=joined["readyPollMs"],
				readyMaxPollMs=joined["readyMaxPollMs"], clockOffsetMs=round(offset), clockErrorMs=round(error),
				ownershipChecked=reply["ownershipChecked"], tendInterval=TEND_INTERVAL_MS)
			check("watch-usable", usable_ms is not None, node=node, **times)
			check("answered-node", joined["node"] == node, node=node, answeredBy=joined["node"], address=joined["address"])
			polls = [joined["bumpPollMs"], joined["answerPollMs"]]
			check("poll-interval", all(p.isdigit() and int(p) <= POLL_BOUND_MS for p in polls),
				bumpPollMs=joined["bumpPollMs"], answerPollMs=joined["answerPollMs"], boundMs=POLL_BOUND_MS)
			check("usable-after-start", usable_ms >= 0, measurementError=usable_ms < 0, usableMs=usable_ms, startAt=start_at,
				**times, clockErrorMs=round(error))
			check("usable-ms", usable_ms <= USABLE_BOUND_MS, node=node, usableMs=usable_ms, startAt=start_at, **times,
				readyToAnswerMs=answer_at - ready_at, boundMs=USABLE_BOUND_MS)
			env.wait_completed()
			_, after, _ = shared_expected(env, "scale-up-after")
			added = [p for p in after if p["pod"] == new_pod]
			check("watched-node", [p["node"] for p in added] == [node], pod=new_pod, watched=node,
				advertisedBy=[p["node"] for p in added])
			snapshot = l5.await_routing(drv, "scale-up-after", [p["node"] for p in after], ROUTING_WAIT_MS)
			assert_nodes_at(env, "scale-up-after", snapshot, {p["node"]: (host, p["nodePort"]) for p in after})
			kept(drv, "scale-up", since, [p["node"] for p in pods])
			never_empty(drv, "scale-up", since)
			l5.serve(drv, "scale-up")
	finally:
		if join is not None:
			l5.kill_group(join[0])
			join[0].communicate()
		env.outcome.guard(env.restore, size)


def row_scale_down(env):
	"""The pod the operator removes leaves the driver's snapshots with nothing unowned, and once it is gone
	failed, clientErrors and clientTimeouts stay flat over several tend intervals while the load succeeds."""
	size = env.at_least(MIN_PODS)
	try:
		_, pods = env.cluster()
		removed = pods[-1]
		remaining = [p["node"] for p in pods[:-1]]
		with env.driver("l3-scale-down", pods[0]["clearAlt"]) as drv:
			l5.await_routing(drv, "scale-down-before", [p["node"] for p in pods], ROUTING_WAIT_MS)
			since = drv.mark()
			emit("L3-SCALE-DOWN", pod=removed["pod"], node=removed["node"], t=since[1])
			env.scale(len(pods) - 1)
			gone, waited = drv.wait(lambda m: m if m["type"] == "snapshot" and l5.routed(m, remaining) else None,
				ROUTING_WAIT_MS, since=since)
			last = gone or drv.latest()
			check("removed", gone is not None, node=removed["node"], expected=remaining, nodes=describe(last["nodes"]),
				unowned=last.get("unowned"), owners=last.get("owners"), waitedMs=waited, boundMs=ROUTING_WAIT_MS)
			after = drv.mark()
			base = drv.between(0, after[0])[-1]
			end, _ = drv.wait(lambda m: m if m["type"] == "snapshot" and m["t"] - base["t"] >= QUIET_WINDOW_MS else None,
				QUIET_WINDOW_MS + REPLY_WAIT_MS, since=after)
			check("quiet-window", end is not None, windowMs=QUIET_WINDOW_MS)
			window = drv.between(base["_index"], end["_index"])
			moved = {k: (base.get(k), end.get(k)) for k in l5.QUIET_COUNTERS
				if any(s.get(k) != base.get(k) for s in window)}
			held = [s["t"] for s in window if removed["node"] in names(s)]
			check("flat-after-removal", not moved and not held and end["succeeded"] > base["succeeded"],
				windowMs=QUIET_WINDOW_MS, moved=moved, removedHeldAt=held[:5], succeeded=[base["succeeded"],
				end["succeeded"]], lastError=end["lastError"])
			l5.serve(drv, "scale-down")
	finally:
		env.outcome.guard(env.restore, size)


def pinned_seed(env, step, service, pinned, size):
	"""No pod advertises the pinned NodePort, every pod advertises its own Service NodePort, and a driver
	seeded only through the pinned NodePort holds one node per pod and serves every partition."""
	_, pods, host = shared_expected(env, step)
	check("pods", len(pods) == size, step=step, pods=len(pods), expected=size)
	advertising = [p["pod"] for p in pods if p["clearAlt"] == "%s:%d" % (host, pinned)]
	check("pinned-not-advertised", not advertising, step=step, service=service, pinned=pinned, advertisedBy=advertising)
	with env.driver("l3-pinned-" + step, "%s:%d" % (host, pinned)) as drv:
		l5.await_routing(drv, "pinned-" + step, [p["node"] for p in pods], ROUTING_WAIT_MS)
		l5.serve(drv, "pinned-" + step)


def row_node_port_stability(env):
	"""A pod's Service keeps its NodePort across reschedule and is recreated on remove and add; a separate
	NodePort Service pinned to pod 0 keeps its port, no pod advertises it, and a driver seeded only through
	it holds every node."""
	size = env.at_least(MIN_PODS)
	services = []
	try:
		cluster, pods = env.cluster()
		first = pods[0]
		check("first-pod", ordinal(first) == 0, pod=first["pod"])
		rng = env.ctl.ok("node-port-range").first("L3-NODE-PORT-RANGE")
		allocated = {int(p) for p in env.ctl.ok("node-ports").first("L3-NODE-PORTS")["ports"].split(",") if p}
		free = [p for p in range(int(rng["last"]), int(rng["first"]) - 1, -1) if p not in allocated]
		check("free-node-port", bool(free), range="%s-%s" % (rng["first"], rng["last"]), allocated=len(allocated))
		pinned = free[0]
		service = cluster["name"] + "-pinned-seed"
		services.append(service)
		created = env.ctl.ok("service-create", service, first["pod"], str(pinned)).first("L3-SERVICE")
		check("pinned-created", int(created["nodePort"] or 0) == pinned, service=service, nodePort=created["nodePort"],
			pinned=pinned)
		pinned_seed(env, "created", service, pinned, len(pods))

		before = env.ctl.ok("service", first["pod"]).first("L3-SERVICE")
		deleted = env.ctl.ok("delete-pod", first["pod"]).first("L3-POD-DELETED")
		env.ctl.ok("wait-ready", first["pod"], "--old-uid", deleted["uid"], "--timeout", str(POD_TIMEOUT_S),
			bound_ms=POD_TIMEOUT_S * 1000 + CTL_TIMEOUT_MS)
		env.wait_completed()
		after = env.ctl.ok("service", first["pod"]).first("L3-SERVICE")
		check("rescheduled-service-kept", (after["uid"], after["nodePort"]) == (before["uid"], before["nodePort"]),
			pod=first["pod"], before="%s/%s" % (before["uid"], before["nodePort"]),
			after="%s/%s" % (after["uid"], after["nodePort"]))
		kept_pin = env.ctl.ok("service", service).first("L3-SERVICE")
		check("pinned-kept", int(kept_pin["nodePort"] or 0) == pinned, step="rescheduled", nodePort=kept_pin["nodePort"],
			pinned=pinned)
		pinned_seed(env, "rescheduled", service, pinned, len(pods))

		last = pods[-1]
		old = env.ctl.ok("service", last["pod"]).first("L3-SERVICE")
		env.scale(len(pods) - 1)
		gone = env.ctl.ok("service", last["pod"]).first("L3-SERVICE")
		check("removed-service-deleted", not gone["uid"], pod=last["pod"], uid=gone["uid"])
		held = cluster["name"] + "-held-node-port"
		services.append(held)
		env.ctl.ok("service-create", held, last["pod"], old["nodePort"])
		env.scale(len(pods))
		new = env.ctl.ok("service", last["pod"]).first("L3-SERVICE")
		env.ctl.ok("service-delete", held)
		services.remove(held)
		check("added-service-recreated", new["uid"] and new["uid"] != old["uid"] and int(new["nodePort"] or 0) > 0 and
			new["nodePort"] != old["nodePort"], pod=last["pod"], before="%s/%s" % (old["uid"], old["nodePort"]),
			after="%s/%s" % (new["uid"], new["nodePort"]))
		kept_pin = env.ctl.ok("service", service).first("L3-SERVICE")
		check("pinned-kept", int(kept_pin["nodePort"] or 0) == pinned, step="recreated", nodePort=kept_pin["nodePort"],
			pinned=pinned)
		pinned_seed(env, "recreated", service, pinned, len(pods))
	finally:
		for service in services:
			env.outcome.guard(env.ctl.ok, "service-delete", service)
		env.outcome.guard(env.restore, size)


def row_reschedule(env):
	"""networkLoadBalancer: the pod its StatefulSet recreates advertises the same address and Service
	NodePort, and the running driver owns every partition again with no restart or reseed."""
	size = env.at_least(MIN_PODS)
	try:
		cluster, pods = env.cluster()
		address = cluster["lbAddress"]
		victim = pods[-1]
		advertised("reschedule-before", pods, "clearAlt", {p["pod"]: "%s:%d" % (address, p["nodePort"]) for p in pods})
		with env.driver("l3-reschedule", pods[0]["clearAlt"]) as drv:
			l5.await_routing(drv, "reschedule-before", [p["node"] for p in pods], ROUTING_WAIT_MS)
			since = drv.mark()
			deleted = env.ctl.ok("delete-pod", victim["pod"]).first("L3-POD-DELETED")
			env.ctl.ok("wait-ready", victim["pod"], "--old-uid", deleted["uid"], "--timeout", str(POD_TIMEOUT_S),
				bound_ms=POD_TIMEOUT_S * 1000 + CTL_TIMEOUT_MS)
			env.wait_completed()
			_, after = env.cluster()
			replaced = [p for p in after if p["pod"] == victim["pod"]][0]
			check("rescheduled", replaced["uid"] != victim["uid"], pod=victim["pod"], uid=[victim["uid"], replaced["uid"]],
				worker=[victim["worker"], replaced["worker"]])
			check("node-port-kept", replaced["nodePort"] == victim["nodePort"], pod=victim["pod"],
				nodePort=[victim["nodePort"], replaced["nodePort"]])
			advertised("reschedule-after", after, "clearAlt", {p["pod"]: "%s:%d" % (address, p["nodePort"]) for p in after})
			snapshot = l5.await_routing(drv, "reschedule-after", [p["node"] for p in after], ROUTING_WAIT_MS)
			assert_nodes_at(env, "reschedule-after", snapshot, at_advertised(after))
			never_empty(drv, "reschedule", since)
			l5.serve(drv, "reschedule")
	finally:
		env.outcome.guard(env.restore, size)


def away_from_endpoint(env, pods, nodes):
	"""The highest-ordinal pod on a worker the endpoint hostname does not resolve to, or None."""
	endpoint_ip = env.hosts.get(env.endpoint)
	candidates = [p for p in pods if nodes.get(p["worker"], {}).get("role") == "worker" and
		nodes[p["worker"]]["ip"] != endpoint_ip]
	return candidates[-1] if candidates else None


def cordon_others(env, worker, cordoned):
	"""Cordon every node but worker, so the replacement pod cannot schedule anywhere else."""
	for line in env.ctl.ok("cordon-others", worker).tagged("L3-CORDONED"):
		cordoned.append(line["node"])


def row_recreated_node(env):
	"""networkLoadBalancer: with every other node cordoned, the Node under a pod away from the endpoint is
	deleted and registered again with a new UID and none of the labels set through the API; the replacement
	pod runs on that Node, advertises the AerospikeCluster's address with its Service NodePort, and the
	running driver owns every partition."""
	size = env.at_least(MIN_PODS)
	cordoned = []
	try:
		cluster, pods = env.cluster()
		address = cluster["lbAddress"]
		nodes = env.nodes()
		victim = away_from_endpoint(env, pods, nodes)
		check("pod-away-from-endpoint", victim is not None, endpoint=env.endpoint, endpointAddress=env.hosts.get(env.endpoint),
			workers={p["pod"]: p["worker"] for p in pods})
		worker = victim["worker"]
		env.ctl.ok("label-node", worker, NODE_MARK, NODE_MARK_VALUE)
		marked = env.nodes()[worker]
		check("label-set", marked["labels"].get(NODE_MARK) == NODE_MARK_VALUE, node=worker, labels=marked["labels"])
		with env.driver("l3-recreated-node", pods[0]["clearAlt"]) as drv:
			l5.await_routing(drv, "node-before", [p["node"] for p in pods], ROUTING_WAIT_MS)
			since = drv.mark()
			cordon_others(env, worker, cordoned)
			env.ctl.ok("delete-node", worker)
			env.ctl.ok("reregister", worker, "--timeout", str(NODE_TIMEOUT_S), bound_ms=NODE_TIMEOUT_S * 1000 + CTL_TIMEOUT_MS)
			ready = env.ctl.ok("wait-ready", victim["pod"], "--old-uid", victim["uid"], "--timeout", str(POD_TIMEOUT_S),
				bound_ms=POD_TIMEOUT_S * 1000 + CTL_TIMEOUT_MS).first("L3-POD-READY")
			check("replacement-on-node", ready["worker"] == worker, pod=victim["pod"], worker=ready["worker"], expected=worker)
			env.wait_completed()
			registered = env.nodes()[worker]
			emit("L3-NODE-RECREATED", node=worker, uid=[marked["uid"], registered["uid"]], labels=registered["labels"])
			check("new-node-uid", registered["uid"] and registered["uid"] != marked["uid"], node=worker,
				uid=[marked["uid"], registered["uid"]])
			check("api-labels-gone", NODE_MARK not in registered["labels"] and ALTERNATE_ACCESS_LABEL not in
				registered["labels"], node=worker, labels=registered["labels"])
			_, after = env.cluster()
			replaced = [p for p in after if p["pod"] == victim["pod"]][0]
			check("pod-node-port", replaced["nodePort"] > 0, pod=replaced["pod"], nodePort=replaced["nodePort"])
			advertised("node-after", after, "clearAlt", {p["pod"]: "%s:%d" % (address, p["nodePort"]) for p in after})
			snapshot = l5.await_routing(drv, "node-after", [p["node"] for p in after], ROUTING_WAIT_MS)
			assert_nodes_at(env, "node-after", snapshot, at_advertised(after))
			never_empty(drv, "recreated-node", since)
			l5.serve(drv, "recreated-node")
	finally:
		if cordoned:
			env.outcome.guard(env.ctl.ok, "uncordon", *cordoned)
		env.outcome.guard(env.restore, size)


def tls_expected(env, cluster, pods):
	"""The TLS address each pod advertises: the endpoint hostname under configuredIP,
	loadBalancerTLSAlternateAccessAddress under networkLoadBalancer, with the pod's TLS Service NodePort."""
	host = env.endpoint
	if cluster["alternateAccess"] == NETWORK_LOAD_BALANCER:
		host = cluster["lbTlsAddress"]
		check("load-balancer-tls-address", host == (env.tls_endpoint or env.endpoint), lbTlsAddress=host,
			expected=env.tls_endpoint or env.endpoint, lbAddress=cluster["lbAddress"])
	missing = [p["pod"] for p in pods if p["tlsNodePort"] <= 0]
	check("tls-node-ports", not missing, withoutTlsNodePort=missing)
	return host, {p["pod"]: "%s:%d" % (host, p["tlsNodePort"]) for p in pods}


def row_tls_certificate(env):
	"""The shared certificate carries the tls-name as a dNSName SAN and no SAN for the endpoint hostname, and
	each pod advertises its TLS address with its own TLS Service NodePort: under networkLoadBalancer
	loadBalancerTLSAlternateAccessAddress, which may differ from the clear address."""
	size = env.at_least(MIN_PODS)
	try:
		cluster, pods = env.cluster()
		check("tls-name", cluster["tlsName"] == env.tls_name, tlsName=cluster["tlsName"], expected=env.tls_name)
		cert = env.ctl.ok("certificate").first("L3-CERT")
		sans = [s for s in cert["sans"].split(",") if s]
		dns = [s.split(":", 1)[1] for s in sans if s.startswith("DNS:")]
		endpoints = {env.endpoint, env.tls_endpoint} - {""}
		naming = [s for s in sans if any(e in s for e in endpoints)]
		check("certificate-sans", dns == [env.tls_name] and not naming, sans=sans, tlsName=env.tls_name,
			endpointSans=naming)
		_, expected = tls_expected(env, cluster, pods)
		advertised("tls", pods, "tlsAlt", expected)
		if cluster["alternateAccess"] == NETWORK_LOAD_BALANCER:
			advertised("tls-clear", pods, "clearAlt", {p["pod"]: "%s:%d" % (cluster["lbAddress"], p["nodePort"]) for p in pods})
	finally:
		env.restore(size)


def row_tls_name(env):
	"""A driver with the tls-name, seeded at the TLS address, holds every node at its advertised TLS address
	and serves every partition; the same seed with the endpoint hostname as tls-name fails to create the
	client."""
	size = env.at_least(MIN_PODS)
	try:
		cluster, pods = env.cluster()
		host, expected = tls_expected(env, cluster, pods)
		advertised("tls", pods, "tlsAlt", expected)
		seed = expected[pods[0]["pod"]]
		with env.driver("l3-tls-name", seed, tls_name=env.tls_name) as drv:
			snapshot = l5.await_routing(drv, "tls-name", [p["node"] for p in pods], ROUTING_WAIT_MS)
			assert_nodes_at(env, "tls-name", snapshot, at_advertised(pods, "tlsAlt"))
			l5.serve(drv, "tls-name")
		drv = env.start("l3-tls-endpoint-name", seed, tls_name=host)
		try:
			try:
				drv.await_ready()
				created = True
			except RowFailure as e:
				if not str(e).startswith("client-created"):
					raise
				created = False
			check("endpoint-name-client-not-created", not created, seed=seed, tlsName=host, status=drv.status)
		finally:
			try:
				drv.close()
			except RowFailure as e:
				if not str(e).startswith("client-created"):
					raise
			env.stop_drivers()
	finally:
		env.outcome.guard(env.restore, size)


def shared_endpoint(shape):
	if shape["access"] == NETWORK_LOAD_BALANCER or shape["multiPodPerHost"]:
		return None
	return "configuredIP with one pod per worker advertises each worker's own hostname, not one shared endpoint"


def changes_cluster(shape):
	if shape["tls"]:
		return "the TLS profiles run the endpoint and TLS rows; this row runs on default, nlb and nlb-per-pod"
	return shared_endpoint(shape)


def nlb_only(shape):
	return changes_cluster(shape) or (None if shape["access"] == NETWORK_LOAD_BALANCER else
		"needs alternateAccess networkLoadBalancer (nlb or nlb-per-pod)")


def tls_only(shape):
	return None if shape["tls"] else "needs a cluster that serves TLS (tls or nlb-tls)"


class Row:
	def __init__(self, name, run, contract=1, applies=lambda shape: None):
		self.name = name
		self.run = run
		self.contract = contract
		self.applies = applies
		self.doc = " ".join(run.__doc__.split())


ROWS = [
	Row("every-pod-advertises-shared-endpoint-with-own-node-port", row_shared_endpoint, applies=shared_endpoint),
	Row("each-pod-at-its-worker-hostname-and-host-port", row_per_node, applies=lambda shape: None
		if shape["access"] == CONFIGURED_IP and not shape["multiPodPerHost"] else
		"needs configuredIP with one pod per labelled worker (per-node)"),
	Row("load-balancer-address-without-node-label-or-host-port", row_load_balancer_per_host, applies=lambda shape: None
		if shape["access"] == NETWORK_LOAD_BALANCER and not shape["multiPodPerHost"] else
		"needs networkLoadBalancer with one pod per host (nlb-per-pod)"),
	Row("scale-up-new-node-usable-within-two-tend-intervals", row_scale_up, applies=changes_cluster),
	Row("scale-down-removed-node-leaves-without-retry-storm", row_scale_down, applies=changes_cluster),
	Row("node-ports-stable-across-reschedule-and-recreate", row_node_port_stability, applies=changes_cluster),
	Row("rescheduled-pod-keeps-address-and-node-port", row_reschedule, applies=nlb_only),
	Row("pod-on-recreated-node-advertises-cluster-address", row_recreated_node, applies=lambda shape: nlb_only(shape) or
		(None if not shape["multiPodPerHost"] else
		"needs a pod on a worker the endpoint hostname does not resolve to; every pod runs on one kind node (nlb-per-pod)")),
	Row("shared-certificate-names-tls-name-not-endpoint", row_tls_certificate, contract=0, applies=tls_only),
	Row("tls-name-holds-every-node-endpoint-name-not-created", row_tls_name, contract=TLS_CONTRACT, applies=tls_only),
]


def plan(profile):
	for row in ROWS:
		needs = "no driver" if row.contract == 0 else "contract %d" % row.contract
		reason = row.applies(PROFILES[profile])
		say("L3-PLAN-ROW profile=%s row=%s needs=%s %s: %s" % (profile, row.name, needs,
			"skip reason=" + reason if reason else "runs", row.doc))


def run_row(env, row):
	env.outcome = l5.Outcome()
	reason = row.applies(env.shape) or ""
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
				reason = "the driver speaks contract %s; this row needs %d%s" % (contract, row.contract,
					" (--tls-name and --tls-ca-file)" if row.contract == TLS_CONTRACT else "")
	if result is None:
		emit("L3-ROW-START", profile=env.profile, row=row.name)
		try:
			row.run(env)
		except (RowFailure, ContractError) as e:
			env.outcome.record(e)
		result = "pass" if env.outcome.status == 0 else "fail"
	if result == "fail" and not reason:
		reason = "see the L3-FAILED lines above"
	say("L3-ROW profile=%s row=%s result=%s%s" % (env.profile, row.name, result, " reason=" + reason if reason else ""))
	return result, env.outcome.status


def main(argv=None):
	parser = argparse.ArgumentParser(description="Run the L3 rows of one profile through a client's driver.")
	parser.add_argument("--profile", required=True, choices=sorted(PROFILES))
	parser.add_argument("--driver", help="command that starts the client's driver where it reaches the kind nodes")
	parser.add_argument("--rows", help="comma-separated rows to run (default: all)")
	parser.add_argument("--plan", action="store_true", help="list the profile's rows and exit")
	try:
		args = parser.parse_args(argv)
	except SystemExit as e:
		return 2 if e.code else 0
	if args.plan:
		plan(args.profile)
		return 0
	rows = ROWS
	if args.rows:
		wanted = args.rows.split(",")
		unknown = [r for r in wanted if r not in [row.name for row in ROWS]]
		if unknown:
			say("ERROR: unknown rows: %s" % ",".join(unknown))
			return 2
		rows = [row for row in ROWS if row.name in wanted]
	if not args.driver:
		say("ERROR: --driver is required")
		return 2
	try:
		env = Env(args.profile, args.driver)
	except UsageError as e:
		say("ERROR: %s" % e)
		return 2
	status = 0
	results = []
	for row in rows:
		result, row_status = run_row(env, row)
		results.append(result)
		status = max(status, row_status, 1 if result == "fail" else 0)
	emit("L3-PROFILE-RESULT", profile=args.profile, passed=results.count("pass"), failed=results.count("fail"),
		skipped=results.count("skip"), exit=status)
	return status


if __name__ == "__main__":
	sys.exit(main())
