data "aws_ssm_parameter" "ubuntu_ami" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

resource "aws_instance" "controller" {
  ami                         = data.aws_ssm_parameter.ubuntu_ami.value
  instance_type               = var.controller_instance_type
  subnet_id                   = aws_subnet.controller.id
  vpc_security_group_ids      = [aws_security_group.controller.id]
  iam_instance_profile        = aws_iam_instance_profile.controller.name
  associate_public_ip_address = true
  source_dest_check           = true
  disable_api_termination     = true
  user_data_replace_on_change = false

  user_data = templatefile("${path.module}/cloud-init.sh.tftpl", {
    region             = var.region
    secret_arn         = aws_secretsmanager_secret.moodle_token.arn
    project_name       = var.project_name
    scheduler_interval = 86400
  })

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "enabled"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_size_gib
    encrypted             = true
    delete_on_termination = true
  }

  tags = {
    Name = "${local.name_prefix}-controller"
    Role = "controller"
  }

  depends_on = [aws_iam_role_policy_attachment.controller_ssm]
}

resource "aws_cloudwatch_metric_alarm" "controller_status_check" {
  alarm_name          = "${local.name_prefix}-controller-status-check"
  alarm_description   = "EC2 controller failed an instance or system status check"
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "breaching"

  dimensions = {
    InstanceId = aws_instance.controller.id
  }
}
