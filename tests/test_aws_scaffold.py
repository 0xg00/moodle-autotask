import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AWS_ROOT = ROOT / "infra" / "aws"


def _terraform_block(source: str, kind: str, resource_type: str, name: str) -> str:
    marker = (
        f'{kind} "{resource_type}" {{'
        if kind in {"variable", "output"}
        else f'{kind} "{resource_type}" "{name}" {{'
    )
    remainder = source.split(marker, 1)[1]
    ends = [
        position
        for position in (
            remainder.find("\nresource "),
            remainder.find("\ndata "),
            remainder.find("\nvariable "),
            remainder.find("\noutput "),
        )
        if position >= 0
    ]
    return remainder[: min(ends)] if ends else remainder


def test_controller_has_no_ingress_and_requires_imdsv2() -> None:
    network = (AWS_ROOT / "controller" / "network.tf").read_text(encoding="utf-8")
    compute = (AWS_ROOT / "controller" / "compute.tf").read_text(encoding="utf-8")

    assert "ingress     = []" in network
    assert 'http_tokens                 = "required"' in compute
    assert "disable_api_termination     = true" in compute
    assert "encrypted             = true" in compute
    assert "key_name" not in compute


def test_controller_updates_do_not_restart_ec2_for_user_data_changes() -> None:
    compute = (AWS_ROOT / "controller" / "compute.tf").read_text(encoding="utf-8")

    assert "lifecycle {" in compute
    assert "ignore_changes = [user_data]" in compute


def test_controller_scheduler_pins_a_campaign_safe_moodle_timeout() -> None:
    cloud_init = (AWS_ROOT / "controller" / "cloud-init.sh.tftpl").read_text(encoding="utf-8")

    assert "moodle-autotask-scheduler run" in cloud_init
    assert "--request-timeout-seconds 60" in cloud_init


def test_controller_health_is_root_published_and_missing_metric_is_breaching() -> None:
    cloud_init = (AWS_ROOT / "controller" / "cloud-init.sh.tftpl").read_text(encoding="utf-8")
    iam = (AWS_ROOT / "controller" / "iam.tf").read_text(encoding="utf-8")
    health = (AWS_ROOT / "controller" / "controller_health.tf").read_text(encoding="utf-8")

    assert "/run/${project_name}-health" in cloud_init
    assert "${project_name}-health.timer" in cloud_init
    assert "enable --now ${project_name}-health.timer" in cloud_init
    assert 'actions   = ["cloudwatch:PutMetricData"]' in iam
    assert 'variable = "cloudwatch:namespace"' in iam
    assert 'values   = ["MoodleAutotask/Controller"]' in iam
    assert 'metric_name         = "ControllerStateMatchesExpectation"' in health
    assert 'statistic           = "Minimum"' in health
    assert "period              = 60" in health
    assert "evaluation_periods  = 5" in health and "datapoints_to_alarm = 3" in health
    assert 'treat_missing_data  = "breaching"' in health
    assert 'Service    = "aggregate"' in health


