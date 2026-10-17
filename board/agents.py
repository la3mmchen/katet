"""Coding-agent adapters for the coordinator.

The coordinator is agent-agnostic: it decides when to run a worker on which agentfile and what to
tell it. An adapter turns that into one command line, reads the agent's output for the board, and
knows how to resume a run. Claude Code is built in; another agent is one more class here,
registered in AGENTS and chosen with `agent <name>` in coordinator.tools.

Contract — every method but `command` has a working default:
  name        the value of `agent` in coordinator.tools
  binary      executable the agent needs on PATH (checked when the coordinator starts)
  settings    extra `<key> <value>` settings this agent reads from coordinator.tools, with defaults
  validate(conf)            raise ValueError for a setting it cannot use
  overrides(fields)         the per-task settings a Briefing may carry (model, effort, budget, …):
                            return the valid subset of the given {field: value} dict
  command(job) -> argv      job: prompt, rules, workspace, repos, protected, allow, deny, conf, name
                            - rules: the worker rules text (system-level instructions)
                            - repos: absolute paths the worker may also work in
                            - protected: absolute glob paths it must never write (the workspace's
                              setup files, every .git/); the adapter enforces that in its own way
                            - allow / deny: the raw rule lines from coordinator.tools, in the
                              agent's own permission syntax
  event(line, entry) -> changed   live output, one line at a time: update entry's sessionId,
                            lastActivity {at, tool, what}, cost, result, isError
  steps(lines) -> [step]    the board's worker view from the raw log lines:
                            {kind: text|tool|info|result|raw, text, tool?, result?, error?, meta?}
  resume(session_id) -> shell command to continue the run interactively, or None
  usage(lines) -> what one run used, from its raw log lines: {turns, input, output, cacheWrite,
                            cacheRead, cost, seconds, error, models: {name: {input, output, cacheRead, cacheWrite, cost}}}
                            or None when the log has no usable end record
"""
import json
import re
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Agent:
    name = ""
    binary = ""
    settings = {}

    def validate(self, conf):
        pass

    def overrides(self, fields):
        return {}

    def command(self, job):
        raise NotImplementedError

    def event(self, line, entry):
        """Default: plain lines are the activity."""
        if line.strip():
            entry["lastActivity"] = {"at": now(), "tool": "text", "what": line.strip()[:160]}
            return True
        return False

    def steps(self, lines):
        return [{"kind": "raw", "text": l[:500]} for l in lines if l.strip()]

    def resume(self, session_id):
        return None

    def usage(self, lines):
        return None


