#!/usr/bin/env python3
"""Stub EC2 Query API endpoint for aws-tags integration tests. See envs/README.md, aws-tags.

Answers DescribeInstances with the current instances, split into pages of page_size with NextToken,
and records every request. It checks only that each request carries a well-formed SigV4
Authorization header for the access key, the region and service ec2, and an X-Amz-Date header, and
answers AuthFailure otherwise. Response bodies follow the EC2 API Reference: the DescribeInstances
response example, and the error response example of "Error codes for the Amazon EC2 API".

Control API, JSON over HTTP on the same port:
  GET  /stub/requests   every recorded request: params, authorization, authorization_valid, signature
  POST /stub/reset      restore the instances given on the command line, mode answer, page size 1000,
                        no delay, and clear the recorded requests
  POST /stub/instances  append instances: [{"id": "i-...", "private_ip": "10.0.0.1"}, ...]
  POST /stub/settings   change any of {"mode": "answer" | "unauthorized" | "hang", "page_size": n, "delay_ms": n}
In mode hang a request is held until the mode changes, or for 60 seconds, then closed without a reply.
"""

import argparse
import html
import json
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EC2_VERSION = "2016-11-15"
ERROR_REQUEST_ID = "ea966190-f9aa-478e-9ede-example"
MODES = ("answer", "unauthorized", "hang")
HANG_HOLD = 60
PAGE_SIZE = 1000


def escape(text):
    return html.escape(text, quote=False)


def authorization_pattern(access_key, region):
    return re.compile(
        "AWS4-HMAC-SHA256 Credential="
        + re.escape(access_key)
        + r"/\d{8}/"
        + re.escape(region)
        + r"/ec2/aws4_request, SignedHeaders=([a-z0-9-]+;)*host(;[a-z0-9-]+)*, Signature=[0-9a-f]{64}"
    )


class State:
    def __init__(self, initial):
        self.lock = threading.Condition()
        self.initial = list(initial)
        self.requests = []
        self.reset()

    def reset(self):
        with self.lock:
            self.instances = list(self.initial)
            self.requests = []
            self.page_size = PAGE_SIZE
            self.delay_ms = 0
            self.set_mode("answer")

    def set_mode(self, mode):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        with self.lock:
            self.mode = mode
            self.lock.notify_all()

    def settings(self, settings):
        if not isinstance(settings, dict):
            raise ValueError("settings must be an object")
        with self.lock:
            if "page_size" in settings:
                if not isinstance(settings["page_size"], int) or settings["page_size"] < 1:
                    raise ValueError("page_size must be a positive integer")
                self.page_size = settings["page_size"]
            if "delay_ms" in settings:
                if not isinstance(settings["delay_ms"], int) or settings["delay_ms"] < 0:
                    raise ValueError("delay_ms must be a non-negative integer")
                self.delay_ms = settings["delay_ms"]
            if "mode" in settings:
                self.set_mode(settings["mode"])

    def add(self, instances):
        if not isinstance(instances, list) or not all(
            isinstance(i, dict) and isinstance(i.get("id"), str) and isinstance(i.get("private_ip"), str) for i in instances
        ):
            raise ValueError('instances must be a list of {"id": ..., "private_ip": ...}')
        with self.lock:
            self.instances.extend((i["id"], i["private_ip"]) for i in instances)

    def hold(self):
        with self.lock:
            self.lock.wait_for(lambda: self.mode != "hang", timeout=HANG_HOLD)


