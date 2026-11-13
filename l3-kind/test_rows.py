#!/usr/bin/env python3
#
# Offline tests of rows.py: a scripted fake driver and a stub l3ctl, sharing a state directory. The
# stub prints the scripted L3-* lines for each call, by command, argument, call count and the fake
# cluster's phase, and can move the cluster to a new phase; the fake driver emits the snapshots of the
# current phase, chosen by the driver's --set and the phase, advancing its t faster than the wall clock,
# and answers commands. Each row has a passing run and a run that breaks it, and the TLS driver row a
# run with a contract 3 driver that skips it.
#
# Usage: python3 -m unittest l3-kind/test_rows.py -v

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_spec = importlib.util.spec_from_file_location("l3_rows", os.path.join(os.path.dirname(os.path.abspath(__file__)),
	"rows.py"))
rows = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rows)

FAKE_DRIVER = r'''
import json, os, sys, threading, time
state = os.environ["FAKE_STATE"]
spec = json.load(open(os.path.join(state, "scenario.json")))["driver"]
args = sys.argv[1:]
with open(os.path.join(state, "driver-args"), "a") as f:
	f.write(json.dumps(args) + "\n")
shape = args[args.index("--shape") + 1]
set_name = args[args.index("--set") + 1]
commands = []

def read_commands():
	for line in sys.stdin:
		commands.append(line.strip())
	commands.append("quit")

def out(obj):
	sys.stdout.write(json.dumps(obj) + "\n")
	sys.stdout.flush()

def phase():
	try:
		with open(os.path.join(state, "phase")) as f:
			return f.read().strip()
	except OSError:
		return "start"

threading.Thread(target=read_commands, daemon=True).start()
if set_name in spec.get("not_created", {}):
	out({"type": "error", "t": 0, "message": spec["not_created"][set_name]})
	sys.exit(3)
contract = spec.get("contract", 3)
out({"type": "ready", "t": 0, "contract": contract, "epochMs": 0, "client": "fake", "clientVersion": "0", "shape": shape})
t = 0
counters = {"succeeded": 0, "failed": 0}
watch = None
entered = None
while True:
	time.sleep(0.002)
	t += 100
	name = phase()
	phases = spec["phases"]
	ph = phases.get(set_name + ":" + name) or phases.get(name) or phases.get(set_name + ":start") or phases["start"]
	first = entered != name
	entered = name
	counters["succeeded"] += 10
	counters["failed"] += ph.get("fail", 0)
	snapshot = {"type": "snapshot", "t": t, "reason": "change" if first else "heartbeat", "nodes": ph["nodes"],
		"unowned": ph.get("unowned", 0), "owners": {n["name"]: 1 for n in ph["nodes"]}, "longestUnownedMs": 0,
		"maxLatencyMs": 5, "lastError": None, "clientErrors": 0, "clientTimeouts": 0}
	snapshot.update(counters)
	out(snapshot)
	while commands:
		words = commands.pop(0).split()
		if words[0] == "quit":
			sys.exit(0)
		elif words[0] == "serve":
			out({"type": "served", "t": t, "step": words[1], "partitions": 4096, "written": 4096, "read": 4096,
				"mismatched": 0, "failures": 0, "firstFailure": None, "elapsedMs": 3000})
		elif words[0] == "reset":
			out({"type": "reset", "t": t})
		elif words[0] == "watch":
			watch = words[1:]
		else:
			out({"type": "error", "t": t, "message": "unknown command: " + " ".join(words)})
	if watch and name in spec.get("watch_phases", ["scaling"]):
		out(dict({"type": "watch", "t": t, "address": watch[0], "node": watch[1], "servingAt": t - 60,
			"lastClosedAt": t - 70, "addresses": [{"address": "172.18.0.2", "firstConnectedAt": t - 60}],
			"usableAt": t - 10, "usableMs": 50, "ownershipChecked": True}, **spec.get("watch", {})))
		watch = None
'''

STUB_L3CTL = r'''
import json, os, sys
state = os.environ["FAKE_STATE"]
spec = json.load(open(os.path.join(state, "scenario.json")))["ctl"]
args = sys.argv[1:]
with open(os.path.join(state, "ctl.log"), "a") as f:
	f.write(" ".join(args) + "\n")

def phase():
	try:
		with open(os.path.join(state, "phase")) as f:
			return f.read().strip()
	except OSError:
		return "start"

def counted(key, entries):
	if not isinstance(entries, list):
		return entries
	path = os.path.join(state, "count-" + key.replace("/", "_"))
	n = int(open(path).read()) if os.path.exists(path) else 0
	with open(path, "w") as f:
		f.write(str(n + 1))
	return entries[min(n, len(entries) - 1)]

entry = counted(args[0], spec.get(args[0], {}))
arg = args[1] if len(args) > 1 else ""
if arg in entry.get("by_arg", {}):
	entry = counted(args[0] + ":" + arg, entry["by_arg"][arg])
entry = entry.get("by_phase", {}).get(phase(), entry)
for line in entry.get("lines", []):
	print(line.format(*args))
if "phase" in entry:
	with open(os.path.join(state, "phase"), "w") as f:
		f.write(entry["phase"])
if "stderr" in entry:
	print(entry["stderr"], file=sys.stderr)
sys.exit(entry.get("exit", 0))
'''

