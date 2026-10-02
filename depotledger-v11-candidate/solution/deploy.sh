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
    api_desired_count, snapshot_interval_seconds,
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
# Deployment-owned recovery, straight against S3 and DynamoDB (the API has no
# restore surface).
#
# * Generation: the stock table's CreationDateTime in epoch milliseconds.
# * Source: the highest generation older than the current one; within it the
#   newest committed snapshot; its committed content is the object VERSION
#   whose sha256 equals the commit marker, even if the object was overwritten.
# * Load: every row with PutItem conditioned on attribute_not_exists, so a row
#   that already exists (written since the table came back, or restored by an
#   earlier, interrupted run) is never overwritten. Resuming is therefore safe.
# * Bookkeeping: restores/<generation>.json is written only AFTER every row is
#   in place. A run killed mid-restore leaves no marker, so the next run
#   finishes the job; a routine redeploy finds the marker and restores nothing.
STOCK_TABLE="$(tf_out stock_table_name)"
python3 - "$BUCKET" "$STOCK_TABLE" "$AWS_ENDPOINT_URL" "$REGION" > /tmp/dl-generation <<'PY'
import hashlib, json, re, sys
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config

bucket, table, endpoint, region = sys.argv[1:5]
s3 = boto3.client("s3", endpoint_url=endpoint, region_name=region, config=Config(s3={"addressing_style": "path"}))
ddb = boto3.client("dynamodb", endpoint_url=endpoint, region_name=region,
                   config=Config(max_pool_connections=32, retries={"max_attempts": 10}))

def log(msg):
    print(f"[restore] {msg}", file=sys.stderr, flush=True)

created = ddb.describe_table(TableName=table)["Table"]["CreationDateTime"]
generation = str(int(round(created.timestamp() * 1000)))
print(generation)
marker = f"restores/{generation}.json"
try:
    s3.head_object(Bucket=bucket, Key=marker)
    log(f"generation {generation} already reconciled")
    sys.exit(0)
except s3.exceptions.ClientError as exc:
    if exc.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey", "NotFound"):
        raise

keys = []
for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="snapshots/"):
    keys += [o["Key"] for o in page.get("Contents", [])]
pattern = re.compile(r"^snapshots/(\d+)/(\d+)\.(jsonl|committed)$")
parsed = [(int(m.group(1)), int(m.group(2)), m.group(3)) for m in map(pattern.match, keys) if m]
older = [g for g, _, _ in parsed if g < int(generation)]

def finish(record):
    s3.put_object(Bucket=bucket, Key=marker, Body=json.dumps(record).encode(), ContentType="application/json")

if not older:
    log("no earlier generation; nothing to restore")
    finish({"generation": generation, "restored_from": None})
    sys.exit(0)
prev = max(older)

source = None
for ts in sorted({t for g, t, k in parsed if g == prev and k == "committed"}, reverse=True):
    data_key = f"snapshots/{prev}/{ts}.jsonl"
    sha = json.loads(s3.get_object(Bucket=bucket, Key=f"snapshots/{prev}/{ts}.committed")["Body"].read()).get("sha256")
    versions = []
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=data_key):
        versions += [v["VersionId"] for v in page.get("Versions", []) if v["Key"] == data_key]
    for vid in versions:
        body = s3.get_object(Bucket=bucket, Key=data_key, VersionId=vid)["Body"].read()
        if sha and hashlib.sha256(body).hexdigest() == sha:
            source = (data_key, vid, body)
            break
    if source:
        break
    log(f"skipping {data_key}: no version matches its commit marker")
if source is None:
    sys.exit(f"no committed snapshot found in generation {prev}")

def attr(value):
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    if value is None:
        return {"NULL": True}
    return {"S": str(value)}

rows = [json.loads(line) for line in source[2].decode().splitlines() if line.strip()]

def put(row):
    try:
        ddb.put_item(TableName=table, Item={k: attr(v) for k, v in row.items()},
                     ConditionExpression="attribute_not_exists(sku)")
        return 1
    except ddb.exceptions.ConditionalCheckFailedException:
        return 0

log(f"restoring {len(rows)} rows from {source[0]} version {source[1]}")
with ThreadPoolExecutor(max_workers=16) as pool:
    restored = sum(pool.map(put, rows))
finish({"generation": generation, "restored_from": source[0], "version_id": source[1],
        "rows": len(rows), "restored": restored, "skipped": len(rows) - restored})
log(f"restored {restored}, kept {len(rows) - restored} existing")
PY
generation=$(head -n1 /tmp/dl-generation)
[ -n "$generation" ] || die "could not determine the stock table generation"

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
