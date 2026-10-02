# ---------------------------------------------------------------------------
# Durable data: the stock ledger, the reservation/idempotency ledger and the
# snapshot bucket. None of these may be replaced by a routine redeploy.
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "stock" {
  name         = "${local.prefix}-stock"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "sku"
  range_key    = "warehouse_id"

  attribute {
    name = "sku"
    type = "S"
  }
  attribute {
    name = "warehouse_id"
    type = "S"
  }
  # Only present while an item is at or below its reorder point, which is what
  # makes the low_stock index sparse.
  attribute {
    name = "low_stock_warehouse"
    type = "S"
  }

  global_secondary_index {
    name               = "by_warehouse"
    hash_key           = "warehouse_id"
    range_key          = "sku"
    projection_type    = "INCLUDE"
    non_key_attributes = local.projected_stock_attributes
  }

  global_secondary_index {
    name               = "low_stock"
    hash_key           = "low_stock_warehouse"
    range_key          = "sku"
    projection_type    = "INCLUDE"
    non_key_attributes = local.projected_stock_attributes
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(local.tags, { Name = "${local.prefix}-stock" })
}

resource "aws_dynamodb_table" "reservations" {
  name         = "${local.prefix}-reservations"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "order_id"

  attribute {
    name = "order_id"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  tags = merge(local.tags, { Name = "${local.prefix}-reservations" })
}

resource "aws_s3_bucket" "snapshots" {
  bucket = "${local.prefix}-snapshots"
  # Snapshots are versioned; destroy must be able to remove every version and
  # delete marker, not only the current objects.
  force_destroy = true
  tags          = merge(local.tags, { Name = "${local.prefix}-snapshots" })
}

resource "aws_s3_bucket_versioning" "snapshots" {
  bucket = aws_s3_bucket.snapshots.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "snapshots" {
  bucket     = aws_s3_bucket.snapshots.id
  depends_on = [aws_s3_bucket_versioning.snapshots]

  rule {
    id     = "expire-noncurrent-snapshots"
    status = "Enabled"
    filter {
      prefix = "snapshots/"
    }
    noncurrent_version_expiration {
      noncurrent_days = var.snapshot_noncurrent_retention_days
    }
  }
}

resource "aws_s3_bucket_public_access_block" "snapshots" {
  bucket                  = aws_s3_bucket.snapshots.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "snapshots" {
  bucket = aws_s3_bucket.snapshots.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
