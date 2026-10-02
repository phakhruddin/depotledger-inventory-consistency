#!/usr/bin/env bash
# Removes only what this deployment owns: everything in Terraform state.
# Pre-existing resources that merely share the prefix are never touched,
# which is why nothing here deletes by name pattern.
set -Eeuo pipefail

CONFIG_PATH="/workspace/config/config.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="${SCRIPT_DIR}/infra"

log() { echo "[destroy] $*" >&2; }

REGION=$(jq -r '.region' "$CONFIG_PATH")
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
export AWS_DEFAULT_REGION="$REGION" AWS_REGION="$REGION" AWS_PAGER=""

if [ ! -f "${INFRA_DIR}/terraform.tfstate" ]; then
  log "no state present, nothing owned by this deployment"
  exit 0
fi

if [ ! -f "${INFRA_DIR}/config.auto.tfvars.json" ]; then
  jq '{
      region, aws_endpoint_url, resource_prefix, api_image, snapshotter_image,
      api_desired_count, snapshot_interval_seconds,
      idempotency_ttl_seconds, snapshot_noncurrent_retention_days
    }' "$CONFIG_PATH" > "${INFRA_DIR}/config.auto.tfvars.json"
fi

terraform -chdir="$INFRA_DIR" init -input=false >&2
log "terraform destroy"
if ! terraform -chdir="$INFRA_DIR" destroy -input=false -auto-approve >&2; then
  log "first destroy attempt failed, retrying once"
  sleep 10
  terraform -chdir="$INFRA_DIR" destroy -input=false -auto-approve >&2
fi
log "destroy complete"
