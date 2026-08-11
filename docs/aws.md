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
  or change IAM. Digest-bound transfer of per-task inputs into a lab is deferred; OVA work reaches
  a lab only through its imported AMI.
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

## Link the central Codex agent

Each deployment installs the pinned official Codex CLI archive only after verifying both the archive
and extracted binary SHA-256. Codex runs as the separate `moodle-agent` system account. That account
is not a member of the application group, cannot read `/etc/moodle-autotask`, and stores its login in
`/var/lib/moodle-agent/.codex/auth.json` with private permissions.

A root-owned `/etc/codex/requirements.toml` forces approval policy `never`, disables tool and web
network access, permits only the managed workspace profile, and denies sandboxed commands any read
access to both `/var/lib/moodle-agent/.codex` and `/etc/moodle-autotask`. The systemd unit also blocks
both EC2 Instance Metadata Service addresses, so the agent cannot obtain the controller role credentials.

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

After linking, run the live smoke test. It checks the root-owned policy and cache permissions, proves
inside the actual Codex sandbox that neither the Codex cache nor Moodle token is readable, and makes
one ephemeral Codex request:

```powershell
.\scripts\aws-deploy.ps1 `
  -Action CodexSmoke `
  -AccountId '<AWS_ACCOUNT_ID>' `
  -Profile 'moodle-autotask'
```

## Verify operations

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
