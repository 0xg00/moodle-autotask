# AWS controller baseline

The AWS baseline uses a continuously running Ubuntu 24.04 controller and prepares Windows lab
workers that are created only for an approved task. Terraform owns the controller, lab roles,
network boundary, and fixed launch profile; the application owns each ephemeral lab lifecycle.
Terraform does not own Moodle token values, student files, or individual lab instances.

## Security boundary

- The controller security group has no ingress rules. Use AWS Systems Manager Session Manager;
  there is no SSH key, SSH port, or RDP port.
- EC2 requires IMDSv2, uses an encrypted gp3 root volume, has API termination protection, and
  receives temporary credentials from an instance profile.
- The controller instance profile can access only the project artifact prefixes, the exact Moodle
  and Telegram secrets, Systems Manager channels, and one exact `sts:AssumeRole` target. It cannot call
  EC2 lab APIs or change IAM directly.
- The separate lab provisioner can use only the approved Windows AMI, dedicated subnet,
  no-ingress security group, fixed instance profile, approved instance sizes, encrypted volume
  bounds, and project tags. Termination requires the project, environment, and lab ownership tags.
- A lab receives a different instance profile with Systems Manager runtime permissions only. It
  cannot read the shared artifact bucket, Moodle token, assume the provisioner, create another lab,
  or change IAM. Before hybrid or in-guest dispatch, the controller transfers exact approved
  inputs selected by the execution topology with short-lived exact-object S3 GET presigns in bounded
  SSM commands. The direct-AMI path excludes only its exact imported OVA object; a future nested
  Hyper-V topology can transfer OVA bytes by supplying no imported-object exclusion. The
  guest verifies size and SHA-256 and writes `manifest.json` last under
  `C:\ProgramData\MoodleAutotask\inputs\<transferDigest>`; it has no S3 permission. URLs are
  absent from SQLite, spool jobs/results, reports, Telegram, and command output. The URL necessarily
  reaches AWS/SSM control-plane history and may be handled transiently by the SSM Agent; this
  application writes no URL-bearing guest script or marker. OVA work still reaches a lab
  only through its imported AMI; a future compatible appliance gate must define changed handling.
- S3 state and artifacts have public access blocked, versioning, default encryption, and an
  explicit deny for non-TLS requests.
- Terraform creates separate Moodle and Telegram Secrets Manager containers but never secret
  versions. Put their complete JSON values into AWS outside Terraform so neither enters state or Git.
- The subnet has an internet route because real Moodle and package repositories are external. The
  instance receives a public egress address, but the security group accepts no inbound traffic.

## Prerequisites

- Terraform `1.15.x`.
- AWS CLI v2 with a temporary SSO/login profile.
- An authenticated profile with permission to create the documented baseline.
- The expected 12-digit account ID supplied locally; never rely on the current CLI default account.

Copy each example variables file to `terraform.tfvars` in the same directory. Terraform variable
files and state are ignored by Git. Replace placeholders locally.

## Bootstrap remote state

The state bucket is deliberately separate and protected against Terraform deletion. Its first
apply uses local state because the backend does not exist yet, then immediately migrates that state
to the new bucket:

```powershell
$env:AWS_PROFILE = 'moodle-autotask'
terraform -chdir=infra/aws/bootstrap init -backend=false
terraform -chdir=infra/aws/bootstrap plan -out bootstrap.tfplan
terraform -chdir=infra/aws/bootstrap apply bootstrap.tfplan

$accountId = '<AWS_ACCOUNT_ID>'
$region = 'eu-south-2'
$stateBucket = "moodle-autotask-tfstate-$accountId-$region"
terraform -chdir=infra/aws/bootstrap init -migrate-state -force-copy `
  -backend-config="bucket=$stateBucket" `
  -backend-config="region=$region"
```

The bucket name is deterministic: `moodle-autotask-tfstate-<ACCOUNT_ID>-<REGION>`. Both bootstrap
and controller state then use native S3 lock files and contain no application secret values.

## Plan and apply the controller

Initialize the partial S3 backend using the bucket from the bootstrap output:

