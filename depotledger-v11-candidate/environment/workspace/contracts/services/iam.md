# IAM

Create three roles, all assumable by `ecs-tasks.amazonaws.com`:

| Role | Purpose | May | Must not |
|---|---|---|---|
| Execution role | Pull images, ship logs | Write to this deployment's log groups | Access either table or the bucket |
| API task role | The API's identity | Read and write both tables and the stock table's indexes (the API has no snapshot or restore surface, so it needs no bucket access) | Write, overwrite or delete bucket objects |
| Snapshotter task role | The snapshotter's identity | Describe and scan the stock table; put objects under `snapshots/` | Write, update or delete table items; touch the reservations table |

No policy may grant `Action: "*"` on `Resource: "*"`, and no API or
snapshotter policy may grant a service-wide wildcard action such as
`dynamodb:*` or `s3:*`.

IAM policies are recorded by this endpoint but not evaluated. They are
checked as declarations only.

## Manifest fields

Record in `manifest.roles`: `execution_role_arn`, `api_task_role_arn` and
`snapshotter_task_role_arn`.
