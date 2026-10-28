#!/usr/bin/env python3
"""Serve dns and aws-tags fixture refreshes to a runner. See README.md, Provider fixtures."""

import argparse
import html
import ipaddress
import json
import re
import socketserver
import struct
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TYPE_A, TYPE_AAAA, TYPE_SRV = 1, 28, 33
TYPES = {"A": TYPE_A, "AAAA": TYPE_AAAA, "SRV": TYPE_SRV}
RCODE_SERVFAIL, RCODE_NXDOMAIN = 2, 3
EC2_VERSION = "2016-11-15"
UNANSWERED_HOLD = 60


def escape(text):
    return html.escape(text, quote=False)


class State:
    """The refresh the runner selected. Unanswered requests wait until the selection changes."""

    def __init__(self):
        self.lock = threading.Condition()
        self.region = None
        self.refresh = None
        self.generation = 0

    def select(self, selection):
        refresh = selection["refresh"]
        if not isinstance(refresh, dict):
            raise ValueError("refresh must be an object")
        with self.lock:
            self.region = selection.get("region")
            self.refresh = refresh
            self.generation += 1
            self.lock.notify_all()

    def current(self):
        with self.lock:
            return self.region, self.refresh, self.generation

    def hold(self, generation):
        with self.lock:
            self.lock.wait_for(lambda: self.generation != generation, timeout=UNANSWERED_HOLD)


def normalize(name):
    return name.rstrip(".").lower()


def read_name(data, offset):
    labels = []
    while True:
        length = data[offset]
        offset += 1
        if length == 0:
            return ".".join(labels), offset
        if length & 0xC0:
            raise ValueError("compressed name in question")
        labels.append(data[offset : offset + length].decode("ascii"))
        offset += length


def encode_name(name):
    out = b""
    for label in normalize(name).split("."):
        if label:
            out += bytes([len(label)]) + label.encode("ascii")
    return out + b"\0"


def dns_answer(state, query):
    """Return the response to one DNS query, or None to leave it unanswered."""
    qid, flags, qdcount = struct.unpack("!HHH", query[:6])
    if qdcount != 1:
        return None
    qname, offset = read_name(query, 12)
    qtype, qclass = struct.unpack("!HH", query[offset : offset + 4])
    question = query[12 : offset + 4]

    _, refresh, generation = state.current()
    if refresh is None or not ("records" in refresh or "rcode" in refresh or refresh.get("unanswered")):
        rcode, answers = RCODE_SERVFAIL, []
    elif refresh.get("unanswered"):
        state.hold(generation)
        return None
    elif refresh.get("rcode") == "SERVFAIL":
        rcode, answers = RCODE_SERVFAIL, []
    else:
        records = [r for r in refresh["records"] if normalize(r["name"]) == normalize(qname)]
        rcode = 0 if records else RCODE_NXDOMAIN
        answers = [r for r in records if TYPES[r["type"]] == qtype]

    body = b""
    for record in answers:
        if record["type"] == "A":
            rdata = ipaddress.IPv4Address(record["address"]).packed
        elif record["type"] == "AAAA":
            rdata = ipaddress.IPv6Address(record["address"]).packed
        else:
            rdata = struct.pack("!HHH", record["priority"], record["weight"], record["port"]) + encode_name(
                record["target"]
            )
        body += struct.pack("!HHHIH", 0xC00C, qtype, qclass, 1, len(rdata)) + rdata

    reply_flags = 0x8400 | (flags & 0x7900) | rcode
    return struct.pack("!HHHHHH", qid, reply_flags, 1, len(answers), 0, 0) + question + body


class DnsUdpHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        reply = dns_answer(self.server.state, data)
        if reply is not None:
            sock.sendto(reply, self.client_address)


class DnsTcpHandler(socketserver.BaseRequestHandler):
    def handle(self):
        header = self.request.recv(2)
        if len(header) < 2:
            return
        (length,) = struct.unpack("!H", header)
        data = b""
        while len(data) < length:
            chunk = self.request.recv(length - len(data))
            if not chunk:
                return
            data += chunk
        reply = dns_answer(self.server.state, data)
        if reply is not None:
            self.request.sendall(struct.pack("!H", len(reply)) + reply)


class ThreadingUdp(socketserver.ThreadingMixIn, socketserver.UDPServer):
    daemon_threads = True


