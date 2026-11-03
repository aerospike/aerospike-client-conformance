#!/usr/bin/env python3
#
# Offline tests of rows.py: a scripted fake driver and a stub l5ctl, sharing a state directory. The
# stub prints the scripted L5-* lines for each call and can move the fake cluster to a new phase; the
# fake driver emits the snapshots of the current phase, advancing its t faster than the wall clock,
# and answers commands. Each row has a passing run, and a run per assertion that breaks only it.
#
# Usage: python3 -m unittest l5-aws/test_rows.py -v

import contextlib
import copy
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rows  # noqa: E402

FAKE_DRIVER = r'''
import json, os, sys, threading, time
state = os.environ["FAKE_STATE"]
spec = json.load(open(os.path.join(state, "scenario.json")))["driver"]
args = sys.argv[1:]
with open(os.path.join(state, "driver-args"), "a") as f:
	f.write(json.dumps(args) + "\n")
shape = args[args.index("--shape") + 1]
commands = []

def read_commands():
	for line in sys.stdin:
		commands.append(line.strip())
	commands.append("quit")

def out(obj):
	sys.stdout.write((obj if isinstance(obj, str) else json.dumps(obj)) + "\n")
	sys.stdout.flush()

def phase():
	try:
		with open(os.path.join(state, "phase")) as f:
			return f.read().strip()
	except OSError:
		return "start"

threading.Thread(target=read_commands, daemon=True).start()
for raw in spec.get("raw", []):
	out(raw)
if "exit_before_ready" in spec:
	out({"type": "error", "t": 0, "message": "client not created"})
	sys.exit(spec["exit_before_ready"])
if spec.get("ready", True):
	out(dict({"type": "ready", "t": 0, "contract": 1, "epochMs": 0, "client": "fake", "clientVersion": "0",
		"shape": shape}, **spec.get("ready_fields", {})))
t = 0
counters = {"succeeded": 0, "failed": 0, "clientErrors": 0, "clientTimeouts": 0}
watch = None
entered = None
while True:
	time.sleep(0.002)
	t += spec.get("step_ms", 100)
	name = phase()
	ph = spec["phases"].get(shape + ":" + name) or spec["phases"].get(name) or spec["phases"]["start"]
	first = entered != name
	entered = name
	counters["succeeded"] += ph.get("succeed", 10)
	counters["failed"] += ph.get("fail", 0) + (ph.get("fail_first", 0) if first else 0)
	counters["clientErrors"] += ph.get("errors", 0)
	snapshot = {"type": "snapshot", "t": t, "reason": "change" if first else "heartbeat", "nodes": ph["nodes"],
		"unowned": 0, "owners": {n["name"]: 1 for n in ph["nodes"]}, "longestUnownedMs": 0, "maxLatencyMs": 5,
		"lastError": None}
	snapshot.update(counters)
	snapshot.update(ph.get("fields", {}))
	for key in ph.get("omit", []):
		snapshot.pop(key, None)
	out(snapshot)
	if first:
		for message in ph.get("logs", []):
			out({"type": "log", "t": t, "level": "WARN", "message": message})
	while commands:
		words = commands.pop(0).split()
		if words[0] == "quit":
			if spec.get("ignore_quit"):
				continue
			sys.exit(spec.get("quit_exit", 0))
		elif words[0] == "serve":
			out(dict({"type": "served", "t": t, "step": words[1], "partitions": 4096, "written": 4096, "read": 4096,
				"mismatched": 0, "failures": 0, "firstFailure": None, "elapsedMs": 3000}, **spec.get("served", {})))
		elif words[0] == "reset":
			out({"type": "reset", "t": t})
		elif words[0] == "watch":
			watch = words[1:]
		else:
			out({"type": "error", "t": t, "message": "unknown command: " + " ".join(words)})
	if watch and name in spec.get("watch_phases", ["listener"]):
		out(dict({"type": "watch", "t": t, "address": watch[0], "node": watch[1], "servingAt": t - 60,
			"lastClosedAt": t - 70, "usableAt": t - 10, "usableMs": 50, "ownershipChecked": True}, **spec.get("watch", {})))
		watch = None
'''

