#!/usr/bin/env python3
"""Tests for discovery/providers/stub-ec2-server.py, run against the server as a separate process."""
import http.client
import json
import subprocess
import sys
import threading
import time
import unittest
import urllib.parse
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "discovery/providers/stub-ec2-server.py"
ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
SIGNATURE = "0123456789abcdef" * 4
AUTH = (
    f"AWS4-HMAC-SHA256 Credential={ACCESS_KEY}/20261028/us-east-1/ec2/aws4_request, "
    f"SignedHeaders=content-type;host;x-amz-date, Signature={SIGNATURE}"
)
DESCRIBE = {"Action": "DescribeInstances", "Version": "2016-11-15",
            "Filter.1.Name": "tag:aerospike-cluster", "Filter.1.Value.1": "prod-a"}


class StubEc2ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.process = subprocess.Popen(
            [sys.executable, str(SERVER), "--access-key", ACCESS_KEY, "--region", "us-east-1",
             "--instance", "i-01=127.0.0.1", "--instance", "i-02=192.0.2.2"],
            stdout=subprocess.PIPE, text=True)
        cls.port = json.loads(cls.process.stdout.readline())["http"]

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(5)
        cls.process.stdout.close()

    def setUp(self):
        self.assertEqual(200, self.request("POST", "/stub/reset")[0])

    def request(self, method, path, body=b"", headers=None, timeout=5):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, response.read().decode()
        finally:
            connection.close()

    def control(self, path, value):
        status, body = self.request("POST", path, json.dumps(value).encode())
        self.assertEqual(200, status, body)

    def ec2(self, params=None, authorization=AUTH, date="20261028T000000Z", timeout=5):
        headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
        if authorization is not None:
            headers["Authorization"] = authorization
        if date is not None:
            headers["X-Amz-Date"] = date
        return self.request("POST", "/", urllib.parse.urlencode(params or DESCRIBE).encode(), headers, timeout)

    def requests(self):
        status, body = self.request("GET", "/stub/requests")
        self.assertEqual(200, status)
        return json.loads(body)

    def test_describe_returns_every_instance_as_running(self):
        status, body = self.ec2()
        self.assertEqual(200, status)
        self.assertIn('<DescribeInstancesResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">', body)
        self.assertIn("<instanceId>i-01</instanceId>", body)
        self.assertIn("<privateIpAddress>127.0.0.1</privateIpAddress>", body)
        self.assertIn("<privateIpAddress>192.0.2.2</privateIpAddress>", body)
        self.assertEqual(2, body.count("<name>running</name>"))
        self.assertEqual(2, body.count("<key>aerospike-cluster</key>"))
        self.assertNotIn("nextToken", body)

    def test_paging_with_next_token(self):
        self.control("/stub/instances", [{"id": "i-03", "private_ip": "192.0.2.3"}])
        self.control("/stub/settings", {"page_size": 2})
        _, first = self.ec2()
        self.assertIn("i-01", first)
        self.assertIn("i-02", first)
        self.assertNotIn("i-03", first)
        self.assertIn("<nextToken>page-2</nextToken>", first)
        _, second = self.ec2(dict(DESCRIBE, NextToken="page-2"))
        self.assertIn("<privateIpAddress>192.0.2.3</privateIpAddress>", second)
        self.assertNotIn("i-01", second)
        self.assertNotIn("nextToken", second)

    def test_page_size_one_pages_through_every_instance(self):
        self.control("/stub/settings", {"page_size": 1})
        _, first = self.ec2()
        self.assertIn("<nextToken>page-1</nextToken>", first)
        _, second = self.ec2(dict(DESCRIBE, NextToken="page-1"))
        self.assertIn("i-02", second)
        self.assertNotIn("nextToken", second)

    def test_invalid_next_token(self):
        status, body = self.ec2(dict(DESCRIBE, NextToken="bogus"))
        self.assertEqual(400, status)
        self.assertIn("<Code>InvalidParameterValue</Code>", body)

    def test_auth_failure_body(self):
        status, body = self.ec2(authorization=None)
        self.assertEqual(401, status)
        self.assertEqual(
            '<?xml version="1.0" encoding="UTF-8"?>\n<Response><Errors><Error><Code>AuthFailure</Code>'
            "<Message>AWS was not able to validate the provided access credentials</Message></Error></Errors>"
            "<RequestID>ea966190-f9aa-478e-9ede-example</RequestID></Response>", body)

    def test_sigv4_header_check(self):
        bad = {
            "other access key": AUTH.replace(ACCESS_KEY, "AKIDOTHEREXAMPLE"),
            "other region": AUTH.replace("us-east-1", "eu-west-1"),
            "other service": AUTH.replace("/ec2/", "/sts/"),
            "other algorithm": AUTH.replace("AWS4-HMAC-SHA256", "AWS4-HMAC-SHA1"),
            "host not signed": AUTH.replace("content-type;host;x-amz-date", "content-type;x-amz-date"),
            "short signature": AUTH[:-1],
            "uppercase signature": AUTH.replace(SIGNATURE, SIGNATURE.upper()),
            "no date in scope": AUTH.replace("/20261028/", "/"),
            "not sigv4": "Bearer token",
        }
        for name, authorization in bad.items():
            with self.subTest(name):
                status, body = self.ec2(authorization=authorization)
                self.assertEqual(401, status)
                self.assertIn("<Code>AuthFailure</Code>", body)
        self.assertEqual(401, self.ec2(date=None)[0])
        self.assertEqual(200, self.ec2(authorization=AUTH.replace("content-type;host;x-amz-date", "host"))[0])

    def test_requests_are_recorded_with_validity_and_signature(self):
        self.ec2()
        self.ec2(authorization=AUTH.replace("us-east-1", "eu-west-1"))
        recorded = self.requests()
        self.assertEqual(2, len(recorded))
        self.assertEqual(DESCRIBE, recorded[0]["params"])
        self.assertEqual(AUTH, recorded[0]["authorization"])
        self.assertTrue(recorded[0]["authorization_valid"])
        self.assertEqual(SIGNATURE, recorded[0]["signature"])
        self.assertFalse(recorded[1]["authorization_valid"])

    def test_unauthorized_mode(self):
        self.control("/stub/settings", {"mode": "unauthorized"})
        status, body = self.ec2()
        self.assertEqual(403, status)
        self.assertIn("<Code>UnauthorizedOperation</Code>", body)
        self.assertIn("<Message>You are not authorized to perform this operation.</Message>", body)
        self.assertIn("<RequestID>ea966190-f9aa-478e-9ede-example</RequestID>", body)
        self.assertEqual(401, self.ec2(authorization=None)[0])

    def test_wrong_action_or_version(self):
        self.assertEqual(400, self.ec2(dict(DESCRIBE, Version="2014-01-01"))[0])
        status, body = self.ec2(dict(DESCRIBE, Action="DescribeVolumes"))
        self.assertEqual(400, status)
        self.assertIn("<Code>InvalidAction</Code>", body)

    def test_hang_until_the_mode_changes(self):
        self.control("/stub/settings", {"mode": "hang"})
        with self.assertRaises(OSError):
            self.ec2(timeout=0.5)
        result = {}
        thread = threading.Thread(target=lambda: result.update(error=self.capture(lambda: self.ec2(timeout=5))))
        thread.start()
        time.sleep(0.3)
        self.control("/stub/settings", {"mode": "answer"})
        thread.join(5)
        self.assertIsInstance(result["error"], http.client.RemoteDisconnected)
        self.assertEqual(200, self.ec2()[0])

    @staticmethod
    def capture(call):
        try:
            call()
        except Exception as e:
            return e
        return None

    def test_delay(self):
        self.control("/stub/settings", {"delay_ms": 300})
        started = time.monotonic()
        self.assertEqual(200, self.ec2()[0])
        self.assertGreaterEqual(time.monotonic() - started, 0.3)

    def test_reset_restores_the_initial_instances(self):
        self.control("/stub/instances", [{"id": "i-09", "private_ip": "192.0.2.9"}])
        self.control("/stub/settings", {"mode": "unauthorized", "page_size": 1, "delay_ms": 1})
        self.ec2()
        self.control("/stub/reset", {})
        self.assertEqual([], self.requests())
        status, body = self.ec2()
        self.assertEqual(200, status)
        self.assertNotIn("i-09", body)
        self.assertIn("i-02", body)
        self.assertNotIn("nextToken", body)

    def test_control_rejects_bad_input(self):
        for path, value in (("/stub/settings", {"mode": "sometimes"}), ("/stub/settings", {"page_size": 0}),
                            ("/stub/settings", {"delay_ms": -1}), ("/stub/settings", []),
                            ("/stub/instances", [{"id": "i-01"}]), ("/stub/instances", {})):
            with self.subTest(path=path, value=value):
                self.assertEqual(400, self.request("POST", path, json.dumps(value).encode())[0])
        self.assertEqual(404, self.request("POST", "/stub/unknown")[0])
        self.assertEqual(404, self.request("GET", "/")[0])
        self.assertEqual([], self.requests())


if __name__ == "__main__":
    unittest.main()
