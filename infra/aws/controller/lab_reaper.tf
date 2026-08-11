data "archive_file" "lab_reaper" {
  type             = "zip"
  source_file      = "${path.module}/lab_reaper.py"
  output_file_mode = "0664"
  output_path      = "${path.module}/.terraform/lab_reaper.zip"
}

data "aws_iam_policy_document" "lab_reaper_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lab_reaper" {
  name               = "${local.name_prefix}-lab-reaper"
  description        = "Independent hard-TTL reaper for tagged Moodle Autotask labs"
  assume_role_policy = data.aws_iam_policy_document.lab_reaper_assume_role.json
}

resource "aws_cloudwatch_log_group" "lab_reaper" {
  name              = "/aws/lambda/${local.name_prefix}-lab-reaper"
  retention_in_days = 30
}

data "aws_iam_policy_document" "lab_reaper" {
  statement {
    sid       = "WriteOwnLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lab_reaper.arn}:*"]
  }

  statement {
    sid       = "ReadTaggedLabState"
    effect    = "Allow"
    actions   = ["ec2:DescribeInstances"]
    resources = ["*"]
  }

  statement {
    sid       = "TerminateOnlyOwnedStaleLabs"
    effect    = "Allow"
    actions   = ["ec2:TerminateInstances"]
    resources = ["arn:${data.aws_partition.current.partition}:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:instance/*"]

    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Project"
      values   = [var.project_name]
    }

    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Environment"
      values   = [var.environment]
    }

    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/ManagedBy"
      values   = ["moodle-autotask"]
    }

    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Role"
      values   = ["lab"]
    }

    condition {
      test     = "Null"
      variable = "ec2:ResourceTag/ProvisionKey"
      values   = ["false"]
    }
  }
}

resource "aws_iam_role_policy" "lab_reaper" {
  name   = "${local.name_prefix}-lab-reaper"
  role   = aws_iam_role.lab_reaper.id
  policy = data.aws_iam_policy_document.lab_reaper.json
}

resource "aws_lambda_function" "lab_reaper" {
  function_name    = "${local.name_prefix}-lab-reaper"
  description      = "Terminates only over-age tagged lab instances independent of the controller"
  filename         = data.archive_file.lab_reaper.output_path
  source_code_hash = data.archive_file.lab_reaper.output_base64sha256
  role             = aws_iam_role.lab_reaper.arn
  handler          = "lab_reaper.lambda_handler"
  runtime          = "python3.13"
  timeout          = 30
  memory_size      = 128

  environment {
    variables = {
      PROJECT_NAME             = var.project_name
      ENVIRONMENT              = var.environment
      LAB_HARD_TTL_SECONDS     = tostring(var.lab_hard_ttl_seconds)
      MAX_TERMINATIONS_PER_RUN = tostring(var.lab_reaper_max_terminations_per_run)
    }
  }

  logging_config {
    log_format = "JSON"
  }

  depends_on = [aws_cloudwatch_log_group.lab_reaper]
}

resource "aws_lambda_function_event_invoke_config" "lab_reaper" {
  function_name                = aws_lambda_function.lab_reaper.function_name
  maximum_event_age_in_seconds = 300
  maximum_retry_attempts       = 0
}

resource "aws_cloudwatch_event_rule" "lab_reaper" {
  name                = "${local.name_prefix}-lab-reaper"
  description         = "Run the independent lab hard-TTL reaper every five minutes"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "lab_reaper" {
  rule = aws_cloudwatch_event_rule.lab_reaper.name
  arn  = aws_lambda_function.lab_reaper.arn

  retry_policy {
    maximum_event_age_in_seconds = 300
    maximum_retry_attempts       = 0
  }

  depends_on = [
    aws_lambda_permission.eventbridge_lab_reaper,
    aws_lambda_function_event_invoke_config.lab_reaper,
  ]
}

resource "aws_lambda_permission" "eventbridge_lab_reaper" {
  statement_id  = "AllowEventBridgeLabReaper"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.lab_reaper.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.lab_reaper.arn
}

resource "aws_cloudwatch_metric_alarm" "lab_reaper_errors" {
  alarm_name          = "${local.name_prefix}-lab-reaper-errors"
  alarm_description   = "The independent lab hard-TTL reaper has failed."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.lab_reaper.function_name
  }
}
