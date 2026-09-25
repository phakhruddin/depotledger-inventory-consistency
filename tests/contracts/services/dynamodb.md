# DynamoDB

Create the two tables described in [`../data-model.md`](../data-model.md). The key
schema, attribute types, index names, index keys, projections, billing
mode, point-in-time recovery and TTL settings given there are all required.

Declare only the attributes that are keys of the table or of an index, which
is what DynamoDB requires. Everything else the API writes is schemaless.

Neither table may be replaced by a routine redeploy. Choose names and settings
that are stable across runs: the name must be derived from `resource_prefix`
and must not change between deployments of the same configuration.

## Manifest fields

Record in `manifest.data`:

| Field | Required value |
|---|---|
| `stock_table.name`, `stock_table.arn` | The stock table. |
| `reservations_table.name`, `reservations_table.arn` | The reservations table. |
