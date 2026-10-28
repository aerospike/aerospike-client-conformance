#!/usr/bin/env python3
"""Validate every fixture file against schema/fixture.schema.json, plus the rules the schema cannot express."""
import ipaddress
import json
import sys
from pathlib import Path

import yaml
from cryptography import x509
from cryptography.x509.oid import NameOID
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIRS = ["discovery", "tls"]


def canonical_san(name):
    kind, _, value = name.partition(":")
    return f"IP:{ipaddress.ip_address(value)}" if kind == "IP" else name


def host_port_error(value):
    host, _, port = value.rpartition(":")
    if not 0 <= int(port) <= 65535:
        return f"port {port} out of range"
    if host.startswith("["):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            return f"{host} is not a bracketed IPv6 address"
    return None


def cert_names(path):
    cert = x509.load_pem_x509_certificate(path.read_bytes())
    cn = [a.value for a in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)]
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return cn, []
    san = []
    for name in ext:
        if isinstance(name, x509.DNSName):
            san.append(f"DNS:{name.value}")
        elif isinstance(name, x509.IPAddress):
            san.append(f"IP:{name.value}")
        else:
            san.append(f"{type(name).__name__}:{name.value}")
    return cn, san


def host_key(host):
    return (host["host"], host["port"], host.get("tlsName"))


def check_provider_row(row):
    errors = []
    refreshes = row["input"]["refreshes"]
    expected = row["expect"]["refreshes"]
    if len(expected) != len(refreshes):
        errors.append("expect.refreshes count differs from input.refreshes count")
        return errors
    if "error" in expected[0]:
        errors.append("refresh 0 runs at client initialization and must return hosts")
    options = row["config"]["discovery"].get("dns") or row["config"]["discovery"].get("aws")
    if any(r.get("unanswered") for r in refreshes) and "timeout_ms" not in options:
        errors.append("a row with an unanswered refresh must set timeout_ms")
    previous = []
    for i, step in enumerate(expected):
        seeds = sorted(map(host_key, step["seeds"]))
        if "error" in step or (i > 0 and not step["hosts"]):
            if seeds != previous:
                errors.append(f"refresh {i}: seeds must be the previous seeds when the refresh returns no host")
        elif seeds != sorted(map(host_key, step["hosts"])):
            errors.append(f"refresh {i}: seeds must equal hosts")
        previous = seeds
    for i, refresh in enumerate(refreshes):
        for record in refresh.get("records", []):
            if record["type"] in ("A", "AAAA"):
                try:
                    address = ipaddress.ip_address(record["address"])
                except ValueError:
                    errors.append(f"refresh {i}: {record['name']}: {record['address']} is not an IP address")
                    continue
                if (address.version == 4) != (record["type"] == "A"):
                    errors.append(f"refresh {i}: {record['name']}: {record['address']} is not a {record['type']} address")
        ids = [instance["id"] for instance in refresh.get("instances", [])]
        if len(ids) != len(set(ids)):
            errors.append(f"refresh {i}: duplicate instance id")
    return errors


def check_file(path, validator):
    errors = []
    doc = yaml.safe_load(path.read_text())

    for e in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path)):
        errors.append(f"/{'/'.join(str(p) for p in e.absolute_path)}: {e.message}")
    if errors or not isinstance(doc, dict):
        return errors

    seen = set()
    for row in doc["rows"]:
        if row["case"] in seen:
            errors.append(f"{row['case']}: duplicate case")
        seen.add(row["case"])

    if doc["kind"] == "translate":
        for row in doc["rows"]:
            port_map = row["config"].get("translation", {}).get("port_map", {})
            for key, value in port_map.items():
                for kind, text in (("key", key), ("value", value)):
                    if (kind == "value" or ":" in text) and (error := host_port_error(text)):
                        errors.append(f"{row['case']}: port_map {kind} {text}: {error}")

    if doc["kind"] == "merge":
        for row in doc["rows"]:
            if "expect" in row and len(row["expect"]["snapshots"]) != len(row["input"]["discoveryResults"]):
                errors.append(f"{row['case']}: snapshot count differs from discoveryResults count")

    if doc["kind"] in ("dns", "aws-tags"):
        for row in doc["rows"]:
            if "expect" in row:
                errors += [f"{row['case']}: {error}" for error in check_provider_row(row)]

    if doc["kind"] == "tls":
        shapes = doc["shapes"]
        certs = path.parent / "certs"
        for row in doc["rows"]:
            if row["input"]["shape"] not in shapes:
                errors.append(f"{row['case']}: unknown shape {row['input']['shape']}")
        for name, shape in shapes.items():
            pem = certs / f"{name}.pem"
            if not pem.is_file():
                errors.append(f"shape {name}: {pem.relative_to(ROOT)} not found")
                continue
            cn, san = cert_names(pem)
            if cn != [shape["subjectCN"]]:
                errors.append(f"shape {name}: certificate CN {cn} does not match subjectCN {shape['subjectCN']}")
            if san != [canonical_san(s) for s in shape["san"]]:
                errors.append(f"shape {name}: certificate SAN {san} does not match san {shape['san']}")
        for pem in sorted(certs.glob("*.pem")):
            if pem.stem not in shapes:
                errors.append(f"{pem.relative_to(ROOT)}: no shape {pem.stem}")
    return errors


def main():
    validator = Draft202012Validator(json.loads((ROOT / "schema/fixture.schema.json").read_text()))
    files = sorted(p for d in FIXTURE_DIRS for p in (ROOT / d).rglob("*.yaml"))
    failed = False

    for path in files:
        for error in check_file(path, validator):
            print(f"{path.relative_to(ROOT)}: {error}")
            failed = True

    print(f"{len(files)} fixture files checked")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
