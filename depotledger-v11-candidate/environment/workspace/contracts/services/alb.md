# Application Load Balancer

Create one internet-facing application load balancer in the public subnets,
with an HTTP listener on port `80` that forwards to one IP target group for the
API tasks on port `8080`. Use `/health/ready` as the health check path.

## Reaching the load balancer

The listener socket is served by the cloud endpoint host. From the workspace
and from the verifier, the load balancer is reached at
`http://<host of aws_endpoint_url>:80` with the `Host` header set to the load
balancer's generated DNS name. That DNS name is not itself resolvable.

## Manifest fields

Record in `manifest.edge`:

| Field | Required value |
|---|---|
| `load_balancer_arn` | Load balancer ARN. |
| `listener_arn` | Port 80 listener ARN. |
| `dns_name` | Generated DNS name. |
| `connect_url` | `http://<host of aws_endpoint_url>:80` |
| `host_header` | The `Host` value that selects this load balancer, its DNS name. |
| `target_group_arn` | API target group ARN. |
