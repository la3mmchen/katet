# Demo workspace

Three fictional tasks of a small platform team, to see the board with something on it:

- `tasks/open/acme-9-onboarding-checklist.md` — just created on the board, waits for an agent to draft it
- `tasks/waiting/acme-12-rotate-ci-token.md` — in progress with the human: one question answered, one open, steps for the human
- `tasks/waiting/acme-27-nightly-backup-alert.md` — finished work with a commit hand-over, waits for acceptance

Open it in the app (**Workspace › Open Workspace…**, or drop this folder on the Dock icon), or in a terminal:

```bash
cd examples/demo-workspace
katet board            # http://127.0.0.1:8766 — the port is set in katet.conf so it does not clash with yours
```

Answering, ticking and chatting write into these files; `git checkout -- .` puts the demo back.
Starting the coordinator here would run real agents on fictional repos, so leave it off.