STUB_L5CTL = r'''
import json, os, sys
state = os.environ["FAKE_STATE"]
spec = json.load(open(os.path.join(state, "scenario.json")))["ctl"]
args = sys.argv[1:]
with open(os.path.join(state, "ctl.log"), "a") as f:
	f.write(" ".join(args) + "\n")
key = " ".join([args[0]] + [a for a in args[1:] if a in ("--wait", "--prune")])
entries = spec.get(key, spec.get(args[0], {}))
entries = entries if isinstance(entries, list) else [entries]
count = os.path.join(state, "count-" + key.replace(" ", "_"))
n = int(open(count).read()) if os.path.exists(count) else 0
with open(count, "w") as f:
	f.write(str(n + 1))
entry = entries[min(n, len(entries) - 1)]
entry = entry.get("by_arg", {}).get(args[1] if len(args) > 1 else "", entry)
for line in entry.get("lines", []):
	print(line.format(*args))
if "phase" in entry:
	with open(os.path.join(state, "phase"), "w") as f:
		f.write(entry["phase"])
if "stderr" in entry:
	print(entry["stderr"], file=sys.stderr)
sys.exit(entry.get("exit", 0))
'''

ENDPOINT = "127.0.0.1"
PODS = [("aerospike-0-0", "zone-a", "A1", 31001), ("aerospike-1-0", "zone-b", "B1", 31002),
	("aerospike-2-0", "zone-c", "C1", 31003)]


def free_port():
	with socket.socket() as s:
		s.bind((ENDPOINT, 0))
		return s.getsockname()[1]


def node(name, port, address="10.0.0.1", host=ENDPOINT, active=True, reachable=True):
	return {"name": name, "host": host, "port": port, "address": address, "active": active, "reachable": reachable}


def three_nodes(**changes):
	"""The three pods' nodes, with changes by node name."""
	return [dict(node(n, p), **changes.get(n, {})) for _, _, n, p in PODS]


def pods_entry(pods=PODS, size=None):
	return {"lines": ["L5-CLUSTER size=%d phase=Completed" % (len(pods) if size is None else size)] +
		["L5-POD pod=%s phase=Running uid=u-%s zone=%s worker=w instance=i-%s nodeId=%s node=%s nodePort=%d" %
			(pod, pod, zone, pod, name, name, port) for pod, zone, name, port in pods]}


def base_ctl():
	return {
		"wait-ready": {"lines": ["L5-READY size=3"]},
		"wait-healthy": {"lines": ["L5-HEALTHY all listeners"]},
		"pods": pods_entry(),
		"verify": {"lines": ["L5-VERIFY PASS"]},
		"resolve-check": {"lines": ["L5-RESOLVE where=consumer name=x addresses=127.0.0.1",
			"L5-RESOLVE where=producer name=x resolves={2}"]},
		"scale": {"lines": ["L5-SCALE size={1}"]},
	}


def w1():
	return {"ctl": base_ctl(), "driver": {"phases": {"start": {"nodes": three_nodes()}}}}


def w2():
	ctl = base_ctl()
	ctl["spot-interrupt --wait"] = {"phase": "spot", "lines": ["L5-SPOT pod={1} node=w instance=i-old",
		"L5-FIS experiment=E1 status=completed",
		"L5-REPLACED pod={1} oldWorker=w oldInstance=i-old newWorker=w2 newInstance=i-new"]}
	ctl["rollout --wait"] = {"phase": "rollout", "lines": ["L5-ROLLOUT nodegroup=ng done"] +
		["L5-REPLACED pod=%s oldWorker=w oldInstance=i-%s newWorker=w2 newInstance=i-new-%s" % (p, p, p)
			for p, _, _, _ in PODS]}
	nodes = three_nodes()
	return {"ctl": ctl, "driver": {"phases": {"start": {"nodes": nodes}, "spot": {"nodes": nodes},
		"rollout": {"nodes": nodes}}}}


