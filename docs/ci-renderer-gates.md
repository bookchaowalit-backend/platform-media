# CI and optional renderer acceptance

Correlation: `SHARED-INFRA-2026-10-15`. Hosted API/contract checks and real browser
acceptance require different environments. A green Shared runtime job does not
provide renderer acceptance.

## Observed failure and cause

[Shared runtime run 38701556273](https://github.com/bookchaowalit-backend/platform-media/actions/runs/38701556273)
on `ubuntu-latest` discovered the installed Edge executable and attempted
`test_headless_edge_waits_for_a_complete_local_preview`. Edge then aborted during
sandbox initialization: exit `-6`, no screenshot, and stderr referencing the
`edge-sandbox` root ownership/mode `4755` requirement. The supplied log does not
establish the executable's actual ownership/mode or the runner's complete sandbox
configuration. This failed before a render result existed, not at a layout or
image comparison assertion. The run reported 84 passed, 5 skipped and 1 failed,
with two subtests passed.

The existing Check workflow already used `BOOK_MEDIA_SKIP_RENDERER_TESTS=1` for
this unreviewed hosted renderer environment. Shared runtime omitted that explicit
choice and therefore attempted the smoke case, whose prerequisites do not
include Arial. Finding a browser is not proof of a working sandbox, usable fonts
or reviewed raster fingerprints. No sandbox disabling flags, permission changes,
host security changes or shipping renderer changes are part of this correction.

## Automatic hosted checks

Check and Shared runtime explicitly use the existing renderer-test flag on their
hosted Linux jobs. Only the six cases in `tests/test_graphics_renderer.py` honor
it. All original schema, graphics contract, deterministic layout/artifact,
asset-validation and locking tests remain enabled; PostgreSQL API, concurrency
and workspace-isolation tests also remain enabled. Shared runtime additionally
runs the original `bash scripts/check.sh` and uses `pytest -rs` to print each skip.
Its job summary calls renderer acceptance unavailable, rather than passed.

Equivalent local commands, with a disposable fixture-admin `PLATFORM_TEST_DSN`
already supplied explicitly, are:

```bash
BOOK_MEDIA_SKIP_RENDERER_TESTS=1 bash scripts/check.sh
BOOK_MEDIA_SKIP_RENDERER_TESTS=1 python -m pytest -q -rs
```

This is an explicitly scoped API/contract result. It does not fix the hosted
Edge installation and must not be used to claim full graphics rendering parity.
Ordinary local tests without the flag still attempt available Edge integration;
an actual browser failure remains a failure.

## Separate Windows renderer gate

The manual **Optional graphics renderer** workflow runs on `windows-latest`,
matching the source's native Windows Edge/PowerShell supervision and process-tree
test. It is dispatched separately through the repository's Actions UI on the
reviewed branch. It has not been run or accepted by this local correction; runner
availability and prerequisite compatibility remain unverified here. It does not
run automatically for a pull request or provision a new self-hosted runner.

`python scripts/check_graphics_renderer.py` discovers only the six intended
renderer cases. It fails on a test failure, no discovered cases, or **any skipped
case**. It also refuses the skip-renderer and update-goldens flags. Thus missing
Edge/Arial, fingerprint drift or unavailable Windows process tools cannot turn
into a green renderer gate. No unrelated Linux-only cases are required.

The six cases cover a complete local screenshot, deep-path rendering/commit,
embedded raster assets, long product descriptions, nine reviewed golden images,
and Windows supervisor/child-process cleanup on timeout. Prerequisites are:

- Native Windows with Edge, PowerShell and taskkill available to the runner user.
- Installed Arial, including the reviewed font fingerprint for exact comparison.
- The reviewed Edge version and Arial hash in
  `tests/golden/graphics-v1a/manifest.json` (currently Edge `153.0.4234.48`, font
  SHA256 `b3658eadae55e682b5f69eb64c439c1ecc8f196c0bb8d4756d145d13bc86476a`).

A moving Windows image may have different browser/font fingerprints. That result
is unavailable acceptance, even if functional renders pass. Do not downgrade a
browser, weaken sandboxing or regenerate golden files just to obtain green CI.
If the baseline needs updating, review all nine renders and approve that separate
baseline change according to `tests/golden/graphics-v1a/README.md`.

To run the same strict gate from a prepared native Windows checkout:

```powershell
Remove-Item Env:BOOK_MEDIA_SKIP_RENDERER_TESTS -ErrorAction SilentlyContinue
Remove-Item Env:BOOK_MEDIA_GRAPHICS_UPDATE_GOLDENS -ErrorAction SilentlyContinue
python scripts/check_graphics_renderer.py
```

The module needs only Python's standard library and the repository's original
graphics modules. Normal documented `BOOK_MEDIA_EDGE_PATH`/`BOOK_MEDIA_FONT_DIRS`
may point to approved installed prerequisites; no credential discovery is used.
The workflow preserves a visible outcome summary even on failure. A renderer
release gate requires a linked run where all six cases actually executed and
passed. A workflow that was not dispatched supplies no such evidence.