ENDPOINT = "as-endpoint.test"
TLS_ENDPOINT = "as-endpoint-tls.test"
ENDPOINT_IP = "172.18.0.2"
TLS_NAME = "aerospike-tls"
LABEL = rows.ALTERNATE_ACCESS_LABEL
PORTS = [31000, 31001, 31002]
TLS_PORTS = [32000, 32001, 32002]


def pod_line(i, worker="cp", host=ENDPOINT, port=None, host_port=0, uid=None, tls=None, service_uid=None):
	port = PORTS[i] if port is None else port
	tls_alt = "%s:%d" % (tls, TLS_PORTS[i]) if tls else ""
	return ("L3-POD pod=aerocluster-0-%d worker=%s ip=10.244.0.%d phase=Running ready=True uid=%s nodePort=%d "
		"tlsNodePort=%s serviceUid=%s hostPort=%s node=0A%d clearAlt=%s:%d tlsAlt=%s" % (i, worker, 10 + i,
		uid or "p%d" % i, PORTS[i], TLS_PORTS[i] if tls else "", service_uid or "s%d" % i, host_port or "", i, host,
		port, tls_alt))


def cluster_line(size, access="configuredIP", multi="true", lb="", lb_tls="", tls=""):
	return ("L3-CLUSTER name=aerocluster namespace=aerospike size=%d phase=Completed alternateAccess=%s "
		"multiPodPerHost=%s lbAddress=%s lbTlsAddress=%s servicePort=3000 tlsPort=%s tlsName=%s" % (size, access, multi,
		lb, lb_tls, "4333" if tls else "", tls))


def pods_entry(size, cluster=None, **pod):
	return {"lines": [cluster or cluster_line(size)] + [pod_line(i, **pod) for i in range(size)]}


def node(i, host=ENDPOINT, port=None):
	return {"name": "0A%d" % i, "host": host, "port": PORTS[i] if port is None else port, "address": ENDPOINT_IP,
		"active": True, "reachable": True}


def nodes(size, changes=None):
	return [dict(node(i), **(changes or {}).get(i, {})) for i in range(size)]


def node_line(name, role="worker", ip=ENDPOINT_IP, labels=None, uid=None):
	return "L3-NODE name=%s uid=%s ip=%s role=%s unschedulable=false ready=True labels=%s" % (name, uid or "u-" + name,
		ip, role, json.dumps(labels or {}, separators=(",", ":")).replace("{", "{{").replace("}", "}}"))


def base_ctl():
	return {
		"pods": pods_entry(2),
		"nodes": {"lines": [node_line("cp", "control-plane", labels={LABEL: ENDPOINT})]},
		"drivers-stop": {},
		"wait-completed": {"lines": ["L3-COMPLETED size=2"]},
	}


def scenario(phases=None, contract=3, ctl=None, **driver):
	return {"ctl": dict(base_ctl(), **(ctl or {})), "driver": dict({"contract": contract,
		"phases": phases or {"start": {"nodes": nodes(2)}}}, **driver)}


VALUES = {"L3_ENDPOINT_HOSTNAME": ENDPOINT, "L3_CLIENT_HOSTS": "%s:%s" % (ENDPOINT, ENDPOINT_IP), "L3_CLUSTER": "t"}


