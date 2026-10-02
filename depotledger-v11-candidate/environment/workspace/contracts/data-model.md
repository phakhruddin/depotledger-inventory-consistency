# Data Model

This is the product contract for DepotLedger's durable state. **Read it
first.** The API and snapshotter images are fixed. The tables, indexes and
bucket they depend on are yours to build, and they must match this file
exactly where it says so.

## Stock table

One row per SKU per warehouse.

| Setting | Required value |
|---|---|
| Name | Contains `resource_prefix`. Passed to both images as `STOCK_TABLE`. |
| Partition key | `sku`, type `S` |
| Sort key | `warehouse_id`, type `S` |
| Billing | On demand (`PAY_PER_REQUEST`) |
| Point-in-time recovery | Enabled |

Attributes the API writes on every row:

| Attribute | Type | Meaning |
|---|---|---|
| `sku` | S | Partition key. |
| `warehouse_id` | S | Sort key. |
| `on_hand` | N | Physical units in the warehouse. |
| `reserved` | N | Units promised to confirmed reservations. |
| `available` | N | `on_hand - reserved`. Never negative. |
| `reorder_point` | N | At or below this `available` level, the row is low stock. |
| `version` | N | Optimistic concurrency counter. |
| `updated_at` | S | ISO-8601 time of the last write. |
| `low_stock_warehouse` | S | Present **only** while `available <= reorder_point`. Equal to `warehouse_id`. |

### Global secondary indexes

Both indexes are required on the stock table, with exactly these names and
keys:

| Index name | Partition key | Sort key |
|---|---|---|
| `by_warehouse` | `warehouse_id` (S) | `sku` (S) |
| `low_stock` | `low_stock_warehouse` (S) | `sku` (S) |

Each index must project at least `on_hand`, `reserved`, `available`,
`reorder_point` and `updated_at`. That means projection type `INCLUDE` with
those non-key attributes, or `ALL`. `KEYS_ONLY` is not sufficient: the API
answers index queries from the index alone.

`low_stock` is sparse. Because rows only carry `low_stock_warehouse` while they
are low, querying it for a warehouse returns exactly that warehouse's
low-stock rows.

## Reservations table

One row per `order_id`. It is the idempotency ledger, so a retried order never
takes stock twice.

| Setting | Required value |
|---|---|
| Name | Contains `resource_prefix`. Passed to the API as `RESERVATIONS_TABLE`. |
| Partition key | `order_id`, type `S` |
| Billing | On demand (`PAY_PER_REQUEST`) |
| Time to live | Enabled on attribute `expires_at` |

The API writes `order_id`, `reservation_id`, `sku`, `warehouse_id`,
`quantity`, `status`, `created_at` and `expires_at`.

## Snapshot bucket

Holds the snapshotter's output and anything your deployment needs to record
about restores. `services/s3.md` gives the required bucket configuration, and
`runtime.md` describes the object layout the snapshotter writes.

## Durability rules

1. **A routine redeploy never replaces durable data.** Rerunning `deploy.sh`
   must not recreate either table or the bucket. It must not remove rows or
   object versions either.
2. **Repair keeps what survived.** When one durable resource is lost, the
   others keep their identity and contents. For example, after the stock table
   is lost, the reservations table still answers replays of earlier orders,
   and the bucket still holds every earlier snapshot version.
3. **A lost stock table is restored from a committed snapshot of the
   generation that was just lost.** Your deployment performs the restore
   itself. It never overwrites a row that already exists, and a restore that
   is interrupted is finished by the next `deploy.sh`. The table may be lost
   more than once. See *Restoring after table loss* in `runtime.md`.
4. **Nothing is resurrected.** Restores happen only into a new table
   generation, never into the table they were taken from, never from an older
   generation, never from an uncommitted snapshot object, and never from a
   version of a data object that does not match its commit marker.
5. **The bucket's durable controls repair in place.** If bucket versioning is
   suspended or the lifecycle configuration is removed outside Terraform, the
   next `deploy.sh` must restore both on the same bucket. The bucket must keep
   its name and identity, and every existing object version must be kept.
   Recreating the bucket, or emptying it to "reset" it, is a failure.
6. **Destruction removes it all.** `destroy.sh` deletes both tables and the
   bucket, including every object version and delete marker.

## Pre-existing resources

Resources whose names start with `<resource_prefix>-legacy-` may already exist
when your deployment starts, for example a table `<resource_prefix>-legacy-stock`
or a bucket `<resource_prefix>-legacy-snapshots`. They belong to someone else.
Do not read, modify, adopt or delete them, and do not name any of your own
resources with that prefix.
