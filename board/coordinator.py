#!/usr/bin/env python3
"""Coordinator: watch tasks/, hand each agentfile with a fresh human action to a headless run of
the configured coding agent (board/agents.py; Claude Code by default), one at a time. Started by `katet coordinator`; see the Coordinator section in KATET.md.

    katet coordinator            # live: dispatch workers
    katet coordinator --dry      # only report what it would dispatch

Human actions (the only triggers): an Answer filled or changed, an Outcome cleared, a message
from you as the last one in ## Chat, a Next step or *(human)* acceptance criterion ticked, a new
agentfile ending with your message (created on the board). Everything agents write
(Outcome, Updated, logs, status moves) is ignored, so workers never trigger each other.

State for the board: .coordinator/status.json (rewritten on every change) and
.coordinator/events.log (one line per event). Worker output: logs/<slug>.<stamp>.jsonl.
Settings (model, per-run budget, ticks, setting sources) live in coordinator.tools and are re-read
when it changes; a line that cannot be read keeps the last good settings and shows on the board.
COORDINATOR_MODEL / COORDINATOR_BUDGET override model and per-run budget.
Stdlib only.
"""
import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

BOARD = Path(__file__).resolve().parent
sys.path.insert(0, str(BOARD))
import serve   # noqa: E402  (parser and workspace lookup shared with the board)
import agents  # noqa: E402  (how to run, read and resume a coding agent)

REPO = serve.REPO                    # the workspace

STATE_DIR = REPO / ".coordinator"
STATUS = STATE_DIR / "status.json"
EVENTS = STATE_DIR / "events.log"
LOGS = REPO / "logs"
TOOLS = REPO / "coordinator.tools"   # allowed / denied tools for workers, editable by the human
QUIET = 10                           # seconds without file changes before acting (editor autosave)
RECENT = 20
PAUSE = 15 * 60                      # seconds without new runs after a worker hit a usage/rate limit or lost its login
LOOP_RUNS = 3                        # runs in a row started by changes made while a worker ran → hold the task

WORKER_RULES = """You were started unattended by the katet coordinator because the human acted in this agentfile.
Nobody reads this chat. Everything the human needs goes into the agentfile, following CLAUDE.md:
- move the task to in-progress first; when you stop, move it to waiting (answer/action/external)
  — finished work is `waiting — action: accept …` naming what to check; never done, only the human accepts;
- if you need a decision or input, write it as an Open question and move the task to waiting;
- if the last message in ## Chat is from the human, reply once at its end as
  `- **agent** · YYYY-MM-DD HH:MM` (take the time from `date '+%Y-%m-%d %H:%M'`, don't guess)
  with the body indented two spaces: what you did or the answer,
  short; decisions you need go to Open questions and the reply names them (pending Q7).
  Append after the last line of the section — never retype, move or duplicate the human's
  message: a copy of it after your reply reads as a new message and breaks the hand-over;
- write the Outcome line of every answered question you handle, also when the result is a
  follow-up question (name it) — an answered question without Outcome counts as unhandled;
- never commit, push, call AWS or kubectl, or install anything — write those as Next steps for
  the human instead (a commit step names repo and files, then the subject and the message as two
  separate fenced blocks, see CLAUDE.md);
- writes to outside systems (a ticket tracker, a wiki) only when the human asked for that exact
  write in this agentfile — in an Answer, a chat message or a ticked step — never on your own
  judgement, and the allow-list is the limit, not the invitation. Before creating, search for an
  existing item so nothing is created twice; afterwards put the key or link into the Briefing
  (`- ticket:`) and the Outcome or log line. Without that go-ahead, draft the content in the
  agentfile and ask;
- tools outside your allow-list are refused; when one is, record the step for the human instead
  of looking for a way around it.
Shell use — a command is allowed only if every part of it is, so:
- one command per shell call: no `&&`, `;`, pipes, `cd … &&`, variable assignments or heredocs;
- change files with your file-editing tools, never with sed, awk or python;
- move the task with exactly one call, from the workspace root, untracked files with plain mv:
  `git mv tasks/<from>/<slug>.md tasks/<to>/<slug>.md`
- before you finish, make sure the task is no longer in tasks/in-progress/."""


