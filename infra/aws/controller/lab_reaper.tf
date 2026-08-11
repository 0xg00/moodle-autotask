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

resource "aws_sns_topic" "operator_alerts" {
  name = "${local.name_prefix}-operator-alerts"
}

data "aws_iam_policy_document" "operator_alerts" {
  statement {
    sid    = "AllowAccountAdministration"
    effect = "Allow"
    actions = [
      "sns:GetTopicAttributes",
      "sns:SetTopicAttributes",
      "sns:AddPermission",
      "sns:RemovePermission",
      "sns:DeleteTopic",
      "sns:Subscribe",
      "sns:ListSubscriptionsByTopic",
      "sns:Publish",
    ]
    resources = [aws_sns_topic.operator_alerts.arn]

    principals {
      type        = "AWS"
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }

  statement {
    sid       = "AllowCloudWatchAlarms"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.operator_alerts.arn]

    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${data.aws_partition.current.partition}:cloudwatch:${var.region}:${data.aws_caller_identity.current.account_id}:alarm:*"]
    }
  }
}

resource "aws_sns_topic_policy" "operator_alerts" {
  arn    = aws_sns_topic.operator_alerts.arn
  policy = data.aws_iam_policy_document.operator_alerts.json
}

resource "aws_sns_topic_subscription" "operator_alert_email" {
  topic_arn = aws_sns_topic.operator_alerts.arn
  protocol  = "email"
  endpoint  = var.operator_alert_email
}

resource "aws_sqs_queue" "lab_reaper_failures" {
  name                      = "${local.name_prefix}-lab-reaper-failures"
  fifo_queue                = false
  sqs_managed_sse_enabled   = true
  message_retention_seconds = 1209600
}

data "aws_iam_policy_document" "lab_reaper_failures" {
  statement {
    sid       = "AllowEventBridgeTargetDlq"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.lab_reaper_failures.arn]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.lab_reaper.arn]
    }
  }

  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["sqs:*"]
    resources = [aws_sqs_queue.lab_reaper_failures.arn]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_sqs_queue_policy" "lab_reaper_failures" {
  queue_url = aws_sqs_queue.lab_reaper_failures.id
  policy    = data.aws_iam_policy_document.lab_reaper_failures.json
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
    sid       = "SendOwnFailureRecords"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.lab_reaper_failures.arn]
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

  destination_config {
    on_failure {
      destination = aws_sqs_queue.lab_reaper_failures.arn
    }
  }

  depends_on = [aws_iam_role_policy.lab_reaper]
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

  dead_letter_config {
    arn = aws_sqs_queue.lab_reaper_failures.arn
  }

  depends_on = [
    aws_lambda_permission.eventbridge_lab_reaper,
    aws_lambda_function_event_invoke_config.lab_reaper,
    aws_sqs_queue_policy.lab_reaper_failures,
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
  alarm_actions       = [aws_sns_topic.operator_alerts.arn]

  dimensions = {
    FunctionName = aws_lambda_function.lab_reaper.function_name
  }
}

resource "aws_cloudwatch_metric_alarm" "lab_reaper_throttles" {
  alarm_name          = "${local.name_prefix}-lab-reaper-throttles"
  alarm_description   = "The lab reaper is being throttled."
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.operator_alerts.arn]

  dimensions = {
    FunctionName = aws_lambda_function.lab_reaper.function_name
  }
}

resource "aws_cloudwatch_metric_alarm" "lab_reaper_async_events_dropped" {
  alarm_name          = "${local.name_prefix}-lab-reaper-async-events-dropped"
  alarm_description   = "Lambda dropped a lab-reaper asynchronous invocation."
  namespace           = "AWS/Lambda"
  metric_name         = "AsyncEventsDropped"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.operator_alerts.arn]

  dimensions = {
    FunctionName = aws_lambda_function.lab_reaper.function_name
  }
}

resource "aws_cloudwatch_metric_alarm" "lab_reaper_destination_delivery_failures" {
  alarm_name          = "${local.name_prefix}-lab-reaper-destination-delivery-failures"
  alarm_description   = "Lambda could not send a lab-reaper failure record to SQS."
  namespace           = "AWS/Lambda"
  metric_name         = "DestinationDeliveryFailures"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.operator_alerts.arn]

  dimensions = {
    FunctionName = aws_lambda_function.lab_reaper.function_name
  }
}

resource "aws_cloudwatch_metric_alarm" "lab_reaper_eventbridge_failed_invocations" {
  alarm_name          = "${local.name_prefix}-lab-reaper-eventbridge-failed-invocations"
  alarm_description   = "EventBridge could not invoke the lab reaper."
  namespace           = "AWS/Events"
  metric_name         = "FailedInvocations"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.operator_alerts.arn]

  dimensions = {
    RuleName = aws_cloudwatch_event_rule.lab_reaper.name
  }
}

resource "aws_cloudwatch_metric_alarm" "lab_reaper_eventbridge_dlq_delivery_failures" {
  alarm_name          = "${local.name_prefix}-lab-reaper-eventbridge-dlq-delivery-failures"
  alarm_description   = "EventBridge could not send a failed lab-reaper invocation to SQS."
  namespace           = "AWS/Events"
  metric_name         = "InvocationsFailedToBeSentToDlq"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.operator_alerts.arn]

  dimensions = {
    RuleName = aws_cloudwatch_event_rule.lab_reaper.name
  }
}

resource "aws_cloudwatch_metric_alarm" "lab_reaper_failure_queue_messages" {
  alarm_name          = "${local.name_prefix}-lab-reaper-failure-queue-messages"
  alarm_description   = "The lab-reaper failure queue has records requiring operator inspection."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.operator_alerts.arn]

  dimensions = {
    QueueName = aws_sqs_queue.lab_reaper_failures.name
  }
}

resource "aws_cloudwatch_metric_alarm" "lab_reaper_missing_invocations" {
  alarm_name          = "${local.name_prefix}-lab-reaper-missing-invocations"
  alarm_description   = "EventBridge has not invoked the lab reaper during the expected schedule window."
  namespace           = "AWS/Events"
  metric_name         = "Invocations"
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.operator_alerts.arn]

  dimensions = {
    RuleName = aws_cloudwatch_event_rule.lab_reaper.name
  }
}
