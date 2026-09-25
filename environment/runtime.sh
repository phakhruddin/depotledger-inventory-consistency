#!/bin/sh
# Builds the supplied images and writes the per-run configuration. Every value
# that a submission might be tempted to hard code is randomized here.
set -eu

APPLICATION_DIR="${APPLICATION_DIR:-/application}"
CONFIG_DIR="${CONFIG_DIR:-/config}"

mkdir -p "$CONFIG_DIR"
APPLICATION_DIR="$APPLICATION_DIR" /bin/sh "$APPLICATION_DIR/build.sh"

image_id() {
  docker image inspect --format '{{.Id}}' "$1"
}

rand() {
  od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'
}

resource_prefix="dl-$(rand 6)"
retention_days=$(( 0x$(rand 1) % 24 + 7 ))
ttl_seconds=$(( (0x$(rand 1) % 20 + 4) * 3600 ))
config_tmp="$CONFIG_DIR/config.json.tmp"

cat >"$config_tmp" <<JSON
{
  "resource_prefix": "$resource_prefix",
  "region": "us-east-1",
  "aws_endpoint_url": "http://aws:4566",
  "api_image": "depotledger/api:1.0.0",
  "snapshotter_image": "depotledger/snapshotter:1.0.0",
  "api_image_id": "$(image_id depotledger/api:1.0.0)",
  "snapshotter_image_id": "$(image_id depotledger/snapshotter:1.0.0)",
  "admin_token": "dladm_$(rand 16)",
  "api_desired_count": 2,
  "snapshot_interval_seconds": 15,
  "idempotency_ttl_seconds": $ttl_seconds,
  "snapshot_noncurrent_retention_days": $retention_days
}
JSON

chmod 0444 "$config_tmp"
mv "$config_tmp" "$CONFIG_DIR/config.json"
echo "DepotLedger application images and configuration are ready."
