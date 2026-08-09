from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AWS_ROOT = ROOT / "infra" / "aws"


def test_controller_has_no_ingress_and_requires_imdsv2() -> None:
    network = (AWS_ROOT / "controller" / "network.tf").read_text(encoding="utf-8")
    compute = (AWS_ROOT / "controller" / "compute.tf").read_text(encoding="utf-8")

    assert "ingress     = []" in network
    assert 'http_tokens                 = "required"' in compute
    assert "disable_api_termination     = true" in compute
    assert "encrypted             = true" in compute
    assert "key_name" not in compute


def test_state_and_artifact_storage_are_private_and_encrypted() -> None:
    bootstrap = (AWS_ROOT / "bootstrap" / "main.tf").read_text(encoding="utf-8")
    storage = (AWS_ROOT / "controller" / "storage.tf").read_text(encoding="utf-8")

    for source in (bootstrap, storage):
        assert "block_public_acls       = true" in source
        assert "block_public_policy     = true" in source
        assert "ignore_public_acls      = true" in source
        assert "restrict_public_buckets = true" in source
        assert 'sse_algorithm = "AES256"' in source
        assert 'variable = "aws:SecureTransport"' in source


def test_controller_role_cannot_create_labs_or_change_iam() -> None:
    policy = (AWS_ROOT / "controller" / "iam.tf").read_text(encoding="utf-8")

    assert "AmazonSSMManagedInstanceCore" in policy
    assert '"secretsmanager:GetSecretValue"' in policy
    assert "aws_secretsmanager_secret.moodle_token.arn" in policy
    assert "aws_secretsmanager_secret.telegram_config.arn" in policy
    assert "ec2:RunInstances" not in policy
    assert "iam:Create" not in policy
    assert "iam:Put" not in policy
    assert "iam:Attach" not in policy


def test_lab_boundary_uses_separate_roles_and_no_ingress() -> None:
    controller_policy = (AWS_ROOT / "controller" / "iam.tf").read_text(encoding="utf-8")
    labs = (AWS_ROOT / "controller" / "labs.tf").read_text(encoding="utf-8")
    network = (AWS_ROOT / "controller" / "network.tf").read_text(encoding="utf-8")

    assert "ec2:RunInstances" not in controller_policy
    assert 'actions   = ["sts:AssumeRole"]' in labs
    assert "aws_iam_role.controller.arn" in labs
    assert 'resources = [aws_iam_role.lab_instance.arn]' in labs
    assert 'variable = "iam:PassedToService"' in labs
    assert 'values   = ["ec2.amazonaws.com"]' in labs
    assert 'variable = "ec2:ResourceTag/Project"' in labs
    assert 'variable = "ec2:ResourceTag/Environment"' in labs
    assert 'variable = "ec2:ResourceTag/Role"' in labs
    assert 'variable = "ec2:InstanceType"' in labs
    assert 'variable = "ec2:InstanceProfile"' in labs
    assert 'variable = "ec2:MetadataHttpTokens"' in labs
    assert 'values   = ["required"]' in labs
    assert 'variable = "ec2:Encrypted"' in labs
    assert 'variable = "ec2:VolumeType"' in labs
    assert 'values   = ["gp3"]' in labs
    assert 'resource "aws_security_group" "lab"' in network
    assert 'description = "No ingress; ephemeral labs use AWS Systems Manager"' in network
    assert network.count("ingress     = []") == 2


def test_lab_instance_cannot_read_moodle_secret_or_provision_labs() -> None:
    labs = (AWS_ROOT / "controller" / "labs.tf").read_text(encoding="utf-8")
    instance_policy = labs.split('data "aws_iam_policy_document" "lab_instance_artifacts"', 1)[1]
    instance_policy = instance_policy.split(
        'resource "aws_iam_role_policy" "lab_instance_artifacts"', 1
    )[0]

    assert "assignments/*" in instance_policy
    assert "lab-results/*" in instance_policy
    assert "secretsmanager" not in instance_policy
    assert "ec2:RunInstances" not in instance_policy
    assert "s3:DeleteObject" not in instance_policy


def test_deployment_is_commit_and_digest_bound_over_ssm() -> None:
    script = (ROOT / "scripts" / "aws-deploy.ps1").read_text(encoding="utf-8")

    assert "status', '--porcelain', '--untracked-files=all" in script
    assert "'pip', 'wheel'" in script
    assert "--no-deps" in script
    assert "Get-FileHash -Algorithm SHA256" in script
    assert "sha256sum --check --strict" in script
    assert "AWS-RunShellScript" in script
    assert "[IO.File]::WriteAllText" in script
    assert "'file://' + $parametersPath.Replace" in script
    assert "moodle-autotask-scheduler.service" in script
    assert "moodle-autotask-telegram.service" in script
    assert "moodle-autotask-controller' install" in script
    assert "ValidateSet('Deploy', 'Status', 'Activate', 'Deactivate')" in script
    deploy_commands = script.split("$gitStatus =", 1)[1]
    assert "systemctl enable" not in deploy_commands
    activation = script.split("if ($Action -eq 'Activate')", 1)[1].split(
        "if ($Action -eq 'Deactivate')", 1
    )[0]
    assert activation.index("moodle-autotask-refresh-config") < activation.index(
        "systemctl enable"
    )
    assert "systemctl stop moodle-autotask-scheduler.service" in activation
    assert "systemctl disable moodle-autotask-scheduler.service" in activation
    assert "--secret-string" not in script


def test_telegram_secret_has_no_terraform_value_and_services_stay_disabled() -> None:
    storage = (AWS_ROOT / "controller" / "storage.tf").read_text(encoding="utf-8")
    compute = (AWS_ROOT / "controller" / "compute.tf").read_text(encoding="utf-8")
    cloud_init = (AWS_ROOT / "controller" / "cloud-init.sh.tftpl").read_text(
        encoding="utf-8"
    )

    assert 'resource "aws_secretsmanager_secret" "telegram_config"' in storage
    assert "secret_version" not in storage and "secret_string" not in storage
    assert "telegram_secret_arn" in compute
    assert "moodle-autotask-telegram run" in cloud_init
    assert "--telegram-config-file" in cloud_init and "--approval-state" in cloud_init
    assert "systemctl enable --now moodle-autotask" not in cloud_init