def test_controller_scheduler_scope_is_explicit_and_has_no_fixture_hardcode() -> None:
    cloud_init = (AWS_ROOT / "controller" / "cloud-init.sh.tftpl").read_text(encoding="utf-8")
    compute = (AWS_ROOT / "controller" / "compute.tf").read_text(encoding="utf-8")
    variables = (AWS_ROOT / "controller" / "variables.tf").read_text(encoding="utf-8")
    deploy = (ROOT / "scripts" / "aws-deploy.ps1").read_text(encoding="utf-8")

    for source in (cloud_init, compute, variables, deploy):
        assert "ASIX-CAMPAIGN-01" not in source
    assert "--scheduler-config-file /etc/${project_name}/scheduler.json" in cloud_init
    assert "scheduler_config_base64 = base64encode(" in compute
    assert "var.scheduler_all_courses ? jsonencode({" in compute
    assert "scheduler_course_shortnames" in variables and "scheduler_all_courses" in variables
    assert "scheduler_max_new_events_per_cycle" in variables
    assert "length(base64encode(shortname))" in variables
    assert "length(base64encode(local.controller_user_data)) <= 21848" in compute
    assert "New-SchedulerConfigJson" in deploy
    assert "Deploy requires exactly one scheduler scope" in deploy
    assert "os.replace(temporary, target)" in cloud_init
    assert "os.replace(temporary, target)" in deploy
    assert "os.chmod(temporary, 0o640)" in cloud_init and "os.chmod(temporary, 0o640)" in deploy
    assert "len(courses) <= 64" in cloud_init and "2048" in cloud_init
    assert deploy.index("$schedulerConfigInstallCommand,") < deploy.index(
        "moodle-autotask-controller' install"
    )
    assert deploy.index("trap restore_scheduler_config EXIT") < deploy.index(
        "moodle-autotask-controller' install"
    )
    deploy_block = deploy.split(
        'Send-ControllerCommand -TargetInstanceId $controllerInstanceId', 1
    )[1]
    assert deploy_block.index("$schedulerConfigGuardCommand,") < deploy_block.index(
        'if [ "$scheduler_was_active" = true ]; then systemctl stop'
    )


def test_controller_tfvars_example_is_rejected_until_scope_is_selected() -> None:
    example = (AWS_ROOT / "controller" / "terraform.tfvars.example").read_text(
        encoding="utf-8"
    )
    assert "scheduler_course_shortnames       = []" in example
    assert "scheduler_all_courses             = false" in example


def test_independent_lab_reaper_has_bounded_tag_scoped_hard_ttl() -> None:
    reaper = (AWS_ROOT / "controller" / "lab_reaper.tf").read_text(encoding="utf-8")
    source = (AWS_ROOT / "controller" / "lab_reaper.py").read_text(encoding="utf-8")
    variables = (AWS_ROOT / "controller" / "variables.tf").read_text(encoding="utf-8")
    versions = (AWS_ROOT / "controller" / "versions.tf").read_text(encoding="utf-8")
    controller_iam = (AWS_ROOT / "controller" / "iam.tf").read_text(encoding="utf-8")
    labs = (AWS_ROOT / "controller" / "labs.tf").read_text(encoding="utf-8")

    assert 'source  = "hashicorp/archive"' in versions and 'version = "= 2.7.1"' in versions
    assert 'schedule_expression = "rate(5 minutes)"' in reaper
    assert 'output_file_mode = "0664"' in reaper
    assert "reserved_concurrent_executions" not in reaper
    assert "timeout          = 30" in reaper
    assert 'logging_config {' in reaper and 'log_format = "JSON"' in reaper
    assert "aws_lambda_function_event_invoke_config" in reaper
    invoke_resource = 'resource "aws_lambda_function_event_invoke_config" "lab_reaper"'
    rule_resource = 'resource "aws_cloudwatch_event_rule" "lab_reaper"'
    invoke_config = reaper.split(invoke_resource, 1)[1].split(rule_resource, 1)[0]
    target = reaper.split('resource "aws_cloudwatch_event_target" "lab_reaper"', 1)[1].split(
        'resource "aws_lambda_permission" "eventbridge_lab_reaper"', 1
    )[0]
    for block in (invoke_config, target):
        assert "maximum_event_age_in_seconds = 300" in block
        assert "maximum_retry_attempts       = 0" in block
    assert "aws_lambda_permission.eventbridge_lab_reaper" in target
    assert "aws_lambda_function_event_invoke_config.lab_reaper" in target
    assert "aws_cloudwatch_event_target" in reaper and "aws_lambda_permission" in reaper
    assert "aws_cloudwatch_metric_alarm" in reaper and "retention_in_days = 30" in reaper
    assert "ec2:DescribeInstances" in reaper and "ec2:TerminateInstances" in reaper
    for tag in ("Project", "Environment", "ManagedBy", "Role", "ProvisionKey"):
        assert f"ec2:ResourceTag/{tag}" in reaper
    assert "moodle-autotask" in reaper and "tag-key" in source
    assert "sorted(set(candidates))[:max_terminations]" in source
    assert "lab_hard_ttl_seconds" in variables and "10800" in variables and "86400" in variables
    assert "lab_reaper_max_terminations_per_run" in variables and "<= 20" in variables
    assert "lab_reaper" not in controller_iam
    assert "lab_reaper" not in labs


