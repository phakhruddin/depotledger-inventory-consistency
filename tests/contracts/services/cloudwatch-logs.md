# CloudWatch Logs

Create one log group per service, each with a retention period, and send each
service's container output to it with the `awslogs` driver.

## Manifest fields

Record in `manifest.logs`: `api` and `snapshotter`, each the log group name.