class RowsTest(unittest.TestCase):
	profile = "default"
	values = {}

	def setUp(self):
		self.dir = tempfile.TemporaryDirectory()
		self.state = os.path.join(self.dir.name, "state")
		os.mkdir(self.state)
		self.ctl = os.path.join(self.dir.name, "l3ctl")
		with open(self.ctl, "w") as f:
			f.write("#!%s\n%s" % (sys.executable, STUB_L3CTL))
		os.chmod(self.ctl, 0o700)
		self.fake = os.path.join(self.dir.name, "fake_driver.py")
		with open(self.fake, "w") as f:
			f.write(FAKE_DRIVER)
		self.environ = {"L3_CTL": self.ctl, "L3_STATE": self.state, "FAKE_STATE": self.state}

	def tearDown(self):
		self.dir.cleanup()

	def run_rows(self, row, s):
		values = dict(VALUES, L3_PROFILE=self.profile, **self.values)
		with open(os.path.join(self.state, "topology.env"), "w") as f:
			f.write("".join("%s=%s\n" % kv for kv in values.items()))
		with open(os.path.join(self.state, "scenario.json"), "w") as f:
			json.dump(s, f)
		out = io.StringIO()
		saved = dict(os.environ)
		os.environ.update(self.environ)
		try:
			with contextlib.redirect_stdout(out), mock.patch.object(rows, "clock_offset", lambda drv: (0, 0)):
				code = rows.main(["--profile", self.profile, "--rows", row, "--driver", "%s %s" % (sys.executable, self.fake)])
		finally:
			os.environ.clear()
			os.environ.update(saved)
		return code, out.getvalue()

	def ctl_log(self):
		with open(os.path.join(self.state, "ctl.log")) as f:
			return f.read().splitlines()

	def driver_args(self):
		with open(os.path.join(self.state, "driver-args")) as f:
			return [json.loads(line) for line in f]

	def assertPasses(self, row, s):
		code, out = self.run_rows(row, s)
		self.assertEqual(0, code, out)
		self.assertNotIn("outcome=fail", out)
		self.assertIn("L3-ROW profile=%s row=%s result=pass" % (self.profile, row), out)
		return out

	def assertFailsCheck(self, row, s, name, **fields):
		code, out = self.run_rows(row, s)
		self.assertEqual(1, code, out)
		failed = [line for line in out.splitlines() if line.startswith("L3-CHECK check=%s " % name) and
			line.endswith("outcome=fail")]
		self.assertTrue(failed, "no failed check %s in:\n%s" % (name, out))
		for key, value in fields.items():
			self.assertIn("%s=%s" % (key, value), failed[0])
		self.assertIn("L3-ROW profile=%s row=%s result=fail" % (self.profile, row), out)
		return out

	def assertSkips(self, row, s, reason):
		code, out = self.run_rows(row, s)
		self.assertEqual(0, code, out)
		self.assertIn("L3-ROW profile=%s row=%s result=skip reason=%s" % (self.profile, row, reason), out)
		self.assertNotIn("L3-ROW-START profile=%s row=%s" % (self.profile, row), out)
		return out


class SharedEndpointTest(RowsTest):
	row = "every-pod-advertises-shared-endpoint-with-own-node-port"

	def test_pass(self):
		self.assertPasses(self.row, scenario())
		args = self.driver_args()[0]
		self.assertEqual("%s:%d" % (ENDPOINT, PORTS[0]), args[args.index("--seed") + 1])
		self.assertEqual("pass-through", args[args.index("--shape") + 1])
		self.assertEqual("admin", args[args.index("--user") + 1])
		self.assertNotIn("--tls-name", args)

	def test_node_held_at_another_port_fails(self):
		self.assertFailsCheck(self.row, scenario({"start": {"nodes": nodes(2, {1: {"port": 30999}})}}), "node-address")

	def test_pod_advertising_another_port_fails(self):
		s = scenario(ctl={"pods": {"lines": [cluster_line(2), pod_line(0), pod_line(1, port=31999)]}})
		self.assertFailsCheck(self.row, s, "advertised-clearAlt")

	def test_unlabelled_node_fails(self):
		self.assertFailsCheck(self.row, scenario(ctl={"nodes": {"lines": [node_line("cp", "control-plane")]}}),
			"node-label")

	def test_per_node_profile_skips(self):
		self.profile = "per-node"
		self.assertSkips(self.row, scenario(), "configuredIP with one pod per worker advertises each worker's own "
			"hostname, not one shared endpoint")


class SharedEndpointLoadBalancerTest(RowsTest):
	profile = "nlb"
	row = "every-pod-advertises-shared-endpoint-with-own-node-port"

	def test_pass(self):
		s = scenario(ctl={"pods": pods_entry(2, cluster_line(2, "networkLoadBalancer", lb=ENDPOINT)), "nodes": {"lines": [
			node_line("cp", "control-plane")]}})
		self.assertPasses(self.row, s)

	def test_other_load_balancer_address_fails(self):
		s = scenario(ctl={"pods": pods_entry(2, cluster_line(2, "networkLoadBalancer", lb="other.test"))})
		self.assertFailsCheck(self.row, s, "load-balancer-address", lbAddress="other.test")


PER_NODE_NODES = {"lines": [node_line("cp", "control-plane", "172.18.0.2"),
	node_line("w1", ip="172.18.0.3", labels={LABEL: "node-1.as-endpoint.test"}),
	node_line("w2", ip="172.18.0.4", labels={LABEL: "node-2.as-endpoint.test"})]}


