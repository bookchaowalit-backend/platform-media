# 15 — CI/CD pipeline

สาม workflow ใน `.github/workflows/` ไม่มี workflow ไหน deploy; deploy เป็น CLI (`railway up`) ตาม ops record

```mermaid
flowchart TD
  PUSH["push any branch"] --> CHECK
  PR["pull_request"] --> CHECK
  WD1["workflow_dispatch"] --> CHECK
  PUSH --> SR
  PR --> SR
  WD2["workflow_dispatch"] --> SR
  WD3["workflow_dispatch only"] --> GR

  subgraph CHECK["check.yml: Check"]
    C1["contract: reusable ci-python.yml at 87dc5e1c v1.1.1, matrix Python 3.11 and 3.12"]
    C1a["test-command: BOOK_MEDIA_SKIP_RENDERER_TESTS=1 bash scripts/check.sh"]
    C2["security: reusable security.yml at 87dc5e1c v1.1.1"]
    C1 --> C1a
  end

  subgraph SR["shared-runtime.yml: Shared runtime"]
    R0["service postgres:16"]
    R1["runtime job: uv sync --frozen --all-extras"]
    R2["ruff check, mypy ."]
    R3["Step summary: renderer unavailable"]
    R4["bash scripts/check.sh"]
    R5["pytest -q -rs --cov --cov-fail-under=72"]
    R6["uv build --wheel"]
    I1["image job: docker build --pull, no push"]
    I2["Image imports boto3, core, platform_media, graphics; --help; non-root uid"]
    R0 --> R1 --> R2 --> R3 --> R4 --> R5 --> R6
    I1 --> I2
  end

  subgraph GR["graphics-renderer.yml: Optional graphics renderer"]
    G1["windows-latest, Python 3.12"]
    G2["python scripts/check_graphics_renderer.py, fails on any skip"]
    G3["Step summary with outcome"]
    G1 --> G2 --> G3
  end

  CHECK --> GATE{"All green on main?"}
  SR --> GATE
  GATE -->|"manual, outside CI"| DEP["Owner: git archive main, railway up --ci"]
  DEP --> RAIL["Railway media-api-production-91a5"]
```

Sources: `.github/workflows/check.yml`, `.github/workflows/shared-runtime.yml`, `.github/workflows/graphics-renderer.yml`, `scripts/check.sh`, `scripts/check_graphics_renderer.py`, `docs/ci-renderer-gates.md`, `tests/test_build_hygiene.py` (SHA-pin rule), ops record: book-platforms-status (2026-11-03, deploy recipe)

| Workflow | Triggers | Jobs | Concurrency | Permissions |
|---|---|---|---|---|
| `check.yml` | push, pull_request, workflow_dispatch | `contract` (matrix 3.11/3.12), `security` | `check-<ref>`, cancel in progress | `contents: read` |
| `shared-runtime.yml` | push, pull_request, workflow_dispatch | `runtime` (15 min, postgres:16), `image` (15 min) | `shared-runtime-<ref>` | `contents: read` |
| `graphics-renderer.yml` | workflow_dispatch | `renderer` (windows-latest, 20 min) | `graphics-renderer-<ref>` | `contents: read` |

ทุก `uses:` pin ด้วย commit SHA (`actions/checkout@11d5960a…` v4, `actions/setup-python@a26af69b…` v5, reusable workflows `@87dc5e1c…` v1.1.1)

## Open questions

- `graphics-renderer.yml` เคยถูก dispatch และผ่านหรือยัง (`docs/ci-renderer-gates.md`: "has not been run or accepted")
- ตามนโยบาย CI hosting repo นี้เป็น public จึงใช้ GitHub-hosted runner: ยืนยัน visibility ของ repo
- ไม่มี CD workflow: จะย้ายไป deploy จาก CI (เช่น Railway GitHub source) หรือคง CLI