def w3(new_port):
	ctl = base_ctl()
	ctl["scale"] = [{"lines": ["L5-SCALE size={1}"], "phase": "scaled"},
		{"lines": ["L5-SCALE size={1}"], "phase": "restored"}]
	ctl["pending"] = [{"lines": []}, {"lines": ["L5-PENDING pod=aerospike-0-1 rack=1 zone=zone-a"]}]
	ctl["add-node --wait"] = {"lines": ["L5-NODES az={1} nodegroup=ng-a desired=2 update=u1"]}
	ctl["set-nodes --wait"] = {"lines": ["L5-NODES az={1} nodegroup=ng-a desired={2} update=u2"]}
	ctl["wait-advertised"] = {"phase": "advertised", "lines": [
		"L5-ADVERTISED pod={1} node=A2 nodePort=%d address=127.0.0.1:%d by=aerospike-1-0" % (new_port, new_port)]}
	ctl["sync-listeners"] = {"phase": "listener", "lines": ["L5-LISTENER added port=%d tg=tg" % new_port]}
	ctl["sync-listeners --prune"] = {"lines": ["L5-LISTENER removed port=%d" % new_port]}
	nodes = three_nodes()
	four = nodes + [node("A2", new_port)]
	return {"ctl": ctl, "driver": {"phases": {"start": {"nodes": nodes}, "scaled": {"nodes": nodes},
		"advertised": {"nodes": nodes, "logs": ["peer A2 127.0.0.1:%d connection refused" % new_port]},
		"listener": {"nodes": four}, "restored": {"nodes": nodes}}}}


def w5():
	ctl = base_ctl()
	ctl["endpoint-zone"] = {"by_arg": {"10.0.0.1": {"lines": ["L5-ENDPOINT-ZONE address={1} zone=zone-b"]},
		"10.0.0.2": {"lines": ["L5-ENDPOINT-ZONE address={1} zone=zone-c"]}}}
	ctl["az-outage"] = {"phase": "outage", "lines": ["L5-AZ-OUTAGE az={1} duration={3} producer-subnet=p endpoint-subnet=c",
		"L5-FIS action=az-outage experiment=E5 template=T5", "L5-FIS experiment=E5 status=running"]}
	ctl["experiment"] = [{"lines": ["L5-FIS experiment={1} status=running"]}] * 20 + [
		{"lines": ["L5-FIS experiment={1} status=completed"], "phase": "after"}]
	ctl["experiment --wait"] = {"lines": ["L5-FIS experiment={1} status=completed"]}
	nodes = three_nodes(C1={"address": "10.0.0.2"})
	outage = three_nodes(C1={"address": "10.0.0.2"}, B1={"active": False, "reachable": False})
	return {"ctl": ctl, "driver": {"step_ms": 1000, "phases": {"start": {"nodes": nodes},
		"outage": {"nodes": outage, "fields": {"unowned": 0, "longestUnownedMs": 12000, "maxLatencyMs": 21000}},
		"after": {"nodes": nodes}}}}


class RowsTest(unittest.TestCase):
	def setUp(self):
		self.dir = tempfile.TemporaryDirectory()
		self.state = os.path.join(self.dir.name, "state")
		os.mkdir(self.state)
		self.ctl = os.path.join(self.dir.name, "l5ctl")
		with open(self.ctl, "w") as f:
			f.write("#!%s\n%s" % (sys.executable, STUB_L5CTL))
		os.chmod(self.ctl, 0o755)
		self.fake = os.path.join(self.dir.name, "fake_driver.py")
		with open(self.fake, "w") as f:
			f.write(FAKE_DRIVER)
		self.env = {"L5_CONTROL": "none", "L5_ENDPOINT_NAME": ENDPOINT, "L5_SEED_PORT": "31001", "L5_CTL": self.ctl,
			"L5_KUBECONFIG": "/dev/null", "AEROSPIKE_ADMIN_PASSWORD": "secret", "L5_NODEGROUPS": "zone-a=ng-a",
			"L5_RUNNER_AZ": "zone-a", "L5_STACK": "l5-aws-20261103-000000", "AWS_REGION": "us-east-2",
			"FAKE_STATE": self.state}

	def tearDown(self):
		self.dir.cleanup()

	def run_row(self, row, scenario, **env):
		with open(os.path.join(self.state, "scenario.json"), "w") as f:
			json.dump(scenario, f)
		out = io.StringIO()
		environ = dict(self.env, **env)
		with mock.patch.dict(os.environ, environ), contextlib.redirect_stdout(out):
			code = rows.main(["--row", row, "--driver", "%s %s" % (sys.executable, self.fake)])
		self.output = out.getvalue()
		return code, self.output

	def ctl_log(self):
		with open(os.path.join(self.state, "ctl.log")) as f:
			return f.read().splitlines()

	def driver_args(self):
		with open(os.path.join(self.state, "driver-args")) as f:
			return [json.loads(line) for line in f]

	def assertPasses(self, code, out):
		self.assertEqual(0, code, out)
		self.assertNotIn("outcome=fail", out)

	def assertFailsCheck(self, code, out, name, **fields):
		self.assertEqual(1, code, out)
		failed = [line for line in out.splitlines() if line.startswith("L5-CHECK check=%s " % name) and
			line.endswith("outcome=fail")]
		self.assertTrue(failed, "no failed check %s in:\n%s" % (name, out))
		for key, value in fields.items():
			self.assertIn("%s=%s" % (key, value), failed[0])
		others = [line for line in out.splitlines() if line.startswith("L5-CHECK") and line.endswith("outcome=fail") and
			not line.startswith("L5-CHECK check=%s " % name)]
		self.assertEqual([], others)
		self.assertIn("L5-FAILED kind=assertion reason=%s: " % name, out)

	def assertContract(self, code, out, reason):
		self.assertEqual(3, code, out)
		self.assertIn("L5-FAILED kind=contract", out)
		self.assertIn(reason, out)


