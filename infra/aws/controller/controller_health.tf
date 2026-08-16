resource "aws_cloudwatch_metric_alarm" "controller_application_health" {
  alarm_name          = "${local.name_prefix}-controller-application-health"
  alarm_description   = "Controller services are unhealthy or the root health publisher is missing."
  namespace           = "MoodleAutotask/Controller"
  metric_name         = "ControllerStateMatchesExpectation"
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 3
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.operator_alerts.arn]

  dimensions = {
    InstanceId = aws_instance.controller.id
    Service    = "aggregate"
  }
}

resource "aws_cloudwatch_metric_alarm" "controller_storage_admission" {
  alarm_name          = "${local.name_prefix}-controller-storage-admission"
  alarm_description   = "Controller storage admission is closed or the root health publisher is missing."
  namespace           = "MoodleAutotask/Controller"
  metric_name         = "StorageAdmissionOpen"
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.operator_alerts.arn]

  dimensions = {
    InstanceId = aws_instance.controller.id
    Service    = "storage"
  }
}
