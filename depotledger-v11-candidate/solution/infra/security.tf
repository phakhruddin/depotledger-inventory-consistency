# Inline ingress/egress blocks: standalone aws_vpc_security_group_*_rule
# resources can produce an invalid replacement plan against this endpoint.

resource "aws_security_group" "edge" {
  name        = "${local.prefix}-edge-sg"
  description = "Public load balancer"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "Client traffic"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "Forward to API tasks"
    from_port   = 8080
    to_port     = 8080
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  tags = merge(local.tags, { Name = "${local.prefix}-edge-sg" })
}

resource "aws_security_group" "api" {
  name        = "${local.prefix}-api-sg"
  description = "API tasks"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "From the load balancer only"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.edge.id]
  }

  egress {
    description = "Cloud endpoint"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.tags, { Name = "${local.prefix}-api-sg" })
}

# The snapshotter accepts no inbound traffic at all.
resource "aws_security_group" "snapshotter" {
  name        = "${local.prefix}-snapshotter-sg"
  description = "Snapshotter task, egress only"
  vpc_id      = aws_vpc.main.id

  egress {
    description = "Cloud endpoint"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.tags, { Name = "${local.prefix}-snapshotter-sg" })
}
