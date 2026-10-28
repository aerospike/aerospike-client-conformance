import io
import json
import os
import tempfile
import unittest
from unittest import mock

import check

HOSTS = ["10.71.1.11:3000", "10.71.1.12:3000", "10.71.1.13:3000"]
DENIED = ("Error -1,0: AWS EC2 seed provider: DescribeInstances failed: HTTP 403 UnauthorizedOperation: "
          "You are not authorized to perform this operation. (request id bf169d81-b2e3)")


def latency(**fields):
    run = {"mode": "latency", "credentials_source": "imds", "hosts": HOSTS, "cold_us": 650000,
           "warm_us": [60000, 81000, 121000], "warm_failures": []}
    run.update(fields)
    return run


def join(**fields):
    result = {"mode": "join", "nodes": 3, "owned_partitions": 4096, "partitions": 4096, "refreshes": 30,
              "overruns": 1, "failures": [], "tripped": False}
    result.update(fields)
    return result


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def write(self, name, objects, noise=True):
        path = os.path.join(self.dir.name, name)
        with open(path, "w", encoding="utf-8") as f:
            if noise:
                f.write("[INFO] build output that is not JSON\n")
            for o in objects:
                f.write(json.dumps(o) + "\n")
        return path

    def run_check(self, runs, denied, joined, expected_runs=None, excluded="10.71.1.21,10.71.1.22"):
        argv = ["--latency", self.write("latency.jsonl", runs), "--denied", self.write("denied.jsonl", denied),
                "--join", self.write("join.jsonl", joined), "--runs", str(expected_runs or len(runs)),
                "--expected-nodes", "3", "--excluded", excluded]
        out = io.StringIO()
        rc = check.main(argv, out)
        return rc, out.getvalue()

    def test_passing_run(self):
        rc, out = self.run_check([latency(), latency(cold_us=700000)], [{"mode": "denied", "error": DENIED}], [join()])
        self.assertEqual(0, rc, out)
        self.assertIn("RESULT PASS", out)
        self.assertIn("AWS-REAL-LATENCY cold min=650.0ms median=700.0ms p99=700.0ms max=700.0ms runs=2", out)
        self.assertIn("warmOverTimeout=4 warmOverDeadline=2", out)
        self.assertIn("coldOverTimeout=2 coldOverDeadline=2", out)

    def test_warm_failure_reasons_are_counted(self):
        rc, out = self.run_check([latency(warm_failures=["timeout after 80ms", "timeout after 80ms"])],
                                 [{"error": DENIED}], [join()])
        self.assertEqual(0, rc, out)
        self.assertIn("failures=2", out)
        self.assertIn("AWS-REAL-WARM-FAILURE count=2 timeout after 80ms", out)

    def test_credentials_not_from_imds_fail(self):
        rc, out = self.run_check([latency(credentials_source="environment")], [{"error": DENIED}], [join()])
        self.assertEqual(1, rc)
        self.assertIn("credentials from environment, expected imds", out)

    def test_excluded_host_in_seeds_fails(self):
        rc, out = self.run_check([latency(hosts=HOSTS[:2] + ["10.71.1.22:3000"])], [{"error": DENIED}], [join()])
        self.assertEqual(1, rc)
        self.assertIn("excluded 10.71.1.22", out)

    def test_wrong_seed_count_fails(self):
        rc, out = self.run_check([latency(hosts=HOSTS[:2])], [{"error": DENIED}], [join()])
        self.assertEqual(1, rc)
        self.assertIn("2 seeds, expected 3", out)

    def test_missing_runs_fail(self):
        rc, out = self.run_check([latency()], [{"error": DENIED}], [join()], expected_runs=20)
        self.assertEqual(1, rc)
        self.assertIn("1 runs reported, expected 20", out)

    def test_latency_error_fails(self):
        rc, out = self.run_check([latency(error="cannot reach IMDS")], [{"error": DENIED}], [join()])
        self.assertEqual(1, rc)
        self.assertIn("latency run 1: cannot reach IMDS", out)

    def test_denied_not_refused_fails(self):
        rc, out = self.run_check([latency()], [{"mode": "denied", "hosts": HOSTS}], [join()])
        self.assertEqual(1, rc)
        self.assertIn("was not refused", out)

    def test_denied_without_request_id_fails(self):
        rc, out = self.run_check([latency()], [{"error": "HTTP 403 UnauthorizedOperation"}], [join()])
        self.assertEqual(1, rc)
        self.assertIn("lacks UnauthorizedOperation or a request id", out)

    def test_denied_error_with_secret_fails(self):
        with mock.patch.dict(os.environ, {"AWS_REAL_DENIED_SECRET_ACCESS_KEY": "s3cr3tValue"}):
            rc, out = self.run_check([latency()], [{"error": DENIED + " s3cr3tValue"}], [join()])
        self.assertEqual(1, rc)
        self.assertIn("contains a secret", out)
        self.assertNotIn("s3cr3tValue", out)

    def test_join_tripped_fails(self):
        rc, out = self.run_check([latency()], [{"error": DENIED}], [join(tripped=True)])
        self.assertEqual(1, rc)
        self.assertIn("the provider tripped", out)

    def test_join_missing_node_fails(self):
        rc, out = self.run_check([latency()], [{"error": DENIED}], [join(nodes=2, error="nodes=2 owned=2730 after 60s")])
        self.assertEqual(1, rc)
        self.assertIn("join: 2 nodes, expected 3", out)
        self.assertIn("join: nodes=2 owned=2730 after 60s", out)

    def test_access_key_id_in_output_fails(self):
        rc, out = self.run_check([latency(warm_failures=["signed with ASIAABCDEFGHIJKLMNOP"])], [{"error": DENIED}],
                                 [join()])
        self.assertEqual(1, rc)
        self.assertIn("latency.jsonl contains an access key id", out)

    def test_no_results_fail(self):
        rc, out = self.run_check([], [], [], expected_runs=20)
        self.assertEqual(1, rc)
        self.assertIn("denied: no result", out)
        self.assertIn("join: no result", out)


if __name__ == "__main__":
    unittest.main()
