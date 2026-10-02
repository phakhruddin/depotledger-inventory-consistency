output "vpc_id" { value = aws_vpc.main.id }
output "public_subnet_ids" { value = aws_subnet.public[*].id }
output "private_subnet_ids" { value = aws_subnet.private[*].id }

output "edge_arn" { value = aws_lb.edge.arn }
output "edge_listener_arn" { value = aws_lb_listener.edge.arn }
output "edge_dns_name" { value = aws_lb.edge.dns_name }
output "api_target_group_arn" { value = aws_lb_target_group.api.arn }

output "cluster_arn" { value = aws_ecs_cluster.main.id }
output "api_service_arn" { value = aws_ecs_service.api.id }
output "snapshotter_service_arn" { value = aws_ecs_service.snapshotter.id }
output "api_desired_count" { value = aws_ecs_service.api.desired_count }

output "stock_table_name" { value = aws_dynamodb_table.stock.name }
output "stock_table_arn" { value = aws_dynamodb_table.stock.arn }
output "reservations_table_name" { value = aws_dynamodb_table.reservations.name }
output "reservations_table_arn" { value = aws_dynamodb_table.reservations.arn }
output "snapshot_bucket_name" { value = aws_s3_bucket.snapshots.bucket }
output "snapshot_bucket_arn" { value = aws_s3_bucket.snapshots.arn }

output "execution_role_arn" { value = aws_iam_role.execution.arn }
output "api_task_role_arn" { value = aws_iam_role.api.arn }
output "snapshotter_task_role_arn" { value = aws_iam_role.snapshotter.arn }

output "api_log_group" { value = aws_cloudwatch_log_group.api.name }
output "snapshotter_log_group" { value = aws_cloudwatch_log_group.snapshotter.name }
