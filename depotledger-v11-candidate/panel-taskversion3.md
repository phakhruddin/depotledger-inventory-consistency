# DepotLedger — Panel review, task version 3

Runs reviewed: `~/Downloads/realm-run/depotledger/taskversion3/`: the 6 panel runs, 1 extra run, and a later GPT 5.6 Sol run
(`224f0f9c`, added 2026-09-26, likely a re-run of a GPT Sol slot).
Method: `context/panel.md` / `DevCloud-Instruction.md` trace review. Every verdict below was checked
against the verifier report, the saved submission and the lifecycle logs (`deploy*.txt`, `destroy.txt`).

| Run | Model | Score (raw → capped) | Failed gates / caps | Verdict |
|---|---|---|---|---|
| `d86a5d78-9577-4419-8403-6e8751daa793` | Gemini 3.7 Flash | 100 → 100 | — | **True pass** |
| `7492a958-63a8-43d4-8d57-509e30376623` | Claude Opus 4.8 | 91 → 79 | `lifecycle.destroy_clean`, cap `cleanup_leak` | **False fail** |
| `bb701765-3120-4c5d-840f-ea450e9ae396` | Claude Opus 4.8 | 91 → 91 | `lifecycle.destroy_clean` | **Invalid (host kill), rerun** |
| `96614385-3379-433b-971a-82b2529d17f6` | GPT 5.6 Sol | 91 → 79 | `lifecycle.destroy_clean`, cap `cleanup_leak` | **False fail** |
| `cc43eba6-0e6d-4238-ba92-f13ad2543ac4` | GPT 5.6 Sol | 91 → 79 | `lifecycle.destroy_clean`, cap `cleanup_leak` | **False fail** |
| `a3f8e63b-5e3f-461d-9c1e-ad77f31306bd` | Gemini 3.7 Flash | 91 → 79 | `lifecycle.destroy_clean`, cap `cleanup_leak` | **False fail** |
| `004bf0b5-14fb-42e7-967f-c7782495ddb6` | GPT Astra (extra, not panel) | 0 → 0 | deployment never completed | **True fail** |
| `224f0f9c-ff8f-4d8b-8797-fea50fe71bc2` | GPT 5.6 Sol (later run) | 91 → 79 | `lifecycle.destroy_clean`, cap `cleanup_leak` | **False fail** |

**Panel outcome: not submittable.** 5 of 7 scored attempts (the 6 panel runs plus `224f0f9c`) are
false fails from one verifier defect, and one is invalid. The defect is fixed in the working tree (v4).
With it applied, **6 of those 7 attempts would score 100.** So v3 is also **far too easy**
(target: at most 1 pass in 6), and a straight panel re-run on v4 would only confirm that.

## Per-run records

### d86a5d78 — Gemini 3.7 Flash — 100/100 — True pass
- **Why:** All 14 obligations passed. Its `deploy.sh` records the last seen generation in
  `.last_generation.json`, compares it with `current_generation`, and on a new generation restores
  the newest snapshot whose generation differs (restored 15/15 rows from
  `snapshots/1790381007000/…`, then waited for `LATEST.json` to cover the new generation). Tables,
  indexes and projections matched the contract; 30 concurrent reservations took exactly 10 units.
- **Caveat:** its log groups were named `/ecs/<prefix>-api|snapshotter`, which coincides with the
  emulator's auto-created names, so the destroy defect below could not hit it. The pass is genuine on
  the merits; it is not evidence that the destroy check was fair.
- **Action taken:** none.

### 7492a958, 96614385, cc43eba6, a3f8e63b — Opus / GPT Sol ×2 / Gemini — 91 raw, 79 capped — False fail
- **Why:** Each passed all 13 other obligations, including table-loss restore, oversell and the
  standalone plan. Each failed only `lifecycle.destroy_clean` with
  `log_groups: ['/ecs/<prefix>-api', '/ecs/<prefix>-snapshotter']`, which triggered the 79 cap.
  None of these submissions declared those names: they declared `/depotledger/<prefix>/…`
  (7492a958, cc43eba6) or `/<prefix>/…` (96614385, a3f8e63b), and `terraform destroy` removed them.
  The leftover groups are named `/ecs/<task definition family>`. Floci's ECS creates them itself for
  every task, whatever `awslogs-group` says. Real ECS does not do this, and the public contract never
  mentioned it, so the check enforced an unstated emulator artifact (**misaligned test**).
- **Why the oracle passed:** the reference names its groups `/ecs/<prefix>-api`, identical to the
  auto-created names, so Terraform deleted them by coincidence.
- **Action taken (v4):**
  - `tests/suite/test_lifecycle.py::test_destroy_is_clean` now reads Terraform state before destroy
    and excuses only `/ecs/<family>` groups for the submission's own task families that the
    submission did **not** declare. A declared group that survives destroy is still a leak and still
    caps the run.
  - `contracts/runtime.md` (both copies) documents the behavior in the emulator table.

