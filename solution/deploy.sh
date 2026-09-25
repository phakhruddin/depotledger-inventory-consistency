#!/usr/bin/env bash
# DepotLedger reference deployment.
#
# Idempotent: safe to rerun, repairs managed resources deleted since the last
# apply, restores the stock ledger from S3 when the table itself was lost, and
# finishes only once the API serves through the load balancer.
set -Eeuo pipefail

CONFIG_PATH="/workspace/config/config.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="${SCRIPT_DIR}/infra"
MANIFEST_PATH="${SCRIPT_DIR}/manifest.json"

log() { echo "[deploy] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# ---- configuration (always from the fixed absolute path) --------------------
cfg() { jq -r "$1" "$CONFIG_PATH"; }
RESOURCE_PREFIX=$(cfg '.resource_prefix')
REGION=$(cfg '.region')
AWS_ENDPOINT_URL=$(cfg '.aws_endpoint_url')
ADMIN_TOKEN=$(cfg '.admin_token')
ENDPOINT_HOST=$(printf '%s' "$AWS_ENDPOINT_URL" | sed -E 's#^[a-zA-Z]+://##; s#[:/].*$##')

export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
export AWS_DEFAULT_REGION="$REGION" AWS_REGION="$REGION" AWS_PAGER=""
# The endpoint host is not a wildcard DNS name, so S3 must be path-style.
AWS_CONFIG_FILE="$(mktemp)"
printf '[default]\ns3 =\n    addressing_style = path\n' > "$AWS_CONFIG_FILE"
export AWS_CONFIG_FILE
AWSCLI=(aws --endpoint-url "$AWS_ENDPOINT_URL" --region "$REGION")

log "prefix=$RESOURCE_PREFIX region=$REGION endpoint=$AWS_ENDPOINT_URL"

# ---- persist dynamic inputs to an auto-loaded var file -----------------------
# A standalone `terraform plan` against infra/ must resolve every variable
# without going through this script.
jq '{
    region, aws_endpoint_url, resource_prefix, api_image, snapshotter_image,
    admin_token, api_desired_count, snapshot_interval_seconds,
    idempotency_ttl_seconds, snapshot_noncurrent_retention_days
  }' "$CONFIG_PATH" > "${INFRA_DIR}/config.auto.tfvars.json"

# ---- apply ------------------------------------------------------------------
log "terraform init"
terraform -chdir="$INFRA_DIR" init -input=false >&2
log "terraform apply"
terraform -chdir="$INFRA_DIR" apply -input=false -auto-approve >&2

tf_out() { terraform -chdir="$INFRA_DIR" output -raw "$1"; }
tf_json() { terraform -chdir="$INFRA_DIR" output -json "$1"; }

CONNECT_URL="http://${ENDPOINT_HOST}:80"
EDGE_HOST="$(tf_out edge_dns_name)"
TARGET_GROUP="$(tf_out api_target_group_arn)"
DESIRED="$(tf_out api_desired_count)"
BUCKET="$(tf_out snapshot_bucket_name)"

# ---- manifest -----------------------------------------------------------------
jq -n \
  --arg deployment "$RESOURCE_PREFIX" \
  --arg vpc_id "$(tf_out vpc_id)" \
  --argjson public_subnet_ids "$(tf_json public_subnet_ids)" \
  --argjson private_subnet_ids "$(tf_json private_subnet_ids)" \
  --arg lb_arn "$(tf_out edge_arn)" \
  --arg listener_arn "$(tf_out edge_listener_arn)" \
  --arg dns_name "$EDGE_HOST" \
  --arg connect_url "$CONNECT_URL" \
  --arg target_group_arn "$TARGET_GROUP" \
  --arg cluster_arn "$(tf_out cluster_arn)" \
  --arg api_service "$(tf_out api_service_arn)" \
  --arg snapshotter_service "$(tf_out snapshotter_service_arn)" \
  --argjson api_count "$DESIRED" \
  --arg stock_name "$(tf_out stock_table_name)" \
  --arg stock_arn "$(tf_out stock_table_arn)" \
  --arg res_name "$(tf_out reservations_table_name)" \
  --arg res_arn "$(tf_out reservations_table_arn)" \
  --arg bucket "$BUCKET" \
  --arg bucket_arn "$(tf_out snapshot_bucket_arn)" \
  --arg exec_role "$(tf_out execution_role_arn)" \
  --arg api_role "$(tf_out api_task_role_arn)" \
  --arg snap_role "$(tf_out snapshotter_task_role_arn)" \
  --arg api_logs "$(tf_out api_log_group)" \
  --arg snap_logs "$(tf_out snapshotter_log_group)" \
  '{
    deployment: $deployment,
    network: {vpc_id: $vpc_id, public_subnet_ids: $public_subnet_ids, private_subnet_ids: $private_subnet_ids},
    edge: {load_balancer_arn: $lb_arn, listener_arn: $listener_arn, dns_name: $dns_name,
           connect_url: $connect_url, host_header: $dns_name, target_group_arn: $target_group_arn},
    compute: {cluster_arn: $cluster_arn,
              services: {api: $api_service, snapshotter: $snapshotter_service},
              desired_counts: {api: $api_count, snapshotter: 1}},
    data: {stock_table: {name: $stock_name, arn: $stock_arn},
           reservations_table: {name: $res_name, arn: $res_arn},
           snapshot_bucket: {name: $bucket, arn: $bucket_arn}},
    roles: {execution_role_arn: $exec_role, api_task_role_arn: $api_role,
            snapshotter_task_role_arn: $snap_role},
    logs: {api: $api_logs, snapshotter: $snap_logs}
  }' > "$MANIFEST_PATH"
