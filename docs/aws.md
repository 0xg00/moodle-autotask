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
- A lab receives a different instance profile. It can read `assignments/*`, write but not delete
  `lab-results/*`, and use Systems Manager. It cannot read the Moodle token, assume the provisioner,
  create another lab, or change IAM.
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

`AwsEc2LabProvider` uses those outputs and the AWS CLI already installed on the controller. It
assumes the lab provisioner for one hour, uses the request-derived SHA-256 as the EC2 client token,
and verifies EC2 ownership tags plus Systems Manager readiness. There is no RDP listener. A future
agent runtime will use audited Systems Manager commands after the existing start-work approval; the
provider alone does not execute practice instructions.

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

The scheduler and outbound Telegram poller services are installed but remain disabled until a
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
  -Profile 'moodle-autotask'

.\scripts\aws-deploy.ps1 `
  -Action Status `
  -AccountId '<AWS_ACCOUNT_ID>' `
  -Profile 'moodle-autotask'
```

After both secret values exist, activation first refreshes and validates them, then enables and
starts both units together. Any start failure stops and disables both. Deactivation is explicit:

```powershell
.\scripts\aws-deploy.ps1 -Action Activate -AccountId '<AWS_ACCOUNT_ID>' -Profile 'moodle-autotask'
.\scripts\aws-deploy.ps1 -Action Deactivate -AccountId '<AWS_ACCOUNT_ID>' -Profile 'moodle-autotask'
```

Both units use outbound HTTPS only, run as the unprivileged `moodle-autotask` account, and share the
approval SQLite database. The root-only pre-start refresher serializes concurrent refreshes, validates
both JSON shapes, writes mode-`0600` files atomically, and never places secret values on a command line.

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

Do not manually call `run-instances`. Telegram start decisions are now persisted for an exact Moodle
revision, but no process consumes them to call this provider. The next application milestone wires
that approved record to a durable workflow and adds audited Systems Manager execution and mandatory
teardown.
