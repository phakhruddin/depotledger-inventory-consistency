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

An image that is missing a required variable exits at start and logs
`config_missing`.

## `api_image`: inventory API

Runs the HTTP API described in `openapi.yaml`. Listens on `8080`.

| Variable | Required value |
|---|---|
| `RESERVATIONS_TABLE` | Name of the reservations table. |
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

### No snapshot or restore surface

The API has no admin, snapshot-listing or restore endpoints, and it never
reads the snapshot bucket. Recovering a lost stock table is the deployment's
job (see *Restoring after table loss*), done directly against S3 and DynamoDB.

## `snapshotter_image`: snapshot writer

Runs as one long-lived task with no listener.

| Variable | Required value |
|---|---|
| `SNAPSHOT_BUCKET` | Name of the snapshot bucket (see `services/s3.md`). |
| `SNAPSHOT_INTERVAL_SECONDS` | `snapshot_interval_seconds` from `config.json`. |

Behavior:

- Every second it describes the stock table. The table's **generation** is its
  creation time in epoch milliseconds: `DescribeTable`'s `CreationDateTime`
  (seconds) multiplied by 1000 and rounded to the nearest integer, written in
  decimal. A table that is deleted and created
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

Nothing in the supplied images does this for you. `deploy.sh` (or a program
it runs and waits for) reads the bucket and writes the stock table itself,
using the S3 and DynamoDB operations described in `services/s3.md` and
`services/dynamodb.md`:

- **Finding the source.** List `snapshots/` to find generations, data objects
  and commit markers. Read a marker's `sha256`, list the data object's
  versions, and load the version whose bytes hash to it.
- **Writing rows.** Each JSON Lines record is one stock row. Write every
  attribute it carries, with JSON strings as `S`, JSON numbers as `N` and JSON
  booleans as `BOOL`. A number stored as a string is a defect: the API and the
  indexes read these attributes as numbers.
- **Never overwrite.** A restore must not overwrite a row that already exists
  in the replacement table. Any row present is newer than the snapshot: it was
  written since the table came back, or by an earlier, interrupted restore and
  then updated. Use a write that is conditional on the item not existing, such
  as `PutItem` with `ConditionExpression: attribute_not_exists(sku)`.
  `BatchWriteItem` cannot carry a condition, so it overwrites blindly.
- **Volume.** A snapshot can hold several thousand rows. The whole of
  `deploy.sh`, restore included, has the same 720-second budget as every run.

### Interruption and retry

A restore can be cut short. The verifier may kill `deploy.sh` with `SIGKILL`,
together with every process in its process group, **as soon as any row is
visible in the replacement table**, that is, as soon as the first restored row
lands. It may then write to the
replacement table, updating rows that were already restored and adding new
ones, and run `deploy.sh` again. When that rerun returns:

- every row of the snapshot being restored is present with its snapshot
  values, except rows written after the interruption, which keep their newer
  values; and
- the stock table was not recreated by the rerun, and nothing else durable was
  replaced.

So whatever you record to mean "the restore for this generation is done" must
be written only after every row is in place, and the rerun must recognize an
unfinished restore and finish it. A killed run leaves no chance to clean up:
no trap or exit handler runs.

### Other losses and redeploys

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
finished, is your design. Any state you keep must survive between runs of
`deploy.sh` in the same submission directory, including a run that was killed.
The verifier writes rows through the API, or directly to the stock table in
the shape `data-model.md` describes.

## Logging

Both images write one JSON object per line to stdout. Requests carry
`request_id`.

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
