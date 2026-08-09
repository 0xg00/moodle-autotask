data "aws_ssm_parameter" "windows_server_2022_ami" {
  name = "/aws/service/ami-windows-latest/Windows_Server-2022-English-Full-Base"
}

data "aws_iam_policy_document" "lab_instance_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lab_instance" {
  name               = "${local.name_prefix}-lab-instance"
  description        = "Runtime role for ephemeral Moodle Autotask labs"
  assume_role_policy = data.aws_iam_policy_document.lab_instance_assume_role.json
}

resource "aws_iam_role_policy_attachment" "lab_instance_ssm" {
  role       = aws_iam_role.lab_instance.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "lab_instance_artifacts" {
  statement {
    sid       = "ListTaskInputsAndResults"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.artifacts.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["assignments/*", "lab-results/*"]
    }
  }

  statement {
    sid       = "ReadTaskInputs"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.artifacts.arn}/assignments/*"]
  }

  statement {
    sid       = "WriteLabResults"
    effect    = "Allow"
    actions   = ["s3:AbortMultipartUpload", "s3:PutObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/lab-results/*"]
  }
}

resource "aws_iam_role_policy" "lab_instance_artifacts" {
  name   = "${local.name_prefix}-lab-artifacts"
  role   = aws_iam_role.lab_instance.id
  policy = data.aws_iam_policy_document.lab_instance_artifacts.json
}

resource "aws_iam_instance_profile" "lab" {
  name = "${local.name_prefix}-lab"
  role = aws_iam_role.lab_instance.name
}

data "aws_iam_policy_document" "lab_provisioner_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.controller.arn]
    }
  }
}

resource "aws_iam_role" "lab_provisioner" {
  name               = "${local.name_prefix}-lab-provisioner"
  description        = "Capability-limited role for ephemeral lab lifecycle operations"
  assume_role_policy = data.aws_iam_policy_document.lab_provisioner_assume_role.json
}

data "aws_iam_policy_document" "lab_provisioner" {
  statement {
    sid    = "ReadLabState"
    effect = "Allow"
    actions = [
      "ec2:DescribeInstances",
      "ec2:DescribeInstanceStatus",
      "ssm:DescribeInstanceInformation",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "ReadApprovedWindowsImage"
    effect    = "Allow"
    actions   = ["ssm:GetParameter"]
    resources = [data.aws_ssm_parameter.windows_server_2022_ami.arn]
  }

  statement {
    sid     = "UseApprovedLaunchResources"
    effect  = "Allow"
    actions = ["ec2:RunInstances"]
    resources = [
      "arn:${data.aws_partition.current.partition}:ec2:${var.region}::image/${data.aws_ssm_parameter.windows_server_2022_ami.value}",
      aws_subnet.lab.arn,
      aws_security_group.lab.arn,
      "arn:${data.aws_partition.current.partition}:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:network-interface/*",
    ]

  }

  statement {
    sid     = "CreateTaggedLabInstance"
    effect  = "Allow"
    actions = ["ec2:RunInstances"]
    resources = [
      "arn:${data.aws_partition.current.partition}:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:instance/*"
    ]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Project"
      values   = [var.project_name]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Environment"
      values   = [var.environment]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Role"
      values   = ["lab"]
    }

    condition {
      test     = "Null"
      variable = "aws:RequestTag/ProvisionKey"
      values   = ["false"]
    }

    condition {
      test     = "StringEquals"
      variable = "ec2:InstanceType"
      values   = [var.lab_instance_type]
    }

    condition {
      test     = "ArnEquals"
      variable = "ec2:InstanceProfile"
      values   = [aws_iam_instance_profile.lab.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "ec2:MetadataHttpTokens"
      values   = ["required"]
    }

    condition {
      test     = "NumericEquals"
      variable = "ec2:MetadataHttpPutResponseHopLimit"
      values   = [1]
    }

  }

  statement {
    sid     = "CreateTaggedLabVolume"
    effect  = "Allow"
    actions = ["ec2:RunInstances"]
    resources = [
      "arn:${data.aws_partition.current.partition}:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:volume/*"
    ]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Project"
      values   = [var.project_name]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Environment"
      values   = [var.environment]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Role"
      values   = ["lab"]
    }

    condition {
      test     = "Null"
      variable = "aws:RequestTag/ProvisionKey"
      values   = ["false"]
    }

    condition {
      test     = "NumericEquals"
      variable = "ec2:VolumeSize"
      values   = [var.lab_root_volume_size_gib]
    }

    condition {
      test     = "Bool"
      variable = "ec2:Encrypted"
      values   = ["true"]
    }

    condition {
      test     = "StringEquals"
      variable = "ec2:VolumeType"
      values   = ["gp3"]
    }
  }

  statement {
    sid     = "TagNewLabCompute"
    effect  = "Allow"
    actions = ["ec2:CreateTags"]
    resources = [
      "arn:${data.aws_partition.current.partition}:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:instance/*",
      "arn:${data.aws_partition.current.partition}:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:volume/*",
    ]

    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values   = ["RunInstances"]
    }
  }

  statement {
    sid       = "TerminateOwnedLabs"
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
      variable = "ec2:ResourceTag/Role"
      values   = ["lab"]
    }


    condition {
      test     = "Null"
      variable = "ec2:ResourceTag/ProvisionKey"
      values   = ["false"]
    }
  }

  statement {
    sid       = "PassOnlyLabInstanceRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.lab_instance.arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "lab_provisioner" {
  name   = "${local.name_prefix}-lab-provisioner"
  role   = aws_iam_role.lab_provisioner.id
  policy = data.aws_iam_policy_document.lab_provisioner.json
}

data "aws_iam_policy_document" "controller_assume_lab_provisioner" {
  statement {
    sid       = "AssumeLabProvisioner"
    effect    = "Allow"
    actions   = ["sts:AssumeRole"]
    resources = [aws_iam_role.lab_provisioner.arn]
  }
}

resource "aws_iam_role_policy" "controller_assume_lab_provisioner" {
  name   = "${local.name_prefix}-assume-lab-provisioner"
  role   = aws_iam_role.controller.id
  policy = data.aws_iam_policy_document.controller_assume_lab_provisioner.json
}
