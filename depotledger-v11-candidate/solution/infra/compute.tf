resource "aws_ecs_cluster" "main" {
  name = "${local.prefix}-cluster"
  tags = local.tags
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.prefix}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.api.arn

  container_definitions = jsonencode([{
    name         = "api"
    image        = var.api_image
    essential    = true
    portMappings = [{ containerPort = 8080, hostPort = 8080, protocol = "tcp" }]
    environment = concat(local.aws_environment, [
      { name = "STOCK_TABLE", value = aws_dynamodb_table.stock.name },
      { name = "RESERVATIONS_TABLE", value = aws_dynamodb_table.reservations.name },
      { name = "IDEMPOTENCY_TTL_SECONDS", value = tostring(var.idempotency_ttl_seconds) },
    ])
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.api.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "api"
      }
    }
  }])

  tags = local.tags

  # This endpoint does not return container definitions in the shape they were
  # registered; see the normalization table in contracts/runtime.md.
  lifecycle {
    ignore_changes = [container_definitions]
  }
}

resource "aws_ecs_task_definition" "snapshotter" {
  family                   = "${local.prefix}-snapshotter"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.snapshotter.arn

  container_definitions = jsonencode([{
    name      = "snapshotter"
    image     = var.snapshotter_image
    essential = true
    environment = concat(local.aws_environment, [
      { name = "STOCK_TABLE", value = aws_dynamodb_table.stock.name },
      { name = "SNAPSHOT_BUCKET", value = aws_s3_bucket.snapshots.bucket },
      { name = "SNAPSHOT_INTERVAL_SECONDS", value = tostring(var.snapshot_interval_seconds) },
    ])
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.snapshotter.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "snapshotter"
      }
    }
  }])

  tags = local.tags

  lifecycle {
    ignore_changes = [container_definitions]
  }
}

resource "aws_ecs_service" "api" {
  name            = "${local.prefix}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8080
  }

  depends_on          = [aws_lb_listener.edge, aws_iam_role_policy.api]
  scheduling_strategy = "REPLICA"
  tags                = local.tags

  lifecycle {
    ignore_changes = [scheduling_strategy]
  }
}

resource "aws_ecs_service" "snapshotter" {
  name            = "${local.prefix}-snapshotter"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.snapshotter.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.snapshotter.id]
    assign_public_ip = false
  }

  depends_on = [
    aws_iam_role_policy.snapshotter,
    aws_s3_bucket_versioning.snapshots,
  ]
  scheduling_strategy = "REPLICA"
  tags                = local.tags

  lifecycle {
    ignore_changes = [scheduling_strategy]
  }
}