```powershell
$accountId = '<AWS_ACCOUNT_ID>'
$region = 'eu-south-2'
$stateBucket = "moodle-autotask-tfstate-$accountId-$region"

terraform -chdir=infra/aws/controller init `
  -backend-config="bucket=$stateBucket" `
  -backend-config="region=$region"
terraform -chdir=infra/aws/controller plan -out controller.tfplan
terraform -chdir=infra/aws/controller apply controller.tfplan
```

The scheduler scope is mandatory infrastructure input. Set exactly one of
`scheduler_course_shortnames` (a non-empty, exactly unique list) or
`scheduler_all_courses = true`, plus `scheduler_max_new_events_per_cycle` from 1 through 100.
There is intentionally no implicit all-course default. Terraform serializes that decision into
root-owned `/etc/moodle-autotask/scheduler.json` (`root:moodle-autotask`, mode `0640`); the
unprivileged scheduler reads only this file. Course shortnames may contain normal Moodle text,
including Unicode or spaces. Names are matched exactly: no Unicode/case normalization occurs.
They cannot be empty or contain ASCII controls. The scope allows at most 64 names, 255 UTF-8 bytes
per name, and 2048 UTF-8 bytes total; the runtime canonically sorts names only after validation.

Review every plan before applying it. Never commit a plan file: it can contain account metadata.
This apply creates the lab roles, profile, subnet, and security group, but does not launch a Windows
instance and therefore does not start Windows compute charges.

The controller cloud-init is creation-only. Terraform ignores later `user_data` changes so an
application release cannot stop the persistent controller or change its public IP. Deploy updated
application artifacts and systemd units through `scripts/aws-deploy.ps1` over Systems Manager.

The default lab profile is deliberately constrained to Windows Server 2022, `t3.large`, and an
encrypted 80 GiB root volume. Operators may choose only the Terraform-validated alternatives;
Moodle content never becomes an AMI ID, instance type, subnet, security group, profile, or volume
size. Obtain the exact adapter configuration after apply:

```powershell
terraform -chdir=infra/aws/controller output -json
```

Lab cleanup has two independent bounds. The controller schedules normal teardown two hours after
execution. Terraform additionally deploys an EventBridge-triggered Lambda every five minutes that
uses the EC2 server-side `LaunchTime` and terminates only instances at least
`lab_hard_ttl_seconds` old (default 14,400 seconds / four hours; allowed range three through
24 hours). It accepts at most `lab_reaper_max_terminations_per_run` instances per invocation
(default and maximum 20), in deterministic instance-ID order. The reaper requires exact
`Project`, `Environment`, `ManagedBy=moodle-autotask`, and `Role=lab` tags in both IAM and code.
IAM requires that `ProvisionKey` is present; Python additionally requires it to be non-empty. It
does not trust an expiry tag. The five-minute schedule is eventual,
not an exact termination deadline. This independent Lambda has a dedicated role and cannot be
modified by the controller role; its CloudWatch log group retains bounded JSON operational records
for 30 days and its alarms publish through the managed SNS notification path below. It does
not reserve Lambda concurrency or serialize executions. Both the EventBridge target and Lambda
asynchronous invoke configuration have a 300-second maximum event age. The EventBridge target has
zero pre-invocation delivery retries, and Lambda's zero retry-attempt setting suppresses retries
after a function error; Lambda can still retry throttling or system errors until that age expires.
The next five-minute schedule is the normal function-error retry path, but not the only possible
retry. AWS may still deliver a duplicate event, so each invocation independently applies the
deterministic 20-instance cap and relies on idempotent EC2 termination rather than a global
per-period cap.

### Operator alerts and retained failures

`operator_alert_email` is a required controller Terraform input. It accepts a bounded UTF-8
address with exactly one `@`, no whitespace or control characters; this is intentionally not full RFC email
validation. Terraform creates an SNS email subscription, but AWS leaves it in `PendingConfirmation`
until the operator confirms the email. No alert delivery should be assumed before that confirmation.
The managed topic receives the controller status-check alarm and all reaper alarms: Lambda errors,
throttles, async drops, destination-delivery failures; EventBridge failed invocations and failures
to send to its DLQ; visible SQS failure records; and absence of EventBridge invocations over a
15-minute schedule window.

