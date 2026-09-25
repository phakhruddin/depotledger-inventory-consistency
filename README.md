# devcloud/depotledger-inventory-consistency

A Dev Cloud reinforcement-learning environment in the **Data and Storage**
taxonomy. The model must build the data platform behind a multi-warehouse
inventory ledger on an AWS-compatible local control plane using Terraform or
OpenTofu, then operate it through redeploy, table loss and destruction.

**Taxonomy:** Data and Storage · **Difficulty:** hard · **Pass mark:** 100/100

## The problem

An inventory API (ECS ×N behind an ALB) and a snapshotter (ECS ×1) are
supplied. The model supplies everything they stand on:

- a stock table with two exact global secondary indexes, one of them sparse,
  each projecting five attributes, plus point-in-time recovery;
- a reservations/idempotency table with TTL;
- a versioned snapshot bucket with a noncurrent-version retention rule;
- the network, load balancer, services, least-privilege roles and logs;
- a `deploy.sh` that restores a lost stock table from the right snapshot
  generation before it returns, and never resurrects deleted rows.

Traps that carry the difficulty:

1. **Index shape.** Wrong keys, a missing key attribute or a `KEYS_ONLY`
   projection breaks the index-backed views. The emulator applies GSI
   projections on query.
2. **Restore generation.** After table loss, `LATEST.json` already points at
   an empty baseline of the *new* table within about a second. The restore
   must come from the newest snapshot of the previous generation.
3. **No resurrection, no replacement.** A routine redeploy must not restore
   and must not replace a table.
4. **Versioned bucket teardown next to look-alikes.** Destroy removes every
   version of its own bucket and nothing of the pre-existing
   `<prefix>-legacy-*` table and bucket.
5. **Concurrency.** Thirty concurrent reservations for ten units must yield
   exactly ten.

## Layout

| Path | Purpose |
|---|---|
| `instruction.md` | What the agent is asked to build. |
| `reasoning.md` | Design, flows and score rationale for reviewers. |
| `environment/` | Agent workspace image, supplied application images, public contracts. |
| `environment/workspace/contracts/data-model.md` | The product contract: tables, keys, indexes, durability rules. |
| `solution/` | Reference Terraform and lifecycle scripts. One correct answer, not the required layout. |
| `tests/` | Verifier image and the weighted obligation suite. `tests/application`, `tests/contracts` and `tests/runtime/runtime.sh` mirror `environment/`, because Realm uploads only `tests/` as the verifier context. Keep them in sync. |
| `tests/suite/obligations.yaml` | Single source of truth for scoring. The verifier refuses to start if its weights do not reconcile to 100. |

## Validating

```bash
rv health     # static package checks
rv oracle     # runs the reference solution through the full verifier; expect 100
```
# depotledger-inventory-consistency
