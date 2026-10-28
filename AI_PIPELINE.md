# AI_PIPELINE.md: aerospike-client-conformance

## Overview

Language-neutral conformance fixtures (YAML data plus JSON Schemas) that every Aerospike client
runtime runs unchanged: address translation, seed merge policy, and TLS certificate name
matching. There is no library code here. The only executable code is the Python validator
`scripts/validate-fixtures.py` and the shell script `tls/generate-certs.sh`.

## Build

There is no build. The release artifact is a zip produced by `.github/workflows/release.yml`:

```sh
git archive --format=zip --prefix="aerospike-client-conformance-$(cat VERSION)/" \
  -o "dist/aerospike-client-conformance-$(cat VERSION).zip" HEAD VERSION README.md LICENSE schema discovery tls
```

Only `VERSION`, `README.md`, `LICENSE`, `schema/`, `discovery/` and `tls/` ship. Anything placed
elsewhere is not published to client CI.

## Test

The fixture validator is the test suite (same as CI `validate-fixtures.yml`, Python 3.13). From the
repo root:

```sh
python3 -m venv venv
venv/bin/pip install -r scripts/requirements.txt
venv/bin/python scripts/validate-fixtures.py
```

Success prints `N fixture files checked` and exits 0; each failure prints
`<path>: <error>` and exits 1. No external services. It checks every `*.yaml` under `discovery/`
and `tls/` against `schema/fixture.schema.json`, plus: unique `case` per file, every
`input.shape` defined, every shape has `tls/certs/<shape>.pem` whose CN and SAN match exactly
(in order), every cert has a shape, and each merge row has one snapshot per discovery result.

There is no subset mode; the whole run takes about a second.

## Lint / Format / Type-check

CI runs Trunk Check (`.trunk/trunk.yaml`): prettier, yamllint, markdownlint (prettier style),
shellcheck, actionlint, checkov, trufflehog, dotenv-linter, git-diff-check. Locally:
`trunk check` (Trunk CLI 1.25.0; not installed on every machine). `.prettierrc.yaml` sets
`useTabs: true`. yamllint: quote strings only when needed, no duplicate keys, no implicit octal.

## Architecture & key directories

- `VERSION`: fixture set version for the whole repo (currently `0.1.0`).
- `schema/fixture.schema.json`: JSON Schema 2020-12 for every fixture file (`kind`: `translate`,
  `merge`, `tls`).
- `schema/report.schema.json`: the report each client runtime writes. Not exercised by the
  validator.
- `discovery/translate/*.yaml`: address translation rows (`kind: translate`).
- `discovery/merge/merge-policy.yaml`: seed merge policy rows (`kind: merge`).
- `tls/name-matching.yaml`: `shapes` map plus rows (`kind: tls`).
- `tls/certs/<shape>.pem`: one self-signed cert per shape, certificate only.
- `tls/generate-certs.sh`: regenerates all of `tls/certs/` from the shapes (needs `yq`, `openssl`).
- `scripts/validate-fixtures.py`, `scripts/requirements.txt`: the CI check and its pinned deps.
- `.github/workflows/`: `validate-fixtures.yml`, `trunk.yml`, `release.yml` (JFrog publish on
  `v*` tag, dry run on PRs), `pr-hygiene.yml`. `cicd-standard.yaml`/`cicd-composable.yaml` are
  disabled template leftovers.

## Conventions

- README.md is the format specification. Read its Format, Results and Unnamed config keys
  sections before touching fixtures.
- Expected values are never adjusted to match a runtime. A fixture records required behaviour.
- Row keys: `case` (lowercase kebab, unique in file), `because` (one line), `config`, `input`,
  `expect`. A blocked row has `state: blocked` and `blockedBy`, and no `expect`.
- Values are only strings, integers, booleans, null, lists and maps.
- Translate rows carry `tlsName` in both `input` and `expect` (translation must never change it).
- Expected errors are a category plus required message facts, never an exception type.
- Config keys use the public configuration surface; new provisional keys go in the README
  Unnamed config keys table.
- Versioning (bump `VERSION`): patch = wording only; minor = new rows, files or optional keys;
  major = changed expectation, removed or renamed row, or incompatible schema change.
- The file's repo-relative path is its identity in client reports and declarations, so renaming
  or moving a fixture file is a major change.
- Flow-style YAML (`{ ... }` / `[ ... ]`) is the house style for compact inputs and expectations.
- PR titles: conventional commits (`type(scope): description`), with a `[JIRA-123]` ticket for
  `feat`, `fix`, `docs`, `ci`, `refactor`.

## Gotchas

- Adding or changing a TLS shape requires regenerating its cert (`tls/generate-certs.sh`
  regenerates all certs, deleting `tls/certs/` first). SAN order in the cert must match the
  `san` list order. IP SANs are compared in canonical form.
- Never commit private keys. `*.key` and `*.p12` are gitignored and the generator deletes keys.
- A `.pem` in `tls/certs/` with no matching shape fails validation.
- `venv/` is gitignored; other venv names are not.
- `setup.sh`, `setup-github-protection.sh` and `SETUP.md` are repo-template scaffolding; do not
  edit them as part of fixture work.