The reaper has one standard SQS queue encrypted with SQS-managed server-side encryption and a
14-day retention limit. EventBridge sends pre-invocation delivery failures there through a
rule-scoped queue policy; Lambda sends asynchronous function failures there through its narrow
execution-role permission and on-failure destination. This queue is for diagnosis, not automatic
replay. When an alarm fires, inspect the alarm state, the reaper JSON log group, and one queue
message without deleting it. Resolve the permission, destination, or runtime cause first. The
scheduled event has no task payload, so after the cause is fixed it is safe to wait for the next
five-minute run (or invoke the reaper once with an empty `{}` event under normal operator IAM).
Do not blindly replay opaque queue records. Delete only an individually inspected record after
confirming the root cause is resolved and the subsequent reaper run is healthy.

`AwsEc2LabProvider` uses the fixed launch values installed with the service and the AWS CLI already
on the controller. Deployment reads the exact AMI authorized by the inline provisioner policy, so a
moving public `latest` parameter cannot diverge from IAM. The provider assumes the lab provisioner
for one hour, uses the request-derived SHA-256 as the EC2 client token,
and verifies EC2 ownership tags plus Systems Manager readiness. There is no RDP listener. The
approved-work service now calls this provider only after the exact Telegram approval, limits active
non-central work to one lab, sends only bounded PowerShell plans through the official
`AWS-RunPowerShellScript` document, and tears a completed lab down after two hours. Guest-side
execution markers make retries idempotent and reject an ambiguous in-progress replay.

## Store runtime secret values

The secret value must be the complete JSON token file already accepted by the connector. Do not
pass a token literal on the command line:

```powershell
$env:AWS_CLI_FILE_ENCODING = 'UTF-8'
aws secretsmanager put-secret-value `
  --secret-id 'moodle-autotask/development/moodle-token' `
  --secret-string file://.runtime/moodle-token.json `
  --region eu-south-2 `
  --profile moodle-autotask
```

Create a Telegram bot separately, open its private chat, and store the exact protected configuration
accepted by the application. `chatId` and `allowedUserId` are positive numeric IDs and normally equal
for the intended single-user private chat:

```json
{"botToken":"<TELEGRAM_BOT_TOKEN>","chatId":123456789,"allowedUserId":123456789}
```

```powershell
aws secretsmanager put-secret-value `
  --secret-id 'moodle-autotask/development/telegram-config' `
  --secret-string file://.runtime/telegram.json `
  --region eu-south-2 `
  --profile moodle-autotask
```

The controller role can read exactly these two secret ARNs. Terraform does not receive either value.

The scheduler, outbound Telegram poller, approved-work worker, and isolated agent services are
installed but remain disabled until a
reviewed application artifact and both valid secret values exist. This prevents a half-installed
controller from polling Moodle or Telegram. The bootstrap pins AWS CLI v2 and verifies its archive
against the committed SHA-256 before installation.

Deploy a clean committed revision as a digest-bound wheel. The helper uploads it to the private
artifact bucket, verifies the same SHA-256 on EC2, installs an immutable release, and atomically
updates `/opt/moodle-autotask/current`. `Deploy` never enables either service:

```powershell
.\scripts\aws-deploy.ps1 `
  -Action Deploy `
  -AccountId '<AWS_ACCOUNT_ID>' `
  -Profile 'moodle-autotask' `
  -CourseShortname '<EXACT_MOODLE_COURSE_SHORTNAME>' `
  -MaxNewEventsPerCycle 4

.\scripts\aws-deploy.ps1 `
  -Action Status `
  -AccountId '<AWS_ACCOUNT_ID>' `
  -Profile 'moodle-autotask'
```

`Deploy` requires this explicit selection every time. To deliberately scan every Moodle course,
use `-AllCourses` instead of `-CourseShortname`; the two options cannot be combined. `Status`,
`Activate`, and `Deactivate` do not require scope arguments. Upgrade writes the configuration
through a validated temporary file and atomic rename, rejecting unsafe links. On an upgrade it
retains and restores a prior configuration on any later failure; a legacy controller without this
file keeps the newly migrated configuration so its new unit is never left without scope.

