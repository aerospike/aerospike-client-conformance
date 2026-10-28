# aws-tags against real AWS

`run.sh` runs one client's `aws-tags` provider on a real EC2 instance against a real three-node Aerospike cluster, checks the results with `check.py`, and deletes everything it created. A stub EC2 endpoint cannot show any of this. Every client uses the same stack, the same run and the same checks; a client only supplies a probe and a manifest.

## What a run proves

- AWS accepts the provider's SigV4 signature for `DescribeInstances`, with credentials resolved from the instance profile through IMDSv2 (`HttpTokens: required`) and no other credential source.
- The minimum policy, `ec2:DescribeInstances` on `*`, is enough: the runner role has that one inline policy and nothing else.
- A role with no policy is refused with `UnauthorizedOperation` and a request id, and the error carries no secret.
- The seed list holds the three running tagged nodes, and neither the untagged running instance nor the stopped tagged instance.
- A client seeded only by the provider joins every tagged node, and the overrun and failure counts over the tend refreshes that follow.
- Cold (fresh process, credentials included) and warm `DescribeInstances` latency, min, median, p99 and max, against the default tend refresh deadline (100 ms) and the recommended provider timeout (80 ms).

## What a client supplies

A manifest file in the client repository, sourced by bash on the runner (Amazon Linux 2023) from the repository root. It sets three commands, each run with `bash -c` from the repository root:

| Variable         | Runs                                                                           |
| ---------------- | ------------------------------------------------------------------------------ |
| `AWS_REAL_SETUP` | Once, to install the client's toolchain (`sudo dnf install ...`)               |
| `AWS_REAL_BUILD` | Once, after setup, to build the client and its probe                           |
| `AWS_REAL_PROBE` | Once per probe run, with the mode as its only argument; prints one JSON object |

The runner has only `git`, `python3`, `jq` and `curl`; everything else comes from `AWS_REAL_SETUP`. The Java client's manifest is `test/discovery/aws-real.manifest` in aerospike-client-java.

## Probe contract

The probe builds the provider with the region and tag filter below, using the client's defaults for everything else. Each run prints one JSON object on a line of its own on stdout; anything else, on stdout or stderr, is ignored. Durations are integer microseconds. A probe never prints an access key id, secret or session token.

Environment for every mode: `AWS_REAL_REGION`, and `AWS_REAL_TAG` as `key=value`.

| Mode      | Extra environment                                                     | Credentials                                                                                  |
| --------- | --------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `latency` | `AWS_REAL_WARM`, warm refreshes per run                               | The instance profile only; `run.sh` refuses to run when any other source is set              |
| `denied`  |                                                                       | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` of the role with no policy |
| `join`    | `AWS_REAL_EXPECTED_NODES`, `AWS_REAL_NAMESPACE`, `AWS_REAL_REFRESHES` | The instance profile only                                                                    |

`latency` runs as a fresh process each time: one cold refresh, then `AWS_REAL_WARM` warm refreshes.

```json
{
	"mode": "latency",
	"credentials_source": "imds",
	"hosts": ["10.71.1.11:3000", "10.71.1.12:3000", "10.71.1.13:3000"],
	"cold_us": 677582,
	"warm_us": [80912, 61234],
	"warm_failures": ["<message of each failed warm refresh>"]
}
```

`denied` runs one refresh.

```json
{
	"mode": "denied",
	"credentials_source": "environment",
	"error": "<the provider's error message>"
}
```

`join` starts a client seeded only by the provider, with the refresh interval equal to the tend interval. It waits up to 60 seconds for `AWS_REAL_EXPECTED_NODES` nodes that own every partition of `AWS_REAL_NAMESPACE`, then counts the next `AWS_REAL_REFRESHES` tend refreshes, stopping early if the provider trips.

```json
{
	"mode": "join",
	"nodes": 3,
	"owned_partitions": 4096,
	"partitions": 4096,
	"refreshes": 30,
	"overruns": 1,
	"failures": ["<message of each failed refresh>"],
	"tripped": false
}
```

A run that cannot finish prints the same object with an `error` field, for example a cold refresh that failed or a join that never saw every node. `credentials_source` names the source that answered, as the client logs it: `imds` for the instance profile.

## What it creates

One CloudFormation stack (`stack.yaml`) named `aws-tags-real-<UTC timestamp>`, in one VPC and one public subnet:

| Resource          | Detail                                                                                                                                                                                                        |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 3 Aerospike nodes | `t3.small`, Amazon Linux 2023, `aerospike/aerospike-server` (community, no feature key) in Docker, mesh heartbeat on private IPs 10.71.1.11 to .13, namespace `test`, tagged `aerospike-cluster=<stack name>` |
| Untagged instance | `t3.micro`, 10.71.1.21, running, no `aerospike-cluster` tag                                                                                                                                                   |
| Stopped instance  | `t3.micro`, 10.71.1.22, tagged `aerospike-cluster=<stack name>`, stopped by `run.sh`                                                                                                                          |
| Runner            | `t3.small`, 10.71.1.30, git, python3, jq; instance profile role with only `ec2:DescribeInstances` on `*`; IMDSv2 required                                                                                     |
| No-policy role    | Assumable only by the IAM principal that created the stack                                                                                                                                                    |
| Security groups   | Ports 3000 to 3002 open only inside the cluster group; SSH to the runner only from the operator's public IP /32                                                                                               |

Both roles carry the permissions boundary from `AWS_PERMISSIONS_BOUNDARY`. Every resource that CloudFormation can tag carries `aws-tags-real-run=<stack name>` (the instance profile, the route, the gateway attachment and the security group rule cannot be tagged). There are no IAM users, access keys, key pairs, Elastic IPs, NAT gateways or load balancers. Instances reach Docker Hub, the client's git host and its package repositories through their auto-assigned public IPs. SSH uses EC2 Instance Connect with a key pair generated in a temporary directory and deleted on exit, so nothing is stored in EC2 and the runner role needs no SSM permission.

## Prerequisites

- AWS CLI v2, `ssh`, `ssh-keygen`, `curl`, `jq`, `perl` and `python3` on the operator's machine.
- `AWS_PROFILE` with permission to create the stack's resources (CloudFormation, EC2, IAM roles and instance profiles), `iam:GetRole`, `sts:AssumeRole`, `ec2-instance-connect:SendSSHPublicKey` and `tag:GetResources`.
- `AWS_REGION`, for example `us-east-2`.
- `AWS_PERMISSIONS_BOUNDARY`, the ARN of the permissions boundary the account requires on new roles.
- The client commit pushed to a repository the runner can clone over https.

`run.sh` fails, and creates nothing, when any of the three is unset, an argument is invalid or `aws sts get-caller-identity` fails. It prints the account id and caller ARN, never a key.

## Run

```bash
AWS_PROFILE=<profile> AWS_REGION=us-east-2 AWS_PERMISSIONS_BOUNDARY=<arn> aws-real/run.sh \
  --client-repo https://github.com/aerospike/aerospike-client-java --commit <sha> \
  --manifest test/discovery/aws-real.manifest
