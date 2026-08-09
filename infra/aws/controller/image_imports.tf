data "aws_iam_policy_document" "vmimport_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["vmie.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "sts:Externalid"
      values   = ["vmimport"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${data.aws_partition.current.partition}:vmie:*:${data.aws_caller_identity.current.account_id}:*"]
    }
  }
}

resource "aws_iam_role" "vmimport" {
  name               = "${local.name_prefix}-vmimport"
  description        = "Service role used only for Moodle Autotask VM image imports"
  assume_role_policy = data.aws_iam_policy_document.vmimport_assume_role.json
}

data "aws_iam_policy_document" "vmimport" {
  statement {
    sid       = "ReadApprovedImportObjects"
    effect    = "Allow"
    actions   = ["s3:GetBucketLocation", "s3:ListBucket"]
    resources = [aws_s3_bucket.artifacts.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["assignments/*"]
    }
  }

  statement {
    sid       = "ReadApprovedImportObjectBytes"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/assignments/*"]
  }

  statement {
    sid    = "ConvertApprovedImages"
    effect = "Allow"
    actions = [
      "ec2:CopySnapshot",
      "ec2:Describe*",
      "ec2:ModifySnapshotAttribute",
      "ec2:RegisterImage",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "vmimport" {
  name   = "${local.name_prefix}-vmimport"
  role   = aws_iam_role.vmimport.id
  policy = data.aws_iam_policy_document.vmimport.json
}

data "aws_iam_policy_document" "image_importer_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.controller.arn]
    }
  }
}

resource "aws_iam_role" "image_importer" {
  name               = "${local.name_prefix}-image-importer"
  description        = "Capability-limited role for approved Moodle OVA imports"
  assume_role_policy = data.aws_iam_policy_document.image_importer_assume_role.json
}

data "aws_iam_policy_document" "image_importer" {
  statement {
    sid    = "ManageApprovedImageImports"
    effect = "Allow"
    actions = [
      "ec2:CancelImportTask",
      "ec2:CreateTags",
      "ec2:DeleteSnapshot",
      "ec2:DeregisterImage",
      "ec2:DescribeImages",
      "ec2:DescribeImportImageTasks",
      "ec2:DescribeSnapshots",
      "ec2:ImportImage",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "PassOnlyVmImportRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.vmimport.arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["vmie.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "image_importer" {
  name   = "${local.name_prefix}-image-importer"
  role   = aws_iam_role.image_importer.id
  policy = data.aws_iam_policy_document.image_importer.json
}

data "aws_iam_policy_document" "controller_assume_image_importer" {
  statement {
    sid       = "AssumeImageImporter"
    effect    = "Allow"
    actions   = ["sts:AssumeRole"]
    resources = [aws_iam_role.image_importer.arn]
  }
}

resource "aws_iam_role_policy" "controller_assume_image_importer" {
  name   = "${local.name_prefix}-assume-image-importer"
  role   = aws_iam_role.controller.id
  policy = data.aws_iam_policy_document.controller_assume_image_importer.json
}
