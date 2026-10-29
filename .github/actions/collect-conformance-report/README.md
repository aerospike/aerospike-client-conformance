# Collect conformance report

Read every `conformance-report.json` under a directory, compare each report with the fixture set it ran against, append a step summary, and fail the job for the results that must not pass silently.

The report shape is `schema/report.schema.json` in this repository, which owns its validation; this action reads only the fields the gate needs. The fixture set is every `*.yaml` under `discovery/` and `tls/` in `fixtures-directory`; nothing else there is read. Each client runner writes one report. Declarations live in the client repository as `<runtime>-declared.yaml`. The Java file is `java-declared.yaml`: a fixture path, then `result` and `reason` per case that is not an implicit pass. The action installs `PyYAML==6.0.3` to read those files and the fixture files.

## Gate

`collect_report.py` applies the same rules; keep the two in step.

| Actual result                  | Declared result            | Job                                                    |
| ------------------------------ | -------------------------- | ------------------------------------------------------ |
| `fail`                         | `pass`, or no row          | Fails. This is a pass to fail regression.              |
| `unsupported`                  | anything but `unsupported` | Fails. The unsupported result was not declared.        |
| `blocked`                      | anything but `blocked`     | Fails. The fixture does not block this case.           |
| no row for a fixture case      | any                        | Fails. An empty or partial report cannot pass.         |
| row for a case not in fixtures | any                        | Fails. The report ran against a different fixture set. |
| `unsupported`                  | `unsupported`              | Stays green. The reason is still in the summary.       |
| `fail`                         | `fail`                     | Stays green. The failure stays in the summary.         |
| `blocked`                      | no row, fixture is blocked | Stays green. Blocked is copied from the fixture.       |

A case that is not in the declaration file is declared `pass`, except a case the fixture marks `state: blocked`, which is declared `blocked`. That matches the runner.

Every report's `fixtureSetVersion` must equal `VERSION` in `fixtures-directory`. A declaration for a fixture file or case that is not in the fixture set is an error, as it is in the Java runner.

Other differences, such as a declared failure that now passes, do not fail this action. The runtime test is what forces the declaration to be updated.

Run this step with `if: always()` so the summary is written when the runner itself failed. The runner still owns row-by-row equality. This action is the cross-runtime view that keeps a declared unsupported result from looking like a skipped test.

## Inputs

| Input                    | Required | Default | Description                                                                                        |
| ------------------------ | -------- | ------- | -------------------------------------------------------------------------------------------------- |
| `reports-directory`      | Yes      | —       | Directory searched for `conformance-report.json`                                                   |
| `fixtures-directory`     | Yes      | —       | Checkout of this repository at the commit the runner ran against                                   |
| `declarations-directory` | No       | `""`    | Directory searched for `<runtime>-declared.yaml`. Without it, every unsupported row is undeclared. |

## Outputs

| Output | Description                                                            |
| ------ | ---------------------------------------------------------------------- |
| `gate` | `success`, or `failure` when the gate trips or a report cannot be read |

## Example

```yaml
- uses: actions/checkout@<sha>
  with:
    repository: aerospike/aerospike-client-conformance
    ref: <sha>
    path: conformance

# ... run the client conformance test against conformance

- name: Collect conformance reports
  if: always()
  uses: aerospike/aerospike-client-conformance/.github/actions/collect-conformance-report@<sha>
  with:
    reports-directory: client/target
    fixtures-directory: conformance
    declarations-directory: client/src/test/resources/conformance
```

`reports-directory` may contain one report per runtime, including after `actions/download-artifact` with `merge-multiple: false`. The runtime name comes from the report, and it must match the `<runtime>` in `<runtime>-declared.yaml`. A separate aggregation job needs its own checkout of this repository for `fixtures-directory`.