```

`--runs` (default 20) sets the latency runs, `--warm` (default 20) the warm refreshes per run and `--refreshes` (default 30) the tend refreshes the join counts. A run takes about 25 minutes. The output goes to `results/<stack name>/`:

| File            | Content                                                                                                |
| --------------- | ------------------------------------------------------------------------------------------------------ |
| `build.log`     | The client checkout, setup and build on the runner                                                     |
| `latency.jsonl` | One probe object per latency run                                                                       |
| `denied.jsonl`  | The denied probe object                                                                                |
| `join.jsonl`    | The join probe object                                                                                  |
| `<mode>.log`    | Each probe's stderr                                                                                    |
| `check.txt`     | `check.py`'s summary, one `CHECK FAIL` line per failed check, ending in `RESULT PASS` or `RESULT FAIL` |
| `stack.txt`     | Both roles' boundary and policies, the runner's metadata options, and the excluded instances           |
| `cleanup.txt`   | Resource Groups Tagging API output for the run tag after the stack delete                              |

`results/` is ignored by git, so a run's output and any summary written from it stay local.

The denied role's temporary credentials reach the runner only through the SSH session environment (`SendEnv`, accepted by the runner's `sshd`), never on a command line or in a file. Everything copied back passes through a filter that redacts access key ids. Before exiting, `run.sh` checks every result file for those credentials and for the operator's own, and fails if it finds any.

`run.sh` ends in `RUN PASS` only when `check.py` passed, the stack was deleted and nothing with the run tag is left. The tagging API keeps listing terminated instances and deleted volumes for a while, so `run.sh` asks EC2 for those and ignores the ones that are gone.

## Checking results again

`check.py` needs only the probe output, so a run can be checked again without AWS:

```bash
python3 aws-real/check.py --latency results/<stack>/latency.jsonl --denied results/<stack>/denied.jsonl \
  --join results/<stack>/join.jsonl --runs 20 --expected-nodes 3 --excluded 10.71.1.21,10.71.1.22
```

`--deadline-ms` (default 100) and `--timeout-ms` (default 80) set the two reference lines. Its tests are `python3 -m unittest discover -s aws-real`.

## Cost of one run

us-east-2 on-demand prices, about 30 minutes of resources:

| Item                                        | Rate            | One run |
| ------------------------------------------- | --------------- | ------- |
| 4 x `t3.small` (nodes, runner)              | $0.0208/h       | $0.042  |
| 2 x `t3.micro` (one stopped after start-up) | $0.0104/h       | $0.006  |
| 6 public IPv4 addresses                     | $0.005/h        | $0.015  |
| EBS gp3, 5 x 8 GiB + 16 GiB                 | $0.08/GiB-month | < $0.01 |
| Data transfer (Docker image, packages, git) | inbound, free   | $0      |

About **$0.07** per run, under $0.10 with a slow delete.

## Deleting a stack by hand

`run.sh` deletes the stack on success, failure, Ctrl-C and `kill`. If it was killed with `kill -9` or the machine went away:

```bash
aws cloudformation describe-stacks --query "Stacks[?starts_with(StackName,'aws-tags-real-')].StackName" --output text
aws cloudformation delete-stack --stack-name <stack name>
aws cloudformation wait stack-delete-complete --stack-name <stack name>
aws resourcegroupstaggingapi get-resources --tag-filters Key=aws-tags-real-run,Values=<stack name>
```

The last command must return an empty `ResourceTagMappingList`, apart from instances in state `terminated` and volumes that `aws ec2 describe-volumes` no longer finds.