After both secret values exist, activation first refreshes and validates them, then enables and
starts all four units together. Any start failure stops and disables all four. Deactivation is explicit:

```powershell
.\scripts\aws-deploy.ps1 -Action Activate -AccountId '<AWS_ACCOUNT_ID>' -Profile 'moodle-autotask'
.\scripts\aws-deploy.ps1 -Action Deactivate -AccountId '<AWS_ACCOUNT_ID>' -Profile 'moodle-autotask'
```

The scheduler, poller, and worker run as the unprivileged `moodle-autotask` account. The agent runs
as the separate `moodle-agent` account and exchanges digest-bound jobs through two setgid spool
directories; it cannot read the approval database or application secret files. The root-only
pre-start refresher serializes concurrent refreshes, validates both JSON shapes, writes mode-`0600`
files atomically, and never places secret values on a command line.

The worker and agent are long-running services. Before normal approved-work processing or Codex
execution, each cycle performs at most one bounded retention action. The worker explicitly uses
`/var/lib/moodle-autotask`, `/var/lib/moodle-agent`,
`/var/lib/moodle-agent/workspaces`, and
`/var/spool/moodle-autotask/results/bundles`; scratch data expires after 24 hours, evidence after
seven days, and candidate/scan passes are each capped at 1,024 entries. The agent receives its
explicit bundles and agent-private retention roots. Durable terminal receipts and barriers remain
immutable evidence: no service automatically sweeps terminal metadata.

The supported controller root profile is 80 GiB or larger. First application deployment and every
upgrade use `moodle-autotask-controller install` to create or exactly validate the separate 16 GiB
agent-workspace image at `/var/lib/moodle-autotask-root/agent-workspaces.img`. Its dedicated parent
is `root:root` mode `0700`, outside the controller-writable state tree. It is ext4 with at least
100,000 inodes, six percent of total blocks reserved, and an exact `loop,nodev,nosuid` persistent mount at
`/var/lib/moodle-agent/workspaces`. Validation permits at most 64 MiB of backing-file allocation slack
for ext4/loop metadata while the separate root-filesystem admission reserve remains 12 GiB. On upgrade,
the installer copies a non-empty legacy workspace
tree into a private staging mount and verifies every regular file and directory byte, owner, group,
and mode. A root-owned state file advances through `copying`, `copied`, and `active`; after `copied`,
the bare workspace is atomically renamed to a protected backup before mounting the verified image.
Only an `active` image permits idempotent backup cleanup. The installer removes only an exact empty
ext4 `lost+found` and resumes safely after a partial copy, rename, mount, or cleanup. Unsafe paths,
links, special files, hard-linked files, changed content, or any image/filesystem/mount mismatch fail
closed without advancing the state. A root-owned, no-follow kernel lock serializes the entire helper.
The deploy transport allows five minutes for SSM delivery, 35 minutes for remote execution, and 40
minutes for local polling around the 30-minute installer budget before rollback.

For an operational check, run `-Action Status` and inspect both services and their bounded-cycle
records on the controller:

```bash
systemctl status moodle-autotask-worker.service moodle-autotask-agent.service --no-pager
journalctl -u moodle-autotask-worker.service -u moodle-autotask-agent.service --no-pager -n 100
```

The JSON cycle records include the retention outcome; investigate a failed service or repeated
non-idle retention result before changing files manually.

The root health publisher additionally emits one storage dimension each minute: admission state,
root free bytes/inodes, and workspace free bytes. Admission requires at least 12 GiB and 100,000
free inodes on root, plus the exact mounted workspace with at least 2 GiB and 20,000 free inodes.
`StorageAdmissionOpen` alarms after three consecutive failed/missing 60-second samples. Fresh
cloud-init retains its pre-release bootstrap health publisher, so this storage alarm can remain
missing/breaching until the first successful application deployment installs the canonical publisher.

## Link the central Codex agent

