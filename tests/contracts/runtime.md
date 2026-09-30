# Runtime

Two images are supplied and already built into the local Docker daemon used by
the cloud endpoint. Do not rebuild, replace or modify them. Their references
and image IDs are in `/workspace/config/config.json`.

Both images talk to DynamoDB and S3 through `AWS_ENDPOINT_URL` and sign every
request with the static credentials this endpoint accepts.

## Shared environment

Both images require these variables:

| Variable | Required value |
|---|---|
| `AWS_ENDPOINT_URL` | `aws_endpoint_url` from `config.json`. Tasks resolve that host. |
| `AWS_REGION` | `region` from `config.json`. |
| `AWS_ACCESS_KEY_ID` | `test` |
| `AWS_SECRET_ACCESS_KEY` | `test` |
| `STOCK_TABLE` | Name of the stock table (see `data-model.md`). |
| `SNAPSHOT_BUCKET` | Name of the snapshot bucket (see `services/s3.md`). |

An image that is missing a required variable exits at start and logs
`config_missing`.

## `api_image`: inventory API

Runs the HTTP API described in `openapi.yaml`. Listens on `8080`.

| Variable | Required value |
|---|---|
| `RESERVATIONS_TABLE` | Name of the reservations table. |
| `ADMIN_TOKEN` | `admin_token` from `config.json`. Guards `/admin/*`. |
| `IDEMPOTENCY_TTL_SECONDS` | `idempotency_ttl_seconds` from `config.json`. |
| `PORT` | Optional. Defaults to `8080`. |

Behavior that depends on the infrastructure:

- `GET /health/ready` answers `200` only when both tables exist and are
  `ACTIVE`. Otherwise it answers `503`.
- `GET /warehouses/{warehouse_id}/stock` queries the `by_warehouse` index and
  `GET /warehouses/{warehouse_id}/low-stock` queries the `low_stock` index. The
  API answers from the index alone and never reads the base table for them. If
  an index is missing or its keys do not match `data-model.md`, the call
  answers `500` with code `index_unavailable`. If returned rows lack a
  required projected attribute, the call answers `500` with code
  `index_projection_incomplete`.
- `POST /reservations` claims the `order_id` in the reservations table, then
  takes units from the stock row with a conditional update, so concurrent
  replicas can never drive `available` below zero. A replay of the same
  `order_id` with the same body returns the original reservation with `200`
  and takes no more units. Each claim carries `expires_at`, an epoch-seconds
  number equal to creation time plus `IDEMPOTENCY_TTL_SECONDS`.
- Every stock write maintains `low_stock_warehouse`. The attribute is set to the
  row's `warehouse_id` while `available <= reorder_point` and removed
  otherwise. That is what makes the `low_stock` index sparse.

### Admin surface

Every `/admin/*` call must carry `X-Admin-Token: <admin_token>`. Otherwise it
answers `401`.

- `GET /admin/snapshots` returns the stock table's `current_generation` and
  every snapshot **data object** in the bucket, newest first, each with its
  `key`, `generation`, `taken_at_ms` and `item_count`. It does not say whether
  a snapshot is committed; the commit markers in the bucket are the only
  record of that.
- `POST /admin/restore` with `{"snapshot_key": "<key>"}` loads the current
  version of that snapshot object into the stock table. With
  `{"snapshot_key": "<key>", "version_id": "<id>"}` it loads exactly that object
  version. It checks neither commit markers nor hashes: choosing the committed
  content is the caller's job. It never overwrites a row that already exists: any row
  present is treated as newer than the snapshot. It answers `200` with
  `restored` and `skipped` counts. It refuses with `409` and code
  `snapshot_generation_current` when the snapshot was taken from the current
  table generation, because restoring a table into itself can only resurrect
  rows that were deleted on purpose.

## `snapshotter_image`: snapshot writer

Runs as one long-lived task with no listener.

| Variable | Required value |
|---|---|
| `SNAPSHOT_INTERVAL_SECONDS` | `snapshot_interval_seconds` from `config.json`. |

Behavior:

- Every second it describes the stock table. The table's **generation** is its
  creation time in epoch milliseconds. A table that is deleted and created
  again under the same name is a new generation.
- The first time it sees a generation, it writes a **baseline snapshot** right
  away, even when the table is empty. After that it writes one snapshot every
  `SNAPSHOT_INTERVAL_SECONDS`.
- A snapshot is a consistent scan of the whole stock table, written as JSON
  Lines, one stock row per line with every attribute, to
  `snapshots/<generation>/<taken_at_ms>.jsonl`. Object metadata
  `item-count`, `table-generation` and `taken-at-ms` describe it.