class UsageTest(RowsTest):
	def test_missing_variable_is_named(self):
		self.env["L5_ENDPOINT_NAME"] = ""
		self.env["L5_RUNNER_AZ"] = ""
		code, out = self.run_row("W-1", w1())
		self.assertEqual(2, code)
		self.assertIn("L5-ENVIRONMENT missing=L5_ENDPOINT_NAME,L5_RUNNER_AZ", out)

	def test_unknown_row(self):
		with contextlib.redirect_stderr(io.StringIO()):
			self.assertEqual(2, rows.main(["--row", "W-4", "--driver", "true"]))

	def test_bad_control(self):
		code, out = self.run_row("W-1", w1(), L5_CONTROL="maybe")
		self.assertEqual(2, code)
		self.assertIn("L5_CONTROL must be none or resolvable", out)


class W1Test(RowsTest):
	def test_passes(self):
		code, out = self.run_row("W-1", w1())
		self.assertPasses(code, out)
		args = self.driver_args()
		self.assertEqual(2, len(args))
		for flags, shape in zip(args, ("pass-through", "port-map")):
			text = " ".join(flags)
			self.assertIn("--seed 127.0.0.1:31001 --shape %s" % shape, text)
			self.assertIn("--user admin --tend-interval 250 --timeout 1000 --namespace test --set l5-shapes", text)
		self.assertNotIn("--port-map", args[0])
		self.assertIn("--port-map A1=127.0.0.1:31001,B1=127.0.0.1:31002,C1=127.0.0.1:31003 --on-miss strict",
			" ".join(args[1]))
		log = self.ctl_log()
		self.assertIn("verify", log)
		self.assertIn("resolve-check --expect-producer no", log)
		self.assertEqual(2, out.count("L5-CHECK check=serve "))

	def test_passes_resolvable_control(self):
		code, out = self.run_row("W-1", w1(), L5_CONTROL="resolvable")
		self.assertPasses(code, out)
		self.assertIn("resolve-check --expect-producer yes", self.ctl_log())

	def test_cluster_size(self):
		s = w1()
		s["ctl"]["pods"] = pods_entry(PODS[:2])
		self.assertFailsCheck(*self.run_row("W-1", s), "cluster-size")

	def test_routing_unreachable_node(self):
		s = w1()
		s["driver"]["phases"]["start"]["nodes"] = three_nodes(B1={"reachable": False})
		self.assertFailsCheck(*self.run_row("W-1", s), "routing", step="pass-through", boundMs=60000)

	def test_routing_inactive_node(self):
		s = w1()
		s["driver"]["phases"]["start"]["nodes"] = three_nodes(C1={"active": False})
		self.assertFailsCheck(*self.run_row("W-1", s), "routing", step="pass-through")

	def test_routing_missing_node(self):
		s = w1()
		s["driver"]["phases"]["port-map:start"] = {"nodes": three_nodes()[:2]}
		self.assertFailsCheck(*self.run_row("W-1", s), "routing", step="port-map")

	def test_routing_unowned(self):
		s = w1()
		s["driver"]["phases"]["start"]["fields"] = {"unowned": 12, "longestUnownedMs": 900}
		self.assertFailsCheck(*self.run_row("W-1", s), "routing", step="pass-through")

	def test_node_port(self):
		s = w1()
		s["driver"]["phases"]["port-map:start"] = {"nodes": three_nodes(B1={"port": 31009})}
		self.assertFailsCheck(*self.run_row("W-1", s), "node-port", step="port-map")

	def test_node_host(self):
		s = w1()
		s["driver"]["phases"]["start"]["nodes"] = three_nodes(A1={"host": "10.9.9.9"})
		self.assertFailsCheck(*self.run_row("W-1", s), "node-host", step="pass-through")

	def test_serve_failures(self):
		s = w1()
		s["driver"]["served"] = {"read": 4095, "failures": 1, "firstFailure": "timeout"}
		self.assertFailsCheck(*self.run_row("W-1", s), "serve", step="pass-through", failures=1)

	def test_serve_mismatched(self):
		s = w1()
		s["driver"]["served"] = {"read": 4095, "mismatched": 1}
		self.assertFailsCheck(*self.run_row("W-1", s), "serve", mismatched=1)

	def test_serve_not_every_partition(self):
		s = w1()
		s["driver"]["served"] = {"partitions": 4000, "written": 4000, "read": 4000}
		self.assertFailsCheck(*self.run_row("W-1", s), "serve", written=4000)

	def test_verify(self):
		s = w1()
		s["ctl"]["verify"] = {"exit": 1, "stderr": "ERROR: port 31002 reaches the wrong pod"}
		self.assertFailsCheck(*self.run_row("W-1", s), "verify")

	def test_resolve_check(self):
		s = w1()
		s["ctl"]["resolve-check"] = {"exit": 1, "stderr": "ERROR: expected the producer VPC to resolve it: no, got: yes"}
		self.assertFailsCheck(*self.run_row("W-1", s), "resolve-check")