Each deployment installs the pinned official Codex package only after verifying the archive and the
exact package inventory, sizes, modes, and SHA-256 digests, including the Code Mode host and bundled
tools. Codex runs as the separate `moodle-agent` system account. That account
is not a member of the application group, cannot read `/etc/moodle-autotask`, and stores its login in
`/var/lib/moodle-agent/.codex/auth.json` with private permissions.

A root-owned `/etc/codex/requirements.toml` forces approval policy `never`, disables tool and web
network access, permits only the managed workspace profile, and denies sandboxed commands any read
access to both `/var/lib/moodle-agent/.codex` and `/etc/moodle-autotask`. The systemd unit also blocks
both EC2 Instance Metadata Service addresses, so the agent cannot obtain the controller role credentials.
Ubuntu's user-namespace restriction is kept enabled. A root-owned AppArmor profile grants only the
root-owned `/usr/bin/bwrap` executable permission to create the namespaces used by Codex, and the
agent unit admits `AF_NETLINK` solely so bubblewrap can configure its isolated loopback interface.

Central jobs are not a conversational terminal session. The spool executes three isolated Codex
invocations (`central_planner`, `central_executor`, `central_reviewer`) with different job IDs and
workspaces. Planner text is untrusted operational data and cannot enable commands, network,
AWS, Moodle, or lab access. The executor must create only the planner's expected `outputs/` paths;
the wrapper validates and publishes a deterministic ZIP (at most 64 regular files, 2 MiB per file,
1,900,000 raw bytes, and 512 MiB retained-bundle quota). The reviewer binds every plan/executor
digest and rejects terminally without an automatic replan. Bundle/report delivery is at least once;
Telegram duplicates are possible after a crash. The existing lab plan/SSM/report split is unchanged.

Start the headless device-code flow after the first deployment:

```powershell
.\scripts\aws-deploy.ps1 `
  -Action CodexLogin `
  -AccountId '<AWS_ACCOUNT_ID>' `
  -Profile 'moodle-autotask'
```

Open the URL shown by the command and enter its one-time code. A successful login is cached and
refreshed by Codex during normal use; a new code is not required for every Moodle task. Authentication
is requested again only after logout, revocation, deletion of the persistent cache, an account policy
change, or a refresh failure. Never copy, print, commit, or place `auth.json` in Secrets Manager.

`-Action Status` reports only `authenticated` or `unauthenticated`; it never returns tokens. The login
unit is transient and cannot read the Moodle or Telegram secret directory.

After linking, run the live smoke test. It checks the AppArmor profile, root-owned policy, and cache
permissions; proves inside the actual Codex sandbox that neither the Codex cache nor Moodle token is
readable; then launches one ephemeral Codex request under the same systemd restrictions as the agent.
That request must use Code Mode to create an exact file, which the smoke test verifies by type,
ownership, mode, link count, path set, and SHA-256 before removing its temporary workspace:

```powershell
.\scripts\aws-deploy.ps1 `
  -Action CodexSmoke `
  -AccountId '<AWS_ACCOUNT_ID>' `
  -Profile 'moodle-autotask'
```

## Verify operations

## Controller application health

The controller has a root-owned `moodle-autotask-health.service` oneshot and a
60-second timer. It sends one bounded `PutMetricData` batch in the
`MoodleAutotask/Controller` namespace, with only `InstanceId` and `Service`
dimensions. `ControllerStateMatchesExpectation`, `ServicesExpectedRunning`, and
one `ServiceStateMatchesExpectation` series for scheduler, Telegram receiver,
worker, and agent are intentionally free of
task, Moodle, Telegram, and user content. IMDSv2, AWS CLI, and IAM failures
make the publisher fail, so the Terraform alarm treats missing metrics as
breaching.

The four applications only update the mtime of fixed, root-owned empty files
under `/run/moodle-autotask-health`; they cannot create the directory or
replace paths. With `/var/lib/moodle-autotask/health-enabled` absent, all four
application units must be disabled and inactive. `Activate` starts and enables
all four, waits for their first pulse, then atomically creates that marker and
enables the health timer. `Deactivate` stops and disables every application,
verifies that state, then removes the marker. The health alarm is created only
after the application deployment: deploy the application first, then Terraform.
Fresh cloud-init enables a bootstrap publisher which reports healthy only while
all four application services are disabled and inactive. Deployment replaces it
with the canonical publisher before activation.

