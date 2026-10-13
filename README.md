# Aerospike client conformance

Expectations as data for behaviour every Aerospike client runtime must share: pluggable
discovery, address translation and certificate name matching. Every runtime runs
these rows unchanged and reports one result per row. The repository holds data and schemas only;
each client keeps its own runner and its own declarations.

Expected values are never adjusted to match a runtime.

## Versioning

`VERSION` is the fixture set version, one for the whole repository. Clients pin a release and
upgrade with a deliberate change that also updates their declarations. Every report records the
fixture set version it ran.

- patch: wording only (`because`, `description`, README)
- minor: new rows, new files or new optional keys
- major: a changed expectation, a removed or renamed row, or an incompatible schema change

## Layout

```
VERSION
schema/fixture.schema.json        every fixture file validates against this (JSON Schema 2020-12)
schema/report.schema.json         the report each runtime writes
discovery/translate/*.yaml        address translation (kind: translate)
discovery/merge/*.yaml            seed merge policy (kind: merge)
tls/name-matching.yaml            certificate name matching (kind: tls)
tls/certs/<shape>.pem             one certificate per shape in name-matching.yaml (no private keys)
tls/generate-certs.sh             regenerates tls/certs from the shapes (openssl, yq)
```

A file's path from the repository root, for example `tls/name-matching.yaml`, is its name in reports and
declarations.

## Format

A fixture file has `kind`, an optional `description` and `rows`; a `tls` file also has `shapes`.
Every row has:

| Key | |
|---|---|
| `case` | Name, unique within its file |
| `because` | One line |
| `config` | Configuration, using the public configuration surface |
| `input` | The input to the code under test |
| `expect` | The expected output. Absent on a blocked row |
| `state`, `blockedBy` | Optional. `state: blocked` with a reason; the row has no `expect` |

Values are strings, integers, booleans, null, lists and maps only.

- **translate**: `input` is the advertised endpoint (`nodeName`, `tlsName`, `host`, `port`,
  `sourceList` = `standard` | `alternate`). `expect` is the endpoint to connect to (`host`,
  `port`, `tlsName`), or `error`. `tlsName` is in every input and every expected endpoint, so
  every row checks that translation never changes it. An optional `diagnostic` means the
  runtime must surface the miss (log or event) while still returning the endpoint.
- **merge**: `config.seeds` are the static seeds, `input.discoveryResults` are applied in order
  (the first at client initialization, each later one as a refresh) and `expect.snapshots[i]` is
  the exact seed list after result `i`. Seeds compare by host and port.
- **tls**: `shapes` maps a shape name to a certificate's subject CN and SAN list (`DNS:` /
  `IP:`); the certificate for shape `x` is `certs/x.pem`. `input.tlsName` is the reference
  identifier and `input.shape` names the server certificate. `expect.outcome` is `accept` or
  `reject`. A runner must check each certificate matches its shape. Integration harnesses that
  run against a real server generate one certificate per shape at run time from the same map
  and start one server per shape, so cases sharing a shape share a server.

An expected error is a category plus the facts its message must contain, never an exception
type. `translation-miss` must name the node, the advertised host and port,
and the translator.

## Results

| Result | Meaning |
|---|---|
| `pass` | The runtime produced the expected output |
| `fail` | It did not |
| `unsupported` | The runtime does not implement the translator or option; reason required |
| `blocked` | Copied from the fixture row; the row is not run |

A runtime declares the result it expects for each row, with a one-line reason naming the owning
ticket or decision for every non-`pass` declaration. A row with no declaration is declared
`pass` (`blocked` for a blocked row). The runtime's test fails when any actual result differs
from its declaration, so a row changing in either direction forces a reviewed change to the
declaration. Declarations live in each client repository, next to its runner.

Each runtime writes a report conforming to `schema/report.schema.json`: runtime, client
version, fixture set version, counts per result, and per row the file, case, result and
reason.

## Unnamed config keys

These keys are not final. They are used consistently here and may be renamed:

| Key | Fixtures | Meaning |
|---|---|---|
| `legacyHostMap` | legacy-ip-map, on-miss | The pre-existing client host-to-host map, applied when no translator is configured |
| `translation.staticMap` | static-map, on-miss | `static-map` entries: advertised host to host |
| `translation.portMap` | port-map, on-miss | `port-map` entries, a list of `{match, target}` |
| `translation.portMap[].match.host`, `.match.port`, `.match.nodeName` | port-map, on-miss | Entry key: `(host, port)` or `nodeName` |
| `translation.portMap[].target.host`, `.target.port` | port-map, on-miss | Endpoint to connect to on a hit |
| `seeds` (`host`, `port`) | merge-policy | The static seed list |

## Contributing

Please read [CONTRIBUTING.md](CONTRIBUTING.md) for how to contribute to this repository.

## Security

For information on reporting security vulnerabilities, please see [SECURITY.md](SECURITY.md).

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.