class ContractTest(RowsTest):
	def test_non_json_line(self):
		s = w1()
		s["driver"]["raw"] = ["starting client"]
		self.assertContract(*self.run_row("W-1", s), "non-JSON line on stdout: starting client")

	def test_missing_required_field(self):
		s = w1()
		s["driver"]["phases"]["start"]["omit"] = ["failed"]
		self.assertContract(*self.run_row("W-1", s), "snapshot without failed")

	def test_missing_node_field(self):
		s = w1()
		nodes = three_nodes()
		del nodes[0]["reachable"]
		s["driver"]["phases"]["start"]["nodes"] = nodes
		self.assertContract(*self.run_row("W-1", s), "snapshot without nodes[].reachable")

	def test_unowned_without_longest(self):
		s = w1()
		s["driver"]["phases"]["start"]["omit"] = ["longestUnownedMs"]
		self.assertContract(*self.run_row("W-1", s), "snapshot without longestUnownedMs")

	def test_wrong_contract(self):
		s = w1()
		s["driver"]["ready_fields"] = {"contract": 2}
		self.assertContract(*self.run_row("W-1", s), "ready with contract 2")

	def test_no_ready(self):
		s = w1()
		s["driver"]["ready"] = False
		self.assertContract(*self.run_row("W-1", s), "no ready within 60000 ms")

	def test_non_zero_exit_after_quit(self):
		s = w1()
		s["driver"]["quit_exit"] = 1
		self.assertContract(*self.run_row("W-1", s), "driver exited with status 1 after quit")

	def test_client_not_created_is_a_failure(self):
		s = w1()
		s["driver"]["exit_before_ready"] = 3
		code, out = self.run_row("W-1", s)
		self.assertEqual(1, code, out)
		self.assertIn("client-created: driver exited 3", out)


