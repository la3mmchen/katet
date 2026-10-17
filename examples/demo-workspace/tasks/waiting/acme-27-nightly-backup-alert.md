# [P2] ACME-27: Nightly backup job alerts on every run

> [!IMPORTANT]
> **Agents: keep this callout, never delete or shorten it.**
> This file is the main channel between the coding agent and the human.
> Questions, decisions, findings and human steps go here, in the sections
> below. Keep interaction in the chat minimal. Update Status, Updated
> and Agent logs every time you touch the task.

## Briefing

**Status:** waiting — action: accept — alert threshold fixed, S4 is the commit (A1–A3 ticked)
**Updated:** 2026-10-16 08:51 — fix verified against last night's run; commit handed over

- ticket: ACME-27
- MR:
- branch: `fix/acme-27-backup-alert`
- repos:
  - `~/repos/acme/monitoring` — alert rules in `rules/backups.yml`
- related:
- model: sonnet

## Chat

- **you** · 2026-10-15 17:20
  The "backup too slow" alert fires every night since the retention change, even when the job
  finishes fine. Find out why and fix the rule.
- **agent** · 2026-10-15 17:41
  Cause found: the rule compares against a fixed 20 min, but since the retention change the job
  legitimately takes 25–30 min. Fixed in the branch (threshold from the job's own p95). Details in
  Agent logs; A1–A3 are ticked, the commit is S4.

## Task

The alert `BackupTooSlow` in `rules/backups.yml` fires on every nightly run since the retention
change of 2026-10-10, although the backups complete successfully. Find the cause and fix the rule
so that it fires only when a run is genuinely slow.

## Constraints / non-goals

- don't change the backup job itself
- out of scope: the alert routing

## Acceptance criteria

- [x] **A1** The rule no longer fires for last night's run — check: `promtool test rules tests/backups_test.yml` passes
- [x] **A2** A run that takes twice the usual time still alerts — check: the second case in `tests/backups_test.yml`
- [x] **A3** The rule file is valid — check: `promtool check rules rules/backups.yml`

## Open questions

- [x] **Q1 — Threshold as a fixed 45 min, or relative to the job's own history (p95 × 1.5)?** (asked 2026-10-15)
  Fixed is simpler to read; relative follows future retention changes — recommendation relative
  **Answer:** relative, we will change retention again
  **Outcome:** 2026-10-15 17:38 — rule uses `quantile_over_time(0.95, …[7d]) * 1.5`; both test cases pass.

## Next steps

- [x] **S1** Confirm last night's run duration in the job log (28 min)
- [x] **S2** Review the two test cases in `tests/backups_test.yml`
- [x] **S3** Run the rule tests locally
  ```bash
  promtool test rules tests/backups_test.yml
  ```
- [ ] **S4** Commit the fix — repo `~/repos/acme/monitoring`, files: `rules/backups.yml`, `tests/backups_test.yml`
  subject:
  ```text
  ACME-27: alert on backup duration relative to the job's p95
  ```
  message:
  ```text
  Replaces the fixed 20 min threshold by quantile_over_time(0.95, …[7d]) * 1.5, so the rule
  follows retention changes. Adds a rule test with a normal and a slow run.
  ```

## Agent logs

- 2026-10-15: Compared the last 14 runs: 25–30 min since 2026-10-10, 15–18 min before. Fixed threshold of 20 min explains every alert.
- 2026-10-15: Q1 answered (relative). Rule rewritten, two test cases added, A1–A3 checked. Commit handed over as S4; waiting for acceptance.