log "manifest written to $MANIFEST_PATH"

# ---- readiness ----------------------------------------------------------------
edge() { curl -s -m 10 -H "Host: ${EDGE_HOST}" "$@"; }

deadline=$(( $(date +%s) + 300 ))
while true; do
  healthy=$("${AWSCLI[@]}" elbv2 describe-target-health --target-group-arn "$TARGET_GROUP" \
    --query "length(TargetHealthDescriptions[?TargetHealth.State=='healthy'])" --output text 2>/dev/null || echo 0)
  [ "$healthy" = "None" ] && healthy=0
  code=$(edge -o /dev/null -w '%{http_code}' "${CONNECT_URL}/health/ready" 2>/dev/null || echo 000)
  if [ "${healthy:-0}" -ge "$DESIRED" ] && [ "$code" = "200" ]; then
    log "api ready: $healthy healthy target(s)"
    break
  fi
  [ "$(date +%s)" -ge "$deadline" ] && die "api not ready (healthy=$healthy, ready=$code)"
  sleep 5
done

# ---- restore after table loss -------------------------------------------------
# A table that was deleted and recreated is a new generation. Each generation
# is restored at most once, from the newest snapshot of an earlier
# generation, and a marker object records that it happened. Routine redeploys
# find the marker and never restore, so rows deleted on purpose stay deleted.
admin() { edge -H "X-Admin-Token: ${ADMIN_TOKEN}" "$@"; }

listing=""
deadline=$(( $(date +%s) + 120 ))
while true; do
  listing=$(admin "${CONNECT_URL}/admin/snapshots" 2>/dev/null || true)
  if printf '%s' "$listing" | jq -e '.current_generation != null' >/dev/null 2>&1; then
    break
  fi
  [ "$(date +%s)" -ge "$deadline" ] && die "snapshot listing unavailable: ${listing:0:300}"
  sleep 3
done
generation=$(printf '%s' "$listing" | jq -r '.current_generation')
marker="restores/${generation}.json"

if "${AWSCLI[@]}" s3api head-object --bucket "$BUCKET" --key "$marker" >/dev/null 2>&1; then
  log "generation $generation already reconciled"
else
  source_key=$(printf '%s' "$listing" | jq -r --arg g "$generation" \
    '[.snapshots[] | select(.generation != $g)] | first | .key // empty')
  result='{"restored":0}'
  if [ -n "$source_key" ]; then
    log "stock table generation $generation is new; restoring from $source_key"
    result=$(admin -X POST -H 'Content-Type: application/json' \
      -d "$(jq -cn --arg k "$source_key" '{snapshot_key: $k}')" \
      -w '\n%{http_code}' "${CONNECT_URL}/admin/restore")
    status=$(printf '%s' "$result" | tail -n1)
    result=$(printf '%s' "$result" | sed '$d')
    [ "$status" = "200" ] || die "restore failed ($status): $result"
    log "restore result: $result"
  else
    log "no earlier generation to restore from"
  fi
  printf '%s' "$result" | jq -c --arg g "$generation" --arg k "$source_key" \
    '{generation: $g, restored_from: (if $k == "" then null else $k end), result: .}' > /tmp/depotledger-marker.json
  "${AWSCLI[@]}" s3api put-object --bucket "$BUCKET" --key "$marker" \
    --body /tmp/depotledger-marker.json --content-type application/json >/dev/null
fi

# ---- the snapshotter has covered the current generation -----------------------
deadline=$(( $(date +%s) + 120 ))
while true; do
  latest=$("${AWSCLI[@]}" s3api get-object --bucket "$BUCKET" --key snapshots/LATEST.json \
    /tmp/depotledger-latest.json >/dev/null 2>&1 && jq -r '.generation' /tmp/depotledger-latest.json || echo "")
  if [ "$latest" = "$generation" ]; then
    log "snapshotter covering generation $generation"
    break
  fi
  [ "$(date +%s)" -ge "$deadline" ] && die "snapshotter has not written a snapshot for generation $generation"
  sleep 3
done

log "deploy complete"
