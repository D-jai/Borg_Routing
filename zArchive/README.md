# zArchive/

Historical build artefacts from the BORG_ROUTE initial build (Slices 0-6, completed 2026-04-18).

**Not shipped with releases.** `.gitattributes` in the repo root marks this directory `export-ignore`, so `git archive` / release tarballs exclude it automatically.

## Contents

| File | Purpose |
|---|---|
| `BUILD_PLAN.md` | Extreme-spec slice-by-slice task plan (S1-00..S6-06, all ✅) |
| `CHECKPOINT.md` | Session-by-session build log |
| `SESSION_HANDOFF.md` | Final builder→builder handoff (last session) |
| `HANDOVER_TO_BUILDER.md` | Planner→builder onboarding brief (one-time) |
| `MESSAGE_TO_CLAUDE.md` | Initial architecture-at-a-glance for the builder |
| `DESIGN_PRINCIPLES_FULL.md` | Full 668-line architectural rationale (compact version lives in repo root as `DESIGN_PRINCIPLES.md`) |

## When to read these
- Investigating *why* a past decision was made (design principles full doc).
- Reconstructing the build sequence or a specific slice's intent.
- Onboarding someone who needs the full story.

For day-to-day work, prefer the live docs in the repo root and `docs/`.
