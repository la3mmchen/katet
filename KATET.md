# Working in a katet workspace

This workspace holds task files, not code. Each bigger task gets one file (an **agentfile**)
that is the main channel between the coding agent and the human. These are the framework's rules;
what is specific to this workspace (where repos live, the ticket system, commit habits) is in the
workspace's own `CLAUDE.md`, below the line that imports this file.

## Task files

- Location: `tasks/<state>/<short-slug>.md`, created from `task.template.md`.
  The folder is the status: it always matches the `**Status:**` line.
- Companion files (`<slug>.tickets.md`, payloads, …) live next to their agentfile
  and move with it. Plans and notes stay in `plans/` and `notes/`.
- Refer to another agentfile by bare filename (`acme-42-landing-zone.md`), never by
  path or relative link — its folder changes with its status.

## States

| State | Folder | Means | Whose turn |
|---|---|---|---|
| open | `tasks/open/` | Created, nothing done yet | agent, when picked up |
| in-progress | `tasks/in-progress/` | An agent is working on it **right now** (also the coordinator's lock) | agent |
| waiting | `tasks/waiting/` | Blocked: work can't continue until something happens — kind below | human / external |
| done | `tasks/done/` | Accepted or closed; no open questions or next steps left | nobody |

- Status line format: `**Status:** <state>`, plus ` — <detail>` for `waiting`.
- `waiting` always names its kind first, then what exactly it waits on:
  - `waiting — answer: Q1–Q3` — questions or input from the human
  - `waiting — action: S37 break-glass apply` — a step only the human can do (apply, merge, run AWS calls)
  - `waiting — external: Spacelift run log` — someone or something outside (CI, another team)
  If several apply, name the one that blocks next.
- Finished work goes to `waiting — action: accept …`, naming what to check
  (`waiting — action: accept — retention reads 30 (S6)`), never to `done`: only the human's
  acceptance makes it `done` (the board's **Accept → done** button does the move). Findings from that check bring it back to `in-progress` when an agent
  picks them up.
- Changing the status means moving the file in the same edit:
  `git mv tasks/<old>/<slug>.md tasks/<new>/<slug>.md` (plus companions; plain `mv`
  if untracked). Log the move in Agent logs.
- Check: `grep -H '^\*\*Status:' tasks/*/*.md` — each file's state equals its folder.
- Tasks created on the board's **New task** button land in `tasks/open/` with the human's
  request under Task. With "let an agent draft it", a Chat message asks you to draft the
  agentfile (Briefing, Task, Constraints, Acceptance criteria, Open questions, Next steps)
  without starting the work, then move it to `waiting — answer: …` or `waiting — action: check the draft`.
- When the human asks you to work on a specific task, move it to `in-progress` first,
  before any other work on it, whatever state it is in (`done` included: it is reopened).
  When you stop, move it on to the state that fits (`waiting — answer: …`, `waiting —
  action: …`, `waiting — action: accept …`); don't leave a task in `in-progress` that you are no longer
  working on. Agents never move a task to `done` on their own — the human accepts it.
- Plans longer than ~50 lines: `plans/<short-slug>.plan.md`, linked from the task file.
- Every agentfile opens, right after the callout, with **`## Briefing`**: the task at a
  glance. It holds the `**Status:**` line and the `**Updated:**` line — local date and time,
  `**Updated:** 2026-10-12 13:45 — one sentence on where things stand` — then one `- key: value` field
  per line — fixed fields first, in this order: `ticket`, `MR`, `branch`, `repos`, `related`
  (other agentfiles, tickets, pages); task-specific fields (`audit page`, `module`, `plan`, …)
  after. Keep a fixed field with an empty value when unknown. Facts and pointers only — the
  prose belongs in Task. There is no separate Links section. Three optional fields steer the
  worker for this task alone: `- model: opus`, `- effort: high` (low, medium, high, xhigh, max),
  `- budget: 2.00`; without them the settings in `coordinator.tools` apply. When a task is accepted, the board adds
  `- worker usage:` — runs, turns, time, tokens and the notional cost of every worker run on it.
- Repos a task works in are listed in the **Briefing** as `- repos:` with one sub-bullet per
  repo, even when there is only one:
  ```
  - repos:
    - `~/repos/acme/landing-zone` — main @ `d019062`
    - `~/repos/acme/argocd` — deploys it via `managed-apps/*/observability.yaml`
  ```
  The path comes first, in backticks, absolute or `~/`, to the repo root (or the subfolder
  the task lives in); anything else follows after ` — `. The board's editor button opens these.
- The `> [!IMPORTANT]` callout at the top of every task file is mandatory.
  Never delete, shorten or reword it. If it is missing, restore it from the template.
- The HTML comments in the template are guidance and may be removed once a section is filled in.

## Rules for agents

- Read the task file before doing anything else, and re-read Open questions
  before acting on something you had to guess about.
- **Chat** (right under the Briefing) is the conversation between the human and you: latest
  prompt, updates, questions. Read it first; the newest messages override older plans in the file.
  Format — one message per top-level item, body indented two spaces, newest last:
  ```
  - **you** · 2026-10-12 15:40
    Please also check the dev unit.
  - **agent** · 2026-10-12 15:44
    Checked: the dev unit is untouched. Details in Agent logs.
  ```
  Each side only appends: you never edit or delete the human's messages, the human never yours.
  Text in the section that is not in this form (typed in an editor) is a message from the human.
  If the last message is the human's, reply once at the end — what you did or the answer, short.
  Decisions you need go to Open questions as usual; the reply names them ("pending Q7").
  The human clears the chat when a topic is done. If the section is missing, add an empty
  `## Chat` between the Briefing and `## Task` (older files call it `## Instructions`).
- Write questions into Open questions instead of guessing. Answered questions stay
  there as the decision record. Format, one block per question:
  ```
  - [ ] **Q7 — One question, one line, ends with a question mark?** (asked YYYY-MM-DD)
    Options or recommendation, one line, two at most · longer context goes to Agent logs
    **Answer:**
    **Outcome:**
  ```
  One question per box — tickable when exactly one answer closes it; never bundle
  (1)…(4) into one item. `Qn` ids in asking order, referenced from Next steps and logs
  as "pending Q7". **Answer** belongs to the human and is never edited or extended by
  the agent. **Outcome** is the agent's single dated line after acting; details go to
  Agent logs. Answered questions keep their place and order. If the human empties an
  Outcome and unticks the box, the question is reopened: act on the current Answer and
  write a new Outcome (the old one stays in git history).
- Update **Status**, **Updated** (with the current time) and **Agent logs** every time you
  touch the task, in the same edit as the work itself. The board sorts its columns by the file's
  last modification, so this needs no extra field.
- Acceptance criteria describe the end state with a proving check. Tick a box only
  after you ran the check and saw it pass. Steps marked *(human)* are never ticked by an agent.
  Each criterion starts with a stable id, `- [ ] **A3** …`, like `Sn` and `Qn`: next free number,
  never renumbered or reused. Reference them as "A3" — "AC n" is kept for a ticket's own numbering
  (e.g. "ticket AC 4").
- Next steps is the human's to-do list: ordered, imperative, one action per line, with the
  command or check where there is one. No history, no reasoning, no findings — an item says
  what to do, not why or what happened. Explanations belong in Agent logs or Open questions.
  When a decision changes an item, rewrite the item; don't append the story to it.
  Each item starts with a stable id, `- [ ] **S4** Do the thing`, like the `Qn` ids:
  next free number for a new item, never renumbered or reused, kept when ticked or
  reordered. The board shows items sorted by number, so the number is the order: a new item
  gets the next number and goes at the end; if it must happen before an existing item, say so in
  its text ("before S8: …"). Reference items as "S4" from logs, questions and chat.
  No step for answering a question (`Answer Q3`): open questions are the human's list already and
  the board shows them; a step that depends on an answer says so in its text ("after Q3: …"). Older
  steps of that kind are ticked by the board itself once the answers are saved.
  A step never carries what failed or was tried, no "(local validate fails on AssumeRole, S9)",
  and at most one check. Not this:
  ```text
  - [ ] **S11** Verify the units on the Draft MR (local validate fails on AssumeRole, S9) — check: the plan job shows an Amplify app + branch each for variant-a/ and variant-b/; check meta passes the name validation
  ```
  but this, with the failed local validate as a line in Agent logs:
  ```text
  - [ ] **S11** Check the Draft MR's plan job: one Amplify app and branch each for `variant-a/` and `variant-b/`
  - [ ] **S12** Check the plan job's meta output: `variant-a` passes the meta module's name validation
  ```
  Open steps are kept true every time the task is touched: a "before S6: …" note goes when S6 is
  ticked, and a step whose work may already be done (the human pushed, ran, created it in passing)
  is checked with the read-only tools — `git log`, `ls`, a read of the page — and ticked with the
  evidence in Agent logs, never carried along with "if pushed".
- Anything the human is meant to run or paste — commands, queries, config — goes in a
  fenced block with a language tag (` ```bash `, ` ```sql `, ` ```yaml `; ` ```text ` if
  none fits), never inline backticks or bare text. Under a list item (Next steps), indent
  the fence two spaces so it stays part of the item. Inline backticks stay for names:
  files, flags, IDs. The board puts a copy button on every fenced block.
- When your work on a task is done and something needs committing, the Next step for it names the
  repo and the exact files to stage, then gives the subject and the message as **two separate
  fenced blocks**, so each has its own copy button. State the result — what the commit changes and
  why, not how you got there:
  ````markdown
  - [ ] **S12** Commit the retention change — repo `~/repos/acme/infrastructure`,
    files: `prod/billing/rds/terragrunt.hcl`
    subject:
    ```text
    ACME-249: raise billing-rds-prod backup retention to 30 days
    ```
    message:
    ```text
    Sets backup_retention_period = 30 in the prod unit; dev and QA unchanged.
    ```
  ````
  Files: exact paths, never "everything". Subject: imperative, at most ~72 characters, following
  the repo's own convention (check `git log --oneline -10`: ticket prefix, conventional-commit type).
  Message: one or two lines on the result and anything a reviewer must know. One step per logical
  change. No `git` commands — the human commits from their own tool. The workspace itself is
  committed by the human, never by katet or an agent.
- Keep chat interaction minimal. Anything the human needs later belongs in the file.

## Board

`katet board` starts the board in the browser (http://127.0.0.1:8765; `--port N`, `--no-open`;
the port and the editor buttons are set in `katet.conf`). It reads the agentfiles live and
writes only the human's edits made on it. Its **Settings** tab shows the workspace's `katet.conf`,
`coordinator.tools` and `CLAUDE.md`, read-only. The same board also runs as a Mac app with menu-bar
status and notifications: `katet app build`.

## Coordinator

`katet coordinator` watches `tasks/` and starts a headless run of the configured coding agent on an
agentfile when the human acts in it: an Answer filled or changed, an Outcome cleared,
a chat message from the human as the last one in Chat, a Next step or *(human)* acceptance
criterion ticked. A new agentfile ending with a message from the human (made with **New task**) triggers too, also when it was
created while no coordinator ran. An answered question without Outcome, and a chat message
without reply, count as unhandled: they are
picked up at startup and when its task leaves `in-progress`, so answers never get lost to timing —
workers therefore always write the Outcome. Agent edits never trigger a run. One worker at a time; a task in `tasks/in-progress/` is
skipped (someone is on it).

- Start: `katet coordinator` (live) or `katet coordinator --dry` (report only). Stop: ctrl-c once
  (or closing the terminal) waits for the running worker, twice kills it. Needs `fswatch` and the agent's CLI
  (`agent` in `coordinator.tools`; Claude Code is built in, other agents attach as adapters in `board/agents.py`).
- Watch it: the board's **Coordinator** view, or `tail -f .coordinator/events.log`. Every run
  logs to `logs/<slug>.<stamp>.jsonl`; the board shows the command that resumes a run interactively.
- What workers may do: `coordinator.tools` (allow/deny rules, permission mode `dontAsk`: anything
  unlisted is refused). No commit, push, AWS, kubectl or installs, no edits to the workspace's setup
  (`CLAUDE.md`, `katet.conf`, `coordinator.tools`, `task.template.md`, `.claude/`, `.coordinator/`) and none inside any
  `.git/` or `.claude/`; git's own escape hatches (`-c`, `--exec-path`, `--output`) are denied too. Writes to outside
  systems (tickets, wiki) only if the tool is allowed there *and* you asked for that exact write in
  the agentfile — an Answer, a chat message or a ticked step; a worker never decides that alone.
- Settings, also in `coordinator.tools`: `agent` (the coding agent), worker `model`, `effort`, `budget` per run, `ticks` (whether ticking
  starts a run), `setting-sources` (workers skip your user settings, so no personal plugins or
  hooks). Re-read when the file changes — no restart needed; a line it cannot read keeps the last
  good settings and shows on the board.
- A task you are working in on the board is held: while the focus is in one of its fields or an
  unsent draft exists, and for half a minute after a tick or a saved answer, so a row of them becomes
  one run. A sent chat message does not wait. (The board sends a heartbeat; a lock without one
  expires after 20 s.) The card's **Hold workers** switch puts a task on hold for as
  long as you leave it on, card open or closed; the board marks it "on hold". A worker that is
  already running is not stopped — the panel says so.
- The Coordinator tab's **Start worker** button queues a run by hand, first in line; a hold or a loop
  flag does not stop a run you asked for.
- A worker that fails on the account's usage limit, a rate limit or an expired login pauses new runs
  for 15 minutes; the failed run is queued again, the board and the app say why. A run you start by
  hand ignores the pause.
- A worker that ends with its task still in `tasks/in-progress/` leaves it **stuck**: the coordinator
  logs it and the board flags it with the command to move it on. Workers therefore use one shell
  command per call (a chained command is refused if any part is not allowed), edit files only
  with Edit/Write, and move the task with a single `git mv` (plain `mv` if untracked).
- Loop guard: when three times in a row a run on one task was started by changes made while its
  worker ran (typically a reply the parser reads as yours), the task is held and flagged on the
  board until you act on it there.
- As a worker you run unattended: nobody reads your chat. Put everything into the agentfile,
  ask through Open questions and move the task to `waiting` instead of stopping to ask.
