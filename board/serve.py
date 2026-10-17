#!/usr/bin/env python3
"""The board for a katet workspace (its tasks/ folder).

    katet board                  # serves http://127.0.0.1:8765 and opens it
    katet board --port 9000 --no-open

The workspace is $KATET_WORKSPACE (set by bin/katet from --workspace) or the nearest
folder above the current one that holds katet.conf. This file also carries the parser
and the workspace lookup that the coordinator imports.

Stdlib only. Files are parsed on every request, so edits show up on the next poll.

The only writes are the human's actions on the page: answering a question, clearing an Outcome
(which also unticks it), ticking Next steps / *(human)* acceptance criteria, sending or clearing
## Chat messages, setting a task's worker model/effort, accepting a task (moved to done), creating
a task, putting one on hold or asking for a run, plus the typing heartbeat. Each file edit is
refused (409) if the file changed since the page last read it, so an agent's concurrent edit is
never overwritten.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import textwrap
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

BOARD = Path(__file__).resolve().parent
FRAMEWORK = BOARD.parent

CONF_DEFAULTS = {"port": "8765", "editor": "zed", "ticket-url": "",
                 "repo-hint": "~/repos/<org>/<repo>"}


def find_workspace():
    """$KATET_WORKSPACE, else the nearest folder above the cwd with an katet.conf."""
    env = os.environ.get("KATET_WORKSPACE")
    cands = [Path(env).expanduser()] if env else [Path.cwd(), *Path.cwd().parents]
    for c in cands:
        if (c / "katet.conf").is_file():
            return c.resolve()
    sys.exit("no workspace found: run inside one (a folder with katet.conf), pass --workspace DIR, "
             "or create one with `katet init DIR`")


REPO = find_workspace()
TASKS = REPO / "tasks"


def conf():
    """katet.conf: `key value` lines. Unknown keys are ignored, missing ones take the defaults."""
    out = dict(CONF_DEFAULTS)
    try:
        for line in (REPO / "katet.conf").read_text().splitlines():
            line = re.sub(r"\s+#.*$", "", line).strip()
            if line and not line.startswith("#"):
                k, _, v = line.partition(" ")
                if k in out:
                    out[k] = v.strip()
    except OSError:
        pass
    return out
STATES = ["open", "in-progress", "waiting", "done"]
SERVABLE = {".md", ".json"}

CHECK = re.compile(r"^- \[( |x|X)\] (.*)$")
# "**Q7 — Question?** (asked 2026-10-08)" and the older "**Q7** Question text"
QHEAD = re.compile(r"^\*\*(Q\d+)(?:\s*[—–-]\s*(.+?))?\*\*\s*(?:\(asked ([\d-]+)\))?\s*(.*)$")
KEY = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
REF = re.compile(r"(?:\.\./)*((?:plans|notes)/[\w./-]+?\.md)")


def comment_closes(lines, i):
    """For a `<!--` opened on line i without `-->`: does it close before another `<!--` opens?"""
    for l in lines[i + 1:]:
        if "-->" in l or "<!--" in l:
            return "-->" in l and (("<!--" not in l) or l.find("-->") < l.find("<!--"))
    return False


def sections(text):
    """Split on '## ' headings -> {heading: [(line_no, line)]}; lines before the first go under ''.
    Lines inside ``` fences are kept but never parsed as headings or checklist items. HTML comments
    (template guidance) are left out — real, closed ones; an unclosed `<!--` is ordinary text."""
    out, cur, fence, comment = {"": []}, "", False, False
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if comment:
            if "-->" not in line:
                continue
            comment, line = False, line.split("-->", 1)[1]
            if not line.strip():
                continue
        elif not fence and line.lstrip().startswith("<!--"):
            if "-->" in line:
                line = line.split("-->", 1)[1]       # text after the comment on the same line still counts
                if not line.strip():
                    continue
            elif comment_closes(lines, i):
                comment = True
                continue
            # else: unclosed — plain text, nothing to hide
        if line.lstrip().startswith("```"):
            fence = not fence
            out[cur].append((i, "\0" + line))
        elif fence:
            out[cur].append((i, "\0" + line))
        elif line.startswith("## "):
            cur = line[3:].strip()
            out[cur] = []
        else:
            out[cur].append((i, line))
    return out


def checklist(lines):
    """Top-level '- [ ]' items, with indented continuation lines attached.
    An indented ``` fence under an item belongs to it; `body` is the continuation as markdown."""
    items, cur = [], None
    for i, line in lines:
        fenced = line.startswith("\0")
        raw = line[1:] if fenced else line
        m = None if fenced else CHECK.match(line)
        if m:
            cur = {"line": i, "done": m.group(1) != " ", "text": m.group(2).strip(), "more": [], "raw": []}
            items.append(cur)
        elif cur and raw.startswith((" ", "\t")) and raw.strip():
            cur["more"].append((i, raw.strip()))
            cur["raw"].append(raw)
        elif cur and fenced:
            cur["raw"].append(raw)  # blank line inside a fence
        elif raw.strip() and not raw.startswith("<!--"):
            cur = None
    for it in items:
        it["human"] = "*(human)*" in it["text"]
        it["body"] = textwrap.dedent("\n".join(it.pop("raw"))).strip("\n")
    return items