def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def mtime(path):
    """A file's mtime, or None while an editor is replacing it."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


# ---------------------------------------------------------------- change detection

def snapshot():
    """slug -> the fields that matter for triggers."""
    out = {}
    for folder in sorted(p for p in serve.TASKS.iterdir() if p.is_dir()):
        for p in sorted(folder.glob("*.md")):
            if not serve.is_agentfile(p):
                continue
            try:
                t = serve.parse(p)
            except Exception as e:  # half-written file: skip this round
                log_event(f"parse failed for {p.name}: {e}")
                continue
            out[t["slug"]] = {
                "slug": t["slug"], "abs": t["abs"], "folder": t["folder"], "title": t["title"],
                "chatKey": t["chatKey"],          # your message is the last one in ## Chat
                "questions": {q["id"]: {"answer": q["answer"], "outcome": q["outcome"], "done": q["done"]}
                              for q in t["questions"]},
                "steps": {s["text"]: s["done"] for s in t["nextSteps"]},
                "human_ac": {a["text"]: a["done"] for a in t["acceptance"] if a["human"]},
                "repos": [r["abs"] for r in t["repos"] if r["exists"]],
                "fields": {k: t["links"][k] for k in ("model", "effort", "budget") if t["links"].get(k)},   # the task's own worker settings
            }
    return out


def step_label(text):
    m = serve.re.match(r"^\*\*([SA]\d+)\*\*", text)
    return m.group(1) if m else "step “" + text[:40] + "”"


def human_actions(old, new, ticks=True):
    """List of trigger strings for one task, comparing two snapshot entries."""
    acts = []
    if new["chatKey"] and new["chatKey"] != old.get("chatKey"):
        acts.append("message")
    for qid, q in new["questions"].items():
        o = old["questions"].get(qid, {"answer": "", "outcome": "", "done": False})
        if q["answer"] and q["answer"] != o["answer"]:
            acts.append(f"{qid} answered")
        elif o["outcome"] and not q["outcome"] and not q["done"]:
            acts.append(f"{qid} reopened")
    if ticks:
        for text, done in new["steps"].items():
            if done and old["steps"].get(text) is False:
                acts.append(f"{step_label(text)} ticked")
        for text, done in new["human_ac"].items():
            if done and old["human_ac"].get(text) is False:
                acts.append(f"{step_label(text).replace('step “', 'criterion “')} ticked")
    return acts


def diff(old_snap, new_snap, ticks=True):
    """slug -> triggers. A new agentfile triggers only when it ends with a message from you
    (made on the board with "let an agent draft it"); agents only reply, so that is the human."""
    out = {}
    for slug, new in new_snap.items():
        if slug not in old_snap:
            if new["chatKey"]:
                out[slug] = ["new task"]
            continue
        if slug in old_snap:
            acts = human_actions(old_snap[slug], new, ticks)
            if acts:
                out[slug] = acts
    return out


# ---------------------------------------------------------------- status for the board

lock = threading.RLock()
status = {"coordinator": {}, "worker": None, "queue": [], "recent": [], "handed": {}, "stuck": {}, "loop": {}}


def log_event(msg):
    STATE_DIR.mkdir(exist_ok=True)
    line = f"{now()}  {msg}"
    try:
        print(line, flush=True)
    except OSError:  # the terminal is gone (SIGHUP); the log file still gets it
        pass
    try:
        with open(EVENTS, "a") as f:
            f.write(line + "\n")
    except OSError:  # a full disk must not take the coordinator down
        pass


_written = {"text": None}


def save_status(**coord):
    """Write status.json for the board — only when something in it changed (the loop calls this
    every two seconds)."""
    with lock:
        status["coordinator"].update(coord)
        text = json.dumps(status, indent=2, ensure_ascii=False)
        if text == _written["text"]:
            return
        STATE_DIR.mkdir(exist_ok=True)
        tmp = STATUS.with_suffix(".tmp")
        tmp.write_text(text)
        os.replace(tmp, STATUS)
        _written["text"] = text


# ---------------------------------------------------------------- workers

DEFAULTS = {"agent": agents.DEFAULT, "ticks": "yes", **agents.all_settings()}


def load_config():
    """coordinator.tools: `allow <rule>` / `deny <rule>` lines plus `<setting> <value>` lines.
    Raises ValueError for a line it cannot read."""
    allow, deny, conf = [], [], dict(DEFAULTS)
    for n, line in enumerate(TOOLS.read_text().splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        key, _, val = line.partition(" ")
        if key == "allow":
            allow.append(val.strip())
        elif key == "deny":
            deny.append(val.strip())
        elif key in conf:
            conf[key] = val.strip()
        else:
            raise ValueError(f"line {n}: unknown setting {key!r} (allow, deny, {', '.join(DEFAULTS)})")
    # env overrides the file (handy for tests)
    for key, env in (("model", "COORDINATOR_MODEL"), ("budget", "COORDINATOR_BUDGET")):
        conf[key] = os.environ.get(env, conf[key])
    agents.get(conf["agent"]).validate(conf)
    return allow, deny, conf


CONFIG = {"allow": [], "deny": [], "conf": dict(DEFAULTS), "mtime": None, "error": None}


def config():
    """coordinator.tools as last read successfully, re-read when it changes. A version that cannot
    be read (a typo while editing, the file missing for a moment while the editor saves) keeps the
    previous settings and is reported as configError, instead of stopping the coordinator."""
    try:
        mtime = TOOLS.stat().st_mtime
        if mtime != CONFIG["mtime"]:
            allow, deny, conf = load_config()
            CONFIG.update(allow=allow, deny=deny, conf=conf, mtime=mtime)
            if CONFIG["error"]:
                log_event("coordinator.tools reads fine again")
            CONFIG["error"] = None
    except (OSError, ValueError) as e:
        err = f"coordinator.tools: {e}"
        if err != CONFIG["error"]:
            log_event(f"{err} — keeping the last good settings until it is fixed")
        CONFIG["error"] = err
    return CONFIG


def agent():
    """The adapter for the configured agent."""
    return agents.get(config()["conf"]["agent"])


def previous_runs():
    """Run history across restarts: the last status.json, else rebuilt from logs/*.jsonl."""
    try:
        recent = json.loads(STATUS.read_text()).get("recent") or []
        if recent:
            return recent[:RECENT]
    except (OSError, ValueError):
        pass
    runs = []
    for log in sorted(LOGS.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:RECENT]:
        slug, _, stamp = log.stem.rpartition(".")
        try:
            started = datetime.strptime(stamp, "%Y%m%d-%H%M%S").astimezone().isoformat(timespec="seconds")
        except ValueError:
            continue
        entry = {"slug": slug, "title": slug, "triggers": [], "started": started, "log": str(log.relative_to(REPO)),
                 "pid": None, "sessionId": None, "lastActivity": None, "exit": None, "cost": None, "result": None,
                 "agent": agents.DEFAULT, "resume": None,
                 "ended": datetime.fromtimestamp(log.stat().st_mtime).astimezone().isoformat(timespec="seconds")}
        lines = log.read_text(errors="replace").splitlines()
        head = agents.log_header(lines[0]) if lines else None
        if head:
            entry["triggers"], entry["agent"] = head.get("triggers", []), head["agent"]
        try:
            a = agents.get(entry["agent"])
        except ValueError:
            a = agents.get(agents.DEFAULT)
        for line in lines[1:]:
            a.event(line, entry)
        entry["lastActivity"] = None
        if entry.get("isError") is not None:
            entry["exit"] = 1 if entry["isError"] else 0
        entry["resume"] = a.resume(entry["sessionId"]) if entry["sessionId"] else None
        runs.append(entry)
    return runs


def unhandled(task, handed):
    """Answered questions without an Outcome, not yet handed to a worker with this answer."""
    done = handed.get(task["slug"], {})
    acts = [f"{qid} answered" for qid, q in task["questions"].items()
            if q["answer"] and not q["outcome"] and not q["done"] and done.get(qid) != q["answer"]]
    if task["chatKey"] and done.get("chat") != task["chatKey"]:
        acts.append("message")
    return acts


def handed_covers(slug, act, task):
    """Whether the worker that just ran was given this action's content already. A second message
    or a changed answer during its run has new content and is not covered (it was dropped before)."""
    h = status["handed"].get(slug, {})
    if act == "message":
        return h.get("chat") == task["chatKey"]
    if act.endswith(" answered"):
        qid = act.split()[0]
        return h.get(qid) == task["questions"][qid]["answer"]
    return act in WORKER_TRIGGERS.get(slug, [])   # reopened: the same reopen, not a new one


def viewed():
    """Tasks being typed in on the board right now (its heartbeat lock)."""
    try:
        locks = json.loads((STATE_DIR / "viewing.json").read_text())
    except (OSError, ValueError):
        return set()
    return {slug for slug, v in locks.items() if v.get("until", 0) > time.time()}


def on_hold():
    """Tasks put on hold with the card's switch; stays until switched off (no heartbeat needed)."""
    try:
        return set(json.loads((STATE_DIR / "holds.json").read_text()))
    except (OSError, ValueError):
        return set()


EVENTS_MAX = 1_000_000   # bytes; beyond that the log is rotated to events.log.old at the next start


def spent_today():
    """What today's runs would have cost at API prices (events.log lines '… exited N, cost $X');
    shown only. Reads just the tail of the log: today's lines are at the end."""
    today, total = datetime.now().strftime("%Y-%m-%d"), 0.0
    try:
        with open(EVENTS, "rb") as f:
            f.seek(max(0, os.path.getsize(EVENTS) - 256_000))
            tail = f.read().decode(errors="replace")
        for line in tail.splitlines():
            if line.startswith(today) and ", cost $" in line:
                total += float(line.rsplit("$", 1)[1])
    except (OSError, ValueError):
        pass
    return total


def rotate_events():
    """Keep events.log bounded: above EVENTS_MAX it becomes events.log.old (one generation kept)."""
    try:
        if EVENTS.stat().st_size > EVENTS_MAX:
            os.replace(EVENTS, EVENTS.with_suffix(".log.old"))
    except OSError:
        pass


def prompt_for(task, acts):
    lines = [f"Work on the agentfile {task['abs']}"]
    if "manual" in acts:
        lines.append("I started this run by hand from the board: read the file and continue with what is open.")
    if task["chatKey"]:
        lines.append("Reply to my latest message in its Chat section.")
    answered = [a.split()[0] for a in acts if a.endswith(" answered")]
    reopened = [a.split()[0] for a in acts if a.endswith(" reopened")]
    ticked = [a[: -len(" ticked")] for a in acts if a.endswith(" ticked")]
    if answered:
        lines.append(f"I answered {', '.join(answered)} — process the answers and write the Outcome lines.")
    if reopened:
        lines.append(f"I reopened {', '.join(reopened)} (Outcome cleared) — act on the current answers again.")
    if ticked:
        lines.append(f"I ticked: {', '.join(ticked)}.")
    return "\n".join(lines)


def worker_cmd(task, prompt):
    """The command line for the configured agent, built by its adapter."""
    cfg = config()
    # never the workspace's own setup, and never inside a .git (hooks, config) — the task's repos
    # are writable; the adapter turns these into the agent's own denies
    setup = ["CLAUDE.md", "katet.conf", "coordinator.tools", "task.template.md", ".coordinator/**"]
    protected = [f"{REPO}/{f}" for f in setup] + [f"{d}/{f}" for d in [str(REPO), *task["repos"]] for f in (".git/**", ".claude/**")]
    a = agent()
    fields = task.get("fields") or {}
    overrides = a.overrides(fields)
    for k in fields:
        if k not in overrides:
            log_event(f"{task['slug']}: Briefing field `{k}: {fields[k]}` is not a valid worker setting — using the default")
    job = {"prompt": prompt, "rules": WORKER_RULES, "workspace": REPO, "repos": task["repos"],
           "protected": protected, "allow": list(cfg["allow"]), "deny": list(cfg["deny"]),
           "conf": cfg["conf"], "overrides": overrides, "name": task["slug"]}
    return a.command(job)


def run_worker(task, acts, cmd):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    LOGS.mkdir(exist_ok=True)
    logfile = LOGS / f"{task['slug']}.{stamp}.jsonl"
    a = agent()
    entry = {"slug": task["slug"], "title": task["title"], "triggers": acts, "started": now(),
             "log": str(logfile.relative_to(REPO)), "pid": None, "sessionId": None, "agent": a.name, "binary": a.binary, "resume": None,
             "lastActivity": None, "exit": None, "ended": None, "cost": None, "result": None}
    proc = None
    try:
        with open(logfile, "w") as out:
            # first line: who ran and why; the rest is the agent's own output, read by its adapter
            out.write(json.dumps({"type": "katet", "agent": a.name, "cmd": cmd[:2] + ["…"], "triggers": acts}) + "\n")
            out.flush()
            proc = subprocess.Popen(cmd, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, text=True, start_new_session=True)
            entry["pid"] = proc.pid
            with lock:
                status["worker"] = entry
            save_status(state="working")
            ov = a.overrides(task.get("fields") or {})
            log_event(f"started worker on {task['slug']} ({', '.join(acts)}) pid {proc.pid} → {entry['log']}"
                      + (" [" + ", ".join(f"{k} {v}" for k, v in ov.items()) + "]" if ov else ""))
            for line in proc.stdout:
                out.write(line)
                out.flush()
                if a.event(line, entry):
                    if entry["sessionId"] and not entry["resume"]:
                        entry["resume"] = a.resume(entry["sessionId"])
                    save_status()
            proc.wait()
        entry.update(exit=proc.returncode, ended=now())
    except Exception as e:   # the reader must not die silently: the run would look alive forever
        log_event(f"worker on {task['slug']}: {type(e).__name__}: {e}")
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                pass
        entry.update(exit=-1, ended=now(), isError=True, result=f"{type(e).__name__}: {e}"[:400])
    with lock:
        status["worker"] = None
        status["recent"] = [entry] + status["recent"][: RECENT - 1]
    save_status(state="watching")
    log_event(f"worker on {task['slug']} exited {entry['exit']}"
              + (f", cost ${entry['cost']:.2f}" if isinstance(entry["cost"], (int, float)) else ""))
    if entry.get("failure"):
        # the next run would fail the same way: pause, and let the main loop queue this one again
        until = time.time() + PAUSE
        with lock:
            RETRY.append((task["slug"], acts))
        save_status(paused={"until": datetime.fromtimestamp(until).astimezone().isoformat(timespec="seconds"),
                            "reason": entry["failure"], "slug": task["slug"]})
        log_event(f"PAUSED: worker on {task['slug']} hit a limit — {entry['failure'][:120]} — no new runs for "
                  f"{PAUSE // 60} minutes, this run is queued again; a run started by hand on the board ignores the pause")



# ---------------------------------------------------------------- main loop

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry", action="store_true", help="report what would be dispatched, start nothing")
    ap.add_argument("--quiet", type=float, default=QUIET, help=f"seconds of no changes before acting (default {QUIET})")
    args = ap.parse_args()

    STATE_DIR.mkdir(exist_ok=True)
    rotate_events()
    pidfile = STATE_DIR / "coordinator.pid"
    old = pidfile.read_text().strip() if pidfile.exists() else ""
    # the pid must still be a coordinator: after a crash and a reboot the number belongs to someone else
    if old and old != str(os.getpid()) and serve.pid_alive(old, "coordinator.py"):
        sys.exit(f"coordinator already running (pid {old})")
    pidfile.write_text(str(os.getpid()))
    try:
        conf = load_config()[2]  # a broken coordinator.tools stops the start; edits while running are tolerated (config())
    except (OSError, ValueError) as e:
        sys.exit(f"coordinator.tools: {e}")
    a = agents.get(conf["agent"])
    if not args.dry and not shutil.which(a.binary):
        sys.exit(f"agent {a.name!r} needs `{a.binary}` on PATH")

    changes = threading.Event()
    watcher = subprocess.Popen(["fswatch", "-r", "--latency", "1", "-e", r"\.board-tmp$", str(serve.TASKS)],
                               stdout=subprocess.PIPE, text=True)
    threading.Thread(target=lambda: [changes.set() for _ in watcher.stdout], daemon=True).start()

    stopping = {"n": 0}

    def stop(*_):
        stopping["n"] += 1
        w = status["worker"]
        if stopping["n"] == 1 and w:
            log_event(f"stopping after the running worker on {w['slug']} (ctrl-c again to kill it)")
            save_status(state="stopping")
        else:
            if w and w.get("pid"):
                log_event(f"killing worker on {w['slug']} (pid {w['pid']})")
                try:
                    os.killpg(w["pid"], signal.SIGTERM)
                except OSError:
                    pass
            stopping["n"] = 99
        changes.set()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    if signal.getsignal(signal.SIGHUP) != signal.SIG_IGN:   # closing the terminal stops it cleanly; nohup (the app) keeps ignoring it
        signal.signal(signal.SIGHUP, stop)

    code = {p: mtime(p) for p in (Path(__file__).resolve(), BOARD / "serve.py", BOARD / "agents.py")}
    base = snapshot()
    with lock:
        status["recent"] = previous_runs()
        try:
            prev = json.loads(STATUS.read_text())
            status["handed"], status["stuck"], status["loop"] = prev.get("handed") or {}, prev.get("stuck") or {}, prev.get("loop") or {}
        except (OSError, ValueError):
            status["handed"], status["stuck"], status["loop"] = {}, {}, {}
    queue = {}          # slug -> triggers, insertion order = FIFO
    # created on the board while no coordinator ran: still in open/ with your message
    for slug, t in base.items():
        if slug in status["loop"]:
            continue  # held since its last run (possible loop): your next action on it lifts that
        if t["folder"] == "open" and t["chatKey"] and status["handed"].get(slug, {}).get("chat") != t["chatKey"]:
            queue[slug] = ["new task"]
        # answers given while no coordinator ran (or while the task was locked)
        if t["folder"] not in ("in-progress", "done"):
            acts = unhandled(t, status["handed"])
            if acts:
                queue.setdefault(slug, [])
                queue[slug] += [a for a in acts if a not in queue[slug]]
    running = {}        # slug -> (thread, snapshot at start)
    rerun = {}          # slug -> runs in a row that were started by changes made while its worker ran
    with lock:
        status.update(worker=None, queue=[])
    save_status(pid=os.getpid(), started=now(), mode="dry" if args.dry else "live", state="watching",
                quietSeconds=args.quiet, lastEvent=None, stopped=None, paused=None)
    log_event(f"coordinator started ({'dry' if args.dry else 'live'}), agent {a.name}, watching {serve.TASKS}, {len(base)} agentfiles")
    for slug in queue:
        log_event(("would dispatch" if args.dry else "queued") + f" {slug} ({', '.join(queue[slug])}) — from before this start")
    if args.dry:
        queue.clear()

    try:
        while stopping["n"] < 99:
            changes.wait(timeout=2)
            if changes.is_set():
                # debounce: wait until the files are quiet
                while changes.is_set() and stopping["n"] < 99:
                    changes.clear()
                    time.sleep(args.quiet)
                cur = snapshot()
                ticks = config()["conf"]["ticks"].lower() not in ("no", "false", "off")
                for slug, acts in diff(base, cur, ticks).items():
                    if slug in running:
                        continue  # judged when its worker ends
                    rerun.pop(slug, None)   # you acted while no worker ran: not a loop
                    if status["loop"].pop(slug, None):
                        log_event(f"{slug}: you acted on it — loop hold lifted")
                    if cur[slug]["folder"] == "in-progress":
                        log_event(f"skip {slug} ({', '.join(acts)}): in-progress, someone is on it")
                        continue
                    queue.setdefault(slug, [])
                    queue[slug] += [a for a in acts if a not in queue[slug]]
                    save_status(lastEvent={"at": now(), "slug": slug, "triggers": acts})
                    log_event(("would dispatch" if args.dry else "queued") + f" {slug} ({', '.join(acts)})")
                for slug, t in cur.items():
                    was = base.get(slug)
                    if (was and was["folder"] == "in-progress" and t["folder"] not in ("in-progress", "done")
                            and slug not in running and slug not in queue and slug not in status["loop"]):
                        acts = unhandled(t, status["handed"])
                        if acts:
                            queue[slug] = acts
                            log_event(("would dispatch" if args.dry else "queued") + f" {slug} ({', '.join(acts)}): left in-progress with unhandled answers")
                for slug in list(running):
                    base[slug] = running[slug][1]  # keep the start state until the worker is done
                base.update({s: v for s, v in cur.items() if s not in running})
                if args.dry:
                    queue.clear()

            # a finished worker: judge its task against the state at its start, ignoring ticks. What the
            # worker was given is not new; a second message or a changed answer during its run is.
            for slug, (th, start) in list(running.items()):
                if not th.is_alive():
                    del running[slug]
                    cur = snapshot()
                    if slug in cur:
                        acts = [a for a in human_actions(start, cur[slug], ticks=False) if not handed_covers(slug, a, cur[slug])]
                        if acts:
                            rerun[slug] = rerun.get(slug, 0) + 1
                            if rerun[slug] >= LOOP_RUNS:
                                # a worker whose own edits read as your actions would start itself forever
                                with lock:
                                    status["loop"][slug] = {"at": now(), "runs": rerun[slug], "triggers": acts}
                                log_event(f"LOOP? {slug}: {rerun[slug]} times in a row a run was started by changes made while its "
                                          f"worker ran ({', '.join(acts)}) — held until you act on it; check its Chat for "
                                          "text written by an agent that reads as yours")
                            else:
                                queue[slug] = acts
                                log_event(f"queued {slug} again ({', '.join(acts)}): changed while its worker ran")
                        else:
                            rerun.pop(slug, None)
                        if cur[slug]["folder"] == "in-progress":
                            with lock:
                                status["stuck"][slug] = now()
                            log_event(f"STUCK {slug}: its worker ended but left it in tasks/in-progress/ — "
                                      "move it on (board shows how); until then your edits there are skipped")
                        base[slug] = cur[slug]
                    WORKER_TRIGGERS.pop(slug, None)

            with lock:
                status["queue"] = [{"slug": s, "triggers": a} for s, a in queue.items()]
                for slug in list(status["stuck"]):
                    if base.get(slug, {}).get("folder") != "in-progress":
                        del status["stuck"][slug]
            save_status()

            # runs asked for by hand on the board: a marker file per task, queued ahead of the rest,
            # not held back by a hold or a loop flag (you asked explicitly)
            for marker in sorted((STATE_DIR / "triggers").glob("*")):
                slug = marker.name
                marker.unlink(missing_ok=True)
                if slug.startswith("."):   # .DS_Store and friends are not tasks
                    continue
                if slug in running:
                    log_event(f"{slug}: run requested, a worker is already on it")
                    continue
                if base.get(slug, {}).get("folder") == "in-progress":
                    log_event(f"{slug}: run requested, but it is in-progress (someone is on it)")
                    continue
                rerun.pop(slug, None); status["loop"].pop(slug, None); MANUAL.add(slug)
                acts = ["manual"] + [a for a in queue.get(slug, []) if a != "manual"]
                queue = {slug: acts, **{k: v for k, v in queue.items() if k != slug}}   # first in line
                log_event(f"queued {slug} (manual): run requested on the board")

            # one worker at a time (Q6)
            save_status(spentToday=round(spent_today(), 2), configError=config()["error"])
            # runs that failed on a limit come back into the queue; the pause keeps them waiting
            with lock:
                retries, RETRY[:] = list(RETRY), []
            for slug, acts in retries:
                if slug not in running and slug not in queue:
                    queue[slug] = acts
            paused = status["coordinator"].get("paused")
            if paused:
                try:
                    paused_until = datetime.fromisoformat(paused["until"]).timestamp()
                except (KeyError, ValueError):
                    paused_until = 0
                if time.time() >= paused_until:
                    save_status(paused=None)
                    log_event("pause over — runs start again")
                    paused = None
            holds = on_hold()
            held = (viewed() | holds) & set(queue) - MANUAL
            if paused:
                held |= set(queue) - MANUAL   # everything waits for the pause, except what you start by hand
            for slug in held - HOLDING:
                log_event(f"holding {slug}: " + ("on hold — starts when you switch the hold off on its card" if slug in holds
                                                 else "you are typing in it on the board — starts when you send or leave the field"))
            HOLDING.clear(); HOLDING.update(held)
            save_status(holding=sorted(held))
            ready = [sl for sl in queue if sl not in held]
            if not running and ready and stopping["n"] == 0 and not args.dry:
                slug = ready[0]
                acts = queue.pop(slug)
                MANUAL.discard(slug)
                cur = snapshot()
                task = cur.get(slug)
                if not task:
                    log_event(f"drop {slug}: agentfile gone")
                    continue
                if task["folder"] == "in-progress":
                    log_event(f"skip {slug}: moved to in-progress meanwhile")
                    continue
                cmd = worker_cmd(task, prompt_for(task, acts))
                with lock:
                    status["handed"][slug] = {qid: q["answer"] for qid, q in task["questions"].items() if q["answer"]}
                    if task["chatKey"]:
                        status["handed"][slug]["chat"] = task["chatKey"]
                WORKER_TRIGGERS[slug] = acts
                th = threading.Thread(target=run_worker, args=(task, acts, cmd), daemon=True)
                running[slug] = (th, task)
                th.start()

            if stopping["n"] >= 1 and not running:
                break

            # own code changed (coordinator.py, the shared parser or the agent adapters): restart once idle, queue empty
            if not running and not queue and any(mtime(p) not in (m, None) for p, m in code.items()):
                log_event("code changed, restarting")
                watcher.terminate()
                save_status(state="restarting")
                os.execv(sys.executable, [sys.executable] + sys.argv)
    finally:
        watcher.terminate()
        save_status(state="stopped", stopped=now(), pid=None)
        log_event("coordinator stopped")
        try:
            pidfile.unlink()
        except FileNotFoundError:
            pass


WORKER_TRIGGERS = {}
HOLDING = set()
MANUAL = set()      # slugs queued by hand from the board: not held back
RETRY = []          # (slug, acts) of runs that failed on a limit, to be queued again by the main loop

if __name__ == "__main__":
    main()
