# Aerospike client conformance

Expectations as data for behaviour every Aerospike client runtime must share: pluggable
discovery, address translation and certificate name matching. Every runtime runs
these rows unchanged and reports one result per row. The repository holds data, schemas and the
check that keeps them consistent; each client keeps its own runner and its own declarations.

Expected values are never adjusted to match a runtime.

## Versioning

`VERSION` is the fixture set version, one for the whole repository. Clients pin a `v<VERSION>` tag and
upgrade with a deliberate change that also updates their declarations. Every report records the
fixture set version it ran.

- patch: wording only (`because`, `description`, README)
- minor: new rows, new files or new optional keys
- major: a changed expectation, a removed or renamed row, or an incompatible schema change

The repository is public, so client CI checks it out at a pinned tag or commit with `actions/checkout`
and passes that directory to its runner; nothing is published elsewhere. A `v<VERSION>` tag must match
`VERSION` at that commit, and `release.yml` checks that and the fixtures when the tag is pushed.

```yaml
- uses: actions/checkout@<sha>
  with:
    repository: aerospike/aerospike-client-conformance
    ref: v1.1.0
    path: conformance
```

## Layout

```text
VERSION
schema/fixture.schema.json        every fixture file validates against this (JSON Schema 2020-12)
schema/report.schema.json         the report each runtime writes
discovery/translate/*.yaml        address translation (kind: translate)
discovery/merge/*.yaml            seed merge policy (kind: merge)
tls/name-matching.yaml            certificate name matching (kind: tls)
tls/certs/<shape>.pem             one certificate per shape in name-matching.yaml (no private keys)
tls/generate-certs.sh             regenerates tls/certs from the shapes (openssl, yq)
scripts/validate-fixtures.py      the CI check for every fixture file (see Validation)
```

A file's path from the repository root, for example `tls/name-matching.yaml`, is its name in reports and
declarations.

## Format

A fixture file has `kind`, an optional `description` and `rows`; a `tls` file also has `shapes`.
Every row has:

| Key                  |                                                                   |
| -------------------- | ----------------------------------------------------------------- |
| `case`               | Name, unique within its file                                      |
| `because`            | One line                                                          |
| `config`             | Configuration, using the public configuration surface             |
| `input`              | The input to the code under test                                  |
| `expect`             | The expected output. Absent on a blocked row                      |
| `state`, `blockedBy` | Optional. `state: blocked` with a reason; the row has no `expect` |

Values are strings, integers, booleans, null, lists and maps only.

Config keys are the public configuration surface named by the PRD and the tech spec: `ip_map`,
`seeds`, `discovery.merge_policy` (`merge` | `replace` | `discovery_only`), `translation.provider`
(`static-map` | `port-map` | `pass-through`), `translation.on_miss` (`pass_through` | `strict`),
`translation.static_map` and `translation.port_map`. A row sets `ip_map` or `translation`, never
both.

- **translate**: `input` is the advertised endpoint (`nodeName`, `tlsName`, `host`, `port`,
  `sourceList` = `standard` | `alternate`). `expect` is the endpoint to connect to (`host`,
  `port`, `tlsName`), or `error`. `tlsName` is in every input and every expected endpoint, so
  every row checks that translation never changes it.
- **merge**: `config.seeds` are the static seeds, `input.discoveryResults` are applied in order
  (the first at client initialization, each later one as a refresh) and `expect.snapshots[i]` is
  the exact seed list after result `i`. Seeds compare by host and port.
- **tls**: `shapes` maps a shape name to a certificate's subject CN and SAN list (`DNS:` /
  `IP:`); the certificate for shape `x` is `certs/x.pem`. `input.tlsName` is the reference
  identifier and `input.shape` names the server certificate. `expect.outcome` is `accept` or
  `reject`. Integration harnesses that
  run against a real server generate one certificate per shape at run time from the same map
  and start one server per shape, so cases sharing a shape share a server.

An expected error is a category plus the facts its message must contain, never an exception
type. `translation-miss` must name the node, the advertised host and port,
and the translator.

## Validation

CI runs `scripts/validate-fixtures.py` on every pull request. It validates each fixture file
against the schema and checks what the schema cannot: case names are unique within a file, every
`input.shape` is defined, every shape has a certificate whose CN and SAN match it, every
certificate has a shape, and a merge row has one snapshot per discovery result. A released
fixture set has passed it, so runners can trust the format and only read the rows.

```sh
pip install -r scripts/requirements.txt
python scripts/validate-fixtures.py
```

## Results

| Result        | Meaning                                                                  |
| ------------- | ------------------------------------------------------------------------ |
| `pass`        | The runtime produced the expected output                                 |
| `fail`        | It did not                                                               |
| `unsupported` | The runtime does not implement the translator or option; reason required |
| `blocked`     | Copied from the fixture row; the row is not run                          |

A runtime declares the result it expects for each row, with a one-line reason naming the owning
ticket or decision for every non-`pass` declaration. A row with no declaration is declared
`pass` (`blocked` for a blocked row). The runtime's test fails when any actual result differs
from its declaration, so a row changing in either direction forces a reviewed change to the
declaration. Declarations live in each client repository, next to its runner.

Each runtime writes a report conforming to `schema/report.schema.json`: runtime, client
version, fixture set version, counts per result, and per row the file, case, result and
reason.

## Unnamed config keys

The tech spec defines `translation.port_map` as "Keys are host:port and node name. Values are
host:port." It does not state the grammar below, which the fixtures use until the spec owner
ratifies or replaces it:

| `translation.port_map` | Fixtures          | Rule                                                                                                                              |
| ---------------------- | ----------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Key                    | port-map, on-miss | A key containing `:` outside brackets, or a bracketed key, is `host:port`; any other key is a node name (hex, never contains `:`) |
| Value                  | port-map, on-miss | Always `host:port`                                                                                                                |
| IPv6 host              | port-map, on-miss | Written in brackets, in keys and values: `[2001:db8::10]:3000`                                                                    |

## Contributing

Please read [CONTRIBUTING.md](CONTRIBUTING.md) for how to contribute to this repository.

## Security

For information on reporting security vulnerabilities, please see [SECURITY.md](SECURITY.md).

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.