- **Every snapshot is written in two steps.** First the data object, then a
  commit marker `snapshots/<generation>/<taken_at_ms>.committed` containing
  `{"key", "generation", "item_count", "taken_at_ms", "sha256"}`, where
  `sha256` is the hex SHA-256 of the committed data object's bytes.
- **What counts as committed.** A snapshot is committed when its marker exists
  and **some version** of its data object has exactly the marker's `sha256`.
  That object version is the snapshot's **committed content**. A data object
  can be overwritten after it was committed, for example by a retried or
  misconfigured writer. The versioned bucket still holds the committed bytes
  as an older version, and the current version is then *not* the committed
  content. A data object with no marker is what a snapshotter that died
  mid-write leaves behind. It may be incomplete or stale, and it must never be
  restored.
- After each committed snapshot it overwrites `snapshots/LATEST.json` with
  the same fields as the marker. The bucket's version history is the only
  record of earlier pointers.
- While the stock table does not exist, it logs `table_missing` and writes
  nothing.

Because of the baseline rule, within about a second of a lost table being
recreated, `LATEST.json` points at an empty snapshot of the new generation.
The snapshot you want after a loss is the newest one from the **previous**
generation.

## Restoring after table loss

If the stock table is deleted, the next `deploy.sh` must recreate it and, before
it returns, restore it from **the committed content of the newest committed
snapshot of the generation that was live immediately before this loss**,
meaning the table that was just deleted. Generations are creation times, so
that is the most recent generation older than the new table. "Newest" is by
`taken_at_ms` within that generation. If the newest committed snapshot's data
object has been overwritten since, restore its committed version, not the
current one, and do not fall back to an older snapshot.

The verifier may delete the table at any point after a successful deploy and
then run `deploy.sh`, and it may do so **more than once**, writing, updating
and deleting rows between losses. After each loss it expects every row of the
lost generation's newest committed snapshot to be served with that snapshot's
values when `deploy.sh` returns. Rows deleted or updated before the loss keep
their pre-loss state. The lost generation may also contain a newer
**uncommitted** snapshot object, and none of its rows may appear. Its newest
committed snapshot may also have an overwritten data object, and no row that
exists only in the overwriting version may appear.

A routine redeploy, where the table was not lost, must not restore anything.
Rows deleted through the API stay deleted, and rows written after the last
snapshot are kept.

How `deploy.sh` decides that a restore is due, and how it records that one was
done, is your design. The admin endpoints above are sufficient. Any state you
keep must survive between runs of `deploy.sh` in the same submission
directory.

## Logging

Both images write one JSON object per line to stdout. Requests carry
`request_id`. The admin token is never logged. Do not add configuration that
echoes it into a log group.

## Emulator behavior that affects deployment

- **Resource refreshes may report non-material drift.** The endpoint does not
  return every field AWS returns, so a refresh can show differences that do not
  reflect a real change. Use `terraform plan -refresh=false` when checking
  whether configuration and state agree. Do not rely on `-refresh=false` when
  *applying*: a deployment has to see real drift on its durable controls (see
  `services/s3.md`) to repair it.

- **Some attributes cannot be read back in the shape they were written.** The
  difference is recorded in state as soon as `apply` finishes, so it survives
  `-refresh=false`, and every later plan wants to **replace** the resource.

  | Resource | Attribute | What happens |
  |---|---|---|
  | `aws_ecs_task_definition` | `container_definitions` | Returned in a different shape than registered. |
  | `aws_ecs_service` | `scheduling_strategy` | Not echoed back. |

  Declare both attributes normally. Then add a **narrow**
  `lifecycle { ignore_changes = [...] }` for each so redeployment stays
  stable. Keep the ignore list to attributes like these: ignoring a table's key
  schema, indexes or TTL would hide real configuration and is a defect.
- **Indexes are active immediately.** A new global secondary index reports
  `ACTIVE` at once. Queries against it read the base table with the index
  projection applied, exactly as a backfilled index would answer.
- **DynamoDB TTL is enforced.** Expired items are removed.
- **ECS creates its own log groups.** Besides the `awslogs-group` you
  configure, this endpoint writes every task's output to a log group it
  creates itself, named `/ecs/<task definition family>`. Those groups belong
  to the endpoint, not to your deployment, and destruction is not judged on
  them. A log group you declare yourself must still be removed by
  `destroy.sh`.
- **Bucket default encryption is recorded, not simulated.** Declare it anyway;
  it is checked as configuration.
- **Security groups and IAM policies are recorded, not enforced.** They are
  checked as declarations only.
- **Standalone security group rules may produce an invalid replacement plan.**
  Define ingress and egress as inline blocks on the security group resource.
