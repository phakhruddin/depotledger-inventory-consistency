# Every value here is written by deploy.sh into infra/config.auto.tfvars.json
# before init/apply, so `terraform plan` run directly against infra/ resolves
# them without going through deploy.sh.

variable "region" { type = string }
variable "aws_endpoint_url" { type = string }
variable "resource_prefix" { type = string }
variable "api_image" { type = string }
variable "snapshotter_image" { type = string }
variable "api_desired_count" { type = number }
variable "snapshot_interval_seconds" { type = number }
variable "idempotency_ttl_seconds" { type = number }
variable "snapshot_noncurrent_retention_days" { type = number }
