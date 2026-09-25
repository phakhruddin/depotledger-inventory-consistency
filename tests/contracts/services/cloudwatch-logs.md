# CloudWatch Logs

Create one log group per service, each with a retention period, and send each
service's container output to it with the `awslogs` driver.

Neither image logs the admin token. Do not add configuration that would put it
in a log group.

## Manifest fields

Record in `manifest.logs`: `api` and `snapshotter`, each the log group name.
