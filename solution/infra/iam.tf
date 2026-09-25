data "aws_iam_policy_document" "ecs_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.prefix}-exec-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy" "execution" {
  name = "${local.prefix}-exec-policy"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = ["${aws_cloudwatch_log_group.api.arn}:*", "${aws_cloudwatch_log_group.snapshotter.arn}:*"]
    }]
  })
}

# API: reads and writes both ledgers and their indexes, reads snapshots for
# restore. It cannot write, overwrite or delete snapshot objects.
resource "aws_iam_role" "api" {
  name               = "${local.prefix}-api-task-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy" "api" {
  name = "${local.prefix}-api-policy"
  role = aws_iam_role.api.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Ledgers"
        Effect = "Allow"
        Action = [
          "dynamodb:DescribeTable", "dynamodb:GetItem", "dynamodb:PutItem",
          "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query",
        ]
        Resource = [
          aws_dynamodb_table.stock.arn,
          "${aws_dynamodb_table.stock.arn}/index/*",
          aws_dynamodb_table.reservations.arn,
        ]
      },
      {
        Sid      = "ListSnapshots"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [aws_s3_bucket.snapshots.arn]
      },
      {
        Sid      = "ReadSnapshots"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${aws_s3_bucket.snapshots.arn}/snapshots/*"]
      },
    ]
  })
}

# Snapshotter: reads the stock table and writes snapshot objects. It cannot
# modify either ledger.
resource "aws_iam_role" "snapshotter" {
  name               = "${local.prefix}-snapshotter-task-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy" "snapshotter" {
  name = "${local.prefix}-snapshotter-policy"
  role = aws_iam_role.snapshotter.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadStock"
        Effect   = "Allow"
        Action   = ["dynamodb:DescribeTable", "dynamodb:Scan"]
        Resource = [aws_dynamodb_table.stock.arn]
      },
      {
        Sid      = "WriteSnapshots"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = ["${aws_s3_bucket.snapshots.arn}/snapshots/*"]
      },
    ]
  })
}