class ThreadingTcp(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def signature_pattern(region):
    return re.compile(
        r"AWS4-HMAC-SHA256 Credential=[A-Z0-9]+/\d{8}/"
        + re.escape(region)
        + r"/ec2/aws4_request, SignedHeaders=([a-z0-9-]+;)*host(;[a-z0-9-]+)*, Signature=[0-9a-f]{64}"
    )


def filters(params):
    result = {}
    n = 1
    while f"Filter.{n}.Name" in params:
        values = []
        v = 1
        while f"Filter.{n}.Value.{v}" in params:
            values.append(params[f"Filter.{n}.Value.{v}"])
            v += 1
        result[params[f"Filter.{n}.Name"]] = values
        n += 1
    return result


def matches(instance, name, values):
    if name == "instance-state-name":
        return instance["state"] in values
    if name == "instance-id":
        return instance["id"] in values
    if name == "tag-key":
        return any(key in values for key in instance.get("tags", {}))
    if name.startswith("tag:"):
        return instance.get("tags", {}).get(name[4:]) in values
    raise KeyError(name)


def instance_xml(instance):
    fields = (
        ("privateIpAddress", "private_ip"),
        ("ipAddress", "public_ip"),
        ("privateDnsName", "private_dns"),
        ("dnsName", "public_dns"),
    )
    parts = [f"<instanceId>{escape(instance['id'])}</instanceId>"]
    parts.append(f"<instanceState><name>{escape(instance['state'])}</name></instanceState>")
    for element, key in fields:
        if key in instance:
            parts.append(f"<{element}>{escape(instance[key])}</{element}>")
    tags = "".join(
        f"<item><key>{escape(k)}</key><value>{escape(v)}</value></item>" for k, v in instance.get("tags", {}).items()
    )
    parts.append(f"<tagSet>{tags}</tagSet>")
    return f"<item><reservationId>r-{escape(instance['id'])}</reservationId><instancesSet><item>{''.join(parts)}</item></instancesSet></item>"


def error_xml(code, message, request_id):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<Response><Errors><Error><Code>{escape(code)}</Code><Message>{escape(message)}</Message></Error></Errors>"
        f"<RequestID>{escape(request_id)}</RequestID></Response>"
    )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def reply(self, status, body, content_type="text/xml;charset=UTF-8"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        state = self.server.state
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")

        if self.path == "/fixture/select":
            try:
                state.select(json.loads(body))
            except (KeyError, ValueError) as e:
                self.reply(400, f"{e}\n", "text/plain")
                return
            self.reply(200, "ok\n", "text/plain")
            return

        params = dict(urllib.parse.parse_qsl(body, keep_blank_values=True))
        region, refresh, generation = state.current()

        if refresh is None or region is None or not ("instances" in refresh or "error" in refresh or refresh.get("unanswered")):
            self.reply(503, error_xml("Unavailable", "no aws-tags refresh selected", "fixture-server"))
            return

        if refresh.get("unanswered"):
            state.hold(generation)
            self.close_connection = True
            return

        authorization = self.headers.get("Authorization", "")
        if not signature_pattern(region).fullmatch(authorization) or not self.headers.get("X-Amz-Date"):
            self.reply(401, error_xml("AuthFailure", "AWS was not able to validate the provided access credentials", "fixture-auth"))
            return

        if "error" in refresh:
            error = refresh["error"]
            self.reply(error["status"], error_xml(error["code"], error["message"], error["request_id"]))
            return

        if params.get("Action") != "DescribeInstances" or params.get("Version") != EC2_VERSION:
            self.reply(400, error_xml("InvalidAction", "The action or operation requested is not valid.", "fixture-action"))
            return

        try:
            selected = [i for i in refresh["instances"] if all(matches(i, n, v) for n, v in filters(params).items())]
        except KeyError as e:
            self.reply(400, error_xml("InvalidParameterValue", f"The filter '{e.args[0]}' is invalid", "fixture-filter"))
            return

        token = params.get("NextToken")
        start = int(token[5:]) if token and token.startswith("page-") else 0
        end = min(start + refresh.get("page_size", len(selected) or 1), len(selected))
        items = "".join(instance_xml(i) for i in selected[start:end])
        next_token = f"<nextToken>page-{end}</nextToken>" if end < len(selected) else ""
        self.reply(
            200,
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<DescribeInstancesResponse xmlns="http://ec2.amazonaws.com/doc/{EC2_VERSION}/">'
            f"<requestId>fixture-{generation}</requestId><reservationSet>{items}</reservationSet>{next_token}"
            "</DescribeInstancesResponse>",
        )


def dns_servers(state, host):
    for _ in range(20):
        udp = ThreadingUdp((host, 0), DnsUdpHandler)
        port = udp.server_address[1]
        try:
            tcp = ThreadingTcp((host, port), DnsTcpHandler)
        except OSError:
            udp.server_close()
            continue
        udp.state = tcp.state = state
        return udp, tcp, port
    raise OSError("no free port for DNS on both UDP and TCP")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    state = State()
    http = ThreadingHTTPServer((args.host, 0), Handler)
    http.daemon_threads = True
    http.state = state
    udp, tcp, dns_port = dns_servers(state, args.host)
    servers = [http, udp, tcp]
    ports = {"http": http.server_address[1], "dns": dns_port}

    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print(json.dumps(ports), flush=True)
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.server_close()


if __name__ == "__main__":
    main()
