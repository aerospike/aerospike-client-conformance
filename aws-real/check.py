#!/usr/bin/env python3
"""Check the probe output of one aws-real run and print its summary. See README.md."""

import argparse
import json
import math
import os
import re
import sys
from collections import Counter

ACCESS_KEY_ID = re.compile(r"(AKIA|ASIA)[A-Z0-9]{16}")


def load(path):
    """Return every JSON object line of a probe output file, ignoring any other line."""
    objects = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                objects.append(value)
    return objects


def stats(micros):
    s = sorted(micros)
    p99 = s[min(len(s) - 1, math.ceil(len(s) * 0.99) - 1)]
    return "min=%.1fms median=%.1fms p99=%.1fms max=%.1fms" % (
        s[0] / 1000.0, s[len(s) // 2] / 1000.0, p99 / 1000.0, s[-1] / 1000.0)


def over(micros, ms):
    return sum(1 for m in micros if m > ms * 1000)


def host_ip(host):
    return host.rsplit(":", 1)[0]


class Checker:
    def __init__(self, out):
        self.out = out
        self.failures = []

    def check(self, ok, message):
        if not ok:
            self.failures.append(message)
            self.out.write("CHECK FAIL " + message + "\n")

    def line(self, text):
        self.out.write(text + "\n")


def check_latency(c, runs, expected_runs, expected_nodes, excluded, deadline_ms, timeout_ms):
    c.check(len(runs) == expected_runs, "latency: %d runs reported, expected %d" % (len(runs), expected_runs))
    cold = []
    warm = []
    reasons = Counter()

    for i, run in enumerate(runs, 1):
        source = run.get("credentials_source")
        hosts = run.get("hosts")
        c.line("AWS-REAL-RUN %d credentials_source=%s hosts=%s cold=%sus" % (i, source, hosts, run.get("cold_us")))
        if run.get("error") is not None:
            c.check(False, "latency run %d: %s" % (i, run["error"]))
            continue
        c.check(source == "imds", "latency run %d: credentials from %s, expected imds" % (i, source))
        if not isinstance(hosts, list):
            c.check(False, "latency run %d: no hosts" % i)
            continue
        c.check(len(hosts) == expected_nodes,
                "latency run %d: %d seeds, expected %d: %s" % (i, len(hosts), expected_nodes, hosts))
        for ip in excluded:
            c.check(ip not in map(host_ip, hosts), "latency run %d: excluded %s in %s" % (i, ip, hosts))
        if isinstance(run.get("cold_us"), int):
            cold.append(run["cold_us"])
        warm.extend(m for m in run.get("warm_us", []) if isinstance(m, int))
        reasons.update(run.get("warm_failures", []))

    if cold:
        c.line("AWS-REAL-LATENCY cold %s runs=%d" % (stats(cold), len(cold)))
    if warm:
        c.line("AWS-REAL-LATENCY warm %s calls=%d failures=%d" % (stats(warm), len(warm), sum(reasons.values())))
    c.line("AWS-REAL-LATENCY reference deadline=%dms timeout=%dms coldOverTimeout=%d coldOverDeadline=%d "
           "warmOverTimeout=%d warmOverDeadline=%d" % (deadline_ms, timeout_ms, over(cold, timeout_ms),
                                                       over(cold, deadline_ms), over(warm, timeout_ms),
                                                       over(warm, deadline_ms)))
    for reason, n in reasons.most_common():
        c.line("AWS-REAL-WARM-FAILURE count=%d %s" % (n, reason))


def check_denied(c, results, secrets):
    if not results:
        c.check(False, "denied: no result")
        return
    result = results[-1]
    error = result.get("error")
    shown = error
    for secret in secrets:
        shown = shown.replace(secret, "[redacted]") if shown else shown
    c.line("AWS-REAL-DENIED credentials_source=%s error=%s" % (result.get("credentials_source"), shown))
    if error is None:
        c.check(False, "denied: the role with no policy was not refused")
        return
    c.check("UnauthorizedOperation" in error and "(request id " in error,
            "denied: error lacks UnauthorizedOperation or a request id")
    for secret in secrets:
        c.check(secret not in error, "denied: error contains a secret or session token")


def check_join(c, results, expected_nodes):
    if not results:
        c.check(False, "join: no result")
        return
    r = results[-1]
    c.line("AWS-REAL-JOIN nodes=%s expected=%d ownedPartitions=%s/%s refreshes=%s overruns=%s failures=%d tripped=%s"
           % (r.get("nodes"), expected_nodes, r.get("owned_partitions"), r.get("partitions"), r.get("refreshes"),
              r.get("overruns"), len(r.get("failures", [])), r.get("tripped")))
    for reason, n in Counter(r.get("failures", [])).most_common():
        c.line("AWS-REAL-JOIN-FAILURE count=%d %s" % (n, reason))
    if r.get("error") is not None:
        c.check(False, "join: " + r["error"])
    c.check(r.get("nodes") == expected_nodes, "join: %s nodes, expected %d" % (r.get("nodes"), expected_nodes))
    c.check(r.get("tripped") is False, "join: the provider tripped")


def check_no_key_ids(c, paths):
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as f:
            c.check(not ACCESS_KEY_ID.search(f.read()), "%s contains an access key id" % os.path.basename(path))


def main(argv=None, out=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--latency", required=True)
    parser.add_argument("--denied", required=True)
    parser.add_argument("--join", required=True)
    parser.add_argument("--runs", type=int, required=True)
    parser.add_argument("--expected-nodes", type=int, required=True)
    parser.add_argument("--excluded", default="", help="comma-separated private IPs")
    parser.add_argument("--deadline-ms", type=int, default=100)
    parser.add_argument("--timeout-ms", type=int, default=80)
    args = parser.parse_args(argv)

    excluded = [ip.strip() for ip in args.excluded.split(",") if ip.strip()]
    secrets = [v for v in (os.environ.get("AWS_REAL_DENIED_SECRET_ACCESS_KEY"),
                           os.environ.get("AWS_REAL_DENIED_SESSION_TOKEN")) if v]
    c = Checker(out)
    check_latency(c, load(args.latency), args.runs, args.expected_nodes, excluded, args.deadline_ms, args.timeout_ms)
    check_denied(c, load(args.denied), secrets)
    check_join(c, load(args.join), args.expected_nodes)
    check_no_key_ids(c, [args.latency, args.denied, args.join])
    c.line("RESULT FAIL" if c.failures else "RESULT PASS")
    return 1 if c.failures else 0


if __name__ == "__main__":
    sys.exit(main())