def questions(lines):
    qs = []
    for it in checklist(lines):
        m = QHEAD.match(it["text"])
        if not m:
            continue
        text, tail = (m.group(2) or m.group(4)).strip(), (m.group(4) if m.group(2) else "").strip()
        q = {"id": m.group(1), "text": text, "asked": m.group(3), "line": it["line"],
             "done": it["done"], "notes": [], "answer": "", "answerLine": None, "outcome": "", "outcomeLine": None}
        if tail:
            q["notes"].append(tail)
        for i, extra in it["more"]:
            if extra.startswith("**Answer:**"):
                q["answer"], q["answerLine"] = extra[len("**Answer:**"):].strip(), i
            elif extra.startswith("**Outcome:**"):
                q["outcome"], q["outcomeLine"] = extra[len("**Outcome:**"):].strip(), i
            else:
                q["notes"].append(extra)
        q["lastLine"] = it["more"][-1][0] if it["more"] else it["line"]
        qs.append(q)
    return qs


def public(items):
    return [dict(it, more=[t for _, t in it["more"] if not t.startswith("```")]) for it in items]


def parse_repos(lines):
    """`- repos:` followed by one `  - \`<path>\` — note` sub-bullet per repo.
    The legacy single-line form `- repos: \`<path>\` (note)` is read too."""
    out, inside = [], False
    for _, line in lines:
        head = re.match(r"^- repos?:\s*(.*)$", line)
        if head:
            inside = True
            if head.group(1).strip():
                out.append(head.group(1))
            continue
        if inside and re.match(r"^\s+- ", line):
            out.append(line.strip()[2:])
        elif line.strip():
            inside = False
    repos = []
    for item in out:
        m = re.match(r"^`([~/][^`]*)`\s*(?:[—–-]\s*)?(.*)$", item.strip())
        if not m:
            continue
        path, note = m.group(1).rstrip("/"), m.group(2).strip()
        full = Path(os.path.expanduser(path))
        repos.append({"path": path, "abs": str(full), "name": full.name, "note": note,
                      "exists": full.is_dir(), "git": (full / ".git").exists()})
    return repos


COMMENT = re.compile(r"<!--.*?-->", re.S)


MSG = re.compile(r"^- \*\*(you|agent)\*\*\s*[·-]?\s*(\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2})?\s*$", re.I)


def parse_chat(lines):
    """## Chat (older files: ## Instructions) as messages: [{who, at, text}].
    `- **you** · 2026-10-12 15:40` / `- **agent** · …` start a message, its body is indented two
    spaces. Text outside that form (typed in an editor) counts as a message from you."""
    msgs, cur = [], None
    for _, l in lines:
        fenced = l.startswith("\0")
        raw = l[1:] if fenced else l
        m = None if fenced else MSG.match(raw)
        if m:
            cur = {"who": m.group(1).lower(), "at": (m.group(2) or "").replace("T", " ") or None, "body": [], "free": False}
            msgs.append(cur)
        elif cur and (fenced or raw.startswith("  ") or not raw.strip()):
            cur["body"].append(raw[2:] if raw.startswith("  ") else raw)
        elif raw.strip():
            if not (cur and cur["free"]):
                cur = {"who": "you", "at": None, "body": [], "free": True}
                msgs.append(cur)
            cur["body"].append(raw)
    out = []
    for m in msgs:
        text = "\n".join(m["body"]).strip("\n")
        if text.strip():
            out.append({"who": m["who"], "at": m["at"], "text": text})
    return out


def chat_key(msgs):
    """Identifies the latest message from you when it is the last one (= waiting for a reply).
    A last message that repeats an earlier one of yours which already got a reply is a copy an
    agent made while editing (it happens), not a new message: no key."""
    if msgs and msgs[-1]["who"] == "you":
        last = msgs[-1]
        for i, m in enumerate(msgs[:-1]):
            if m["who"] == "you" and m["at"] == last["at"] and m["text"] == last["text"] \
                    and any(x["who"] == "agent" for x in msgs[i + 1:-1]):
                return None
        return hashlib.sha1(f"{last['at']}|{last['text']}".encode()).hexdigest()[:12]
    return None


def headings(lines):
    """Indexes of '## ' headings outside code fences and outside closed HTML comments — the same
    view of the file as sections(), so edits land where the parser sees the sections."""
    out, fence, comment = [], False, False
    for i, l in enumerate(lines):
        if comment:
            comment = "-->" not in l
            continue
        if not fence and l.lstrip().startswith("<!--"):
            if "-->" in l:
                continue
            if comment_closes(lines, i):
                comment = True
                continue
        if l.lstrip().startswith("```"):
            fence = not fence
        elif not fence and l.startswith("## "):
            out.append(i)
    return out