class PerNodeTest(RowsTest):
	profile = "per-node"
	row = "each-pod-at-its-worker-hostname-and-host-port"
	values = {"L3_ENDPOINT_HOSTNAME": "node-1.as-endpoint.test",
		"L3_CLIENT_HOSTS": "node-1.as-endpoint.test:172.18.0.3,node-2.as-endpoint.test:172.18.0.4"}

	def pods(self, label2="node-2.as-endpoint.test"):
		return {"lines": [cluster_line(2, multi="false"),
			pod_line(0, "w1", "node-1.as-endpoint.test", 3000, 3000), pod_line(1, "w2", label2, 3000, 3000)]}

	def driver_nodes(self):
		return [node(0, "node-1.as-endpoint.test", 3000), node(1, "172.18.0.4", 3000)]

	def test_pass(self):
		self.assertPasses(self.row, scenario({"start": {"nodes": self.driver_nodes()}},
			ctl={"pods": self.pods(), "nodes": PER_NODE_NODES}))
		args = self.driver_args()[0]
		self.assertEqual("node-1.as-endpoint.test:3000", args[args.index("--seed") + 1])

	def test_shared_label_fails(self):
		nodes_entry = {"lines": [node_line("w1", labels={LABEL: "node-1.as-endpoint.test"}),
			node_line("w2", labels={LABEL: "node-1.as-endpoint.test"})]}
		self.assertFailsCheck(self.row, scenario({"start": {"nodes": self.driver_nodes()}},
			ctl={"pods": self.pods("node-1.as-endpoint.test"), "nodes": nodes_entry}), "worker-labels")

	def test_node_at_other_worker_fails(self):
		wrong = [node(0, "node-1.as-endpoint.test", 3000), node(1, "node-1.as-endpoint.test", 3000)]
		self.assertFailsCheck(self.row, scenario({"start": {"nodes": wrong}}, ctl={"pods": self.pods(),
			"nodes": PER_NODE_NODES}), "node-address")


NLB_NODES = {"by_phase": {
	"start": {"lines": [node_line("cp", "control-plane", "172.18.0.2"), node_line("w1", ip="172.18.0.3"),
		node_line("w2", ip="172.18.0.4"), node_line("w3", ip="172.18.0.5")]},
	"marked": {"lines": [node_line("cp", "control-plane", "172.18.0.2"), node_line("w1", ip="172.18.0.3"),
		node_line("w2", ip="172.18.0.4", labels={rows.NODE_MARK: rows.NODE_MARK_VALUE}), node_line("w3", ip="172.18.0.5")]},
	"registered": {"lines": [node_line("cp", "control-plane", "172.18.0.2"), node_line("w1", ip="172.18.0.3"),
		node_line("w2", ip="172.18.0.4", uid="u-w2-new"), node_line("w3", ip="172.18.0.5")]},
}}


def nlb_cluster(size=2):
	return cluster_line(size, "networkLoadBalancer", "false", lb=ENDPOINT)


class LoadBalancerPerHostTest(RowsTest):
	profile = "nlb-per-pod"
	row = "load-balancer-address-without-node-label-or-host-port"

	def pods(self, host_port=0):
		return {"lines": [nlb_cluster(), pod_line(0, "w1"), pod_line(1, "w2", host_port=host_port)]}

	def test_pass(self):
		self.assertPasses(self.row, scenario(ctl={"pods": self.pods(), "nodes": NLB_NODES}))

	def test_host_port_fails(self):
		self.assertFailsCheck(self.row, scenario(ctl={"pods": self.pods(3000), "nodes": NLB_NODES}), "no-host-port")

	def test_labelled_node_fails(self):
		labelled = {"lines": [node_line("w1", labels={LABEL: ENDPOINT}), node_line("w2")]}
		self.assertFailsCheck(self.row, scenario(ctl={"pods": self.pods(), "nodes": labelled}), "no-node-label")

	def test_nlb_profile_skips(self):
		self.profile = "nlb"
		self.assertSkips(self.row, scenario(), "needs networkLoadBalancer with one pod per host (nlb-per-pod)")


def scale_up_ctl():
	return {
		"pods": {"by_phase": {"start": pods_entry(2), "scaling": pods_entry(3)}},
		"join-watch": {"lines": ["L3-JOINED pod={1} observer={2} address=as-endpoint.test:31002 node=0A2 generation=4-5 "
			"bumpEpochMs=1000000 bumpPollMs=12 peersMaxPollMs=19 answerEpochMs=1000100 answerPollMs=14 answerMaxPollMs=60 "
			"answerAttempts=40 answerFailures=connect:20,login-closed:19 readyEpochMs=999900 readyPollMs=15 readyMaxPollMs=30 "
			"readyToAnswerMs=200 readyTransition=2026-11-13T10:00:00Z"], "phase": "scaling"},
		"wait-pod": {"lines": ["L3-POD-UP pod={1} node=0A2 nodePort=31002 ready=False"]},
		"scale": {"lines": ["L3-COMPLETED size={1}"], "phase": "start"},
	}