### 224f0f9c — GPT 5.6 Sol (later run) — 91 raw, 79 capped — False fail
- **Why:** Same signature as the four false fails above. All 13 other obligations passed. That includes
  table-loss restore: all 15 rows of `snapshots/1790426941000/1790427090380.jsonl` came back, driven by
  a `.restore-required` marker and a "newest snapshot whose generation differs" selection. Oversell
  was exact at 10/30, and redeploy neither resurrected nor replaced anything. It failed only
  `lifecycle.destroy_clean` on `/ecs/dl-9aee79e855bb-api` and `/ecs/dl-9aee79e855bb-snapshotter`.
  It declared `/depotledger/<prefix>/api|snapshotter` with task families `<prefix>-api|snapshotter`,
  and its `destroy.sh` is a plain `terraform destroy` that exited 0. The leftovers are the emulator's
  auto-created `/ecs/<family>` groups. The legacy decoys were intact, and nothing else carrying the
  prefix remained.
- **Version check:** the verifier details carry no `emulator_log_groups_excused` field, so this ran on
  the v3 verifier, before the fix. Under the v4 check both groups are excused and the run scores 100.
- **Host health:** normal completion in 18 min, no exception, no OOM, watchdog alive throughout.
- **Action taken:** covered by the v4 verifier fix. No re-run needed for evidence; it is now the
  clearest single case of the defect.

### bb701765 — Claude Opus 4.8 — 91 raw — Invalid, rerun
- **Why:** The agent process was killed with exit 137 at 09:46 (`UnknownApiError`) while it was still
  debugging teardown. `kill-forensics.txt` shows no OOM, and the watchdog log shows the agent alive
  and making progress until it vanished, so this was a host-side kill, not the model. The submission
  it left scored 91: its `destroy.sh` exited 1 because it assigns to bash's read-only `GROUPS`
  variable under `set -e`. That sweep was the agent working around the same `/ecs/<family>` artifact.
  Because the run was cut short, and the defect it was chasing is the verifier bug above, it cannot
  be counted as a true fail.
- **Action taken:** re-run on v4.

### 004bf0b5 — GPT Astra (extra) — 0/100 — True fail
- **Why:** `deploy.sh` delegates to `lifecycle.py`, which ships a solve-time journal
  (`.deployment.json`) pinning the agent-session prefix. It aborts with
  `Configuration prefix does not match this directory's deployment` when the verifier supplies a
  fresh prefix. `instruction.md` states the prefix is generated fresh for every run and must be read
  from `/workspace/config/config.json` at execution time, so this is a real contract miss.
- **Action taken:** none.

## Failure pattern and next step
Seven scored v3 attempts, excluding the Astra extra run:

| After the fix | Count | Runs |
|---|---:|---|
| 100 | 6 | d86a5d78, 7492a958, 96614385, cc43eba6, a3f8e63b, 224f0f9c |
| Invalid (host kill) | 1 | bb701765 |

**Every model family solved the intended hard parts on every valid attempt:** the exact GSI
projections, the sparse index, conditional-write oversell safety, generation-aware restore and
no-replacement redeploy. The only discriminator left is persisting solve-time state (Astra), which
is narrow. The destroy "difficulty" in v3 came entirely from the emulator artifact, not from cloud
skill.

### Recommended next actions (in order)
1. **Do not spend a panel run on v4 as it stands.** It will pass roughly 6 of 6. Keep the verifier
   fix: it is correct regardless.
2. **Raise difficulty in the same revision (v5)**, in areas the traces show models don't yet handle,
   while keeping everything stated in the public contract:
   - **Two losses, pick the right generation.** Delete the stock table twice, with writes between.
     Every v3 restore used "newest snapshot whose generation ≠ current". After a second loss that
     rule is still correct only if restore markers and generation tracking survive the first
     restore. Make the contract require restoring the generation that was live **immediately before
     this loss**, and have the verifier seed a stale older generation with conflicting rows.
   - **Durable-control drift repair.** Suspend bucket versioning and delete the lifecycle rule out
     of band. `deploy.sh` must restore both on the **same** bucket without losing object versions
     (the declared plane checks config, the live plane checks versions retained).
   - **Online index evolution.** Add a third index or a new projected attribute through a config
     change between deploys. It must land without replacing the table, and old rows must appear in
     the new view (backfill). Every v3 model got the static schema right first time, so this tests
     change management rather than first-shot design.
3. **Re-verify the oracle** (`rv oracle` → 100) and add a known-bad variant for each new check:
   restore-from-oldest, versioning left suspended, and index added by replacement. Each must be
   rejected.
4. **Then run the full panel once on v5** and review every trace again with this file's format.
5. Optionally re-run `bb701765` (Opus). It was a host kill and adds no evidence about v3 difficulty.