def chat_range(lines):
    """(start, end) of ## Chat (or a legacy ## Instructions); created before ## Task if missing."""
    hs = headings(lines)
    start = next((i for i in hs if lines[i].strip() in ("## Chat", "## Instructions")), None)
    if start is None:
        at = next((i for i in hs if lines[i].strip() == "## Task"), hs[0] if hs else len(lines))
        lines[at:at] = ["## Chat", ""]
        start, hs = at, headings(lines)
    lines[start] = "## Chat"
    return start, next((i for i in hs if i > start), len(lines))


def append_message(lines, who, text):
    """Add one message at the end of ## Chat, body indented two spaces."""
    start, end = chat_range(lines)
    last = end
    while last - 1 > start and not lines[last - 1].strip():
        last -= 1
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    msg = [f"- **{who}** · {stamp}", *[("  " + l) if l.strip() else "" for l in text.split("\n")]]
    lines[last:end] = ["", *msg, ""]       # one blank line before the message and before the next section


def clear_chat(lines):
    """Empty ## Chat, keeping the template's guidance comment."""
    start, end = chat_range(lines)
    body = "\n".join(lines[start + 1:end])
    m = re.match(r"\s*(<!--.*?-->)", body, re.S)
    lines[start + 1:end] = (["", *m.group(1).split("\n")] if m else []) + [""]


def set_field(lines, name, value):
    """Rewrite the first `**name:** …` line (the Briefing's)."""
    for i, l in enumerate(lines):
        if l.startswith(f"**{name}:**"):
            lines[i] = f"**{name}:** {value}"
            return


def add_log(lines, entry):
    """Append one `- YYYY-MM-DD: …` line to ## Agent logs (created at the end if missing)."""
    hs = headings(lines)
    start = next((i for i in hs if lines[i].strip() == "## Agent logs"), None)
    if start is None:
        lines += ["", "## Agent logs", "", entry]
        return
    end = next((i for i in hs if i > start), len(lines))
    while end - 1 > start and not lines[end - 1].strip():
        end -= 1
    lines.insert(end, entry)


def _mv(src, dest):
    """`git mv` when tracked, a plain move otherwise; raises ValueError with git's message."""
    tracked = subprocess.run(["git", "-C", str(REPO), "ls-files", "--error-unmatch", str(src)],
                             capture_output=True).returncode == 0
    if tracked:
        r = subprocess.run(["git", "-C", str(REPO), "mv", str(src), str(dest)], capture_output=True, text=True)
        if r.returncode:
            raise ValueError(r.stderr.strip() or "git mv failed")
    else:
        os.replace(src, dest)


def move_task(path, folder):
    """Move an agentfile and its companions (<slug>.*) to another state folder. Companions first,
    the agentfile last; nothing is overwritten, and a failure moves back what was moved."""
    folder.mkdir(exist_ok=True)
    files = sorted(set(path.parent.glob(f"{path.stem}.*")) - {path}) + [path]
    for p in files:
        if (folder / p.name).exists():
            raise ValueError(f"{(folder / p.name).relative_to(REPO)} exists already")
    moved = []
    try:
        for p in files:
            _mv(p, folder / p.name)
            moved.append(p)
    except Exception:
        for p in reversed(moved):
            try:
                _mv(folder / p.name, p)
            except Exception:
                pass
        raise


def set_briefing_field(lines, name, value):
    """Set, replace or (empty value) remove a `- name: value` line in ## Briefing."""
    hs = headings(lines)
    start = next((i for i in hs if lines[i].strip() == "## Briefing"), None)
    if start is None:
        raise ValueError("no ## Briefing section")
    end = next((i for i in hs if i > start), len(lines))
    at = next((i for i in range(start + 1, end) if re.match(rf"^- {re.escape(name)}:", lines[i])), None)
    if at is not None:
        if value:
            lines[at] = f"- {name}: {value}"
        else:
            del lines[at]
        return
    if not value:
        return
    last = max((i for i in range(start + 1, end) if lines[i].startswith("- ") or lines[i].startswith("  - ")), default=None)
    lines.insert((last + 1) if last is not None else end, f"- {name}: {value}")


def field(lines, name):
    for _, line in lines:
        if line.startswith(f"**{name}:**"):
            return line[len(name) + 5:].strip()
    return ""