class ScaleUpTest(RowsTest):
	row = "scale-up-new-node-usable-within-two-tend-intervals"
	phases = {"start": {"nodes": nodes(2)}, "scaling": {"nodes": nodes(3)}}

	watch = {"servingAt": 999700, "lastClosedAt": 999690, "usableAt": 1000300, "usableMs": 600}

	def test_pass(self):
		out = self.assertPasses(self.row, scenario(self.phases, ctl=scale_up_ctl(), watch=self.watch))
		self.assertIn("command=watch %s:31002 0A2" % ENDPOINT, out)
		self.assertIn("L3-SCALE-UP-TIMES node=0A2 pod=aerocluster-0-2 advertised=%s:31002 bumpAt=1000000 answerAt=1000100 "
			"readyAt=999900 servingAt=999700 usableAt=1000300 startAt=1000100 usableMs=200 readyToAnswerMs=200 "
			"usableFromServingMs=600" % ENDPOINT, out)
		log = self.ctl_log()
		self.assertIn("join-watch aerocluster-0-2 aerocluster-0-0 --resize 3 --host %s --timeout 600" % ENDPOINT, log)
		self.assertIn("wait-pod aerocluster-0-2 --timeout 600", log)
		self.assertEqual("scale 2 --timeout 600", [c for c in log if c.startswith("scale")][-1])
		self.assertEqual(1, len(self.driver_args()))

	def test_usable_after_bound_fails(self):
		s = scenario(self.phases, ctl=scale_up_ctl(), watch=dict(self.watch, usableAt=1000700))
		self.assertFailsCheck(self.row, s, "usable-ms", usableMs=600, startAt=1000100, bumpAt=1000000, answerAt=1000100,
			readyAt=999900, servingAt=999700, usableAt=1000700, readyToAnswerMs=200, boundMs=500)
		self.assertIn("scale 2 --timeout 600", self.ctl_log())

	def test_clock_starts_at_later_bump(self):
		ctl = scale_up_ctl()
		ctl["join-watch"]["lines"] = [ctl["join-watch"]["lines"][0].replace("bumpEpochMs=1000000", "bumpEpochMs=1000250")]
		out = self.assertPasses(self.row, scenario(self.phases, ctl=ctl, watch=self.watch))
		self.assertIn("startAt=1000250 usableMs=50 ", out)

	def test_usable_before_start_fails(self):
		s = scenario(self.phases, ctl=scale_up_ctl(), watch=dict(self.watch, usableAt=1000050))
		self.assertFailsCheck(self.row, s, "usable-after-start", measurementError="true", usableMs=-50, startAt=1000100)

	def test_coarse_poll_fails(self):
		ctl = scale_up_ctl()
		ctl["join-watch"]["lines"] = [ctl["join-watch"]["lines"][0].replace("answerPollMs=14", "answerPollMs=150")]
		self.assertFailsCheck(self.row, scenario(self.phases, ctl=ctl, watch=self.watch), "poll-interval", answerPollMs=150)

	def test_first_probe_answered_fails(self):
		ctl = scale_up_ctl()
		ctl["join-watch"]["lines"] = [ctl["join-watch"]["lines"][0].replace("answerPollMs=14", "answerPollMs=none")]
		self.assertFailsCheck(self.row, scenario(self.phases, ctl=ctl, watch=self.watch), "poll-interval", answerPollMs="none")

	def test_clock_starts_at_later_answer(self):
		ctl = scale_up_ctl()
		ctl["join-watch"]["lines"] = [ctl["join-watch"]["lines"][0].replace("answerEpochMs=1000100", "answerEpochMs=1000280")]
		out = self.assertPasses(self.row, scenario(self.phases, ctl=ctl, watch=self.watch))
		self.assertIn("startAt=1000280 usableMs=20 readyToAnswerMs=380 ", out)

	def test_never_usable_fails(self):
		s = scenario(self.phases, ctl=scale_up_ctl(), watch=dict(self.watch, usableAt=None, usableMs=None))
		self.assertFailsCheck(self.row, s, "watch-usable")

	def test_existing_node_dropped_fails(self):
		phases = {"start": {"nodes": nodes(2)}, "scaling": {"nodes": nodes(3)},
			"l3-scale-up:scaling": {"nodes": [node(1), node(2)]}}
		s = scenario(phases, ctl=scale_up_ctl(), watch=self.watch)
		self.assertFailsCheck(self.row, s, "routing")

	def test_tls_profile_skips(self):
		self.profile = "tls"
		self.assertSkips(self.row, scenario(), "the TLS profiles run the endpoint and TLS rows; this row runs on default, "
			"nlb and nlb-per-pod")


def scale_down_ctl():
	return {
		"pods": {"by_phase": {"start": pods_entry(2), "down": pods_entry(1)}},
		"scale": {"by_arg": {"1": {"lines": ["L3-COMPLETED size=1"], "phase": "down"},
			"2": {"lines": ["L3-COMPLETED size=2"], "phase": "start"}}},
	}