def describe_xml(instances, start, page_size):
    end = min(len(instances), start + page_size)
    items = []
    for instance_id, private_ip in instances[start:end]:
        items.append(
            f"    <item>\n      <reservationId>r-{escape(instance_id)}</reservationId>\n"
            "      <ownerId>123456789012</ownerId>\n      <groupSet/>\n      <instancesSet>\n        <item>\n"
            f"          <instanceId>{escape(instance_id)}</instanceId>\n"
            "          <imageId>ami-0abcdef1234567890</imageId>\n"
            "          <instanceState>\n            <code>16</code>\n            <name>running</name>\n"
            "          </instanceState>\n"
            f"          <privateDnsName>ip-{escape(instance_id)}.ec2.internal</privateDnsName>\n"
            "          <instanceType>c5.large</instanceType>\n"
            f"          <privateIpAddress>{escape(private_ip)}</privateIpAddress>\n"
            "          <tagSet>\n            <item>\n              <key>aerospike-cluster</key>\n"
            "              <value>prod-a</value>\n            </item>\n          </tagSet>\n"
            "        </item>\n      </instancesSet>\n    </item>\n"
        )
    next_token = f"  <nextToken>page-{end}</nextToken>\n" if end < len(instances) else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<DescribeInstancesResponse xmlns="http://ec2.amazonaws.com/doc/{EC2_VERSION}/">\n'
        "  <requestId>8f7724cf-496f-496e-8fe3-example</requestId>\n  <reservationSet>\n"
        f"{''.join(items)}  </reservationSet>\n{next_token}</DescribeInstancesResponse>\n"
    )


def error_xml(code, message):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<Response><Errors><Error><Code>{code}</Code><Message>{message}</Message></Error></Errors>"
        f"<RequestID>{ERROR_REQUEST_ID}</RequestID></Response>"
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

    def body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8")

    def do_GET(self):
        state = self.server.state
        if self.path != "/stub/requests":
            self.reply(404, "not found\n", "text/plain")
            return
        with state.lock:
            requests = list(state.requests)
        self.reply(200, json.dumps(requests), "application/json")

    def do_POST(self):
        if self.path.startswith("/stub/"):
            self.control(self.body())
        else:
            self.ec2(self.body())

    def control(self, body):
        state = self.server.state
        try:
            if self.path == "/stub/reset":
                state.reset()
            elif self.path == "/stub/instances":
                state.add(json.loads(body))
            elif self.path == "/stub/settings":
                state.settings(json.loads(body))
            else:
                self.reply(404, "not found\n", "text/plain")
                return
        except ValueError as e:
            self.reply(400, f"{e}\n", "text/plain")
            return
        self.reply(200, "ok\n", "text/plain")

    def ec2(self, body):
        state = self.server.state
        params = dict(urllib.parse.parse_qsl(body, keep_blank_values=True))
        authorization = self.headers.get("Authorization")
        valid = (
            authorization is not None
            and self.server.authorization.fullmatch(authorization) is not None
            and self.headers.get("X-Amz-Date") is not None
        )
        signature = authorization[authorization.rfind("=") + 1 :] if authorization else ""
        with state.lock:
            state.requests.append(
                {"params": params, "authorization": authorization, "authorization_valid": valid, "signature": signature}
            )
            mode, delay_ms, page_size, instances = state.mode, state.delay_ms, state.page_size, list(state.instances)

        if mode == "hang":
            state.hold()
            self.close_connection = True
            return

        if delay_ms > 0:
            time.sleep(delay_ms / 1000)

        if not valid:
            self.reply(401, error_xml("AuthFailure", "AWS was not able to validate the provided access credentials"))
        elif mode == "unauthorized":
            self.reply(403, error_xml("UnauthorizedOperation", "You are not authorized to perform this operation."))
        elif params.get("Action") != "DescribeInstances" or params.get("Version") != EC2_VERSION:
            self.reply(400, error_xml("InvalidAction", "The action or operation requested is not valid."))
        else:
            token = params.get("NextToken")
            if token is not None and not re.fullmatch(r"page-\d+", token):
                self.reply(400, error_xml("InvalidParameterValue", "The NextToken value is not valid."))
                return
            start = int(token[5:]) if token else 0
            self.reply(200, describe_xml(instances, start, page_size))


def instance(text):
    instance_id, sep, private_ip = text.partition("=")
    if not sep or not instance_id or not private_ip:
        raise argparse.ArgumentTypeError("expected <instance id>=<private ip>")
    return instance_id, private_ip


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 picks a free port")
    parser.add_argument("--access-key", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--instance", type=instance, action="append", default=[], help="<instance id>=<private ip>")
    args = parser.parse_args()

    http = ThreadingHTTPServer((args.host, args.port), Handler)
    http.daemon_threads = True
    http.state = State(args.instance)
    http.authorization = authorization_pattern(args.access_key, args.region)
    print(json.dumps({"http": http.server_address[1]}), flush=True)
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        http.server_close()


if __name__ == "__main__":
    main()