class W2Test(RowsTest):
	def test_passes(self):
		code, out = self.run_row("W-2", w2())
		self.assertPasses(code, out)
		log = self.ctl_log()
		self.assertIn("spot-interrupt aerospike-0-0 --wait", log)
		self.assertIn("rollout all --wait", log)
		self.assertLess(log.index("spot-interrupt aerospike-0-0 --wait"), log.index("rollout all --wait"))
		self.assertEqual(2, log.count("verify"))
		self.assertIn("L5-REPLACED pod=aerospike-0-0", out)
		self.assertIn("L5-RECOVERY step=spot-interrupt", out)
		self.assertIn("L5-RECOVERY step=rollout", out)
		self.assertIn("L5-CHECK check=quiet step=rollout windowMs=1250 boundMs=60000", out)

	def test_spot_interrupt(self):
		s = w2()
		s["ctl"]["spot-interrupt --wait"] = {"exit": 2, "stderr": "ERROR: FIS experiment failed"}
		self.assertFailsCheck(*self.run_row("W-2", s), "spot-interrupt")

	def test_spot_not_replaced(self):
		s = w2()
		s["ctl"]["spot-interrupt --wait"]["lines"][-1] = \
			"L5-REPLACED pod={1} oldWorker=w oldInstance=i-old newWorker=w newInstance=i-old"
		self.assertFailsCheck(*self.run_row("W-2", s), "replaced", step="spot-interrupt")

	def test_rollout_not_replaced(self):
		s = w2()
		s["ctl"]["rollout --wait"]["lines"].pop()
		self.assertFailsCheck(*self.run_row("W-2", s), "replaced", step="rollout")

	def test_wait_healthy(self):
		s = w2()
		s["ctl"]["wait-healthy"] = [{"lines": ["L5-HEALTHY all listeners"]}, {"exit": 2, "stderr": "ERROR: no healthy"}]
		self.assertFailsCheck(*self.run_row("W-2", s), "wait-healthy")

	def test_routing_after_spot(self):
		s = w2()
		s["driver"]["phases"]["spot"]["nodes"] = three_nodes(A1={"reachable": False})
		self.assertFailsCheck(*self.run_row("W-2", s), "routing", step="spot-interrupt", boundMs=60000)

	def test_verify_after_rollout(self):
		s = w2()
		s["ctl"]["verify"] = [{"lines": ["L5-VERIFY PASS"]}, {"exit": 1}]
		self.assertFailsCheck(*self.run_row("W-2", s), "verify")
		self.assertIn("rollout all --wait", self.ctl_log())

	def test_node_port_after_rollout(self):
		s = w2()
		s["driver"]["phases"]["rollout"]["nodes"] = three_nodes(C1={"port": 31009})
		self.assertFailsCheck(*self.run_row("W-2", s), "node-port", step="rollout")

	def test_node_host_after_spot(self):
		s = w2()
		s["driver"]["phases"]["spot"]["nodes"] = three_nodes(A1={"host": "10.9.9.9"})
		self.assertFailsCheck(*self.run_row("W-2", s), "node-host", step="spot-interrupt")

	def test_quiet_failed_grows(self):
		s = w2()
		s["driver"]["phases"]["spot"]["fail"] = 1
		self.assertFailsCheck(*self.run_row("W-2", s), "quiet", step="spot-interrupt", boundMs=60000)

	def test_quiet_client_errors_grow(self):
		s = w2()
		s["driver"]["phases"]["rollout"]["errors"] = 1
		self.assertFailsCheck(*self.run_row("W-2", s), "quiet", step="rollout")

	def test_quiet_nothing_succeeds(self):
		s = w2()
		s["driver"]["phases"]["rollout"]["succeed"] = 0
		self.assertFailsCheck(*self.run_row("W-2", s), "quiet", step="rollout")


