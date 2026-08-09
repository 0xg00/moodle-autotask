# AWS controller baseline

The AWS baseline uses a continuously running Ubuntu 24.04 controller and creates Windows lab
workers only when a later approved task requires one. Terraform owns the controller resources; it
does not own Moodle token values, student files, or lab instances in this milestone.

## Security boundary

- The controller security group has no ingress rules. Use AWS Systems Manager Session Manager;
  there is no SSH key, SSH port, or RDP port.
- EC2 requires IMDSv2, uses an encrypted gp3 root volume, has API termination protection, and
  receives temporary credentials from an instance profile.
- The instance profile can access only the project artifact prefixes, the exact Moodle token
  secret, and Systems Manager channels. It cannot create EC2 labs or change IAM.
- S3 state and artifacts have public access blocked, versioning, default encryption, and an
  explicit deny for non-TLS requests.
- Terraform creates the Secrets Manager container but never a secret version. Put the complete
  local Moodle token JSON into the secret outside Terraform so it never enters state or Git.
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

## Store the Moodle token value

The secret value must be the complete JSON token file already accepted by the connector. Do not
pass a token literal on the command line:

```powershell
aws secretsmanager put-secret-value `
  --secret-id 'moodle-autotask/development/moodle-token' `
  --secret-string file://.runtime/moodle-token.json `
  --region eu-south-2 `
  --profile moodle-autotask
```

The scheduler service is installed but remains disabled until a reviewed application artifact is
deployed under `/opt/moodle-autotask/venv`. This prevents a half-installed controller from polling
Moodle. The bootstrap pins AWS CLI v2 and verifies its archive against the committed SHA-256 before
installation.

## Verify operations

Wait until the instance appears as `Online` in Systems Manager, then use the output command:

```powershell
aws ssm describe-instance-information --region eu-south-2 --profile moodle-autotask
aws ssm start-session --target <INSTANCE_ID> --region eu-south-2 --profile moodle-autotask
```

The next infrastructure milestone adds a separate, capability-limited lab role and ephemeral
Windows workers. The controller role intentionally cannot create them yet.