def parse(path):
    text = path.read_text(encoding="utf-8")
    sec = sections(text)
    # Status/Updated and the link fields live in ## Briefing; older files have them
    # above the first section and under ## Links — both are read
    head = sec[""] + sec.get("Briefing", [])
    meta = sec.get("Briefing", []) + sec.get("Links", [])
    title_line = next((l for _, l in head if l.startswith("# ")), f"# {path.stem}")[2:].strip()
    prio = None
    m = re.match(r"^\[(P\d)\]\s*(.*)$", title_line)
    if m:
        prio, title_line = m.group(1), m.group(2)

    status = field(head, "Status")
    state_word, _, waiting_on = status.partition("—")
    updated = field(head, "Updated")
    upd_date, _, upd_note = updated.partition("—")
    upd_date = upd_date.strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}:\d{2}))?$", upd_date)   # time is optional
    upd_date, upd_time = (m.group(1), m.group(2)) if m else (None, None)
    if not m:
        upd_note = updated

    links = {}
    for _, line in meta:
        m = re.match(r"^- ([\w ]+):\s*(.*)$", line)
        if m and m.group(2).strip() and m.group(1).strip() not in links:
            links[m.group(1).strip()] = m.group(2).strip()
    repos = list({r["path"]: r for r in parse_repos(meta)}.values())
    chat = parse_chat(sec.get("Chat", sec.get("Instructions", [])))
    links.pop("repos", None)

    keys = KEY.findall(links.get("ticket", "")) or KEY.findall(title_line)
    ticket_url = next(iter(re.findall(r"https?://\S+", links.get("ticket", ""))), None)
    if not ticket_url and keys and "{key}" in conf()["ticket-url"]:
        ticket_url = conf()["ticket-url"].replace("{key}", keys[0])

    folder = path.parent.name
    companions = sorted(p.name for p in path.parent.glob(f"{path.stem}.*") if p != path)
    refs = sorted(set(REF.findall(text)))

    ac = checklist(sec.get("Acceptance criteria", []))
    nxt = checklist(sec.get("Next steps", []))
    qs = questions(sec.get("Open questions", []))
    logs = [l[2:] for _, l in sec.get("Agent logs", []) if l.startswith("- ")]

    return {
        "slug": path.stem,
        "file": str(path.relative_to(REPO)),
        "abs": str(path),
        "folder": folder,
        "state": state_word.strip() or folder,
        "mismatch": (state_word.strip() or folder) != folder,
        "waitingOn": waiting_on.strip(),
        "title": title_line,
        "priority": prio,
        "keys": sorted(set(keys)),
        "ticketUrl": ticket_url,
        "updated": upd_date,
        "updatedTime": upd_time,
        # last touch by anyone (agent, board edit, editor): the file's modification time
        "touched": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
        "updatedNote": upd_note.strip(),
        "links": links,
        "repos": repos,
        "chat": chat,
        "chatKey": chat_key(chat),          # set while your message waits for a reply
        "acceptance": public(ac),
        "nextSteps": public(nxt),
        "questions": qs,
        "lastLog": logs[-1] if logs else "",
        "logCount": len(logs),
        "companions": companions,
        "refs": refs,
        "markdown": text,
        "hash": hashlib.sha1(text.encode()).hexdigest(),
    }


def is_agentfile(p):
    # companions are '<slug>.<something>.md'; agentfiles have a single suffix
    return p.suffix == ".md" and "." not in p.stem


def snapshot():
    tasks, broken = [], []
    for folder in sorted(TASKS.iterdir()) if TASKS.exists() else []:
        if folder.is_dir():
            for p in sorted(folder.glob("*.md")):
                if not is_agentfile(p):
                    continue
                try:
                    tasks.append(parse(p))
                except Exception as e:   # one unreadable file: reported, the rest still shows
                    broken.append({"file": str(p.relative_to(REPO)), "error": f"{type(e).__name__}: {e}"[:200]})
    stray = [str(p.relative_to(REPO)) for p in TASKS.glob("*.md")] if TASKS.exists() else []
    body = {"repo": str(REPO), "states": STATES, "today": date.today().isoformat(),
            "conf": conf(), "tasks": tasks, "stray": stray, "broken": broken, "holds": sorted(read_holds())}
    raw = json.dumps(body, ensure_ascii=False).encode()
    return raw, hashlib.sha1(raw).hexdigest()


def pid_alive(pid, needle):
    """True if `pid` runs and its command line mentions `needle`. A pid left behind by a crash
    belongs to some other process after a reboot, so the number alone proves nothing."""
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(int(pid))],
                             capture_output=True, text=True, timeout=3).stdout
    except (ValueError, OSError, subprocess.SubprocessError):
        return False
    return needle in out


def coordinator_status():
    """.coordinator/status.json plus liveness and the last events, for the board."""
    d = REPO / ".coordinator"
    try:
        st = json.loads((d / "status.json").read_text())
    except (OSError, ValueError):
        return {"present": False}
    pid = st.get("coordinator", {}).get("pid")
    alive = bool(pid) and pid_alive(pid, "coordinator.py")
    try:
        events = (d / "events.log").read_text().splitlines()[-40:]
    except OSError:
        events = []
    return {"present": True, "alive": alive, **st, "events": events}


def worker_log(path, limit=400):
    """A worker's log as readable steps for the board. The first line says which agent ran; its
    adapter (agents.py) reads the rest."""
    import agents
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    head = agents.log_header(lines[0]) if lines else None
    steps = []
    if head:
        steps.append({"kind": "start", "text": f"{head['agent']} · triggers: " + ", ".join(head.get("triggers", []))})
        lines = lines[1:]
    try:
        a = agents.get(head["agent"] if head else agents.DEFAULT)
    except ValueError:
        a = agents.get(agents.DEFAULT)
    steps += a.steps(lines)
    return {"steps": steps[-limit:], "truncated": max(0, len(steps) - limit), "path": str(path.relative_to(REPO))}


