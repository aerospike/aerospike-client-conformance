#!/usr/bin/env python3
#
# Offline tests of rows.py: a scripted fake driver and a stub l2ctl, sharing a state directory. The
# stub prints the scripted L2-* lines for each call and can move the fake topology to a new phase; the
# fake driver emits the snapshots of the current phase, chosen by the driver's --set and the phase,
# advancing its t faster than the wall clock, and answers commands. Each row has a passing run, a run
# that breaks it, and, when it needs a newer contract, a run with an older driver that skips it.
#
# Usage: python3 -m unittest l2-proxy/test_rows.py -v

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_spec = importlib.util.spec_from_file_location("l2_rows", os.path.join(os.path.dirname(os.path.abspath(__file__)),
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
	sys.stdout.write((obj if isinstance(obj, str) else json.dumps(obj)) + "\n")
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
for raw in spec.get("raw", []):
	out(raw)
t = 0
counters = {"succeeded": 0, "failed": 0}
watch = None
entered = None
while True:
	time.sleep(0.002)
	t += spec.get("step_ms", 100)
	name = phase()
	phases = spec["phases"]
	ph = phases.get(set_name + ":" + name) or phases.get(name) or phases.get(set_name + ":start") or phases["start"]
	first = entered != name
	entered = name
	if first:
		entered_t = t
	for later in ph.get("later", []):
		if t - entered_t >= later["after_ms"]:
			ph = dict(ph, **later)
	counters["succeeded"] += ph.get("succeed", 10)
	failures = ph.get("fail", 0)
	counters["failed"] += failures
	if contract >= 3:
		for _ in range(failures):
			out({"type": "failure", "t": t, "startedAt": t - ph.get("fail_ms", 1000), "message": ph.get("error", "timeout")})
	snapshot = {"type": "snapshot", "t": t, "reason": "change" if first else "heartbeat", "nodes": ph["nodes"],
		"unowned": ph.get("unowned", 0), "owners": {n["name"]: 1 for n in ph["nodes"]}, "longestUnownedMs": 0,
		"maxLatencyMs": 5, "lastError": None}
	snapshot.update(counters)
	snapshot.update(ph.get("fields", {}))
	out(snapshot)
	while commands:
		words = commands.pop(0).split()
		if words[0] == "quit":
			sys.exit(0)
		elif words[0] == "serve":
			out(dict({"type": "served", "t": t, "step": words[1], "partitions": 4096, "written": 4096, "read": 4096,
				"mismatched": 0, "failures": 0, "firstFailure": None, "elapsedMs": 3000}, **spec.get("served", {})))
		elif words[0] == "reset":
			out({"type": "reset", "t": t})
		elif words[0] == "watch":
			watch = words[1:]
		elif words[0] == "large" and contract >= 5:
			keys = int(words[3])
			out(dict({"type": "large", "t": t, "step": words[1], "bytes": int(words[2]), "keys": keys, "written": keys,
				"read": keys, "mismatched": 0, "failures": 0, "firstFailure": None, "maxMs": 40, "elapsedMs": 300},
				**spec.get("large", {})))
		else:
			out({"type": "error", "t": t, "message": "unknown command: " + " ".join(words)})
	if watch and name in spec.get("watch_phases", ["listener"]):
		out(dict({"type": "watch", "t": t, "address": watch[0], "node": watch[1], "servingAt": t - 60,
			"lastClosedAt": t - 70, "addresses": [{"address": "10.1.0.2", "firstConnectedAt": t - 60}],
			"usableAt": t - 10, "usableMs": 50, "ownershipChecked": True}, **spec.get("watch", {})))
		watch = None
'''

STUB_L2CTL = r'''
import json, os, sys
state = os.environ["FAKE_STATE"]
spec = json.load(open(os.path.join(state, "scenario.json")))["ctl"]
args = sys.argv[1:]
with open(os.path.join(state, "ctl.log"), "a") as f:
	f.write(" ".join(args) + "\n")
entries = spec.get(args[0], {})
entries = entries if isinstance(entries, list) else [entries]
count = os.path.join(state, "count-" + args[0])
n = int(open(count).read()) if os.path.exists(count) else 0
with open(count, "w") as f:
	f.write(str(n + 1))
entry = entries[min(n, len(entries) - 1)]
entry = entry.get("by_arg", {}).get(args[-1] if len(args) > 1 else "", entry)
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
PROXY = "10.1.0.2"
NODES = [("c-1", "A1", "10.0.0.11", 4901), ("c-2", "B1", "10.0.0.12", 4902), ("c-3", "C1", "10.0.0.13", 4903)]


def node(name, port, host=ENDPOINT, address=PROXY, active=True, reachable=True):
	return {"name": name, "host": host, "port": port, "address": address, "active": active, "reachable": reachable}


def three(**changes):
	"""The three nodes at the endpoint and their listener ports, with changes by node name."""
	return [dict(node(n, p), **changes.get(n, {})) for _, n, _, p in NODES]


def nodes_line(alt=lambda c, p: "", std=lambda c, a, p: a + ":3000", extra=()):
	return {"lines": ["L2-NODE container=%s node=%s address=%s listener=%s port=%d std=%s alt=%s peersGeneration=1 "
		"clusterSize=3 state=up" % (c, n, a, c, p, std(c, a, p), alt(c, p)) for c, n, a, p in list(NODES) + list(extra)]}


def base_ctl():
	return {
		"nodes": nodes_line(),
		"route-check": {"lines": ["L2-ROUTE address=%s:3000 node=%s connects=false expected=false" % (a, c)
			for c, _, a, _ in NODES] + ["L2-ROUTE address=%s:%d connects=true expected=true" % (ENDPOINT, p)
			for _, _, _, p in NODES]},
		"drivers-stop": {},
		"listener": {"by_arg": {"off": {"lines": ["L2-LISTENER name={1} enabled=false"], "phase": "fault"},
			"on": {"lines": ["L2-LISTENER name={1} enabled=true"], "phase": "after"}}},
		"toxic-add": {"lines": ["L2-TOXIC listener={1} toxic={2} type={3} added"], "phase": "fault"},
		"toxic-remove": {"lines": ["L2-TOXIC listener={1} toxic={2} removed"], "phase": "after"},
		"wait-cluster": {"lines": ["L2-CLUSTER size=3 key=K"]},
		"stop-node": {"lines": ["L2-STOPPED container={1}"], "phase": "restored"},
		"listeners": {"lines": ["L2-LISTENER name=c-4 listen=0.0.0.0:4910 upstream=c-4:3000 enabled=true toxics="]},
		"listener-remove": {"lines": ["L2-LISTENER name={1} removed"]},
	}


def scenario(phases=None, contract=3, **driver):
	return {"ctl": base_ctl(), "driver": dict({"contract": contract, "phases": phases or {"start": {"nodes": three()}}},
		**driver)}


VALUES = {
	"L2_PREFIX": "t", "DISCOVERY_ENDPOINT_HOSTNAME": ENDPOINT, "DISCOVERY_ENDPOINT_PORTS": "4901,4902,4903",
	"L2_PROXY_CLIENT_ADDRESS": PROXY, "L2_EXTRA_PORT_BASE": "4910", "L2_SUBNET": "10.0.0", "L2_OUTSIDE_OCTET": "40",
	"DISCOVERY_PORT_MAP_RANGE": "10.0.0.11-10.0.0.26", "DISCOVERY_PORT_MAP_PORT_BASE": "5000",
	"DISCOVERY_PROXY_PORT_RANGE": "40000 40009", "DISCOVERY_CLIENT_NETWORK_MTU": "1200", "DISCOVERY_REACHABLE_NODE": "c-3",
	"L2_UNRESOLVED": "0",
}


class RowsTest(unittest.TestCase):
	profile = "default"
	access = ""

	def setUp(self):
		self.dir = tempfile.TemporaryDirectory()
		self.state = os.path.join(self.dir.name, "state")
		os.mkdir(self.state)
		self.ctl = os.path.join(self.dir.name, "l2ctl")
		with open(self.ctl, "w") as f:
			f.write("#!%s\n%s" % (sys.executable, STUB_L2CTL))
		os.chmod(self.ctl, 0o700)
		self.fake = os.path.join(self.dir.name, "fake_driver.py")
		with open(self.fake, "w") as f:
			f.write(FAKE_DRIVER)
		self.values = dict(VALUES, L2_PROFILE=self.profile, L2_ACCESS_HOSTNAME=self.access)
		self.environ = {"L2_CTL": self.ctl, "L2_STATE": self.state, "FAKE_STATE": self.state}

	def tearDown(self):
		self.dir.cleanup()

	def run_rows(self, row, s, *extra):
		with open(os.path.join(self.state, "topology.env"), "w") as f:
			f.write("".join("%s=%s\n" % kv for kv in self.values.items()))
		with open(os.path.join(self.state, "scenario.json"), "w") as f:
			json.dump(s, f)
		out = io.StringIO()
		saved = dict(os.environ)
		os.environ.update(self.environ)
		try:
			with contextlib.redirect_stdout(out):
				code = rows.main(["--profile", self.profile, "--rows", row, "--driver", "%s %s" % (sys.executable, self.fake)] +
					list(extra))
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

	def assertPasses(self, row, s, *extra):
		code, out = self.run_rows(row, s, *extra)
		self.assertEqual(0, code, out)
		self.assertNotIn("outcome=fail", out)
		self.assertIn("L2-ROW profile=%s row=%s result=pass" % (self.profile, row), out)
		return out

	def assertFailsCheck(self, row, s, name, *extra, **fields):
		code, out = self.run_rows(row, s, *extra)
		self.assertEqual(1, code, out)
		failed = [line for line in out.splitlines() if line.startswith("L2-CHECK check=%s " % name) and
			line.endswith("outcome=fail")]
		self.assertTrue(failed, "no failed check %s in:\n%s" % (name, out))
		for key, value in fields.items():
			self.assertIn("%s=%s" % (key, value), failed[0])
		self.assertIn("L2-ROW profile=%s row=%s result=fail" % (self.profile, row), out)
		return out

	def assertSkips(self, row, s, reason):
		code, out = self.run_rows(row, s)
		self.assertEqual(0, code, out)
		self.assertIn("L2-ROW profile=%s row=%s result=skip reason=%s" % (self.profile, row, reason), out)
		self.assertNotIn("L2-ROW-START profile=%s row=%s" % (self.profile, row), out)
		return out

	def assertOlderDriverSkips(self, row, s, needs):
		s["driver"]["contract"] = 3 if needs == 5 else 2
		out = self.assertSkips(row, s, "the driver speaks contract %d; this row needs %d" % (s["driver"]["contract"], needs))
		self.assertEqual(1, len(self.driver_args()), "one driver, to learn the contract")
		return out


class UsageTest(RowsTest):
	def test_plan_lists_every_row(self):
		out = io.StringIO()
		with contextlib.redirect_stdout(out):
			self.assertEqual(0, rows.main(["--plan", "--profile", "default"]))
		self.assertEqual(len(rows.ROWS["default"]), out.getvalue().count("L2-PLAN-ROW profile=default "))
		self.assertIn("row=silent-drop-attempts-time-out-within-bound-and-recover needs=contract 3 fault=true", out.getvalue())

	def test_unknown_row(self):
		code, out = self.run_rows("no-such-row", scenario())
		self.assertEqual(2, code)
		self.assertIn("ERROR: unknown rows: no-such-row", out)

	def test_missing_environment(self):
		self.environ["L2_STATE"] = ""
		code, out = self.run_rows(rows.BY_NAME.name, scenario())
		self.assertEqual(2, code)
		self.assertIn("L2-ENVIRONMENT missing=L2_STATE", out)

	def test_wrong_profile(self):
		self.values["L2_PROFILE"] = "reachable"
		code, out = self.run_rows(rows.BY_NAME.name, scenario())
		self.assertEqual(2, code)
		self.assertIn("the topology is profile reachable, not default", out)

	def test_rows_of_other_profiles_are_left_out(self):
		code, out = self.run_rows("%s,large-records-over-low-mtu-within-timeout" % rows.BY_NAME.name, scenario())
		self.assertEqual(0, code, out)
		self.assertNotIn("large-records", out)


class ContractTest(RowsTest):
	def test_large_from_a_driver_without_contract_5(self):
		s = scenario(raw=[{"type": "large", "t": 0, "step": "x", "bytes": 1, "keys": 1, "written": 1, "read": 1,
			"mismatched": 0, "failures": 0, "firstFailure": None, "maxMs": 1, "elapsedMs": 1}])
		code, out = self.run_rows(rows.BY_NAME.name, s)
		self.assertEqual(3, code, out)
		self.assertIn("large from a driver without contract 5", out)

	def test_contract_4_is_not_spoken(self):
		code, out = self.run_rows(rows.BY_NAME.name, scenario(contract=4))
		self.assertEqual(3, code, out)
		self.assertIn("ready with contract 4, harness speaks 1, 2, 3 or 5", out)

	def test_failure_from_contract_2(self):
		s = scenario(contract=2, raw=[{"type": "failure", "t": 0, "startedAt": 0, "message": "x"}])
		code, out = self.run_rows(rows.BY_NAME.name, s)
		self.assertEqual(3, code, out)
		self.assertIn("failure from a driver without contract 3", out)


class NoRouteTest(RowsTest):
	row = rows.ROUTE.name

	def test_passes_without_a_driver(self):
		self.assertPasses(self.row, scenario())
		self.assertFalse(os.path.exists(os.path.join(self.state, "driver-args")))

	def test_node_address_connects(self):
		s = scenario()
		s["ctl"]["route-check"] = {"exit": 1, "lines": ["L2-ROUTE address=10.0.0.11:3000 connects=true expected=false"]}
		self.assertFailsCheck(self.row, s, "route-check", exit=1)


class PortMapByNameTest(RowsTest):
	row = rows.BY_NAME.name

	def test_passes(self):
		self.assertPasses(self.row, scenario())
		flags = self.driver_args()[0]
		self.assertEqual("A1=as-endpoint.test:4901,B1=as-endpoint.test:4902,C1=as-endpoint.test:4903",
			flags[flags.index("--port-map") + 1])
		self.assertNotIn("--user", flags)

	def test_node_at_wrong_port(self):
		self.assertFailsCheck(self.row, scenario({"start": {"nodes": three(B1={"port": 3000})}}), "node-address",
			wrong='["B1@as-endpoint.test:3000"]')

	def test_partition_not_served(self):
		self.assertFailsCheck(self.row, scenario(served={"read": 4000, "failures": 96}), "serve")


class PortMapByAddressTest(RowsTest):
	row = "port-map-by-advertised-address-routes-every-node"

	def test_passes(self):
		self.assertPasses(self.row, scenario(contract=5))
		flags = self.driver_args()[-1]
		self.assertIn("10.0.0.11:3000=as-endpoint.test:4901", flags[flags.index("--port-map") + 1])
		self.assertEqual("strict", flags[flags.index("--on-miss") + 1])

	def test_node_not_routed(self):
		s = scenario({"start": {"nodes": three()}, "l2-port-map-address:start": {"nodes": three()[:2]}}, contract=5)
		self.assertFailsCheck(self.row, s, "routing", step="port-map-address")

	def test_older_driver_skips(self):
		self.assertOlderDriverSkips(self.row, scenario(), 5)


class StrictMissTest(RowsTest):
	row = "strict-miss-never-holds-the-unmapped-node"

	def test_passes(self):
		self.assertPasses(self.row, scenario({"start": {"nodes": three()[:2], "unowned": 1365}}))
		flags = self.driver_args()[0]
		self.assertEqual("A1=as-endpoint.test:4901,B1=as-endpoint.test:4902", flags[flags.index("--port-map") + 1])

	def test_unmapped_node_held_later(self):
		s = scenario({"start": {"nodes": three()[:2], "later": [{"after_ms": 2000, "nodes": three()}]}})
		self.assertFailsCheck(self.row, s, "never-held", node="C1")

	def test_held_nodes_not_served(self):
		s = scenario({"start": {"nodes": three()[:2]}}, served={"read": 1, "written": 4096, "failures": 4095})
		self.assertFailsCheck(self.row, s, "serve-held", read=1, ownedByHeld=2)


class UnmappedPeersTest(RowsTest):
	row = "pass-through-miss-without-route-client-not-created"

	def test_passes(self):
		s = scenario(not_created={"l2-unmapped-peers": "Peers not reachable: 10.0.0.12 3000, 10.0.0.13 3000"})
		self.assertPasses(self.row, s)

	def test_client_created(self):
		self.assertFailsCheck(self.row, scenario(), "client-not-created")

	def test_error_does_not_name_peers(self):
		s = scenario(not_created={"l2-unmapped-peers": "cannot connect"})
		self.assertFailsCheck(self.row, s, "error-names", unnamed='["10.0.0.12","10.0.0.13"]')


class EmptyAlternateTest(RowsTest):
	row = "empty-alternate-list-client-not-created"

	def test_passes(self):
		self.assertPasses(self.row, scenario(not_created={"l2-empty-alternate": "no alternate address"}))
		self.assertEqual("pass-through", self.driver_args()[0][self.driver_args()[0].index("--shape") + 1])

	def test_client_created(self):
		self.assertFailsCheck(self.row, scenario(), "client-not-created")


class ListenerClosedTest(RowsTest):
	row = "closed-listener-node-returns-with-bounded-latency"

	def phases(self):
		return {"start": {"nodes": three()}, "fault": {"nodes": three(B1={"reachable": False})},
			"after": {"nodes": three()}}

	def test_passes(self):
		self.assertPasses(self.row, scenario(self.phases()))
		log = self.ctl_log()
		self.assertIn("listener c-2 off", log)
		self.assertIn("listener c-2 on", log)

	def test_fault_not_applied(self):
		out = self.assertFailsCheck(self.row, scenario(self.phases()), "fault-observed", "--no-fault")
		self.assertIn("L2-FAULT skipped=listener c-2 off", out)

	def test_latency_after_recovery(self):
		phases = self.phases()
		phases["after"]["fields"] = {"maxLatencyMs": 1500}
		self.assertFailsCheck(self.row, scenario(phases), "recovered-latency", maxLatencyMs=1500, boundMs=1000)

	def test_node_not_back(self):
		phases = self.phases()
		phases["after"] = phases["fault"]
		self.assertFailsCheck(self.row, scenario(phases), "routing", step="listener-closed")
		self.assertIn("listener c-2 on", self.ctl_log())


class SilentDropTest(RowsTest):
	row = "silent-drop-attempts-time-out-within-bound-and-recover"

	def phases(self, fail_ms=1000):
		return {"start": {"nodes": three()}, "fault": {"nodes": three(), "fail": 1, "fail_ms": fail_ms},
			"after": {"nodes": three()}}

	def test_passes(self):
		self.assertPasses(self.row, scenario(self.phases()))
		log = self.ctl_log()
		self.assertIn("toxic-add c-2 drop-down timeout --stream downstream timeout=0", log)
		self.assertIn("toxic-add c-2 drop-up timeout --stream upstream timeout=0", log)
		self.assertIn("toxic-remove c-2 drop-up", log)

	def test_fault_not_applied(self):
		self.assertFailsCheck(self.row, scenario(self.phases()), "fault-observed", "--no-fault", failures=0)

	def test_attempt_past_bound(self):
		self.assertFailsCheck(self.row, scenario(self.phases(fail_ms=2500)), "attempt-bound", slowestMs=2500, boundMs=1500)
		self.assertIn("toxic-remove c-2 drop-down", self.ctl_log())

	def test_older_driver_skips(self):
		self.assertOlderDriverSkips(self.row, scenario(self.phases()), 3)


class LatencyTest(RowsTest):
	row = "delayed-replies-below-timeout-serve-and-recover"

	def phases(self, max_latency=450, fail=0):
		return {"start": {"nodes": three()}, "fault": {"nodes": three(), "fail": fail, "fields": {"maxLatencyMs": max_latency}},
			"after": {"nodes": three()}}

	def test_passes(self):
		self.assertPasses(self.row, scenario(self.phases()))
		self.assertIn("toxic-add c-2 delay latency --stream downstream latency=300", self.ctl_log())

	def test_fault_not_applied(self):
		self.assertFailsCheck(self.row, scenario(self.phases()), "fault-observed", "--no-fault", maxLatencyMs=5)

	def test_delay_past_bound(self):
		self.assertFailsCheck(self.row, scenario(self.phases(max_latency=1400)), "delayed-latency", maxLatencyMs=1400)

	def test_failures_under_delay(self):
		self.assertFailsCheck(self.row, scenario(self.phases(fail=1)), "delayed-no-failures")


class ReseedTest(RowsTest):
	row = "all-listeners-closed-client-reseeds-within-bound"

	def phases(self, reseed_ms=0):
		after = {"nodes": three()}
		if reseed_ms:
			after = {"nodes": [], "later": [{"after_ms": reseed_ms, "nodes": three()}]}
		return {"start": {"nodes": three()}, "fault": {"nodes": []}, "after": after}

	def test_passes(self):
		self.assertPasses(self.row, scenario(self.phases()))
		log = self.ctl_log()
		self.assertEqual(["listener c-%d off" % i for i in (1, 2, 3)], [line for line in log if line.endswith(" off")])
		self.assertEqual(["listener c-%d on" % i for i in (1, 2, 3)], [line for line in log if line.endswith(" on")])

	def test_fault_not_applied(self):
		self.assertFailsCheck(self.row, scenario(self.phases()), "fault-observed", "--no-fault")

	def test_reseed_past_bound(self):
		self.assertFailsCheck(self.row, scenario(self.phases(reseed_ms=3000)), "reseed", boundMs=1500)


class ReachableTest(RowsTest):
	profile = "reachable"
	access = "as-reachable.test"

	def with_nodes(self, s):
		s["ctl"]["nodes"] = nodes_line(alt=lambda c, p: "" if c == "c-3" else "as-reachable.test:%d" % p,
			std=lambda c, a, p: "as-reachable.test:%d" % p if c == "c-3" else a + ":3000")
		return s

	def test_pass_through_miss_passes(self):
		s = self.with_nodes(scenario({"start": {"nodes": three(C1={"host": "as-reachable.test"})}}))
		self.assertPasses("pass-through-miss-holds-reachable-node-at-its-address", s)
		flags = self.driver_args()[0]
		self.assertEqual("A1=as-endpoint.test:4901,B1=as-endpoint.test:4902", flags[flags.index("--port-map") + 1])
		self.assertEqual("pass-through", flags[flags.index("--on-miss") + 1])

	def test_pass_through_miss_at_listener_fails(self):
		self.assertFailsCheck("pass-through-miss-holds-reachable-node-at-its-address", self.with_nodes(scenario()), "node-address",
			wrong='["C1@as-endpoint.test:4903"]')

	def test_strict_passes(self):
		self.assertPasses("strict-miss-never-holds-the-reachable-node", self.with_nodes(scenario({"start": {"nodes": three()[:2]}})))

	def test_strict_holding_reachable_fails(self):
		s = self.with_nodes(scenario({"start": {"nodes": three()[:2], "later": [{"after_ms": 1000,
			"nodes": three(C1={"host": "as-reachable.test"})}]}}))
		self.assertFailsCheck("strict-miss-never-holds-the-reachable-node", s, "never-held", node="C1")

	def test_alternate_passes(self):
		at_alt = three(A1={"host": "as-reachable.test"}, B1={"host": PROXY})[:2]
		self.assertPasses("alternate-list-holds-only-nodes-with-alternate", self.with_nodes(scenario({"start": {"nodes": at_alt}})))

	def test_alternate_holding_reachable_fails(self):
		s = self.with_nodes(scenario({"start": {"nodes": three(A1={"host": "as-reachable.test"}, B1={"host": PROXY})[:2],
			"later": [{"after_ms": 1000, "nodes": three(A1={"host": "as-reachable.test"}, B1={"host": PROXY})}]}}))
		self.assertFailsCheck("alternate-list-holds-only-nodes-with-alternate", s, "never-held", node="C1")

	def test_seed_without_alternate_passes(self):
		s = self.with_nodes(scenario(not_created={"l2-seed-without-alternate": "no alternate address"}))
		self.assertPasses("seed-without-alternate-client-not-created", s)
		flags = self.driver_args()[0]
		self.assertEqual("as-endpoint.test:4903", flags[flags.index("--seed") + 1])

	def test_seed_without_alternate_created_fails(self):
		self.assertFailsCheck("seed-without-alternate-client-not-created", self.with_nodes(scenario()), "client-not-created")


class PassThroughNamesTest(RowsTest):
	profile = "pass-through-names"
	row = "distinct-alternate-names-route-every-node"

	def with_nodes(self, s, name=lambda c: "node-%s.pass-through.test" % c[-1]):
		s["ctl"]["nodes"] = nodes_line(alt=lambda c, p: "%s:%d" % (name(c), p))
		return s

	def test_passes(self):
		self.assertPasses(self.row, self.with_nodes(scenario({"start": {"nodes": three(
			A1={"host": "node-1.pass-through.test"}, B1={"host": "node-2.pass-through.test"}, C1={"host": PROXY})}})))

	def test_shared_name_fails(self):
		self.assertFailsCheck(self.row, self.with_nodes(scenario(), name=lambda c: "as-pass-through.test"), "distinct-names")

	def test_node_at_endpoint_fails(self):
		self.assertFailsCheck(self.row, self.with_nodes(scenario()), "node-address")


class PassThroughPortsTest(RowsTest):
	profile = "pass-through-ports"
	access = "as-pass-through.test"

	def with_nodes(self, s):
		s["ctl"]["nodes"] = nodes_line(alt=lambda c, p: "as-pass-through.test:%d" % p)
		return s

	def test_shared_name_passes(self):
		s = self.with_nodes(scenario({"start": {"nodes": three(A1={"host": "as-pass-through.test"}, B1={"host": PROXY},
			C1={"host": "as-pass-through.test"})}}))
		self.assertPasses("shared-alternate-name-routes-every-node", s)

	def test_shared_name_not_routed_fails(self):
		s = self.with_nodes(scenario({"start": {"nodes": three(A1={"host": "as-pass-through.test"})[:2]}}))
		self.assertFailsCheck("shared-alternate-name-routes-every-node", s, "routing")

	def new_node(self, usable_ms=50):
		s = self.with_nodes(scenario({"start": {"nodes": three()}, "listener": {"nodes": three() + [node("D1", 4910,
			host="as-pass-through.test")]}}, watch={"usableMs": usable_ms}))
		s["ctl"]["start-node"] = {"lines": ["L2-STARTED container=c-4 address=10.0.0.14 conf=c-4.conf"], "phase": "started"}
		s["ctl"]["wait-advertised"] = {"lines": ["L2-ADVERTISED container=c-4 node=D1 address=10.0.0.14 by=c-1,c-2,c-3"]}
		s["ctl"]["listener-add"] = {"lines": ["L2-LISTENER name={1} added"], "phase": "listener"}
		return s

	def test_new_node_passes(self):
		self.assertPasses("new-node-on-shared-name-usable-within-two-tend-intervals", self.new_node())
		log = self.ctl_log()
		self.assertIn("start-node --alternate as-pass-through.test:4910", log)
		self.assertIn("listener-add c-4 4910 c-4:3000", log)
		self.assertIn("stop-node c-4", log)
		self.assertIn("listener-remove c-4", log)
		self.assertEqual("wait-cluster 3 --timeout 120", log[-1])

	def test_new_node_slow_fails(self):
		self.assertFailsCheck("new-node-on-shared-name-usable-within-two-tend-intervals", self.new_node(900), "usable-ms",
			usableMs=900, boundMs=500)
		self.assertIn("stop-node c-4", self.ctl_log())

	def test_unresolved_passes(self):
		self.values["L2_UNRESOLVED"] = "1"
		s = self.with_nodes(scenario())
		s["ctl"]["lookup"] = {"lines": ["L2-LOOKUP container=c-%d name={1} exit=2 resolves=false" % i for i in (1, 2, 3)]}
		self.assertPasses("shared-name-unresolved-in-servers-advertised-verbatim", s)

	def test_unresolved_resolving_fails(self):
		self.values["L2_UNRESOLVED"] = "1"
		s = self.with_nodes(scenario())
		s["ctl"]["lookup"] = {"lines": ["L2-LOOKUP container=c-1 name={1} exit=0 resolves=true"]}
		self.assertFailsCheck("shared-name-unresolved-in-servers-advertised-verbatim", s, "unresolved-in-servers",
			resolvesIn='["c-1"]')

	def test_unresolved_skips_without_the_option(self):
		self.assertSkips("shared-name-unresolved-in-servers-advertised-verbatim", self.with_nodes(scenario()),
			"the topology maps the shared name in the servers; run with --unresolved-hostname")


class PortMapRangeTest(RowsTest):
	profile = "port-map-range"

	def ranged(self, **changes):
		return [dict(node(n, 5000 + int(a.rpartition(".")[2])), **changes.get(n, {})) for _, n, a, _ in NODES]

	def test_range_passes(self):
		self.assertPasses("range-port-map-routes-every-node", scenario({"start": {"nodes": three()},
			"l2-range:start": {"nodes": self.ranged()}}, contract=5))
		flags = self.driver_args()[-1]
		port_map = flags[flags.index("--port-map") + 1].split(",")
		self.assertEqual(16, len(port_map))
		self.assertIn("10.0.0.26:3000=as-endpoint.test:5026", port_map)

	def test_range_wrong_port_fails(self):
		s = scenario({"start": {"nodes": three()}, "l2-range:start": {"nodes": self.ranged(A1={"port": 4901})}}, contract=5)
		self.assertFailsCheck("range-port-map-routes-every-node", s, "node-address")

	def test_range_older_driver_skips(self):
		self.assertOlderDriverSkips("range-port-map-routes-every-node", scenario(), 5)

	def new_node(self, usable_ms=50):
		s = scenario({"start": {"nodes": three()}, "l2-new-node-in-range:start": {"nodes": self.ranged()},
			"l2-new-node-in-range:after": {"nodes": self.ranged() + [node("D1", 5014)]}}, contract=5,
			watch={"usableMs": usable_ms}, watch_phases=["after"])
		s["ctl"]["start-node"] = {"lines": ["L2-STARTED container=c-4 address=10.0.0.14 conf=aerospike.conf"],
			"phase": "started"}
		s["ctl"]["wait-advertised"] = {"lines": ["L2-ADVERTISED container=c-4 node=D1 address=10.0.0.14 by=c-1"]}
		return s

	def test_new_node_passes(self):
		self.assertPasses("new-node-in-range-usable-within-two-tend-intervals", self.new_node())
		log = self.ctl_log()
		self.assertIn("listener range-10.0.0.14 off", log)
		self.assertIn("start-node --address 10.0.0.14", log)
		self.assertIn("listener range-10.0.0.14 on", log)

	def test_new_node_slow_fails(self):
		self.assertFailsCheck("new-node-in-range-usable-within-two-tend-intervals", self.new_node(600), "usable-ms",
			usableMs=600)
		self.assertEqual("listener range-10.0.0.14 on", [line for line in self.ctl_log() if line.startswith("listener ")][-1])

	def test_new_node_older_driver_skips(self):
		self.assertOlderDriverSkips("new-node-in-range-usable-within-two-tend-intervals", self.new_node(), 5)

	def outside(self, held_later=False):
		started = {"nodes": self.ranged()}
		if held_later:
			started["later"] = [{"after_ms": 1000, "nodes": self.ranged() + [node("D1", 3000, host="10.0.0.40")]}]
		s = scenario({"start": {"nodes": three()}, "l2-outside-range:start": {"nodes": self.ranged()},
			"l2-outside-range:started": started}, contract=5)
		s["ctl"]["start-node"] = {"lines": ["L2-STARTED container=c-4 address=10.0.0.40 conf=aerospike.conf"],
			"phase": "started"}
		s["ctl"]["wait-advertised"] = {"lines": ["L2-ADVERTISED container=c-4 node=D1 address=10.0.0.40 by=c-1"]}
		return s

	def test_outside_passes(self):
		self.assertPasses("strict-never-holds-node-outside-range", self.outside())
		self.assertIn("start-node --address 10.0.0.40", self.ctl_log())
		flags = self.driver_args()[-1]
		self.assertEqual("strict", flags[flags.index("--on-miss") + 1])

	def test_outside_held_fails(self):
		self.assertFailsCheck("strict-never-holds-node-outside-range", self.outside(held_later=True), "never-held", node="D1")
		self.assertIn("stop-node c-4", self.ctl_log())

	def test_outside_older_driver_skips(self):
		self.assertOlderDriverSkips("strict-never-holds-node-outside-range", self.outside(), 5)


class PortRangeTest(RowsTest):
	profile = "port-range"
	row = "source-port-exhaustion-fails-within-bound-and-recovers"

	def exhausted(self, fail_ms=900, first="40000"):
		s = scenario({"start": {"nodes": three()}, "l2-source-ports:start": {"nodes": three(), "fail": 1,
			"fail_ms": fail_ms}})
		s["ctl"]["proxy-port-range"] = {"lines": ["L2-PORT-RANGE first=%s last=40009" % first]}
		s["ctl"]["proxy-ports"] = {"lines": [ports(0)]}
		return s

	def test_passes(self):
		self.assertPasses(self.row, self.exhausted())
		heavy, light = self.driver_args()[-2:]
		self.assertEqual("64", heavy[heavy.index("--workers") + 1])
		self.assertEqual("2", light[light.index("--workers") + 1])

	def test_port_range_not_set_fails(self):
		self.assertFailsCheck(self.row, self.exhausted(first="32768"), "proxy-port-range", first=32768)

	def test_no_failures_fails(self):
		s = self.exhausted()
		s["driver"]["phases"]["l2-source-ports:start"]["fail"] = 0
		self.assertFailsCheck(self.row, s, "fault-observed", failures=0)

	def test_attempt_past_bound_fails(self):
		self.assertFailsCheck(self.row, self.exhausted(fail_ms=4000), "attempt-bound", slowestMs=4000)

	def test_waits_for_source_ports_before_low_load(self):
		s = self.exhausted()
		s["ctl"]["proxy-ports"] = [{"lines": [ports(10)]}, {"lines": [ports(0)]}]
		with mock.patch.object(rows, "PORTS_POLL_S", 0):
			self.assertPasses(self.row, s)
		self.assertEqual(2, self.ctl_log().count("proxy-ports"))

	def test_source_ports_never_free_fails(self):
		s = self.exhausted()
		s["ctl"]["proxy-ports"] = {"lines": [ports(10)]}
		with mock.patch.object(rows, "PORTS_FREE_WAIT_MS", 0):
			self.assertFailsCheck(self.row, s, "proxy-ports-free", used=10, timeWait=10)
		sets = [args[args.index("--set") + 1] for args in self.driver_args()]
		self.assertNotIn("l2-source-ports-low", sets)

	def test_older_driver_skips(self):
		self.assertOlderDriverSkips(self.row, self.exhausted(), 3)


def ports(used):
	return "L2-PROXY-PORTS first=40000 last=40009 used=%d timeWait=%d" % (used, used)


class LowMtuTest(RowsTest):
	profile = "low-mtu"
	row = "large-records-over-low-mtu-within-timeout"

	def large(self, mtu="1200", **reply):
		s = scenario(contract=5, large=reply)
		s["ctl"]["client-mtu"] = {"lines": ["L2-MTU network=n mtu=%s" % mtu]}
		return s

	def test_passes(self):
		out = self.assertPasses(self.row, self.large())
		self.assertIn("command=large mtu 1048576 8", out)

	def test_mtu_not_set_fails(self):
		self.assertFailsCheck(self.row, self.large(mtu="1500"), "client-mtu", mtu=1500)

	def test_failed_write_fails(self):
		self.assertFailsCheck(self.row, self.large(written=7, read=7, failures=1, firstFailure="timeout"), "large",
			failures=1)

	def test_slow_command_fails(self):
		self.assertFailsCheck(self.row, self.large(maxMs=1200), "large-ms", maxMs=1200, boundMs=1000)

	def test_older_driver_skips(self):
		self.assertOlderDriverSkips(self.row, self.large(), 5)


class IdleResetTest(RowsTest):
	profile = "idle-reset"
	row = "reset-peer-attempts-fail-within-bound-and-recover"

	def phases(self, fail_ms=20):
		return {"start": {"nodes": three()}, "fault": {"nodes": three(), "fail": 1, "fail_ms": fail_ms},
			"after": {"nodes": three()}}

	def test_passes(self):
		self.assertPasses(self.row, scenario(self.phases()))
		self.assertIn("toxic-add c-2 reset reset_peer --stream downstream timeout=0", self.ctl_log())

	def test_fault_not_applied(self):
		self.assertFailsCheck(self.row, scenario(self.phases()), "fault-observed", "--no-fault")

	def test_attempt_past_bound(self):
		self.assertFailsCheck(self.row, scenario(self.phases(fail_ms=1600)), "attempt-bound", slowestMs=1600)

	def test_older_driver_skips(self):
		self.assertOlderDriverSkips(self.row, scenario(self.phases()), 3)


if __name__ == "__main__":
	unittest.main()
