# VPC

Create one VPC with two public and two private subnets, one of each in two
different availability zones of `region`.

- Public subnets route `0.0.0.0/0` to an internet gateway. The load balancer
  lives in them.
- Private subnets have no route to the internet gateway. Every ECS task runs
  in them, with public IP assignment disabled.
- The load balancer's security group admits TCP `80`. The API tasks' security
  group admits TCP `8080` from the load balancer's security group only. The
  snapshotter's security group admits no inbound traffic.

Security groups and route tables are recorded by this endpoint but do not
filter packets. They are checked as declarations only.

## Manifest fields

Record in `manifest.network`: `vpc_id`, `public_subnet_ids` and
`private_subnet_ids`.