def settings_view():
    """The workspace's settings for the board's read-only Settings tab: katet.conf (parsed and raw),
    coordinator.tools (settings, rule counts, raw) and CLAUDE.md."""
    files = {}
    for name in ("katet.conf", "coordinator.tools", "CLAUDE.md"):
        try:
            files[name] = (REPO / name).read_text(encoding="utf-8")
        except OSError:
            files[name] = None
    tools = {"settings": {}, "allow": [], "deny": []}
    for line in (files["coordinator.tools"] or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        key, _, val = line.partition(" ")
        if key in ("allow", "deny"):
            tools[key].append(val.strip())
        else:
            tools["settings"][key] = val.strip()
    return {"workspace": str(REPO), "framework": str(FRAMEWORK), "conf": conf(), "tools": tools, "files": files}


HOLDS = REPO / ".coordinator" / "holds.json"   # tasks on hold: no worker starts on them until switched off


def read_holds():
    try:
        return set(json.loads(HOLDS.read_text()))
    except (OSError, ValueError):
        return set()


def set_hold(slug, on):
    """Switch a task's hold on or off; kept in the workspace, so it outlives the board tab."""
    with viewing_lock:
        holds = read_holds()
        (holds.add if on else holds.discard)(slug)
        HOLDS.parent.mkdir(exist_ok=True)
        tmp = HOLDS.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(sorted(holds)))
        os.replace(tmp, HOLDS)


def usage_summary(slug):
    """What every worker run on a task used, summed from logs/<slug>.*.jsonl through the agent's adapter."""
    import agents
    tot = {"runs": 0, "failed": 0, "turns": 0, "input": 0, "output": 0, "cacheWrite": 0, "cacheRead": 0,
           "cost": 0.0, "seconds": 0.0, "models": {}, "first": None, "last": None}
    for log in sorted((REPO / "logs").glob(f"{slug}.*.jsonl")):
        tot["runs"] += 1
        stamp = log.stem.rpartition(".")[2]
        tot["first"] = tot["first"] or stamp
        tot["last"] = stamp
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        head = agents.log_header(lines[0]) if lines else None
        try:
            a = agents.get(head["agent"] if head else agents.DEFAULT)
        except ValueError:
            a = agents.get(agents.DEFAULT)
        u = a.usage(lines[1:] if head else lines)
        if not u:
            tot["failed"] += 1
            continue
        if u["error"]:
            tot["failed"] += 1
        for k in ("turns", "input", "output", "cacheWrite", "cacheRead", "cost", "seconds"):
            tot[k] += u[k]
        for name, mu in u["models"].items():
            m = tot["models"].setdefault(name, {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "cost": 0.0})
            for k in m:
                m[k] += mu.get(k, 0)
    tot["context"] = tot["input"] + tot["cacheWrite"] + tot["cacheRead"]
    return tot


def usage_line(u):
    """One line for the agentfile: `21 runs · 305 turns · 16.5 min · 92k out / 7.0M context · $7.17 (notional)`."""
    if not u["runs"]:
        return "no worker runs"
    k = lambda n: f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)
    per_model = ", ".join(f"{name.replace('claude-', '')} ${m['cost']:.2f}" for name, m in sorted(u["models"].items())) if len(u["models"]) > 1 else ""
    return (f"{u['runs']} runs · {u['turns']} turns · {u['seconds'] / 60:.1f} min · {k(u['output'])} out / {k(u['context'])} context"
            f" · ${u['cost']:.2f} notional" + (f" ({per_model})" if per_model else ""))


def task_path(slug):
    """The agentfile for a slug in any state but done; None when there is none."""
    for p in TASKS.glob("*/*.md"):
        if p.stem == slug and is_agentfile(p) and p.parent.name != "done":
            return p
    return None


VIEW_TTL = 20          # seconds a lock lives without a heartbeat
viewing = {}           # browser tab id -> (slug, last heartbeat)
viewing_lock = threading.Lock()


def set_viewing(tab, slug):
    """Record which task a board tab has open; written to .coordinator/viewing.json for the coordinator."""
    nowt = time.time()
    with viewing_lock:
        if slug:
            viewing[tab] = (slug, nowt)
        else:
            viewing.pop(tab, None)
        for t, (_, ts) in list(viewing.items()):
            if nowt - ts > VIEW_TTL:
                del viewing[t]
        out = {}
        for sl, ts in viewing.values():
            out[sl] = max(out.get(sl, 0), ts)
        d = REPO / ".coordinator"
        d.mkdir(exist_ok=True)
        tmp = d / "viewing.json.tmp"
        tmp.write_text(json.dumps({sl: {"until": ts + VIEW_TTL} for sl, ts in out.items()}))
        os.replace(tmp, d / "viewing.json")


CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: https:; connect-src 'self'; object-src 'none'; frame-src 'none'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


class Conflict(Exception):
    pass


