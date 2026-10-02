# ECS Services

Create one ECS cluster and two services.

| Service | Image | Desired count | Load balancer |
|---|---|---:|---|
| API | `api_image` | `api_desired_count` from `config.json` | API target group, container port `8080` |
| Snapshotter | `snapshotter_image` | exactly `1` | none |

Run exactly one snapshotter. Both services use awsvpc networking in the
private subnets with public IP assignment disabled. Each service uses its own
task role (see `iam.md`).

Task definitions must select the supplied images by the references in
`config.json`. The environment variables each image requires are listed in
[`../runtime.md`](../runtime.md).

A deployment is ready when the API target group reports `api_desired_count`
healthy targets, `GET /health/ready` answers `200` through the load balancer,
any restore that is due has completed, and `snapshots/LATEST.json` points at
the current stock table generation.

## Manifest fields

Record in `manifest.compute`:

| Field | Required value |
|---|---|
| `cluster_arn` | ECS cluster ARN. |
| `services` | Object mapping `api` and `snapshotter` to that service's ARN. |
| `desired_counts` | Object mapping the same keys to desired count. |
