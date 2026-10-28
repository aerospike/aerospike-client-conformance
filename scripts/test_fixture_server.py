#!/usr/bin/env python3
"""Tests for discovery/providers/fixture-server.py, run against the server as a separate process."""
import http.client
import ipaddress
import json
import socket
import struct
import subprocess
import sys
import unittest
import urllib.parse
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "discovery/providers/fixture-server.py"
AUTH = (
    "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261028/us-east-1/ec2/aws4_request, "
    "SignedHeaders=content-type;host;x-amz-date, Signature=" + "a" * 64
)


def query(name, qtype, qid=0x1234):
    labels = b"".join(bytes([len(p)]) + p.encode() for p in name.split("."))
    return struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0) + labels + b"\0" + struct.pack("!HH", qtype, 1)


def skip_name(data, offset):
    while True:
        length = data[offset]
        if length & 0xC0:
            return offset + 2
        offset += 1
        if length == 0:
            return offset
        offset += length


def read_name(data, offset):
    labels = []
    while data[offset]:
        labels.append(data[offset + 1 : offset + 1 + data[offset]].decode())
        offset += 1 + data[offset]
    return ".".join(labels)


def parse(reply):
    qid, flags, _, ancount, _, _ = struct.unpack("!HHHHHH", reply[:12])
    offset = skip_name(reply, 12) + 4
    answers = []
    for _ in range(ancount):
        offset = skip_name(reply, offset)
        rtype, _, _, length = struct.unpack("!HHIH", reply[offset : offset + 10])
        rdata = reply[offset + 10 : offset + 10 + length]
        if rtype in (1, 28):
            answers.append(str(ipaddress.ip_address(rdata)))
        else:
            priority, weight, port = struct.unpack("!HHH", rdata[:6])
            answers.append((priority, weight, port, read_name(rdata, 6)))
        offset += 10 + length
    return qid, flags & 0xF, answers


class FixtureServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.process = subprocess.Popen([sys.executable, str(SERVER)], stdout=subprocess.PIPE, text=True)
        cls.ports = json.loads(cls.process.stdout.readline())

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(5)
        cls.process.stdout.close()

    def select(self, refresh, region=None):
        body = json.dumps({"refresh": refresh, "region": region}).encode()
        status, _ = self.post("/fixture/select", body)
        self.assertEqual(200, status)

    def post(self, path, body, headers=None, timeout=5):
        connection = http.client.HTTPConnection("127.0.0.1", self.ports["http"], timeout=timeout)
        try:
            connection.request("POST", path, body, headers or {})
            response = connection.getresponse()
            return response.status, response.read().decode()
        finally:
            connection.close()

    def udp(self, name, qtype, timeout=2):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(query(name, qtype), ("127.0.0.1", self.ports["dns"]))
            return parse(s.recv(4096))

    def tcp(self, name, qtype):
        with socket.create_connection(("127.0.0.1", self.ports["dns"]), timeout=2) as s:
            q = query(name, qtype)
            s.sendall(struct.pack("!H", len(q)) + q)
            (length,) = struct.unpack("!H", s.recv(2))
            data = b""
            while len(data) < length:
                data += s.recv(length - len(data))
            return parse(data)

    def ec2(self, params, authorization=AUTH, timeout=5):
        body = urllib.parse.urlencode(params).encode()
        headers = {"Authorization": authorization, "X-Amz-Date": "20261028T000000Z"}
        return self.post("/", body, headers, timeout)

    def test_a_and_aaaa_answers(self):
        self.select({"records": [
            {"name": "seeds.dns.test", "type": "A", "address": "192.0.2.11"},
            {"name": "seeds.dns.test", "type": "AAAA", "address": "2001:db8::11"},
        ]})
        self.assertEqual((0x1234, 0, ["192.0.2.11"]), self.udp("seeds.dns.test", 1))
        self.assertEqual((0x1234, 0, ["2001:db8::11"]), self.udp("Seeds.DNS.test", 28))

    def test_srv_answer_over_tcp(self):
        self.select({"records": [
            {"name": "_aerospike._tcp.dns.test", "type": "SRV", "priority": 10, "weight": 5, "port": 3101,
             "target": "node-a.dns.test"},
        ]})
        self.assertEqual((0x1234, 0, [(10, 5, 3101, "node-a.dns.test")]), self.tcp("_aerospike._tcp.dns.test", 33))

    def test_unknown_name_is_nxdomain_and_known_name_without_type_is_empty(self):
        self.select({"records": [{"name": "seeds.dns.test", "type": "A", "address": "192.0.2.11"}]})
        self.assertEqual((0x1234, 3, []), self.udp("missing.dns.test", 1))
        self.assertEqual((0x1234, 0, []), self.udp("seeds.dns.test", 28))

    def test_servfail(self):
        self.select({"rcode": "SERVFAIL"})
        self.assertEqual((0x1234, 2, []), self.udp("seeds.dns.test", 1))

    def test_unanswered_dns(self):
        self.select({"unanswered": True})
        with self.assertRaises(socket.timeout):
            self.udp("seeds.dns.test", 1, timeout=0.5)
        self.select({"records": []})

    def instances(self):
        return {"instances": [
            {"id": "i-01", "state": "running", "private_ip": "10.0.0.1", "tags": {"aerospike-cluster": "prod-a"}},
            {"id": "i-02", "state": "stopped", "private_ip": "10.0.0.2", "tags": {"aerospike-cluster": "prod-a"}},
            {"id": "i-03", "state": "running", "private_ip": "10.0.0.3", "tags": {"Name": "web"}},
            {"id": "i-04", "state": "running", "private_ip": "10.0.0.4", "tags": {"aerospike-cluster": "prod-a"}},
        ]}

    def describe(self, **extra):
        params = {"Action": "DescribeInstances", "Version": "2016-11-15",
                  "Filter.1.Name": "tag:aerospike-cluster", "Filter.1.Value.1": "prod-a",
                  "Filter.2.Name": "instance-state-name", "Filter.2.Value.1": "running"}
        params.update(extra)
        return params

    def test_describe_filters_by_tag_and_state(self):
        self.select(self.instances(), "us-east-1")
        status, body = self.ec2(self.describe())
        self.assertEqual(200, status)
        self.assertIn("<privateIpAddress>10.0.0.1</privateIpAddress>", body)
        self.assertIn("<privateIpAddress>10.0.0.4</privateIpAddress>", body)
        self.assertNotIn("10.0.0.2", body)
        self.assertNotIn("10.0.0.3", body)
        self.assertNotIn("nextToken", body)

    def test_describe_without_state_filter_returns_stopped(self):
        self.select(self.instances(), "us-east-1")
        status, body = self.ec2({"Action": "DescribeInstances", "Version": "2016-11-15",
                                 "Filter.1.Name": "tag:aerospike-cluster", "Filter.1.Value.1": "prod-a"})
        self.assertEqual(200, status)
        self.assertIn("10.0.0.2", body)

    def test_pagination(self):
        refresh = self.instances()
        refresh["page_size"] = 1
        self.select(refresh, "us-east-1")
        status, first = self.ec2(self.describe())
        self.assertIn("<nextToken>page-1</nextToken>", first)
        self.assertIn("10.0.0.1", first)
        status, second = self.ec2(self.describe(NextToken="page-1"))
        self.assertIn("10.0.0.4", second)
        self.assertNotIn("nextToken", second)

    def test_signature_shape_checked(self):
        self.select(self.instances(), "us-east-1")
        self.assertEqual(401, self.ec2(self.describe(), AUTH.replace("us-east-1", "eu-west-1"))[0])
        self.assertEqual(401, self.ec2(self.describe(), AUTH.replace("/ec2/", "/sts/"))[0])
        self.assertEqual(401, self.ec2(self.describe(), "Bearer x")[0])

    def test_wrong_action_or_version(self):
        self.select(self.instances(), "us-east-1")
        self.assertEqual(400, self.ec2(self.describe(Version="2014-01-01"))[0])

    def test_error_response(self):
        self.select({"error": {"status": 403, "code": "UnauthorizedOperation", "message": "denied",
                               "request_id": "req-1"}}, "us-east-1")
        status, body = self.ec2(self.describe())
        self.assertEqual(403, status)
        self.assertIn("<Code>UnauthorizedOperation</Code>", body)
        self.assertIn("<RequestID>req-1</RequestID>", body)

    def test_unanswered_ec2(self):
        self.select({"unanswered": True}, "us-east-1")
        with self.assertRaises(OSError):
            self.ec2(self.describe(), timeout=0.5)
        self.select(self.instances(), "us-east-1")

    def test_select_rejects_a_refresh_that_is_not_an_object(self):
        status, _ = self.post("/fixture/select", b'{"refresh": 3}')
        self.assertEqual(400, status)


if __name__ == "__main__":
    unittest.main()
