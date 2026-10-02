resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${local.prefix}-api"
  retention_in_days = 14
  tags              = local.tags
}

resource "aws_cloudwatch_log_group" "snapshotter" {
  name              = "/ecs/${local.prefix}-snapshotter"
  retention_in_days = 14
  tags              = local.tags
}