def test_operator_alerts_and_reaper_failure_capture_are_scoped_and_actionable() -> None:
    reaper = (AWS_ROOT / "controller" / "lab_reaper.tf").read_text(encoding="utf-8")
    compute = (AWS_ROOT / "controller" / "compute.tf").read_text(encoding="utf-8")
    variables = (AWS_ROOT / "controller" / "variables.tf").read_text(encoding="utf-8")
    outputs = (AWS_ROOT / "controller" / "outputs.tf").read_text(encoding="utf-8")
    example = (AWS_ROOT / "controller" / "terraform.tfvars.example").read_text(encoding="utf-8")

    email = _terraform_block(variables, "variable", "operator_alert_email", "")
    assert "type        = string" in email and "default" not in email
    assert "length(base64encode(var.operator_alert_email))" in email
    assert 'regexall("@", var.operator_alert_email)' in email
    assert 'regex("[\\\\s\\\\p{C}]", var.operator_alert_email)' in email
    assert 'operator_alert_email = "<OPERATOR_EMAIL>"' in example
    assert "operator_alert_topic_arn" in outputs and "lab_reaper_failure_queue_url" in outputs

    topic = _terraform_block(reaper, "resource", "aws_sns_topic", "operator_alerts")
    subscription = _terraform_block(
        reaper, "resource", "aws_sns_topic_subscription", "operator_alert_email"
    )
    topic_policy = _terraform_block(reaper, "data", "aws_iam_policy_document", "operator_alerts")
    topic_policy_attachment = _terraform_block(
        reaper, "resource", "aws_sns_topic_policy", "operator_alerts"
    )
    assert 'name = "${local.name_prefix}-operator-alerts"' in topic
    assert (
        'protocol  = "email"' in subscription
        and "endpoint  = var.operator_alert_email" in subscription
    )
    assert 'identifiers = ["cloudwatch.amazonaws.com"]' in topic_policy
    assert "aws:SourceAccount" in topic_policy and "aws:SourceArn" in topic_policy
    assert "policy = data.aws_iam_policy_document.operator_alerts.json" in topic_policy_attachment

    queue = _terraform_block(reaper, "resource", "aws_sqs_queue", "lab_reaper_failures")
    queue_policy = _terraform_block(
        reaper, "data", "aws_iam_policy_document", "lab_reaper_failures"
    )
    queue_policy_attachment = _terraform_block(
        reaper, "resource", "aws_sqs_queue_policy", "lab_reaper_failures"
    )
    reaper_policy = _terraform_block(reaper, "data", "aws_iam_policy_document", "lab_reaper")
    assert "fifo_queue                = false" in queue
    assert "sqs_managed_sse_enabled   = true" in queue
    assert "message_retention_seconds = 1209600" in queue
    assert 'identifiers = ["events.amazonaws.com"]' in queue_policy
    assert "AllowEventBridgeTargetDlq" in queue_policy and "aws:SourceArn" in queue_policy
    assert "DenyInsecureTransport" in queue_policy and "aws:SecureTransport" in queue_policy
    assert (
        "policy    = data.aws_iam_policy_document.lab_reaper_failures.json"
        in queue_policy_attachment
    )
    assert "SendOwnFailureRecords" in reaper_policy
    assert 'actions   = ["sqs:SendMessage"]' in reaper_policy
    assert "resources = [aws_sqs_queue.lab_reaper_failures.arn]" in reaper_policy

    invoke_config = _terraform_block(
        reaper, "resource", "aws_lambda_function_event_invoke_config", "lab_reaper"
    )
    target = _terraform_block(reaper, "resource", "aws_cloudwatch_event_target", "lab_reaper")
    assert "destination = aws_sqs_queue.lab_reaper_failures.arn" in invoke_config
    assert (
        "dead_letter_config" in target and "arn = aws_sqs_queue.lab_reaper_failures.arn" in target
    )
    assert "aws_sqs_queue_policy.lab_reaper_failures" in target

    expected_alarms = {
        "lab_reaper_errors": ("AWS/Lambda", "Errors", "FunctionName"),
        "lab_reaper_throttles": ("AWS/Lambda", "Throttles", "FunctionName"),
        "lab_reaper_async_events_dropped": ("AWS/Lambda", "AsyncEventsDropped", "FunctionName"),
        "lab_reaper_destination_delivery_failures": (
            "AWS/Lambda",
            "DestinationDeliveryFailures",
            "FunctionName",
        ),
        "lab_reaper_eventbridge_failed_invocations": (
            "AWS/Events",
            "FailedInvocations",
            "RuleName",
        ),
        "lab_reaper_eventbridge_dlq_delivery_failures": (
            "AWS/Events",
            "InvocationsFailedToBeSentToDlq",
            "RuleName",
        ),
        "lab_reaper_failure_queue_messages": (
            "AWS/SQS",
            "ApproximateNumberOfMessagesVisible",
            "QueueName",
        ),
        "lab_reaper_missing_invocations": ("AWS/Events", "Invocations", "RuleName"),
    }
    for name, (namespace, metric, dimension) in expected_alarms.items():
        alarm = _terraform_block(reaper, "resource", "aws_cloudwatch_metric_alarm", name)
        assert f'namespace           = "{namespace}"' in alarm
        assert f'metric_name         = "{metric}"' in alarm
        assert f"{dimension} =" in alarm
        assert "alarm_actions       = [aws_sns_topic.operator_alerts.arn]" in alarm

    missing = _terraform_block(
        reaper, "resource", "aws_cloudwatch_metric_alarm", "lab_reaper_missing_invocations"
    )
    assert 'comparison_operator = "LessThanThreshold"' in missing
    assert 'treat_missing_data  = "breaching"' in missing and "period              = 900" in missing
    controller_alarm = _terraform_block(
        compute, "resource", "aws_cloudwatch_metric_alarm", "controller_status_check"
    )
    assert "alarm_actions       = [aws_sns_topic.operator_alerts.arn]" in controller_alarm


