locals {
  controller_user_data = templatefile("${path.module}/cloud-init.sh.tftpl", {
    region              = var.region
    secret_arn          = aws_secretsmanager_secret.moodle_token.arn
    telegram_secret_arn = aws_secretsmanager_secret.telegram_config.arn
    project_name        = var.project_name
    scheduler_interval  = 86400
    scheduler_config_transport = join(".", concat(
      [var.scheduler_all_courses ? "A" : "C", tostring(var.scheduler_max_new_events_per_cycle)],
      [for shortname in var.scheduler_course_shortnames : base64encode(shortname)],
    ))
  })
  controller_user_data_base64 = base64encode(local.controller_user_data)
  controller_user_data_bytes = 3 * floor(length(local.controller_user_data_base64) / 4) - (
    endswith(local.controller_user_data_base64, "==") ? 2 : (
      endswith(local.controller_user_data_base64, "=") ? 1 : 0
    )
  )
}

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

  user_data = local.controller_user_data

  lifecycle {
    ignore_changes = [user_data]

    precondition {
      condition     = local.controller_user_data_bytes <= 16384
      error_message = "Rendered controller user_data exceeds the EC2 16 KiB raw limit."
    }
  }

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
  alarm_actions       = [aws_sns_topic.operator_alerts.arn]

  dimensions = {
    InstanceId = aws_instance.controller.id
  }
}
