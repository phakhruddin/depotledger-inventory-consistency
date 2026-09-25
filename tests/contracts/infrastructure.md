# Infrastructure

Declare every required cloud resource in Terraform or OpenTofu and keep it in
`infra/terraform.tfstate`. Each linked service contract defines the required
settings and relationships for that resource.

## Shared Rules

- Configure the AWS provider and every AWS CLI call with `aws_endpoint_url` and
  `region` from `/workspace/config/config.json`. Use the AWS credentials already
  present in the environment. S3 must be addressed path-style.
- Use `resource_prefix` from `config.json` in the name of every managed
  resource, and tag every taggable resource with
  `DepotLedgerDeployment=<resource_prefix>`.
- Create and manage only resources belonging to this deployment. Do not adopt
  or modify resources that already exist, in particular anything named
  `<resource_prefix>-legacy-*` (see `data-model.md`).
- Resources created only with the AWS CLI are not accepted. You may use the CLI
  to inspect health, fetch identifiers and read or write objects.

## Service Contracts

| Contract | Responsibility |
|---|---|
| [`data-model.md`](data-model.md) | Tables, keys, indexes, TTL and the durability rules. **The product contract.** |
| [`services/dynamodb.md`](services/dynamodb.md) | How the tables are declared and verified |
| [`services/s3.md`](services/s3.md) | Snapshot bucket configuration |
| [`services/vpc.md`](services/vpc.md) | VPC, subnets, routing and security groups |
| [`services/alb.md`](services/alb.md) | Public load balancer, listener and target group |
| [`services/ecs.md`](services/ecs.md) | API and snapshotter services |
| [`services/iam.md`](services/iam.md) | Role separation and least privilege |
| [`services/cloudwatch-logs.md`](services/cloudwatch-logs.md) | Log groups and retention |

## Required Result

```text
  client ──► public ALB (internet-facing, :80) ──► API service ×2 (private subnets)
                                                        │   ▲
                              conditional writes,       │   │ restore reads
                              index queries             ▼   │
                                              ┌──────────────────────┐
                                              │ stock table          │
                                              │  ├ by_warehouse GSI  │
                                              │  └ low_stock GSI     │
                                              │ reservations table   │
                                              └──────────────────────┘
                                                        │ scan
                                                        ▼
                                     snapshotter service ×1 (private subnets)
                                                        │ put
                                                        ▼
                                     snapshot bucket (versioned)
```
