resource "aws_lb" "edge" {
  name               = substr("${local.prefix}-edge", 0, 32)
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.edge.id]
  subnets            = aws_subnet.public[*].id
  tags               = merge(local.tags, { Name = "${local.prefix}-edge" })
}

resource "aws_lb_target_group" "api" {
  name        = substr("${local.prefix}-api", 0, 32)
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id

  health_check {
    path     = "/health/ready"
    protocol = "HTTP"
    port     = "traffic-port"
  }

  tags = merge(local.tags, { Name = "${local.prefix}-api" })
}

resource "aws_lb_listener" "edge" {
  load_balancer_arn = aws_lb.edge.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}
