locals {
  prefix = var.resource_prefix

  tags = {
    DepotLedgerDeployment = var.resource_prefix
    Name                  = var.resource_prefix
  }

  azs = ["${var.region}a", "${var.region}b"]

  # Attributes both stock indexes must carry so the API can answer from the
  # index alone. A KEYS_ONLY projection would return rows without them.
  projected_stock_attributes = ["on_hand", "reserved", "available", "reorder_point", "updated_at"]

  # Environment shared by both images. The endpoint accepts the static test
  # credentials; the images sign every request with them.
  aws_environment = [
    { name = "AWS_ENDPOINT_URL", value = var.aws_endpoint_url },
    { name = "AWS_REGION", value = var.region },
    { name = "AWS_ACCESS_KEY_ID", value = "test" },
    { name = "AWS_SECRET_ACCESS_KEY", value = "test" },
  ]
}
