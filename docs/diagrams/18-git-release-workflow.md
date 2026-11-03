# 18 — Git and release workflow

จาก `git log --first-parent origin/main` และ `gh pr list`: งานทำบน branch สั้น (เช่น `media-runtime`, `media-followups`,
`build-hygiene`, `media-sweeper`, `sentry`), เปิด PR, CI เขียว, merge เข้า `main` ด้วย merge commit
("Merge PR #N: ...") แล้ว owner deploy ด้วย CLI จาก `git archive main` ไม่มี tag / GitHub release

```mermaid
gitGraph
  commit id: "6831d30 scaffold 2026-09-17"
  commit id: "3ffb46b graphics batch"
  commit id: "d493a77 shared CI v1.1.1"
  branch media-runtime
  commit id: "65aa99d media API, content-type gate"
  checkout main
  merge media-runtime id: "72cf848 PR 1"
  branch media-followups
  commit id: "F1a F1b F2"
  checkout main
  merge media-followups id: "916df6e"
  branch media-sweeper
  commit id: "6c9093f sweeper"
  checkout main
  merge media-sweeper id: "d8e6052"
  branch sentry
  commit id: "8faf1ca opt-in Sentry"
  checkout main
  merge sentry id: "140bd1f"
  branch sentry-prs
  commit id: "PRs 3 to 16 scrub fixes"
  checkout main
  merge sentry-prs id: "0f7001e deployed"
  commit id: "f3b4a72 PR 17 uv lock, mypy"
```

```mermaid
flowchart LR
  B["Short branch"] --> PR["Pull request"]
  PR --> CI{"check.yml and shared-runtime.yml green"}
  CI -->|"no"| B
  CI -->|"yes"| M["Merge to main, merge commit"]
  M --> MAINCI["CI runs again on push to main"]
  MAINCI --> D["Owner: git archive main, railway up --ci"]
  D --> RZ["Verify /readyz 200 and sentry enabled log"]
```

Sources: `git log --first-parent origin/main --format='%h %ad %s' --date=short`, `gh pr list -R bookchaowalit-backend/platform-media --state all`, `.github/workflows/*.yml`, `docs/runtime.md` (branch names in Verification sections), ops record: book-platforms-status (2026-11-03, deploy recipe and `0f7001e`)

ข้อสังเกต: gitGraph ย่อประวัติ: `sentry-prs` เป็นตัวแทนของ PR #3–#16 ซึ่งแต่ละตัวมี branch และ merge commit ของตัวเอง (`bfbf56d` … `0f7001e`); merge core pin `6a6b912`, `83de039` และ build hygiene `0f87511` ไม่ได้วาด; PR #17 (`f3b4a72`) ลง main แบบไม่มี merge commit

## Open questions

- branch protection / required checks บน `main` ตั้งไว้อะไร (ดูจาก repo ไม่ได้)
- PR #2 ยัง OPEN (2026-11-02) และ ops record บอกว่ามี PR ซ้ำจาก branch `sentry-hardened-scrubber` ค้าง: ควรปิดหรือไม่
- ต้องการ tag / release version (pyproject ยังเป็น `0.1.0`) เพื่อผูก deploy กับ commit หรือไม่
