# Pluggable discovery L5 on AWS

`run.sh` runs one client's L5 rows (W-1, W-2, W-3, W-5) against a real Aerospike cluster on EKS, reached through an NLB published as a PrivateLink endpoint service, from a client in a second VPC. It deletes everything it created. Every client uses the same stack, the same run and the same disruption commands; a client supplies a manifest and a driver, which `rows.py` runs and asserts on the same way for every client, or its own tests that call `l5ctl`.

## What the environment is

- Producer VPC: EKS across three AZs, AKO 4.6.0, one AerospikeCluster with one rack per AZ, `multiPodPerHost: true` and `alternateAccess: configuredIP`. Aerospike runs on Spot, one node group per AZ, one pod per node. Every Aerospike node carries the endpoint name as the `aerospike.com/configured-alternate-access-address` label, so every pod advertises the endpoint name and its own per-pod Service NodePort in `peers-*-alt`.
- An internal NLB with cross-zone load balancing, one TCP listener per pod NodePort (listener port = NodePort), each forwarding to a target group of the Aerospike node groups' instances. `l5ctl sync-listeners` creates them; the stack does not.
- Consumer VPC: the interface endpoint in three subnets and a private hosted zone that resolves the endpoint name to it. The name resolves here only, as in Cloud. With `--control resolvable` it also resolves in the producer VPC.
- The runner, in the consumer VPC: the client and its tests run here, and it is the only address the EKS API accepts.

## What a client supplies

A manifest file in the client repository, sourced by bash on the runner (Amazon Linux 2023) from the repository root. It sets `L5_SETUP`, `L5_BUILD`, and `L5_DRIVER` or `L5_RUN`, each run with `bash -c` from the repository root:

| Variable    | Runs                                                                                                                                                                                                     |
| ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `L5_SETUP`  | Once, to install the client's toolchain (`sudo dnf install ...`)                                                                                                                                         |
| `L5_BUILD`  | Once, after setup, to build the client and its driver or tests                                                                                                                                           |
| `L5_DRIVER` | Preferred. The client's driver, to the contract in [DRIVER.md](DRIVER.md). Each row runs `rows.py --row <row> --driver "$L5_DRIVER"`, which starts the driver as the row needs and makes every assertion |
| `L5_RUN`    | When `L5_DRIVER` is not set: once per row, with `L5_ROW` set; exit 0 when every assertion of the row holds                                                                                               |