def test_deploy_scope_renderer_preserves_exact_unicode_and_rejects_limits() -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is unavailable")
    path = (ROOT / "scripts" / "aws-deploy.ps1").as_posix().replace("'", "''")
    harness = f"""
$source = [IO.File]::ReadAllText('{path}')
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$pattern = '(?s)function New-SchedulerConfigJson \\{{.*?\\r?\\n\\}}\\r?\\n\\r?\\n'
$match = [regex]::Match($source, $pattern + 'function New-SchedulerConfigInstallCommand')
if (-not $match.Success) {{ throw 'could not extract scope renderer' }}
$definition = $match.Value -replace '\\r?\\nfunction New-SchedulerConfigInstallCommand$', ''
Invoke-Expression $definition
$names = @(
    [string]::Concat('Stra', [char]0x00DF, 'e'), 'STRASSE',
    [string]::Concat([char]0x200E, 'format name'),
    [string]::Concat('A', [char]0xD83D, [char]0xDE00)
)
New-SchedulerConfigJson -SelectedCourseShortnames $names -UseAllCourses $false -EventCap 4
"""
    rendered = subprocess.run(
        [powershell, "-NoProfile", "-Command", harness],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert json.loads(rendered.stdout)["courseShortnames"] == [
        "Straße",
        "STRASSE",
        "\u200eformat name",
        "A😀",
    ]
    oversized = (
        harness
        + "\nNew-SchedulerConfigJson -SelectedCourseShortnames "
        + "@(1..65 | ForEach-Object { [string]$_ }) -UseAllCourses $false -EventCap 4"
    )
    rejected = subprocess.run(
        [powershell, "-NoProfile", "-Command", oversized],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert rejected.returncode != 0 and "At most 64" in rejected.stderr
    surrogate = harness + (
        "\nNew-SchedulerConfigJson -SelectedCourseShortnames @([string][char]0xD800) "
        "-UseAllCourses $false -EventCap 4"
    )
    invalid_unicode = subprocess.run(
        [powershell, "-NoProfile", "-Command", surrogate],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert invalid_unicode.returncode != 0 and "valid UTF-8" in invalid_unicode.stderr
    deploy_rejected = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-Command",
            (
                f"& '{path}' -Action Deploy -AccountId 123456789012 "
                "-CourseShortname @(0..64 | ForEach-Object { [string]$_ }) "
                "-MaxNewEventsPerCycle 4"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert deploy_rejected.returncode != 0
    assert "At most 64" in deploy_rejected.stderr


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
    assert 'id     = "expire-assignment-inputs"' in storage
    assert 'prefix = "assignments/"' in storage
    assert "days = 7" in storage


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
    assert 'actions   = ["ssm:SendCommand"]' in labs
    assert "document/AWS-RunPowerShellScript" in labs
    assert 'actions   = ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"]' in labs
    assert 'variable = "ssm:resourceTag/Project"' in labs
    assert 'variable = "ssm:resourceTag/Environment"' in labs
    assert 'variable = "ssm:resourceTag/Role"' in labs
    assert 'resource "aws_security_group" "lab"' in network
    assert 'description = "No ingress; ephemeral labs use AWS Systems Manager"' in network
    assert network.count("ingress     = []") == 2


def test_lab_instance_has_only_ssm_runtime_permissions() -> None:
    labs = (AWS_ROOT / "controller" / "labs.tf").read_text(encoding="utf-8")
    instance_section = labs.split('resource "aws_iam_role" "lab_instance"', 1)[1]
    instance_section = instance_section.split(
        'resource "aws_iam_instance_profile" "lab"', 1
    )[0]

    assert "AmazonSSMManagedInstanceCore" in instance_section
    assert "aws_s3_bucket.artifacts" not in instance_section
    assert "assignments/*" not in instance_section
    assert "lab-results/*" not in instance_section
    assert "lab_instance_artifacts" not in labs


def test_vm_import_roles_are_separate_and_prefix_limited() -> None:
    imports = (AWS_ROOT / "controller" / "image_imports.tf").read_text(encoding="utf-8")
    labs = (AWS_ROOT / "controller" / "labs.tf").read_text(encoding="utf-8")

    assert 'identifiers = ["vmie.amazonaws.com"]' in imports
    assert 'variable = "sts:Externalid"' in imports
    assert 'values   = ["vmimport"]' in imports
    assert 'variable = "aws:SourceAccount"' in imports
    assert 'values   = ["assignments/*"]' in imports
    assert '"${aws_s3_bucket.artifacts.arn}/assignments/*"' in imports
    assert 'resources = [aws_iam_role.vmimport.arn]' in imports
    assert 'values   = ["vmie.amazonaws.com"]' in imports
    assert 'identifiers = [aws_iam_role.controller.arn]' in imports
    assert 'resources = [aws_iam_role.image_importer.arn]' in imports
    assert 'values   = ["lab-image"]' in labs


def test_deployment_is_commit_and_digest_bound_over_ssm() -> None:
    script = (ROOT / "scripts" / "aws-deploy.ps1").read_text(encoding="utf-8")

    assert "$env:AWS_CLI_FILE_ENCODING = 'UTF-8'" in script
    assert "$env:AWS_CLI_OUTPUT_ENCODING = 'UTF-8'" in script
    assert script.index("$env:AWS_CLI_OUTPUT_ENCODING") < script.index(
        "$script:awsCli = Resolve-AwsCli"
    )
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
    assert "moodle-autotask-worker.service" in script
    assert "moodle-autotask-agent.service" in script
    assert "moodle-autotask-worker' --help" in script
    assert "moodle-autotask-agent' --help" in script
    assert "'ec2', 'describe-subnets'" in script
    assert "'ec2', 'describe-security-groups'" in script
    assert "'iam', 'get-role'" in script
    assert "'iam', 'get-role-policy'" in script
    assert "'iam', 'get-instance-profile'" in script
    assert "--provisioner-role-arn '$labRoleArn'" in script
    assert "--subnet-id '$labSubnetId'" in script
    assert "--security-group-id '$labSecurityGroupId'" in script
    assert "--image-id '$labImageId'" in script
    assert "--artifact-bucket '$artifactBucket'" in script
    assert "--image-importer-role-arn '$imageImporterRoleArn'" in script
    assert "--vmimport-role-name '$vmImportRoleName'" in script
    assert "moodle-autotask-controller' install" in script
    assert (
        "ValidateSet('Deploy', 'Status', 'Activate', 'Deactivate', "
        "'CodexLogin', 'CodexSmoke')"
        in script
    )
    assert "/usr/local/sbin/moodle-autotask-install-codex" in script
    assert "moodle-autotask-codex-login.service" in script
    assert "CODEX_HOME=/var/lib/moodle-agent/.codex" in script
    assert (
        "moodle-autotask-codex sandbox --permission-profile moodle-autotask "
        "--include-managed-config -C /var/lib/moodle-agent/smoke -- sh -c"
    ) in script
    assert "test ! -r /var/lib/moodle-agent/.codex/auth.json" in script
    assert "test ! -r /etc/moodle-autotask/moodle-token.json" in script
    assert "codex-sandbox=isolated" in script
    assert "--ephemeral --skip-git-repo-check" in script
    assert "application-secrets=unreadable" in script
    assert "test -r /etc/moodle-autotask/moodle-token.json" in script
    deploy_commands = script.split("$gitStatus =", 1)[1]
    assert "legacy_three_was_active" in deploy_commands
    assert "systemctl enable --now moodle-autotask-agent.service" in deploy_commands
    activation = script.split("if ($Action -eq 'Activate')", 1)[1].split(
        "if ($Action -eq 'Deactivate')", 1
    )[0]
    assert activation.index("moodle-autotask-refresh-config") < activation.index(
        "systemctl enable"
    )
    assert "systemctl stop moodle-autotask-scheduler.service" in activation
    assert "systemctl disable moodle-autotask-scheduler.service" in activation
    assert "systemctl is-active --quiet moodle-autotask-worker.service" in activation
    assert "--secret-string" not in script
    assert "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" in script


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


def test_controller_bootstrap_installs_bubblewrap() -> None:
    cloud_init = (AWS_ROOT / "controller" / "cloud-init.sh.tftpl").read_text(
        encoding="utf-8"
    )

    assert "apt-get update" in cloud_init
    assert "apt-get install -y bubblewrap " in cloud_init
    assert cloud_init.index("apt-get update") < cloud_init.index(
        "apt-get install -y bubblewrap "
    )


def _scheduler_guard_command(config: Path) -> str:
    deploy = (ROOT / "scripts" / "aws-deploy.ps1").read_text(encoding="utf-8")
    match = re.search(
        r"function New-SchedulerConfigGuardCommand.*?return \(@'\n(.*?)\n'@\)",
        deploy,
        re.DOTALL,
    )
    assert match is not None
    return match.group(1).replace("__CONFIG_PATH__", str(config))


@pytest.mark.skipif(os.name == "nt", reason="requires a POSIX bash service fake")
@pytest.mark.parametrize(
    ("failed_command", "failed_service"),
    (
        ("stop", "scheduler"),
        ("stop", "telegram"),
        ("stop", "worker"),
        ("start", "scheduler"),
        ("start", "telegram"),
        ("start", "worker"),
        ("enable", "agent"),
    ),
)
def test_remote_deploy_recovery_keeps_migrated_legacy_config(
    tmp_path: Path, failed_command: str, failed_service: str
) -> None:
    config = tmp_path / "scheduler.json"
    state = tmp_path / "state"
    script = f"""set -eu
state={state!s}; mkdir -p "$state"; touch "$state/scheduler" "$state/telegram" "$state/worker"
service_name() {{ service=${{1#moodle-autotask-}}; printf '%s' "${{service%.service}}"; }}
systemctl() {{
  command=$1; shift
  case "$command" in
    is-active) service=$(service_name "$2"); test -f "$state/$service"; return ;;
    cat) return 1 ;;
    stop) service=$(service_name "$1") ;;
    start) service=$(service_name "$1") ;;
    enable) test "$1" = --now; service=$(service_name "$2") ;;
    *) return 2 ;;
  esac
  if [ "$command" = {failed_command!r} ] && [ "$service" = {failed_service!r} ] &&
     [ ! -f "$state/failed-$service" ]; then
    touch "$state/failed-$service"; return 1
  fi
  if [ "$command" = stop ]; then rm -f "$state/$service"; else touch "$state/$service"; fi
}}
{_scheduler_guard_command(config)}
systemctl stop moodle-autotask-scheduler.service
systemctl stop moodle-autotask-telegram.service
systemctl stop moodle-autotask-worker.service
systemctl stop moodle-autotask-agent.service
printf migrated > {config!s}; chmod 640 {config!s}
systemctl start moodle-autotask-scheduler.service
systemctl start moodle-autotask-telegram.service
systemctl start moodle-autotask-worker.service
systemctl enable --now moodle-autotask-agent.service
trap - EXIT
"""
    completed = subprocess.run(["bash", "-c", script], check=False, timeout=15)
    assert completed.returncode != 0
    assert (state / f"failed-{failed_service}").is_file()
    if failed_command == "stop":
        assert not config.exists()
    else:
        assert config.read_bytes() == b"migrated"
        assert (config.stat().st_mode & 0o777) == 0o640
    assert (state / "scheduler").is_file()
    assert (state / "telegram").is_file()
    assert (state / "worker").is_file()
    assert not (state / "agent").exists()
    assert not tuple(tmp_path.glob(".scheduler.previous.*"))


@pytest.mark.skipif(os.name == "nt", reason="requires a POSIX bash service fake")
def test_remote_deploy_recovery_restores_existing_config_after_install_failure(
    tmp_path: Path,
) -> None:
    config = tmp_path / "scheduler.json"
    config.write_bytes(b"previous")
    config.chmod(0o600)
    state = tmp_path / "state"
    script = f"""set -eu
state={state!s}; mkdir -p "$state"
touch "$state/scheduler" "$state/telegram" "$state/worker" "$state/agent"
service_name() {{ service=${{1#moodle-autotask-}}; printf '%s' "${{service%.service}}"; }}
systemctl() {{
  case "$1" in
    is-active) service=$(service_name "$3"); test -f "$state/$service" ;;
    cat) return 0 ;;
    stop) service=$(service_name "$2"); rm -f "$state/$service" ;;
    start) service=$(service_name "$2"); touch "$state/$service" ;;
    *) return 0 ;;
  esac
}}
{_scheduler_guard_command(config)}
printf migrated > {config!s}; chmod 640 {config!s}
false
"""
    completed = subprocess.run(["bash", "-c", script], check=False, timeout=15)
    assert completed.returncode != 0
    assert config.read_bytes() == b"previous"
    assert (config.stat().st_mode & 0o777) == 0o600
    assert (state / "scheduler").is_file()
    assert (state / "telegram").is_file()
    assert (state / "worker").is_file()
    assert (state / "agent").is_file()
    assert not tuple(tmp_path.glob(".scheduler.previous.*"))


def _all_active_systemctl_fake(state: Path, *, fail_stop_agent: bool = False) -> str:
    return f"""state={state!s}; mkdir -p "$state"
touch "$state/scheduler" "$state/telegram" "$state/worker" "$state/agent"
service_name() {{ service=${{1#moodle-autotask-}}; printf '%s' "${{service%.service}}"; }}
systemctl() {{
  command=$1
  case "$command" in
    is-active) service=$(service_name "$3"); test -f "$state/$service"; return ;;
    cat) return 0 ;;
    stop)
      service=$(service_name "$2")
      if [ {str(fail_stop_agent).lower()} = true ] && [ "$service" = agent ] &&
         [ ! -f "$state/failed-stop-agent" ]; then
        touch "$state/failed-stop-agent"; return 1
      fi
      rm -f "$state/$service"; return
      ;;
    start) service=$(service_name "$2"); touch "$state/$service"; return ;;
    *) return 2 ;;
  esac
}}
"""


def _assert_all_services_active(state: Path) -> None:
    for service in ("scheduler", "telegram", "worker", "agent"):
        assert (state / service).is_file()


@pytest.mark.skipif(os.name == "nt", reason="requires a POSIX bash service fake")
def test_remote_guard_restores_existing_config_after_stop_agent_failure(tmp_path: Path) -> None:
    config = tmp_path / "scheduler.json"
    config.write_bytes(b"previous")
    config.chmod(0o600)
    state = tmp_path / "state"
    script = f"""set -eu
{_all_active_systemctl_fake(state, fail_stop_agent=True)}
{_scheduler_guard_command(config)}
systemctl stop moodle-autotask-scheduler.service
systemctl stop moodle-autotask-telegram.service
systemctl stop moodle-autotask-worker.service
systemctl stop moodle-autotask-agent.service
"""
    completed = subprocess.run(["bash", "-c", script], check=False, timeout=15)
    assert completed.returncode != 0
    assert (state / "failed-stop-agent").is_file()
    assert config.read_bytes() == b"previous"
    assert (config.stat().st_mode & 0o777) == 0o600
    _assert_all_services_active(state)
    assert not tuple(tmp_path.glob(".scheduler.candidate.*"))
    assert not tuple(tmp_path.glob(".scheduler.previous.*"))


@pytest.mark.skipif(os.name == "nt", reason="requires a POSIX bash service fake")
def test_remote_guard_rejects_dangling_symlink_without_temp_or_state_loss(tmp_path: Path) -> None:
    config = tmp_path / "scheduler.json"
    config.symlink_to(tmp_path / "missing-target")
    state = tmp_path / "state"
    script = f"""set -eu
{_all_active_systemctl_fake(state)}
{_scheduler_guard_command(config)}
"""
    completed = subprocess.run(["bash", "-c", script], check=False, timeout=15)
    assert completed.returncode != 0
    assert config.is_symlink()
    _assert_all_services_active(state)
    assert not tuple(tmp_path.glob(".scheduler.candidate.*"))
    assert not tuple(tmp_path.glob(".scheduler.previous.*"))


@pytest.mark.skipif(os.name == "nt", reason="requires a POSIX bash service fake")
@pytest.mark.parametrize("failing_tool", ("mktemp", "cp"))
def test_remote_guard_cleans_candidate_after_backup_tool_failure(
    tmp_path: Path, failing_tool: str
) -> None:
    config = tmp_path / "scheduler.json"
    config.write_bytes(b"previous")
    config.chmod(0o600)
    state = tmp_path / "state"
    tools = tmp_path / "tools"
    tools.mkdir()
    wrapper = tools / failing_tool
    if failing_tool == "mktemp":
        wrapper.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    else:
        wrapper.write_text("#!/bin/sh\nprintf partial > \"$3\"\nexit 1\n", encoding="utf-8")
    wrapper.chmod(0o700)
    script = f"""set -eu
PATH={tools!s}:$PATH
{_all_active_systemctl_fake(state)}
{_scheduler_guard_command(config)}
"""
    completed = subprocess.run(["bash", "-c", script], check=False, timeout=15)
    assert completed.returncode != 0
    assert config.read_bytes() == b"previous"
    assert (config.stat().st_mode & 0o777) == 0o600
    _assert_all_services_active(state)
    assert not tuple(tmp_path.glob(".scheduler.candidate.*"))
    assert not tuple(tmp_path.glob(".scheduler.previous.*"))
