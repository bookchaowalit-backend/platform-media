# SPEC-001 — Orphan sweep serves every workspace

Standard: ADR-0004 — TDD + SDD.

| Field | Value |
|---|---|
| Spec ID / version | SPEC-001 / 0.2 |
| Status | **in review**: checker R1 revise → 0.2; **R2 pass**; waiting for owner approval |
| Repository / path | bookchaowalit-backend/platform-media / `docs/SPEC-001-MULTI-WORKSPACE-SWEEP.md` |
| Owner (maker) | Claude Code |
| Approver | Owner (bookchaowalit) |
| Requirement links | SaaS gap review 2026-11-11, item 2; platform-identity SPEC-006 |
| Main-only | no: branch → PR, merged locally |
| Created / approved | 2026-11-13 / — |

## 1. Problem and intent

- **Problem:** `python -m platform_media sweep` needs `MEDIA_WORKSPACE_ID` and sweeps only that workspace.
  Objects left by uploads in any other workspace stay in R2 forever, which costs storage and keeps customer
  bytes past the delete the customer asked for (PDPA).
- **Outcome:** one sweep run deletes eligible orphan objects in every workspace, bounded per workspace.

## 2. Scope

| In scope | Out of scope |
|---|---|
| Sweep discovers workspaces with eligible rows and sweeps each up to `MEDIA_SWEEP_BATCH` | Changing the eligibility rules, grace period or object store |
| `MEDIA_WORKSPACE_ID` becomes an optional filter (rollback) | Scheduling the sweep (unchanged) |

## 3. Scenarios

- **AC-01 — Every workspace is swept.** Given eligible rows in W1 and W2, one run deletes both sets of
  objects and stamps `swept_at`. Ready files are never touched.
  Test: `tests/test_sweep_workspaces.py::test_run_sweeps_every_workspace`.
- **AC-02 — Discovery reveals ids only.** A SECURITY DEFINER function `media_sweep_workspaces(grace, limit)`
  returns ids of workspaces with eligible rows, and only to the runtime role. Rows are still read under RLS
  per workspace. Test: `::test_discovery_returns_ids_only`.
- **AC-03 — Bounded and fair.** Each workspace gets at most `MEDIA_SWEEP_BATCH` rows per run. `more=yes`
  if any workspace had more. Test: `::test_batch_is_per_workspace_and_more_is_reported`.
- **AC-04 — Optional filter.** With `MEDIA_WORKSPACE_ID` set, only that workspace is swept (today's
  behaviour). Test: `::test_env_filter`.
- **AC-05 — Failures are isolated.** A store error in W1 leaves W1's rows for the next run, still sweeps W2,
  and exits 3. Test: `::test_store_failure_in_one_workspace`.

## 4. Interfaces and data

- New migration with `media_sweep_workspaces`, the same pattern as communication SPEC-001 §4.
- Env: `MEDIA_WORKSPACE_ID` becomes optional.
- Output stays `swept=… failed=… skipped=… more=…`, with totals across workspaces, and no workspace ids.

## 5. NFR

Logs carry counts only, and there is no new process. A run takes at most (number of workspaces ×
`MEDIA_SWEEP_BATCH`) store calls. The discovery limit caps workspaces per run at 200; the rest come next run.

## 5a. Design decisions (0.2, checker R1)

- **D1 — owner role provisioned out of band.** Use `media_discovery` (NOLOGIN, NOBYPASSRLS, NOINHERIT), with the
  same steps as communication SPEC-001 D1: the migration checks the role exists, sets ACLs, then changes the
  owner.
- **D2 — narrow read path.** The role gets `SELECT (workspace_id, status, expires_at, swept_at)` on `media_files`
  only, plus one `FOR SELECT TO media_discovery USING (true)` policy. It cannot see object keys and cannot write.
- **D3 — predicate.** The predicate is the sweeper's own: `swept_at IS NULL AND status IN
  ('pending','deleting','deleted') AND expires_at <= now() - grace`. `grace` must be ≥ 0, and `p_limit` is
  clamped to 1..200 inside the function. Order is by oldest `min(expires_at)`.
- **D4 — isolation and exits.** Each workspace runs in its own try/except. A store error leaves rows and counts
  as failed. A database error in one workspace counts as failed too and does not abort the run. Exit codes:
  0 = done; 3 = done with any failed workspace or row; 1 = discovery or connection failed before any workspace
  ran. `more=yes` when any workspace had a full batch **or** the 200-workspace cap was hit.
- **D5 — monitoring.** The sweep posts a counts-only heartbeat `media-sweep` (as communication does), so a
  silent sweep alerts.
- **D6 — rollback.** With `MEDIA_WORKSPACE_ID` set, discovery is skipped and a bad UUID fails at start-up.

### Added scenarios

- **AC-06 — No workspaces:** exit 0, `swept=0`, no store call.
- **AC-07 — Discovery failure:** exit 1, no store call.
- **AC-08 — Database error in one workspace:** the others are swept, exit 3.
- **AC-09 — Concurrent runs:** both may delete the same key. S3 DELETE is idempotent, so both exit 0 or 3 and
  each row is stamped once.
- **AC-10 — Discovery role cannot read keys or write.**
- **AC-11 — Logs carry no workspace ids or object keys.**

## 6. Open questions

| # | Question | Owner |
|---|---|---|
| 1 | Workspaces per run cap: 200 (proposed) | Owner |