class ScaleDownTest(RowsTest):
	row = "scale-down-removed-node-leaves-without-retry-storm"

	def test_pass(self):
		out = self.assertPasses(self.row, scenario({"start": {"nodes": nodes(2)}, "down": {"nodes": nodes(1)}},
			ctl=scale_down_ctl()))
		self.assertIn("L3-CHECK check=flat-after-removal", out)
		self.assertEqual(["scale 1 --timeout 600", "scale 2 --timeout 600"], [c for c in self.ctl_log() if c.startswith("scale")])

	def test_failures_after_removal_fail(self):
		s = scenario({"start": {"nodes": nodes(2)}, "down": {"nodes": nodes(1), "fail": 1}}, ctl=scale_down_ctl())
		self.assertFailsCheck(self.row, s, "flat-after-removal")

	def test_removed_node_kept_fails(self):
		s = scenario({"start": {"nodes": nodes(2)}, "down": {"nodes": nodes(2)}}, ctl=scale_down_ctl())
		self.assertFailsCheck(self.row, s, "removed", node="0A1")


def stability_ctl(rescheduled_port="31000"):
	return {
		"pods": {"by_phase": {"start": pods_entry(2), "down": pods_entry(1)}},
		"node-port-range": {"lines": ["L3-NODE-PORT-RANGE first=30000 last=32767"]},
		"node-ports": {"lines": ["L3-NODE-PORTS ports=31000,31001,32767"]},
		"service-create": {"lines": ["L3-SERVICE name={1} uid=x-{1} nodePort={3}"]},
		"service": {"by_arg": {
			"aerocluster-pinned-seed": {"lines": ["L3-SERVICE name={1} uid=pin nodePort=32766"]},
			"aerocluster-0-0": [{"lines": ["L3-SERVICE name={1} uid=s0 nodePort=31000"]},
				{"lines": ["L3-SERVICE name={1} uid=s0 nodePort=%s" % rescheduled_port]}],
			"aerocluster-0-1": [{"lines": ["L3-SERVICE name={1} uid=s1 nodePort=31001"]},
				{"lines": ["L3-SERVICE name={1} uid= nodePort="]},
				{"lines": ["L3-SERVICE name={1} uid=s1b nodePort=31005"]}],
		}},
		"delete-pod": {"lines": ["L3-POD-DELETED pod={1} uid=p0"]},
		"wait-ready": {"lines": ["L3-POD-READY pod={1} uid=p0b oldUid=p0 worker=cp ip=10.244.0.20"]},
		"scale": {"by_arg": {"1": {"lines": ["L3-COMPLETED size=1"], "phase": "down"},
			"2": {"lines": ["L3-COMPLETED size=2"], "phase": "start"}}},
		"service-delete": {"lines": ["L3-SERVICE-DELETED name={1}"]},
	}


class NodePortStabilityTest(RowsTest):
	row = "node-ports-stable-across-reschedule-and-recreate"

	def test_pass(self):
		self.assertPasses(self.row, scenario(ctl=stability_ctl()))
		log = self.ctl_log()
		self.assertIn("service-create aerocluster-pinned-seed aerocluster-0-0 32766", log)
		self.assertIn("service-create aerocluster-held-node-port aerocluster-0-1 31001", log)
		self.assertIn("service-delete aerocluster-pinned-seed", log)
		seeds = [a[a.index("--seed") + 1] for a in self.driver_args()]
		self.assertEqual(["%s:32766" % ENDPOINT] * 3, seeds)

	def test_rescheduled_service_new_port_fails(self):
		self.assertFailsCheck(self.row, scenario(ctl=stability_ctl("31007")), "rescheduled-service-kept")
		self.assertIn("service-delete aerocluster-pinned-seed", self.ctl_log())

	def test_pinned_port_advertised_fails(self):
		s = scenario(ctl=dict(stability_ctl(), pods={"lines": [cluster_line(2), pod_line(0), pod_line(1, port=32766)]}))
		self.assertFailsCheck(self.row, s, "advertised-clearAlt")


def reschedule_ctl(port=None):
	return {
		"pods": {"by_phase": {"start": {"lines": [nlb_cluster(), pod_line(0, "w1"), pod_line(1, "w2")]},
			"rescheduled": {"lines": [nlb_cluster(), pod_line(0, "w1"), pod_line(1, "w3", uid="p1b", port=port)]}}},
		"delete-pod": {"lines": ["L3-POD-DELETED pod={1} uid=p1"], "phase": "rescheduled"},
		"wait-ready": {"lines": ["L3-POD-READY pod={1} uid=p1b oldUid=p1 worker=w3 ip=10.244.0.30"]},
	}