class W3Test(RowsTest):
	def setUp(self):
		super().setUp()
		self.port = free_port()

	def assertRestored(self, nodes=True):
		log = self.ctl_log()
		tail = log[log.index("scale 3"):]
		expected = ["scale 3", "wait-ready --timeout 1200"]
		if nodes:
			expected += ["set-nodes zone-a 1 --wait", "wait-ready --timeout 1200"]
		expected += ["sync-listeners --prune", "pods"]
		self.assertEqual(expected, tail)

	def test_passes(self):
		code, out = self.run_row("W-3", w3(self.port))
		self.assertPasses(code, out)
		log = self.ctl_log()
		self.assertIn("scale 4", log)
		self.assertIn("add-node zone-a --wait", log)
		self.assertIn("wait-advertised aerospike-0-1 --timeout 900", log)
		self.assertLess(log.index("wait-advertised aerospike-0-1 --timeout 900"), log.index("sync-listeners"))
		self.assertIn("L5-DRIVER-COMMAND", out)
		self.assertIn("command=watch 127.0.0.1:%d A2" % self.port, out)
		self.assertIn("L5-PEER-WARNING", out)
		self.assertIn("ownershipChecked=true", out)
		self.assertIn("L5-CHECK check=routing step=served", out)
		self.assertRestored()

	def test_pending(self):
		s = w3(self.port)
		s["ctl"]["pending"] = {"lines": []}
		s["driver"]["step_ms"] = 1000
		self.assertFailsCheck(*self.run_row("W-3", s), "pending", boundMs=300000)
		self.assertEqual(["scale 3", "wait-ready --timeout 1200", "sync-listeners --prune", "pods"],
			self.ctl_log()[self.ctl_log().index("scale 3"):])

	def test_pending_pod_existing(self):
		s = w3(self.port)
		s["ctl"]["pending"] = {"lines": ["L5-PENDING pod=aerospike-0-0 rack=1 zone=zone-a"]}
		self.assertFailsCheck(*self.run_row("W-3", s), "pending-pod")

	def test_pending_quiet(self):
		s = w3(self.port)
		s["driver"]["phases"]["scaled"]["fail"] = 1
		self.assertFailsCheck(*self.run_row("W-3", s), "quiet", step="pending")

	def test_pending_failed(self):
		s = w3(self.port)
		s["driver"]["phases"]["scaled"]["fail_first"] = 1
		self.assertFailsCheck(*self.run_row("W-3", s), "pending-failed")

	def test_pending_nodes(self):
		s = w3(self.port)
		s["driver"]["phases"]["scaled"]["nodes"] = three_nodes() + [node("A2", self.port, active=False, reachable=None)]
		self.assertFailsCheck(*self.run_row("W-3", s), "pending-nodes")

	def test_add_node_failure_still_restores_node_group(self):
		s = w3(self.port)
		s["ctl"]["add-node --wait"]["exit"] = 2
		self.assertFailsCheck(*self.run_row("W-3", s), "add-node")
		self.assertRestored()

	def test_advertised_before_listener(self):
		with socket.socket() as listener:
			listener.bind((ENDPOINT, self.port))
			listener.listen()
			self.assertFailsCheck(*self.run_row("W-3", w3(self.port)), "advertised-before-listener", serving="true")
		self.assertRestored()

	def test_unreachable_peer(self):
		s = w3(self.port)
		s["driver"]["phases"]["advertised"]["nodes"] = three_nodes() + [node("A2", self.port, reachable=False)]
		self.assertFailsCheck(*self.run_row("W-3", s), "unreachable-peer", stuckNodes='["A2"]')

	def test_window_failed(self):
		s = w3(self.port)
		s["driver"]["phases"]["advertised"]["fail"] = 1
		self.assertFailsCheck(*self.run_row("W-3", s), "window-failed")

	def test_window_succeeded(self):
		s = w3(self.port)
		s["driver"]["phases"]["advertised"]["succeed"] = 0
		self.assertFailsCheck(*self.run_row("W-3", s), "window-succeeded")

	def test_watch_reply(self):
		s = w3(self.port)
		s["driver"]["watch_phases"] = []
		s["driver"]["step_ms"] = 2000
		self.assertFailsCheck(*self.run_row("W-3", s), "watch-reply", boundMs=1200000)
		self.assertRestored()

	def test_watch_usable(self):
		s = w3(self.port)
		s["driver"]["watch"] = {"usableAt": None, "usableMs": None}
		self.assertFailsCheck(*self.run_row("W-3", s), "watch-usable")

	def test_usable_ms(self):
		s = w3(self.port)
		s["driver"]["watch"] = {"usableMs": 501}
		self.assertFailsCheck(*self.run_row("W-3", s), "usable-ms", usableMs=501, boundMs=500)

	def test_routing_after_listener(self):
		s = w3(self.port)
		s["driver"]["phases"]["listener"]["nodes"] = three_nodes()
		self.assertFailsCheck(*self.run_row("W-3", s), "routing", step="served")

	def test_serve(self):
		s = w3(self.port)
		s["driver"]["served"] = {"written": 4090, "read": 4090, "failures": 6}
		self.assertFailsCheck(*self.run_row("W-3", s), "serve", step="served")
		self.assertRestored()

	def test_driver_ignoring_quit_is_killed_and_row_restores(self):
		s = w3(self.port)
		s["driver"]["ignore_quit"] = True
		with mock.patch.object(rows, "QUIT_WAIT_MS", 500):
			code, out = self.run_row("W-3", s)
		self.assertContract(code, out, "driver exited with status -9 after quit")
		self.assertIn("L5-DRIVER-KILLED", out)
		self.assertRestored()