Wait until the instance appears as `Online` in Systems Manager, then use the output command:

```powershell
aws ssm describe-instance-information --region eu-south-2 --profile moodle-autotask
aws ssm start-session --target <INSTANCE_ID> --region eu-south-2 --profile moodle-autotask
```

Verify that preparing the boundary did not create a billable worker:

```powershell
aws ec2 describe-instances `
  --filters "Name=tag:Role,Values=lab" `
  "Name=instance-state-name,Values=pending,running,stopping,stopped" `
  --region eu-south-2 `
  --profile moodle-autotask
```

Do not manually call `run-instances`. Telegram start decisions are persisted for an exact Moodle
revision and consumed through a transactional lease. Retries reuse the same EC2 client token; the
worker waits for Systems Manager, runs the isolated agent workflow, returns the report through
Telegram, and schedules mandatory teardown two hours after execution. Execution-report delivery is
at-least-once: a controller crash after Telegram accepts a document and before the lease update can
send the same digest-bound report again.

## Real central E2E acceptance harness

`scripts/aws-central-e2e.ps1` is the one-task acceptance harness for the local fictitious Moodle
and a deployed controller. It requires an explicit account, profile, Region, controller instance,
unique run ID, local `.runtime/moodle-token.json`, and bounded timeout. It does not accept Moodle,
Telegram, or Codex credentials as arguments. Run it only from a clean, committed checkout after
`scripts/moodle.ps1 -Action Bootstrap` and `-Action ExpandFixture` have produced fixture v4:

```powershell
.\scripts\aws-central-e2e.ps1 `
  -Action Run `
  -AccountId '<AWS_ACCOUNT_ID>' `
  -Region '<AWS_REGION>' `
  -Profile '<AWS_PROFILE>' `
  -ControllerInstanceId '<CONTROLLER_INSTANCE_ID>' `
  -RunId '<LOWERCASE-UNIQUE-RUN-ID>' `
  -MoodleTokenFile .runtime/moodle-token.json `
  -TimeoutSeconds 3600
```

It creates `AUTOTASK-LIVE-E2E-<RUN_ID>`, temporarily replaces only the root-owned scheduler scope
with that exact course and cap `1`, and restores the exact prior scheduler configuration,
enabled/active state in `finally`. A normal run first requires all deployed controller services to be active,
so it cannot silently activate a deactivated controller; guarded `Cleanup` restores its exact scope
record before that health check so it can repair a stopped scheduler. The harness waits (and times out) for two
separate human Telegram choices: first `Hacer tarea`, then `Entregar`. It never invokes a callback,
approves work, or submits Moodle work itself.

Canonical read-only controller evidence identifies exactly one event, task key, revision, central
mode, three distinct planner/executor/reviewer job IDs, dependency/result digests, reviewer
acceptance, report digest, manifest, and deterministic ZIP content/digest. It also queries EC2 for
the run-bound provision key and requires zero tagged lab instances, import tasks, AMIs, and snapshots.
Finally it asks the local Moodle fixture to verify the submitted file's SHA-256 against the approved
manifest. A failure or timeout preserves both the local fixture and local forensic evidence; cleanup
happens only after a successful run or through the explicit guarded action below. There is one
root-owned active scope record at `/var/lib/moodle-autotask/e2e/active`; it contains exactly the
backup and state files, including the run-bound desired and backup digests. Valid retired records
remain as `.<RUN_ID>.retired` tombstones, so a run ID is never reusable. Publication uses a unique
same-parent `.<RUN_ID>.pending.*` candidate and atomic rename. An active different run, any foreign
pending candidate, or an unexpected/tampered record fails closed without deleting that evidence.
After the original config and scheduler service are restored, the active record is atomically renamed
to its retired tombstone. A response-loss or killed local process resumes or restores only that
record; `Cleanup` restores it before deleting the Moodle course.

