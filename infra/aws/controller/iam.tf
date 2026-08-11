data "aws_iam_policy_document" "controller_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "controller" {
  name               = "${local.name_prefix}-controller"
  description        = "Runtime role for the Moodle Autotask Linux controller"
  assume_role_policy = data.aws_iam_policy_document.controller_assume_role.json
}

resource "aws_iam_role_policy_attachment" "controller_ssm" {
  role       = aws_iam_role.controller.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "controller" {
  statement {
    sid       = "ListProjectArtifacts"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.artifacts.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["controller/*", "artifacts/*", "assignments/*"]
    }
  }

  statement {
    sid    = "ManageProjectArtifacts"
    effect = "Allow"
    actions = [
      "s3:AbortMultipartUpload",
      "s3:DeleteObject",
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
    ]
    resources = [
      "${aws_s3_bucket.artifacts.arn}/controller/*",
      "${aws_s3_bucket.artifacts.arn}/artifacts/*",
      "${aws_s3_bucket.artifacts.arn}/assignments/*",
    ]
  }

  statement {
    sid     = "ReadRuntimeSecrets"
    effect  = "Allow"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.moodle_token.arn,
      aws_secretsmanager_secret.telegram_config.arn,
    ]
  }

  statement {
    sid       = "PublishControllerHealthOnly"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["MoodleAutotask/Controller"]
    }
  }
}

resource "aws_iam_role_policy" "controller" {
  name   = "${local.name_prefix}-controller-runtime"
  role   = aws_iam_role.controller.id
  policy = data.aws_iam_policy_document.controller.json
}

resource "aws_iam_instance_profile" "controller" {
  name = "${local.name_prefix}-controller"
  role = aws_iam_role.controller.name
}