`rows.py` (Python 3.9 standard library, the runner's `python3`) exits 0 when every assertion of the row holds, 1 when one fails, 2 on bad usage or a missing variable, and 3 when the driver breaks the contract. It prints an `L5-CHECK` line per check with its measurement, bound and outcome, and leaves the cluster at the size it found it. `python3 -m unittest l5-aws/test_rows.py` tests it offline against a fake driver and a stub `l5ctl`.

Environment the row command (`rows.py` and the driver it starts, or `L5_RUN`) gets:

| Variable                                                  | Value                                                                                      |
| --------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `L5_ROW`                                                  | `W-1`, `W-2`, `W-3` or `W-5`                                                               |
| `L5_CONTROL`                                              | `none`, or `resolvable` on the control run                                                 |
| `L5_ENDPOINT_NAME`                                        | Name to seed with; every pod advertises it                                                 |
| `L5_SEED_PORT`                                            | Lowest pod NodePort with a listener                                                        |
| `L5_CTL`                                                  | Path of `l5ctl`                                                                            |
| `L5_KUBECONFIG`                                           | kubeconfig for the EKS cluster; a row should not need it, `l5ctl` reads the cluster for it |
| `AEROSPIKE_ADMIN_PASSWORD`                                | Password of user `admin`                                                                   |
| `L5_NODEGROUPS`, `L5_RUNNER_AZ`, `L5_STACK`, `AWS_REGION` | From the stack                                                                             |

The cluster is namespace `aerospike`, AerospikeCluster `aerocluster`, size 3, replication factor 2, namespace `test`. Each row starts with the cluster Completed at its spec size and a healthy listener per NodePort. A row must leave the cluster at the size it found it; the next row waits for Completed.

## l5ctl

Rows drive and read the environment through `$L5_CTL`, so a client needs no kubectl or AWS code of its own: the driver or test reports what only the client can see, and `l5ctl` checks the cluster, the servers and AWS. Each command prints `L5-*` lines and exits non-zero on failure. `l5ctl` with no arguments prints the full list.

| Command                                                   | Use                                                                                                                                                                                                                                                                                                                   |
| --------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sync-listeners [--prune]`                                | Add a listener for every NodePort without one. A new pod's NodePort has no listener until this runs, which is how a test makes "a peer advertised before its listener serves"                                                                                                                                         |
| `wait-healthy [--timeout S]`                              | Every listener has a healthy target                                                                                                                                                                                                                                                                                   |
| `wait-ready [--timeout S]`                                | Completed at spec size, every pod Ready                                                                                                                                                                                                                                                                               |
| `scale N`                                                 | Set the AerospikeCluster size                                                                                                                                                                                                                                                                                         |
| `pending`                                                 | `L5-PENDING pod=... rack=... zone=...` for each Pending pod                                                                                                                                                                                                                                                           |
| `pods`                                                    | `L5-CLUSTER size=... phase=...`, then `L5-POD pod= phase= uid= zone= worker= instance= nodeId= node= nodePort=` per pod. `node` is the node name as the server and client format it (upper-case hexadecimal, no leading zeros); empty fields mean the pod has none yet                                                |
| `wait-advertised POD [--timeout S]`                       | Wait until POD runs and every other pod lists it in `peers-clear-alt` at the endpoint name and its NodePort alone, then `L5-ADVERTISED pod= node= nodePort= address= by=`. Other pods are asked through the endpoint name, so POD needs no listener                                                                   |
| `add-node AZ [--wait]`, `set-nodes AZ N [--wait]`         | Resize the Aerospike node group in AZ; a real EC2 launch                                                                                                                                                                                                                                                              |
| `spot-interrupt POD [--wait]`                             | Real Spot interruption with a two-minute notice through FIS, on the instance under POD. `--wait` also waits until POD is recreated on another instance and prints `L5-REPLACED pod= oldWorker= oldInstance= newWorker= newInstance=`                                                                                  |
| `rollout AZ\|all [--wait]`                                | Replace every node of the node group(s) through a new launch template version. `--wait` also waits until every pod in those AZs is on another instance, with an `L5-REPLACED` line each                                                                                                                               |
| `az-outage AZ [--duration PT5M] [--wait-running\|--wait]` | Cut all traffic in and out of the producer and endpoint subnets in AZ through FIS. `--wait-running` returns once FIS reports it running, and fails if it ended before that                                                                                                                                            |
| `endpoint-zone ADDRESS`                                   | `L5-ENDPOINT-ZONE address=... zone=...`: the AZ of the endpoint network interface with that address, so a test can cut the zone its client is using                                                                                                                                                                   |
| `experiment ID [--wait]`                                  | FIS experiment state                                                                                                                                                                                                                                                                                                  |
| `resolve-check [--expect-producer yes\|no]`               | Where the endpoint name resolves; fails if the producer lookup gives neither an address nor NXDOMAIN                                                                                                                                                                                                                  |
| `verify`                                                  | Each pod's `build`, `service-clear-alt` and `peers-clear-alt`; the per-pod Service settings; that each listener's only healthy target is the pod's node; and that the endpoint name on each NodePort reaches that pod's server from the runner. `run.sh` runs it before any row; rows run it again after a disruption |
| `status`                                                  | Nodes, pods, Services, rendered access addresses, listeners, target health                                                                                                                                                                                                                                            |

## Running it

Creating the stack creates billable resources in the shared account; get the go-ahead first. A run takes about an hour, most of it EKS create and delete.

```text
AWS_PROFILE=aerospike-sso AWS_REGION=us-east-2 \
AWS_PERMISSIONS_BOUNDARY=arn:aws:iam::848111426540:policy/shared-power-users-boundary \
FEATURES_CONF_PATH=/path/to/features.conf \
l5-aws/run.sh --client-repo https://github.com/aerospike/aerospike-client-java --commit <sha> \
  --manifest test/discovery/l5-aws.manifest --server-image <image with SERVER-1839>
```

Before creating a stack, `run.sh` checks that the region has room for one more: it counts the VPCs, internet gateways and Elastic IPs that `stack.yaml` creates, adds them to those in use in the region and compares each sum with the account quota (the applied quota, or the AWS default when that cannot be read). If any is short it stops before creating anything and lists the live L5 stacks with their owners (the `l5-aws-operator` tag, or the CloudTrail `CreateStack` user). Other users' live stacks no longer block a run; they are only listed. Two runs started at the same moment can both pass the check; the second stack create then fails on the quota and is cleaned up like any failed stack create. `--plan` and `--stack` skip the check.

- `--plan` prints what would be created and makes no AWS call.
- `--rows W-1,W-3` runs a subset; the default order is W-1, W-3, W-2, W-5, least disruptive first.
- `--registry-auth <dockerconfigjson>` adds a pull secret when the server image is private, for example a JFrog build. Write the file with `podman login --authfile <file> <registry host>`, which stores the credentials in the file itself; a `docker login` that uses a credential helper leaves no credentials in `config.json`. The nodes are x86_64, so the image needs an amd64 variant.
- AKO validates the cluster config against the schema for the first three parts of the image tag's version, and AKO 4.6.0 has none newer than 8.2.0. For a later server, pin the image by digest under a tag AKO knows, for example `<repository>:8.2.0.0-l5@sha256:<digest>`; the node pulls by digest and ignores the tag.
- `--rows none` stops once the cluster, listeners and name resolution are up, and needs no client arguments. Otherwise the run checks first that the manifest exists at `--commit`.
- `--keep` leaves the stack for debugging; delete it with `teardown.sh <stack name>`. `--stack <stack name>` runs again on a kept stack instead of creating one; SSH to the runner is allowed only from the IP that created it.
- Control runs on a released server: `--control resolvable` runs the rows with the name also resolvable in the producer VPC (expected to pass); `--control unresolvable` runs no rows and passes only if the server fails to start.

Results go to `l5-aws/results/<stack name>/`: `env`, `install.log`, `setup.log`, `build.log`, one `<row>.log`, `<row>-status.txt` and `<row>-interruptions.txt` per row, `rows.txt` and `cleanup.txt`. AWS can reclaim the Spot nodes at any time; a failed row that overlapped a Spot interruption `spot-interrupt` did not send, or a Spot instance replaced after a rebalance recommendation, is reported as `INTERRUPTED` and should be rerun; when the interruption check itself fails, the row says so instead. A run takes well over an hour, so log in with `aws sso login` right before it. Before each row `run.sh` checks that the AWS credentials still work; once they stop, the remaining rows are reported as `NOT RUN`, and teardown fails and prints the command to run it again after logging in. The feature-key file is copied to the runner, loaded into a Kubernetes secret and deleted; the run fails if any result file contains its content.

## Not yet verified

The Java and Python clients have both run all four rows through `rows.py` on AWS, which exercised `spot-interrupt` and `az-outage` through FIS and `rollout` through a new launch template version. Still unverified:

- Whether the FIS `aws:network:disrupt-connectivity` action also cuts the EKS control plane ENIs in that subnet.
- The teardown retry for CNI ENIs and the EKS cluster security group left after a Spot reclaim.