The harness records the clean local Git commit and the deployed immutable release digest separately.
Preflight compares a domain-separated local/deployed Moodle credential digest without emitting the
token or digest; final acceptance proves both Telegram notifications were delivered and both decisions
were made by the deployed `allowedUserId`, without recording that identity in evidence.
The current deployment interface stores only the wheel digest under `/opt/moodle-autotask/current`,
not a commit-to-wheel attestation, so it does **not** claim those values are cryptographically bound.
Use the reviewed `aws-deploy.ps1` output for that deployment binding until the deployment contract
persists a signed commit mapping.

```powershell
.\scripts\aws-central-e2e.ps1 `
  -Action Cleanup `
  -AccountId '<AWS_ACCOUNT_ID>' `
  -Region '<AWS_REGION>' `
  -Profile '<AWS_PROFILE>' `
  -ControllerInstanceId '<CONTROLLER_INSTANCE_ID>' `
  -RunId '<LOWERCASE-UNIQUE-RUN-ID>' `
  -MoodleTokenFile .runtime/moodle-token.json
```

The evidence record is `.runtime/central-e2e/<RUN_ID>.evidence.json` unless `-EvidencePath` names
another file under `.runtime`. Its bounded schema is `autotask-central-e2e-evidence-v1`:
`kind`, `runId`, `controllerInstanceId`, `region`, `startedAt`, `completedAt`, `status`, and ordered
`phases`; phases contain only canonical IDs/digests, scope state, zero-count EC2 assertions,
submission receipt digest/size, and sanitized failure codes. It excludes tokens, authorization
headers, prompts, Telegram identities, raw SSM output, Moodle names, and user PII.

The current image-import boundary supports exactly one `.ova` in an approved revision. Before any
import it re-reads that exact revision, downloads every attachment with the Moodle token kept out of
AWS arguments, hashes the bytes, uploads them to `assignments/<task>/<revision>/<attachment>/<sha256>/`,
and verifies S3 size, checksum, metadata, and SSE-S3. Assignment inputs expire after seven days.
`ImportImage` is encrypted and uses a stable client token plus the isolated image-importer and
VM Import/Export service roles. The imported AMI and snapshots are ownership-tagged, verified, and
removed during the mandatory lab cleanup.

Imports use `BYOL`; the operator is responsible for having cloud-use rights for the appliance OS.
The source bucket and EC2 import must remain in the same Region. Multi-file OVF/VMDK/VHD layouts,
VDI, and multiple appliance attachments remain fail-closed because their disk order, boot mode, and
licensing cannot be inferred safely from filenames. The bundled local `asix-router-lab.ova` is only
a 76-byte routing fixture and is intentionally not a bootable image, so it must never be used for a
live AWS import test.
## CENTRAL terminal-failure retention

The retention record for a terminal CENTRAL failure or reviewer rejection carries the
actual ordered durable job prefix (one through three jobs), not a synthesized
three-job chain. Controller preflight and agent deletion both verify its canonical
jobs, results, input/workspace material, and any bound executor bundle before
publishing an intent, barrier, or acknowledgement. A failure before any durable
central result carries no provenance and therefore creates no retention plan.

Scratch data is reclaimed through the existing two-owner protocol; an executor bundle
is reclaimed only as evidence after delivery and the evidence TTL. Terminal intents,
barriers, acknowledgements, and receipts remain immutable audit metadata, and a
receipt suppresses replanning of the corresponding retained row.

## Lab terminal retention

HYBRID and IN_GUEST execution records retain the exact durable `lab_plan`/`lab_report` prefix,
result digests, barriers, and, after dispatch, the canonical SSM dispatch record. The same
two-owner protocol verifies every surviving input, job, result, workspace, and dispatch byte
before deletion. A lost dispatch response remains `dispatch_unknown` and is reclaimed without
inventing a command ID. Lab jobs and dispatch metadata share the bounded jobs quota and admission
lock; capacity refusal occurs before input download, SSM dispatch, or partial publication.

# Moodle draft finalization

The worker journal has a durable `finalizing` state between exact draft verification and the
official Moodle finalization call. Restart recovery never repeats a save blindly.
