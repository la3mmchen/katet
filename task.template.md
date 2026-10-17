<!--
One file per bigger task: tasks/<state>/<short-slug>.md (start in tasks/open/)
Plans that outgrow ~50 lines: plans/<short-slug>.plan.md
Lifecycle: update the file while you work on it; the folder always matches Status
(open | in-progress | waiting | done), so a status change is a git mv.
The HTML comments may go once filled in; the callout below stays.
-->
# [P1..3] title

> [!IMPORTANT]
> **Agents: keep this callout, never delete or shorten it.**
> This file is the main channel between the coding agent and the human.
> Questions, decisions, findings and human steps go here, in the sections
> below. Keep interaction in the chat minimal. Update Status, Updated
> and Agent logs every time you touch the task.

## Briefing

<!--
The task at a glance: facts and pointers only, no prose (that goes in Task).
One field per line, value empty when unknown. Fixed fields first, in this order;
task-specific ones (audit page, module, plan, …) follow. Optional worker settings for this
task only: `- model: opus`, `- effort: high`, `- budget: 2.00`.
-->

**Status:** open | in-progress | waiting — answer|action|external: <what> | done
**Updated:** YYYY-MM-DD HH:MM — one sentence on where things stand

- ticket:
- MR:
- branch:
- repos:
  - `~/repos/<org>/<repo>` — one sub-bullet per repo; notes after the dash
- related:
- plan: `plans/<slug>.plan.md`

## Chat

<!--
Conversation between the human and the agent, newest last. One message per item:
- **you** · YYYY-MM-DD HH:MM            (the human)
  body indented two spaces, markdown
- **agent** · YYYY-MM-DD HH:MM          (the agent's reply)
Each side only appends. The human clears it when a topic is done. Usually empty.
-->

## Task

<!-- what and why; point to relevant files/modules -->

## Constraints / non-goals

<!-- what not to touch, what's out of scope; delete if truly none -->

- don't ...
- out of scope: ...

## Acceptance criteria

<!--
Describe the end state, not the code change ("all hosts reconcile green",
not "script edited"). Add the check that proves it where possible.
Mark steps only a human can do with *(human)*.
Tick only what was actually verified.
-->

- [ ] **A1** concrete and measurable — check: `command or observation`
- [ ] **A2** ... *(human)*


## Open questions

<!--
Agent writes here instead of guessing; human answers inline.
One question per box, Qn ids in asking order. Question = one line ending in "?";
options/recommendation one line below; Answer is the human's, Outcome the agent's
single dated line after acting. Answered questions stay here as the decision record.
-->

- [ ] **Q1 — Question?** (asked YYYY-MM-DD)
  Options: a) … · b) … — recommendation and why, one line
  **Answer:**
  **Outcome:**

## Next steps

<!--
What's left for a human, in the order it has to happen, one action per
line, imperative, with the command or check where there is one. Commands
go in a fenced block with a language tag, indented two spaces under the item:
- [ ] **S1** Apply `infra/network` — check: `terraform plan` shows no changes
  ```bash
  cd infra/network && terraform apply
  ```
Each item gets a stable id S1, S2, … (next free number, never renumbered
or reused). No history or reasoning in an item — that goes in Agent logs or
Open questions. When a decision changes an item, rewrite it, don't append.
-->

## Agent plan

<!--
Bigger tasks only, delete this section otherwise.
Longer than ~50 lines: move it to plans/<slug>.plan.md and link it above.
-->

## Agent logs

<!--
Short, human readable, newest last: actions, findings, results.
- YYYY-MM-DD: what happened, what it means
-->