class RescheduleTest(RowsTest):
	profile = "nlb-per-pod"
	row = "rescheduled-pod-keeps-address-and-node-port"

	def test_pass(self):
		self.assertPasses(self.row, scenario(ctl=reschedule_ctl()))
		self.assertIn("delete-pod aerocluster-0-1", self.ctl_log())
		self.assertEqual(1, len(self.driver_args()))

	def test_new_advertised_port_fails(self):
		self.assertFailsCheck(self.row, scenario(ctl=reschedule_ctl(31009)), "advertised-clearAlt")

	def test_driver_left_without_nodes_fails(self):
		ctl = reschedule_ctl()
		ctl["delete-pod"] = dict(ctl["delete-pod"], phase="empty")
		ctl["wait-ready"] = dict(ctl["wait-ready"], phase="rescheduled")
		ctl["pods"]["by_phase"]["empty"] = ctl["pods"]["by_phase"]["start"]
		self.assertFailsCheck(self.row, scenario({"start": {"nodes": nodes(2)}, "empty": {"nodes": []},
			"rescheduled": {"nodes": nodes(2)}}, ctl=ctl), "never-empty")

	def test_default_profile_skips(self):
		self.profile = "default"
		self.assertSkips(self.row, scenario(), "needs alternateAccess networkLoadBalancer (nlb or nlb-per-pod)")


def recreated_ctl(replacement_worker="w2"):
	pods = {"lines": [nlb_cluster(), pod_line(0, "w1"), pod_line(1, "w2")]}
	return {
		"pods": {"by_phase": {"start": pods, "marked": pods,
			"registered": {"lines": [nlb_cluster(), pod_line(0, "w1"), pod_line(1, replacement_worker, uid="p1b")]}}},
		"nodes": NLB_NODES,
		"label-node": {"lines": ["L3-LABELLED node={1} {2}={3}"], "phase": "marked"},
		"cordon-others": {"lines": ["L3-CORDONED node=cp", "L3-CORDONED node=w1", "L3-CORDONED node=w3"]},
		"delete-node": {"lines": ["L3-NODE-DELETED node={1} uid=u-w2 pods=aerocluster-0-1"]},
		"reregister": {"lines": ["L3-KUBELET-RESTARTED node={1}", "L3-NODE-REGISTERED node={1} uid=u-w2-new afterS=20"],
			"phase": "registered"},
		"wait-ready": {"lines": ["L3-POD-READY pod={1} uid=p1b oldUid=p1 worker=%s ip=10.244.0.40" % replacement_worker]},
		"uncordon": {"lines": ["L3-UNCORDONED node=cp"]},
	}


class RecreatedNodeTest(RowsTest):
	profile = "nlb-per-pod"
	row = "pod-on-recreated-node-advertises-cluster-address"

	def test_pass(self):
		self.assertPasses(self.row, scenario(ctl=recreated_ctl()))
		log = self.ctl_log()
		self.assertIn("label-node w2 %s %s" % (rows.NODE_MARK, rows.NODE_MARK_VALUE), log)
		self.assertIn("cordon-others w2", log)
		self.assertIn("delete-node w2", log)
		self.assertIn("uncordon cp w1 w3", log)
		self.assertLess(log.index("cordon-others w2"), log.index("delete-node w2"))

	def test_replacement_on_another_node_fails_and_uncordons(self):
		self.assertFailsCheck(self.row, scenario(ctl=recreated_ctl("w3")), "replacement-on-node", worker="w3", expected="w2")
		self.assertIn("uncordon cp w1 w3", self.ctl_log())

	def test_labels_kept_fail(self):
		ctl = recreated_ctl()
		ctl["nodes"] = {"by_phase": dict(NLB_NODES["by_phase"], registered=NLB_NODES["by_phase"]["marked"])}
		self.assertFailsCheck(self.row, scenario(ctl=ctl), "new-node-uid")

	def test_no_pod_away_from_endpoint_fails(self):
		ctl = recreated_ctl()
		ctl["pods"] = {"lines": [nlb_cluster(), pod_line(0, "cp"), pod_line(1, "cp")]}
		self.assertFailsCheck(self.row, scenario(ctl=ctl), "pod-away-from-endpoint")
		self.assertNotIn("delete-node", " ".join(self.ctl_log()))

	def test_nlb_profile_skips(self):
		self.profile = "nlb"
		self.assertSkips(self.row, scenario(), "needs a pod on a worker the endpoint hostname does not resolve to; every pod "
			"runs on one kind node (nlb-per-pod)")


def tls_ctl(cluster=None, tls_host=ENDPOINT, sans="DNS:aerospike-tls", host=ENDPOINT):
	cluster = cluster or cluster_line(2, tls=TLS_NAME)
	return {
		"pods": {"lines": [cluster] + [pod_line(i, host=host, tls=tls_host) for i in range(2)]},
		"certificate": {"lines": ["L3-CERT secret=aerospike-tls-secret file=/etc/aerospike/tls/server.crt "
			"subject=CN=aerospike-tls sans=%s" % sans]},
	}


