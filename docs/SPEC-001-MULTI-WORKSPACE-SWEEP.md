# SPEC-001 — Orphan sweep serves every workspace

Standard: ADR-0004 — TDD + SDD.

| Field | Value |
|---|---|
| Spec ID / version | SPEC-001 / 0.1 |
| Status | **draft**, waiting for owner approval |
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

## 6. Open questions

| # | Question | Owner |
|---|---|---|
| 1 | Workspaces per run cap: 200 (proposed) | Owner |
