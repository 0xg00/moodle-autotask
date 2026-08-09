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
    assert "ec2:RunInstances" not in policy
    assert "iam:Create" not in policy
    assert "iam:Put" not in policy
    assert "iam:Attach" not in policy


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
    assert "systemctl enable" not in script
    assert "--secret-string" not in script
