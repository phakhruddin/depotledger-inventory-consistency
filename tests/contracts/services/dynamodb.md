# DynamoDB

Create the two tables described in [`../data-model.md`](../data-model.md). The key
schema, attribute types, index names, index keys, projections, billing
mode, point-in-time recovery and TTL settings given there are all required.

Declare only the attributes that are keys of the table or of an index, which
is what DynamoDB requires. Everything else the API writes is schemaless.

Neither table may be replaced by a routine redeploy. Choose names and settings
that are stable across runs: the name must be derived from `resource_prefix`
and must not change between deployments of the same configuration.

## Restore writes

A restore after table loss writes rows straight into the replacement stock
table (see *Restoring after table loss* in `runtime.md`). The endpoint
supports what that needs:

- `DescribeTable` returns `CreationDateTime`, from which the generation is
  computed.
- `PutItem` with `ConditionExpression: attribute_not_exists(sku)` writes a row
  only if no row with that key exists, and fails with
  `ConditionalCheckFailedException` otherwise. Treat that failure as "already
  present, keep it". Concurrent conditional writes are safe.
- `BatchWriteItem` takes no condition and replaces existing items. It is not a
  safe way to restore into a table that may already hold newer rows.

## Manifest fields

Record in `manifest.data`:

| Field | Required value |
|---|---|
| `stock_table.name`, `stock_table.arn` | The stock table. |
| `reservations_table.name`, `reservations_table.arn` | The reservations table. |