def edit(body):
    """Apply one human edit to one agentfile. Raises Conflict / ValueError."""
    target = (REPO / body["file"]).resolve()
    if TASKS not in target.parents or not is_agentfile(target) or not target.is_file():
        raise ValueError("not an agentfile")
    text = target.read_text(encoding="utf-8")
    if hashlib.sha1(text.encode()).hexdigest() != body.get("hash"):
        raise Conflict("the file changed since the page loaded it")
    lines = text.splitlines()
    sec = sections(text)
    op = body.get("op")

    if op == "answer":
        q = next((q for q in questions(sec.get("Open questions", [])) if q["id"] == body["qid"]), None)
        if not q:
            raise ValueError(f"no question {body['qid']}")
        answer = " ".join(str(body.get("text", "")).split())
        if q["answerLine"] is not None:
            old = lines[q["answerLine"]]
            indent = old[: len(old) - len(old.lstrip())]
            lines[q["answerLine"]] = f"{indent}**Answer:** {answer}".rstrip()
        else:
            at = q["outcomeLine"] if q["outcomeLine"] is not None else q["lastLine"] + 1
            lines.insert(at, f"  **Answer:** {answer}".rstrip())
    elif op == "clear-outcome":
        # empties the Outcome line and unticks the question: it is open again for the agent
        q = next((q for q in questions(sec.get("Open questions", [])) if q["id"] == body["qid"]), None)
        if not q or q["outcomeLine"] is None or not q["outcome"]:
            raise ValueError(f"no outcome on {body.get('qid')}")
        old = lines[q["outcomeLine"]]
        lines[q["outcomeLine"]] = old[: len(old) - len(old.lstrip())] + "**Outcome:**"
        lines[q["line"]] = re.sub(r"^- \[[xX]\]", "- [ ]", lines[q["line"]], count=1)
    elif op == "message":
        # one chat message from you: free markdown, several lines
        text = str(body.get("text", "")).replace("\r\n", "\n").replace("\r", "\n")
        text = "\n".join(l.rstrip() for l in text.split("\n")).strip("\n")
        if not text.strip():
            raise ValueError("empty message")
        if re.search(r"^#{1,2} ", text, re.M):
            raise ValueError("a line starting with '#' or '##' would start a new section; use ### or bold instead")
        if len(re.findall(r"^\s*```", text, re.M)) % 2:
            raise ValueError("unclosed ``` code block")
        append_message(lines, "you", text)
    elif op == "clear-chat":
        clear_chat(lines)
    elif op == "field":
        # a worker setting for this task alone, as a Briefing field: `- model: opus`; empty value removes it
        name, value = str(body.get("name", "")), " ".join(str(body.get("value", "")).split())
        if name not in ("model", "effort", "budget"):
            raise ValueError("only model, effort and budget can be set here")   # `worker usage` is written by accept
        if re.search(r"[`\n#]", value):
            raise ValueError("value must be one plain line")
        set_briefing_field(lines, name, value)
    elif op == "accept":
        # the human's acceptance, the one move agents never make: Status → done, then into tasks/done/
        if target.parent.name == "done":
            raise ValueError("already done")
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        set_field(lines, "Status", "done")
        set_field(lines, "Updated", f"{stamp} — accepted on the board")
        usage = usage_line(usage_summary(target.stem))
        set_briefing_field(lines, "worker usage", usage)
        add_log(lines, f"- {stamp[:10]}: Accepted on the board; moved to done. Worker usage: {usage}.")
    elif op == "toggle":
        section = body.get("section")
        if section not in ("Next steps", "Acceptance criteria"):
            raise ValueError("only Next steps and Acceptance criteria can be ticked")
        items = checklist(sec.get(section, []))
        idx = body.get("index")
        if not isinstance(idx, int) or not 0 <= idx < len(items):
            raise ValueError("no such item")
        it = items[idx]
        if it["text"] != body.get("text"):
            raise Conflict("the item moved or changed")
        if section == "Acceptance criteria" and not it["human"]:
            raise ValueError("only *(human)* acceptance criteria are ticked by hand; agents tick the rest after the check")
        mark = "x" if body.get("done") else " "
        lines[it["line"]] = re.sub(r"^- \[[ xX]\]", f"- [{mark}]", lines[it["line"]], count=1)
    else:
        raise ValueError("unknown op")

    new = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    tmp = target.with_name(f".{target.name}.board-tmp")
    tmp.write_text(new, encoding="utf-8")
    os.replace(tmp, target)
    if op == "accept":
        try:
            move_task(target, TASKS / "done")
        except Exception as e:
            target.write_text(text, encoding="utf-8")   # back to how it was: Status, Updated, log
            raise ValueError(f"not moved: {e}") from None


def slugify(text, limit=50):
    words = re.findall(r"[a-z0-9]+", text.lower())
    out = ""
    for w in words:
        if len(out) + len(w) + 1 > limit:
            break
        out = f"{out}-{w}" if out else w
    return out or "task"


DRAFT_INSTRUCTIONS = """Draft this agentfile from the request under Task (written on the board):
fill the Briefing, rewrite Task as what and why with pointers, and add Constraints,
Acceptance criteria with checks, Open questions for anything unclear and Next steps for me.
Don't start the work itself yet. Then move the task to `waiting — answer: …` if you have
questions, else to `waiting — action: check the draft`."""


