# STATE: Airfare Price Index (APIx)

## Current Position
- **Milestone**: M1 — Core Pipeline (MVP)
- **Phase**: Initialized — ready for Phase 1
- **Last Action**: Project initialization with GSD new-project

## Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-10-11 | Fare definition = base fare + mandatory taxes only | Keeps index comparable across sources; OTA convenience fees and optional add-ons distort comparison |
| 2026-10-11 | VLM fallback is optional/degraded mode, not primary | Cost control + primary selectors should work 95%+ of the time |
| 2026-10-11 | One file per commit, conventional commits | User requirement; enables clean PR review |
| 2026-10-11 | Feature branch `feature/apix-pipeline` | User requirement; clean merge to main |
| 2026-10-11 | Git executable at `C:\Users\viraj\AppData\Local\GitHubDesktop\app-3.6.6\resources\app\git\cmd\git.exe` | System `git` not on PATH; using GitHub Desktop's bundled git |

## Blockers
- **No git remote configured** — need repository URL from user before pushing

## Memory
- Git must be invoked via full path (see decision log)
- User wants every file committed individually with descriptive conventional commit messages
- User wants tests run before committing code files
- User wants git log shown after each major piece