class TlsCertificateTest(RowsTest):
	profile = "tls"
	row = "shared-certificate-names-tls-name-not-endpoint"
	values = {"L3_TLS_NAME": TLS_NAME, "L3_TLS_CA_FILE": "/state/tls/ca.crt", "L3_TLS_ENDPOINT_HOSTNAME": ENDPOINT}

	def test_pass_without_driver(self):
		self.assertPasses(self.row, scenario(ctl=tls_ctl()))
		self.assertFalse(os.path.exists(os.path.join(self.state, "driver-args")))

	def test_endpoint_san_fails(self):
		self.assertFailsCheck(self.row, scenario(ctl=tls_ctl(sans="DNS:aerospike-tls,DNS:as-endpoint.test")),
			"certificate-sans")

	def test_wrong_tls_address_fails(self):
		self.assertFailsCheck(self.row, scenario(ctl=tls_ctl(tls_host="other.test")), "advertised-tlsAlt")

	def test_default_profile_skips(self):
		self.profile = "default"
		self.assertSkips(self.row, scenario(), "needs a cluster that serves TLS (tls or nlb-tls)")


class TlsCertificateLoadBalancerTest(RowsTest):
	profile = "nlb-tls"
	row = "shared-certificate-names-tls-name-not-endpoint"
	values = {"L3_TLS_NAME": TLS_NAME, "L3_TLS_CA_FILE": "/state/tls/ca.crt", "L3_TLS_ENDPOINT_HOSTNAME": TLS_ENDPOINT,
		"L3_CLIENT_HOSTS": "%s:%s,%s:%s" % (ENDPOINT, ENDPOINT_IP, TLS_ENDPOINT, ENDPOINT_IP)}

	def cluster(self, lb_tls=TLS_ENDPOINT):
		return cluster_line(2, "networkLoadBalancer", lb=ENDPOINT, lb_tls=lb_tls, tls=TLS_NAME)

	def test_pass_with_distinct_tls_address(self):
		self.assertPasses(self.row, scenario(ctl=tls_ctl(self.cluster(), TLS_ENDPOINT)))

	def test_tls_address_not_from_cluster_fails(self):
		self.assertFailsCheck(self.row, scenario(ctl=tls_ctl(self.cluster(ENDPOINT), ENDPOINT)), "load-balancer-tls-address")


class TlsNameTest(RowsTest):
	profile = "tls"
	row = "tls-name-holds-every-node-endpoint-name-not-created"
	values = {"L3_TLS_NAME": TLS_NAME, "L3_TLS_CA_FILE": "/state/tls/ca.crt", "L3_TLS_ENDPOINT_HOSTNAME": ENDPOINT}
	phases = {"start": {"nodes": nodes(2)}, "l3-tls-name:start": {"nodes": [node(0, port=TLS_PORTS[0]),
		node(1, port=TLS_PORTS[1])]}}
	not_created = {"l3-tls-endpoint-name": "TLS handshake failed: no name matching as-endpoint.test"}

	def test_pass(self):
		self.assertPasses(self.row, scenario(self.phases, contract=4, ctl=tls_ctl(), not_created=self.not_created))
		args = [a for a in self.driver_args() if "--tls-name" in a]
		self.assertEqual(2, len(args))
		self.assertEqual("%s:%d" % (ENDPOINT, TLS_PORTS[0]), args[0][args[0].index("--seed") + 1])
		self.assertEqual(TLS_NAME, args[0][args[0].index("--tls-name") + 1])
		self.assertEqual("/state/tls/ca.crt", args[0][args[0].index("--tls-ca-file") + 1])
		self.assertEqual(ENDPOINT, args[1][args[1].index("--tls-name") + 1])

	def test_endpoint_name_creates_client_fails(self):
		self.assertFailsCheck(self.row, scenario(self.phases, contract=5, ctl=tls_ctl()), "endpoint-name-client-not-created")

	def test_node_at_clear_port_fails(self):
		self.assertFailsCheck(self.row, scenario({"start": {"nodes": nodes(2)}}, contract=4, ctl=tls_ctl(),
			not_created=self.not_created), "node-address")

	def test_contract_3_driver_skips(self):
		out = self.assertSkips(self.row, scenario(self.phases, contract=3, ctl=tls_ctl()),
			"the driver speaks contract 3; this row needs 4 (--tls-name and --tls-ca-file)")
		self.assertFalse([a for a in self.driver_args() if "--tls-name" in a], out)


class PlanTest(unittest.TestCase):
	def test_every_profile_lists_every_row(self):
		for profile in rows.PROFILES:
			out = io.StringIO()
			with contextlib.redirect_stdout(out):
				self.assertEqual(0, rows.main(["--plan", "--profile", profile]))
			lines = out.getvalue().splitlines()
			self.assertEqual([r.name for r in rows.ROWS], [line.split("row=")[1].split()[0] for line in lines])

	def test_unknown_row_is_usage_error(self):
		out = io.StringIO()
		with contextlib.redirect_stdout(out):
			self.assertEqual(2, rows.main(["--profile", "default", "--rows", "nope", "--driver", "x"]))


if __name__ == "__main__":
	unittest.main()