class W5Test(RowsTest):
	def test_passes(self):
		code, out = self.run_row("W-5", w5())
		self.assertPasses(code, out)
		log = self.ctl_log()
		self.assertIn("az-outage zone-b --duration PT5M --wait-running", log)
		self.assertEqual(21, log.count("experiment E5"))
		self.assertIn("experiment E5 --wait", log)
		for when in ("before", "during", "after"):
			self.assertIn("L5-RESOLVED when=%s" % when, out)
		self.assertIn("L5-CHECK check=longest-unowned unownedReported=true longestUnownedMs=12000 boundMs=30000", out)
		self.assertIn("L5-CHECK check=max-latency maxLatencyMs=21000 boundMs=33000", out)
		self.assertIn("command=reset", out)

	def test_passes_without_partition_map(self):
		s = w5()
		for phase in s["driver"]["phases"].values():
			phase["omit"] = ["unowned", "owners", "longestUnownedMs"]
		s["driver"]["phases"]["outage"]["fields"] = {"maxLatencyMs": 33000}
		code, out = self.run_row("W-5", s)
		self.assertPasses(code, out)
		self.assertIn("L5-CHECK check=longest-unowned unownedReported=false", out)

	def test_outage_zone_without_rack(self):
		s = w5()
		s["ctl"]["endpoint-zone"] = {"lines": ["L5-ENDPOINT-ZONE address={1} zone=zone-x"]}
		self.assertFailsCheck(*self.run_row("W-5", s), "outage-zone", zone="zone-x")
		self.assertNotIn("az-outage", " ".join(self.ctl_log()))

	def test_az_outage_not_running_still_waits(self):
		s = w5()
		s["ctl"]["az-outage"] = {"exit": 2, "lines": s["ctl"]["az-outage"]["lines"][:2],
			"stderr": "ERROR: FIS experiment E5 completed before it was seen running"}
		self.assertFailsCheck(*self.run_row("W-5", s), "az-outage")
		self.assertEqual(["experiment E5 --wait", "wait-ready --timeout 1200", "pods"], self.ctl_log()[-3:])

	def test_outage_ended(self):
		s = w5()
		s["ctl"]["experiment"] = {"lines": ["L5-FIS experiment={1} status=running"]}
		self.assertFailsCheck(*self.run_row("W-5", s), "outage-ended", boundMs=900000)
		self.assertEqual(["experiment E5 --wait", "wait-ready --timeout 1200", "pods"], self.ctl_log()[-3:])

	def test_longest_unowned(self):
		s = w5()
		s["driver"]["phases"]["outage"]["fields"]["longestUnownedMs"] = 30001
		self.assertFailsCheck(*self.run_row("W-5", s), "longest-unowned", longestUnownedMs=30001)

	def test_max_latency(self):
		s = w5()
		s["driver"]["phases"]["outage"]["fields"]["maxLatencyMs"] = 33001
		self.assertFailsCheck(*self.run_row("W-5", s), "max-latency", maxLatencyMs=33001)

	def test_experiment_wait(self):
		s = w5()
		s["ctl"]["experiment --wait"] = {"exit": 2, "stderr": "ERROR: FIS experiment E5 failed"}
		self.assertFailsCheck(*self.run_row("W-5", s), "experiment-wait")

	def test_routing_after(self):
		s = w5()
		s["driver"]["phases"]["after"]["nodes"] = three_nodes(B1={"active": False})
		self.assertFailsCheck(*self.run_row("W-5", s), "routing", step="after", boundMs=120000)

	def test_serve_after(self):
		s = w5()
		s["driver"]["served"] = {"read": 0, "mismatched": 4096}
		self.assertFailsCheck(*self.run_row("W-5", s), "serve", step="after")


if __name__ == "__main__":
	unittest.main()