class ClaudeCode(Agent):
    """Claude Code in headless mode: `claude -p` with stream-json output, permission mode dontAsk
    (anything not allowed is refused without asking), rules via --allowedTools / --disallowedTools."""
    name = "claude"
    binary = "claude"
    settings = {"model": "", "effort": "", "budget": "", "setting-sources": ""}
    EFFORTS = ("low", "medium", "high", "xhigh", "max")
    FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
    # a run that failed for a reason that will hit the next run too: the account's usage limit, the
    # API rate limit, an expired login. The coordinator pauses on these instead of burning runs.
    LIMIT_HINTS = ("usage limit", "rate limit", "rate_limit", "overloaded", "429", "login expired", "please run /login",
                   "not logged in", "authentication", "invalid api key", "credit balance")

    def limited(self, text):
        t = (text or "").lower()
        return any(h in t for h in self.LIMIT_HINTS)

    def validate(self, conf):
        if conf["budget"]:
            try:
                float(conf["budget"])
            except ValueError:
                raise ValueError(f"budget must be a number, not {conf['budget']!r}") from None
        if conf["effort"] and conf["effort"] not in self.EFFORTS:
            raise ValueError(f"effort must be one of {', '.join(self.EFFORTS)}, not {conf['effort']!r}")

    def overrides(self, fields):
        """A task's own `- model:`, `- effort:`, `- budget:` Briefing fields, valid ones only."""
        out = {}
        if fields.get("model"):
            out["model"] = fields["model"].strip("`")
        if fields.get("effort") in self.EFFORTS:
            out["effort"] = fields["effort"]
        try:
            if fields.get("budget"):
                out["budget"] = str(float(fields["budget"].lstrip("$")))
        except ValueError:
            pass
        return out

    def expand(self, allow, deny, workspace, user_settings):
        """The rule forms Claude Code needs for what a rule means: absolute tasks/ paths, the
        absolute form of denied files, `git -C <repo> …` and `mise exec -- …` prefixes (and `rtk …`
        when the workers load the user's settings, where the RTK hook rewrites commands first)."""
        # status moves may use absolute paths: `mv /…/workspace/tasks/x /…/workspace/tasks/y`
        with_abs = lambda rules: rules + [r.replace(" tasks/", f" {workspace}/tasks/") for r in rules if " tasks/" in r]
        # file rules: `Edit(notes/**)` is read relative to the settings, so add the absolute `//` form too
        file_tools = tuple(t + "(" for t in self.FILE_TOOLS)
        with_root = lambda rules: rules + [r.split("(", 1)[0] + "(/" + str(workspace) + "/" + r.split("(", 1)[1]
                                           for r in rules if r.startswith(file_tools) and not r.split("(", 1)[1].startswith("/")]
        # rules match from the start of the command: `git -C <repo> status` needs its own rule
        with_c = lambda rules: rules + [r.replace("Bash(git ", "Bash(git -C * ", 1) for r in rules if r.startswith("Bash(git ")]
        # repos that pin their tools with mise run them as `mise exec -- tofu …`
        mise_tools = ("Bash(tofu ", "Bash(terraform ", "Bash(terragrunt ", "Bash(tflint")
        with_mise = lambda rules: rules + [r.replace("Bash(", "Bash(mise exec -- ", 1) for r in rules if r.startswith(mise_tools)]
        with_rtk = lambda rules: rules + [r.replace("Bash(", "Bash(rtk ", 1) for r in rules if r.startswith("Bash(")]
        dedupe = lambda rules: list(dict.fromkeys(rules))
        allow, deny = with_mise(with_c(with_abs(allow))), with_mise(with_c(with_root(with_abs(deny))))
        if user_settings:
            allow, deny = with_rtk(allow), with_rtk(deny)
        return dedupe(allow), dedupe(deny)

    # Guards for the read-only git rules, always on: git itself can run commands through -c,
    # --config-env, --exec-path or a foreign --git-dir/--work-tree, write files through --output,
    # a wildcard after `branch` would let it delete or rename, `mv` with several sources moves files
    # anywhere, `tflint --init` downloads and runs plugins. They go through expand() with the configured
    # denies, so the `git -C <repo> …` and `rtk …` forms are covered as well; deny beats allow.
    GUARDS = (["Bash(git -c *)", "Bash(git --config-env*)", "Bash(git --git-dir*)", "Bash(git --work-tree*)", "Bash(git --exec-path*)",
               "Bash(git * --output*)", "Bash(git grep -O*)", "Bash(git grep * -O*)", "Bash(git grep * --open-files-in-pager*)", "Bash(mv *..*)", "Bash(git mv *..*)", "Bash(mv * * *)", "Bash(git mv * * *)", "Bash(tflint*--init*)"]
              + [f"Bash(git branch {pre}{flag} *)" for pre in ("", "* ") for flag in ("-d", "-D", "-m", "-M", "-f", "--delete", "--move", "--force")])

    def command(self, job):
        conf = dict(job["conf"])
        conf.update(job.get("overrides") or {})   # the task's own model / effort / budget win
        sources = [x.strip() for x in conf["setting-sources"].split(",") if x.strip()]
        allow, deny = self.expand(job["allow"], job["deny"] + self.GUARDS, job["workspace"], user_settings=not sources or "user" in sources)
        # protected paths: absolute, so the `//` form
        deny += [f"{tool}(/{p})" for p in job["protected"] for tool in self.FILE_TOOLS]
        cmd = [self.binary, "-p", job["prompt"],
               "--permission-mode", "dontAsk",
               "--output-format", "stream-json", "--verbose",
               "--append-system-prompt", job["rules"],
               "--name", f"coordinator · {job['name']}",
               "--add-dir", str(job["workspace"]), *sum((["--add-dir", r] for r in job["repos"]), [])]
        if conf["model"]:
            cmd += ["--model", conf["model"]]
        if conf["effort"]:
            cmd += ["--effort", conf["effort"]]
        if conf["budget"]:
            cmd += ["--max-budget-usd", conf["budget"]]
        if conf["setting-sources"]:
            cmd += ["--setting-sources", conf["setting-sources"]]
        if allow:
            cmd += ["--allowedTools", *allow]
        if deny:
            cmd += ["--disallowedTools", *deny]
        return cmd

    def event(self, line, entry):
        try:
            ev = json.loads(line)
        except ValueError:
            if line.strip() and self.limited(line):   # stderr lines are not JSON: "Login expired · Please run /login"
                entry["failure"] = line.strip()[:200]
                return True
            return False
        if not isinstance(ev, dict):
            return False
        changed = False
        if ev.get("session_id") and not entry.get("sessionId"):
            entry["sessionId"], changed = ev["session_id"], True
        if ev.get("type") == "assistant":
            for c in ev.get("message", {}).get("content", []):
                if c.get("type") == "tool_use":
                    inp = c.get("input", {})
                    what = inp.get("file_path") or inp.get("command") or inp.get("pattern") or inp.get("description") or ""
                    entry["lastActivity"] = {"at": now(), "tool": c.get("name"), "what": str(what)[:160]}
                    changed = True
                elif c.get("type") == "text" and c.get("text", "").strip():
                    entry["lastActivity"] = {"at": now(), "tool": "text", "what": c["text"].strip()[:160]}
                    changed = True
        if ev.get("type") == "result":
            entry["cost"] = ev.get("total_cost_usd")
            entry["result"] = (ev.get("result") or "")[:400]
            entry["isError"] = ev.get("is_error")
            if ev.get("is_error") and self.limited(ev.get("result")):
                entry["failure"] = (ev.get("result") or "").strip()[:200]
            changed = True
        return changed

    def steps(self, lines):
        steps, calls = [], {}
        for line in lines:
            try:
                ev = json.loads(line)
            except ValueError:
                if line.strip():
                    steps.append({"kind": "raw", "text": line[:500]})
                continue
            if not isinstance(ev, dict):
                continue
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init":
                steps.append({"kind": "info", "text": f"session {ev.get('session_id', '')[:8]} · model {ev.get('model', '?')}"
                                                      f" · permission mode {ev.get('permissionMode', '?')}"})
            elif t == "assistant":
                for c in ev.get("message", {}).get("content", []):
                    if c.get("type") == "text" and c.get("text", "").strip():
                        steps.append({"kind": "text", "text": c["text"].strip()[:4000]})
                    elif c.get("type") == "tool_use":
                        inp = c.get("input", {})
                        what = (inp.get("command") or inp.get("file_path") or inp.get("pattern")
                                or inp.get("description") or json.dumps(inp)[:200])
                        step = {"kind": "tool", "tool": c.get("name"), "text": str(what)[:600], "result": None, "error": False}
                        calls[c.get("id")] = step
                        steps.append(step)
            elif t == "user":
                for c in ev.get("message", {}).get("content", []):
                    if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") in calls:
                        out = c.get("content")
                        if isinstance(out, list):
                            out = "\n".join(x.get("text", "") for x in out if isinstance(x, dict))
                        step = calls[c["tool_use_id"]]
                        step["result"] = str(out or "")[:1500]
                        step["error"] = bool(c.get("is_error")) or "denied" in step["result"][:200].lower()
            elif t == "result":
                if steps and steps[-1]["kind"] == "text" and steps[-1]["text"] == (ev.get("result") or "").strip()[:4000]:
                    steps.pop()  # the final message is repeated as the result
                steps.append({"kind": "result", "error": bool(ev.get("is_error")),
                              "text": (ev.get("result") or "").strip()[:4000],
                              "meta": f"{ev.get('num_turns', '?')} turns · ${ev.get('total_cost_usd') or 0:.2f}"
                                      f" · {round((ev.get('duration_ms') or 0) / 1000)} s"})
        return steps

    def resume(self, session_id):
        return f"claude --resume {session_id}"

    def usage(self, lines):
        res = None
        for line in lines:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("type") == "result":
                res = ev
        if not res:
            return None
        u = res.get("usage") or {}
        out = {"turns": res.get("num_turns") or 0, "input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0),
               "cacheWrite": u.get("cache_creation_input_tokens", 0), "cacheRead": u.get("cache_read_input_tokens", 0),
               "cost": res.get("total_cost_usd") or 0.0, "seconds": (res.get("duration_ms") or 0) / 1000, "error": bool(res.get("is_error")),
               "models": {}}
        for name, mu in (res.get("modelUsage") or {}).items():
            out["models"][name] = {"input": mu.get("inputTokens", 0), "output": mu.get("outputTokens", 0), "cacheRead": mu.get("cacheReadInputTokens", 0),
                                   "cacheWrite": mu.get("cacheCreationInputTokens", 0), "cost": mu.get("costUSD", 0.0)}
        return out


AGENTS = {a.name: a for a in (ClaudeCode(),)}
DEFAULT = "claude"


def get(name):
    """The adapter for `name`; unknown names raise ValueError with the known ones."""
    try:
        return AGENTS[name or DEFAULT]
    except KeyError:
        raise ValueError(f"unknown agent {name!r} (known: {', '.join(AGENTS)})") from None


def all_settings():
    """Every setting any adapter accepts, with its default — for parsing coordinator.tools."""
    out = {}
    for a in AGENTS.values():
        out.update(a.settings)
    return out


def log_header(line):
    """The first line of a worker log names the agent (old logs: Claude Code)."""
    try:
        ev = json.loads(line)
    except ValueError:
        return None
    if not isinstance(ev, dict):
        return None
    if ev.get("type") in ("katet", "coordinator"):
        ev.setdefault("agent", DEFAULT)
        return ev
    return None