def create(body):
    """New agentfile in tasks/open/ from task.template.md (the workspace's own, else the framework's)."""
    title = " ".join(str(body.get("title", "")).split())
    request = str(body.get("request", "")).replace("\r\n", "\n").strip()
    prio = str(body.get("priority", "P2"))
    ticket = " ".join(str(body.get("ticket", "")).split())
    if not isinstance(body.get("repos", []), list):
        raise ValueError("repos must be a list")
    repos = [" ".join(str(r).split()) for r in body.get("repos", []) if str(r).strip()]
    if not title or len(title) > 120:
        raise ValueError("title is required (at most 120 characters)")
    if not request:
        raise ValueError("describe the request")
    if prio not in ("P1", "P2", "P3"):
        raise ValueError("priority must be P1, P2 or P3")
    if re.search(r"^#{1,2} ", request, re.M):
        raise ValueError("a line starting with '#' or '##' would start a new section; use ### or bold instead")
    if len(re.findall(r"^\s*```", request, re.M)) % 2:
        raise ValueError("unclosed ``` code block")
    bad = [r for r in repos if not r.startswith(("~/", "/"))]
    if bad:
        raise ValueError(f"repo paths start with ~/ or /: {bad[0]}")
    key = KEY.search(ticket)
    if key and key.group(1) not in title:
        title = f"{key.group(1)}: {title}"
    base = slugify(title)
    taken = {p.stem for p in TASKS.glob("*/*.md")}
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1

    today = date.today().isoformat()
    draft = bool(body.get("draft"))
    tpl = next(p for p in (REPO / "task.template.md", FRAMEWORK / "task.template.md") if p.is_file()).read_text(encoding="utf-8")
    tpl = re.sub(r"\A<!--.*?-->\n", "", tpl, flags=re.S)                    # file-level guidance
    # keep headings and guidance comments, drop the template's sample items
    lines, sec, skip_indent = [], "", False
    for line in tpl.split("\n"):
        if line.startswith("## "):
            sec = line[3:].strip()
        if skip_indent and line.startswith("  "):
            continue
        skip_indent = False
        if sec in ("Constraints / non-goals", "Acceptance criteria", "Open questions") and line.startswith("- "):
            skip_indent = True
            continue
        lines.append(line)
    s = "\n".join(lines)
    s = re.sub(r"^# \[P1\.\.3\] title$", lambda m: f"# [{prio}] {title}", s, count=1, flags=re.M)
    s = re.sub(r"^\*\*Status:\*\* .*$", "**Status:** open", s, count=1, flags=re.M)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    s = re.sub(r"^\*\*Updated:\*\* .*$", f"**Updated:** {stamp} — created on the board", s, count=1, flags=re.M)
    s = re.sub(r"^- ticket:.*$", lambda m: f"- ticket: {ticket}".rstrip(), s, count=1, flags=re.M)
    repo_block = "- repos:" + "".join(f"\n  - `{r}`" for r in repos)
    s = re.sub(r"^- repos:\n(?:  - .*\n)*", lambda m: repo_block + "\n", s, count=1, flags=re.M)
    s = re.sub(r"^- plan: .*\n", "", s, count=1, flags=re.M)               # sample field
    s = re.sub(r"(^## Task\n\n(?:<!--.*?-->\n)?)", lambda m: m.group(1) + "\n" + request + "\n", s, count=1, flags=re.M | re.S)
    log = f"- {today}: Created on the board" + (" and handed to an agent to draft (Chat)." if draft else ".")
    s = s.rstrip("\n") + "\n\n" + log + "\n"
    s = re.sub(r"\n{3,}", "\n\n", s)

    lines = s.split("\n")
    if draft:
        append_message(lines, "you", DRAFT_INSTRUCTIONS)
    target = TASKS / "open" / f"{slug}.md"
    target.parent.mkdir(exist_ok=True)
    tmp = target.with_name(f".{target.name}.board-tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    os.replace(tmp, target)
    return slug


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def local(self):
        """Only requests addressed to this server: a web page elsewhere must not read or post here
        (DNS rebinding would otherwise let it, since the browser reaches 127.0.0.1)."""
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def read_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            raise ValueError("bad Content-Length") from None
        if n < 0 or n > 2_000_000:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n))

    def do_GET(self):
        try:
            self.handle_get()
        except Exception as e:   # never drop the connection without an answer
            self.send(500, json.dumps({"error": f"{type(e).__name__}: {e}"[:300]}).encode(), "application/json")

    def do_POST(self):
        try:
            self.handle_post()
        except Exception as e:
            self.send(500, json.dumps({"error": f"{type(e).__name__}: {e}"[:300]}).encode(), "application/json")

    def handle_get(self):
        if not self.local():
            return self.send(403, b"forbidden", "text/plain")
        url = urlparse(self.path)
        if url.path == "/api/tasks":
            raw, etag = snapshot()
            if self.headers.get("If-None-Match") == etag:
                return self.send(304, b"", "application/json", {"ETag": etag})
            return self.send(200, raw, "application/json; charset=utf-8", {"ETag": etag})
        if url.path == "/api/coordinator":
            return self.send(200, json.dumps(coordinator_status(), ensure_ascii=False).encode(),
                             "application/json; charset=utf-8")
        if url.path == "/api/usage":
            slug = unquote(url.query.partition("slug=")[2])[:120]
            if not re.fullmatch(r"[\w.-]+", slug or ""):
                return self.send(400, b"bad slug", "text/plain")
            u = usage_summary(slug); u["line"] = usage_line(u)
            return self.send(200, json.dumps(u, ensure_ascii=False).encode(), "application/json; charset=utf-8")
        if url.path == "/api/settings":
            return self.send(200, json.dumps(settings_view(), ensure_ascii=False).encode(), "application/json; charset=utf-8")
        if url.path == "/api/worker-log":
            rel = unquote(url.query.partition("path=")[2])
            target = (REPO / rel).resolve()
            if (REPO / "logs") not in target.parents or target.suffix != ".jsonl" or not target.is_file():
                return self.send(404, b"not found", "text/plain")
            return self.send(200, json.dumps(worker_log(target), ensure_ascii=False).encode(),
                             "application/json; charset=utf-8")
        if url.path == "/api/file":
            rel = unquote(url.query.partition("path=")[2])
            target = (REPO / rel).resolve()
            if REPO not in target.parents or target.suffix not in SERVABLE or not target.is_file():
                return self.send(404, b"not found", "text/plain")
            return self.send(200, target.read_bytes(), "text/plain; charset=utf-8")
        static = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/vendor/marked.min.js": ("vendor/marked.min.js", "text/javascript"),
                  "/favicon.svg": ("favicon.svg", "image/svg+xml"), "/favicon.ico": ("favicon.ico", "image/x-icon")}
        if url.path in static:
            name, ctype = static[url.path]
            # the page renders markdown written by agents: even if something slips past the page's own
            # sanitizer, it may not load remote code, talk to other hosts or frame anything
            extra = {"Content-Security-Policy": CSP} if url.path == "/" else None
            return self.send(200, (BOARD / name).read_bytes(), ctype, extra)
        self.send(404, b"not found", "text/plain")

    def handle_post(self):
        # Custom header + JSON body force a CORS preflight we never answer, so other
        # websites open in the browser cannot post here. Origin is checked as well.
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if not self.local() or self.headers.get("X-Katet") != "1" or (origin and origin != f"http://{host}"):
            return self.send(403, b"forbidden", "text/plain")
        path = urlparse(self.path).path
        if path == "/api/viewing":
            try:
                body = self.read_json()
                tab, slug = str(body["tab"])[:40], body.get("slug")
                set_viewing(tab, str(slug)[:120] if slug else None)
            except (ValueError, KeyError, TypeError):
                return self.send(400, b"bad request", "text/plain")
            return self.send(200, b'{"ok":true}', "application/json")
        if path == "/api/trigger":
            # a run you ask for by hand: the coordinator picks the marker up on its next loop
            try:
                body = self.read_json()
                slug = str(body["slug"])[:120]
                if not task_path(slug):
                    raise KeyError(slug)
                d = REPO / ".coordinator" / "triggers"
                d.mkdir(parents=True, exist_ok=True)
                (d / slug).write_text(datetime.now().isoformat(timespec="seconds"))
            except (ValueError, KeyError, TypeError):
                return self.send(400, b"bad request", "text/plain")
            return self.send(200, b'{"ok":true}', "application/json")
        if path == "/api/hold":
            try:
                body = self.read_json()
                slug = str(body["slug"])[:120]
                if not task_path(slug):
                    raise KeyError(slug)
                set_hold(slug, bool(body.get("on")))
            except (ValueError, KeyError, TypeError):
                return self.send(400, b"bad request", "text/plain")
            return self.send(200, b'{"ok":true}', "application/json")
        if path not in ("/api/edit", "/api/create"):
            return self.send(404, b"not found", "text/plain")
        try:
            body = self.read_json()
            if path == "/api/create":
                slug = create(body)
                return self.send(200, json.dumps({"ok": True, "slug": slug}).encode(), "application/json")
            edit(body)
        except Conflict as e:
            return self.send(409, json.dumps({"error": str(e)}).encode(), "application/json")
        except (ValueError, KeyError, TypeError) as e:
            return self.send(400, json.dumps({"error": str(e)}).encode(), "application/json")
        self.send(200, b'{"ok":true}', "application/json")


def restart_on_change():
    """Re-exec this process when serve.py changes, so a running board never serves stale code.
    The listening socket is not inherited across exec; HTTPServer re-binds with SO_REUSEADDR."""
    me = Path(__file__).resolve()
    seen = me.stat().st_mtime
    while True:
        time.sleep(1)
        try:
            now = me.stat().st_mtime
        except FileNotFoundError:
            continue
        if now != seen:
            print("serve.py changed, restarting", flush=True)
            argv = sys.argv if "--no-open" in sys.argv else sys.argv + ["--no-open"]
            os.execv(sys.executable, [sys.executable] + argv)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    try:
        default_port = int(conf()["port"])
    except ValueError:
        default_port = 8765
    ap.add_argument("--port", type=int, default=default_port)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    threading.Thread(target=restart_on_change, daemon=True).start()
    url = f"http://127.0.0.1:{args.port}/"
    print(f"katet board → {url}  workspace {REPO}  (ctrl-c to stop)")
    if not args.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
