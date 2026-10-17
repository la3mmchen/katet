# [P1] ACME-12: Rotate the leaked CI deploy token

> [!IMPORTANT]
> **Agents: keep this callout, never delete or shorten it.**
> This file is the main channel between the coding agent and the human.
> Questions, decisions, findings and human steps go here, in the sections
> below. Keep interaction in the chat minimal. Update Status, Updated
> and Agent logs every time you touch the task.

## Briefing

**Status:** waiting — answer: Q2
**Updated:** 2026-10-16 10:42 — rotation prepared in a branch; waits on Q2 (who owns the staging secret)

- ticket: ACME-12
- MR:
- branch: `fix/acme-12-rotate-deploy-token`
- repos:
  - `~/repos/acme/ci-config` — pipeline definitions, the token is referenced in `deploy/*.yml`
  - `~/repos/acme/infra` — the secret itself lives in `vault/ci/`
- related: ACME-9 (the onboarding checklist mentions the old token name)
- effort: high

## Chat

- **you** · 2026-10-16 09:05
  The deploy token from the CI runner showed up in a public paste. Please prepare the rotation,
  but do not touch production yourself.
- **agent** · 2026-10-16 09:14
  Understood. Found 4 places that reference `CI_DEPLOY_TOKEN`: three pipeline files in `ci-config`
  and the Vault path in `infra`. Rotation plan is under Agent plan; two things I cannot decide
  alone are in Open questions (Q1, Q2).
- **you** · 2026-10-16 10:30
  Q1 answered. For Q2 I need to check with the staging owner first.
- **agent** · 2026-10-16 10:42
  Noted — Q1 is done (Outcome below). The branch is ready for everything except staging; S2 and S3
  are yours once Q2 is settled.

## Task

The CI deploy token `CI_DEPLOY_TOKEN` was exposed in a public paste on 2026-10-15. Prepare its
rotation end to end: a new token in Vault, all pipeline references updated, the old token revoked
after the switch. The human applies anything that touches production.

## Constraints / non-goals

- don't rotate production credentials yourself — hand over the exact commands
- out of scope: rotating the unrelated registry token (separate ticket)

## Acceptance criteria

- [x] **A1** No pipeline file references the old token name — check: `git grep -n CI_DEPLOY_TOKEN deploy/` is empty
- [x] **A2** The new token exists in Vault under `ci/deploy-token-v2` — check: `vault kv get ci/deploy-token-v2` *(human)*
- [ ] **A3** The old token is revoked and a deploy still succeeds — check: pipeline `deploy-staging` green *(human)*

## Open questions

- [x] **Q1 — Keep the token name and rotate in place, or introduce `CI_DEPLOY_TOKEN_V2`?** (asked 2026-10-16)
  Options: a) same name, fewer edits · b) new name, no window where both tokens are live — recommendation b)
  **Answer:** b, new name please
  **Outcome:** 2026-10-16 10:40 — pipelines updated to `CI_DEPLOY_TOKEN_V2` in the branch; old name gone (A1).
- [ ] **Q2 — Who owns the staging Vault namespace, so the new secret can be written there?** (asked 2026-10-16)
  The `infra` README lists no owner; the last commit to `vault/ci/` is from a former colleague
  **Answer:**
  **Outcome:**

## Next steps

- [x] **S1** Review the branch `fix/acme-12-rotate-deploy-token` in `ci-config`
- [ ] **S2** Create the new token in Vault *(human)*
  ```bash
  vault kv put ci/deploy-token-v2 token="$(openssl rand -hex 32)"
  ```
- [ ] **S3** After the deploy succeeds, revoke the old token *(human)*
  ```bash
  vault kv delete ci/deploy-token
  ```

## Agent plan

1. Inventory every reference to the token (done).
2. Rename in the pipelines, keep the Vault path change for the human.
3. Hand over creation and revocation as commands; verify with A3.

## Agent logs

- 2026-10-16: Found 4 references (3 pipeline files, 1 Vault path). Asked Q1 and Q2.
- 2026-10-16: Q1 answered (new name). Updated `deploy/build.yml`, `deploy/staging.yml`, `deploy/prod.yml`; A1 checked. Waiting on Q2.
