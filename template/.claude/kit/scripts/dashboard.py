#!/usr/bin/env python3
"""Dashboard of the foreman: every task of every worktree of the repository, and the
agents of the one you pick.

Usage: dashboard.py <repo-root> <port>     (started by `work.sh ui`)

Parallel work runs one task per worktree (`.work/current` is per checkout), so a dashboard
that only read its own `.work/` would show one task at a time. This server reads the work
directory of each worktree from `git worktree list` and serves:

  GET  /api/tasks                    every task, with its status, grouped by worktree
  GET  /api/task?wt=<n>&id=<ID>      purpose, stage files and events of one task
  GET  /api/attention                sessions waiting on the dev, per worktree (hooks/attention.py)
  GET  /api/worktrees                every worktree, with or without a task: its path, its switches
                                     and whether a Claude session is alive there (the page's chat)
  GET  /api/cleanup-plan?wt=<n>&id=<ID> | ?all=done
                                     what a cleanup of done tasks would delete, and what it refuses
  GET  /api/events                   Server-Sent Events: "change" when a work dir changes, so a
                                     background tab (whose timers the browser throttles) learns
                                     at once that it must fetch /api/attention and /api/tasks
                                     (needs ?t=<page token> or a localhost Origin)
  POST /api/answer                   answers or releases a held AskUserQuestion
  POST /api/cleanup                  deletes what a confirmed cleanup plan listed, nothing else
  POST /api/close                    "Mark as done": the same `closed` marker as `work.sh done <ID>`
  POST /api/findings-decision        decides one open finding (fix or accepted) in findings-decision.md

Only those, the page and flow.json are served: nothing under a work directory is reachable
by path, and a worktree is addressed by its index in the list, never by a path the browser
sends. The writes are an answer to a question a hook is holding right now, a cleanup the dev
confirmed and a task the dev marked done; all need the per-server token embedded in the page, a localhost Origin and
Host and a JSON body: a foreign page can POST a form, but cannot read the token nor send that
content type without a preflight.

A task is done when a pull or merge request of its worktree's branch is open or merged
(GitHub or GitLab API, token from the environment or the origin URL, never sent to the page),
when git shows the branch merged into the base, or when `work.sh done` (or "Mark as done")
wrote its `closed` marker. Also run as:
  dashboard.py cleanup <root> <ID>|--all-done [--state] [--worktree] [--memory]   (work.sh cleanup)
  dashboard.py hook <root>          SessionStart: one line naming the finished tasks, or nothing
"""
import hashlib
import hmac
import importlib.util
import http.server
import json
import os
import re
import secrets
import select
import shlex
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import decisions  # noqa: E402  who acts on each finding; the session told of page decisions

# The hooks' own masking and turn classifier, from the server's copy of the kit (never a
# worktree's copy, whose hook may be older or edited).
_FLOW_SPEC = importlib.util.spec_from_file_location(
    "kit_flow_mask", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks", "flow.py"))
_FLOW = importlib.util.module_from_spec(_FLOW_SPEC)
_FLOW_SPEC.loader.exec_module(_FLOW)


def mask_text(text):
    """The hooks' mask on a file's text; None (a file that could not be read) stays None."""
    return _FLOW.mask(text) if isinstance(text, str) else text


KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ID_RX = re.compile(r"[A-Za-z0-9._-]+")
WORD_RX = re.compile(r"^(?:Verdict|Status):\s*(PASS_WITH_FINDINGS|PASS|FAIL|DONE_WITH_CONCERNS|DONE|NEEDS_CONTEXT|BLOCKED)\s*$", re.M)
MARKER_RX = re.compile(r"\[NEEDS CLARIFICATION[^\]]*\]")
# An agent whose start has no stop for this long is a session that died, not a running agent.
STALE_AFTER = 15 * 60
EVENTS_PER_TASK = 400
# The hook never rotates events.jsonl: read its tail, where the newest events are.
EVENTS_TAIL_BYTES = 20_000_000
# Same rule as the hook's ALERT_ID_RX: ids become path components.
ALERT_ID_RX = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}")
ALERT_KINDS = ("question", "permission", "turn")
# Without a Claude Code pid to check, an alert of a killed session would show forever.
NO_PID_MAX_AGE = 24 * 3600
# The holder rewrites its heartbeat every 2 s; older than this, nobody is holding the question.
HOLDER_STALE = 15
MAX_BODY = 64 * 1024
MAX_OTHER = 4000
LOCAL_HOSTS = ("127.0.0.1", "localhost")

# Values the page translates (index.html); stage agent names of flow.json are statuses too,
# returned while the task is between stages.
# "triage" is only the synthetic task of a worktree whose session has not run `work.sh init` yet.
STATUSES = ("purpose", "running", "blocked", "clarify", "findings", "fixing", "ready-for-mr", "done", "FAIL", "triage")
NEXT_STEPS = ("contract", "change", "proof", "reviews", "wait", "unblock", "clarify", "fix-fail",
              "decide-findings", "wait-fixes", "fix-loop", "open-mr", "fix-mr-check", "mark-done", "clean-up")
WARNINGS = ("fail-unresolved",)
MR_ERRORS = ("no-token", "unavailable", "unsupported-remote", "host-not-allowed")
REFUSALS = ("no-such-task", "not-done", "detached", "main-worktree", "serving-worktree", "dirty",
            "unpushed", "no-remote-branch", "other-task-not-done", "live-session", "sensitive-files",
            "too-many-ignored")
ALERT_ACTIONS = ("answer-on-page", "approve-on-page", "reply-on-page", "answer-in-terminal", "answer-in-terminal-or-enable",
                 "approve-in-terminal", "prompt-session", "open-session", "fix-mr-check")
FINDINGS_REASONS = ("no-decision", "decision-without-date", "review-newer", "some-open")
SEVERITIES = ("critical", "high", "medium", "low", "info", "unknown")
ERROR_CODES = ("bad-body", "empty", "plan-changed", "refused", "no-such-task", "already-done",
               "no-such-finding", "not-open")
FINDING_STATES = ("open", "fix-pending", "fixed", "accepted")
STATE_OF_DECISION = {"fix": "fix-pending", "fixed": "fixed", "accepted": "accepted"}
CLOSE_WARNINGS = ("fail", "open-findings")
# Errors a dev fixes by giving the server a usable token; unsupported-remote is fixed by closing.
TOKEN_ERRORS = ("no-token", "unavailable", "host-not-allowed")
WORK_SH = "bash .claude/kit/scripts/work.sh"
# A git-ignored file that goes with a removed worktree and that nobody can recreate from the
# remote: a local secret. Its presence refuses the removal instead of deleting it silently.
# `.env`, `.env.*`, any `*.env` (e.g. docs/<name>.env) and `.envrc` included. A project adds its
# own directories (copies of PCI files, key folders) with `sensitive_ignored` in flow.json.
SENSITIVE_NAME = re.compile(r"^(\.env(\..*)?|\.envrc|.*\.env|.*\.(pem|key|p12|pfx|jks|keystore|asc|gpg)|id_rsa.*|id_ed25519.*|credentials.*)$", re.I)
SENSITIVE_DIRS = {"keys", "secrets", ".ssh", ".aws"}
# The confirmation lists every ignored file that goes with a worktree; past this many it cannot,
# and the removal is refused instead of deleting files the dev never saw listed.
IGNORED_SHOWN = 200
CLEANUP_ITEMS = ("state", "worktree", "memory")
# An MR of develop -> main would make every task of that checkout done: no lookup there.
BASE_BRANCHES = ("main", "master", "develop")
# The page polls every 2 s; the forge is asked at most once a minute per branch, failures included.
MR_TTL = 60
MR_TIMEOUT = 5
# Forges with neither GitHub's nor GitLab's API: asking them would read as "no PR" forever.
OTHER_FORGES = ("bitbucket.org", "dev.azure.com", "ssh.dev.azure.com", "codeberg.org")
TOKEN_VARS = {"github": ("GITHUB_TOKEN", "GH_TOKEN"), "gitlab": ("GITLAB_TOKEN",)}
# Memory cleanup requests a session consumes on its next prompt (hooks/attention.py). The
# leading dot keeps it out of the session id rule, so SessionStart never clears it as a session.
REQUESTS = ".requests"
_mr_cache, _mr_lock = {}, threading.Lock()
# Every token the lookup read, so an error text that quotes one is masked before it is sent.
_seen_tokens = set()


def git_out(wt, *args, timeout=10):
    """stdout of a local git command, or None when it failed: callers refuse on None."""
    try:
        p = subprocess.run(["git", "-C", wt, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def listed_worktrees(root):
    """[(path, branch)] in git's order: the main worktree is the first one."""
    out = git_out(root, "worktree", "list", "--porcelain") or ""
    found, path = [], None
    for line in out.splitlines() + [""]:
        if line.startswith("worktree "):
            path, branch = line[len("worktree "):], ""
        elif line.startswith("branch ") and path:
            branch = line[len("branch "):].removeprefix("refs/heads/")
        elif line.startswith("detached") and path:
            branch = "(detached)"
        elif not line and path:
            found.append((path, branch))
            path = None
    return found


def worktrees(root):
    """[(path, branch)] of every worktree of the repository at root, root's own first."""
    found = listed_worktrees(root)
    real = os.path.realpath(root)
    found.sort(key=lambda w: os.path.realpath(w[0]) != real)
    return found or [(root, "")]


def work_dir(wt):
    """The worktree's work directory, with the same refusal as the hook; None if unusable."""
    try:
        with open(os.path.join(wt, ".claude", "flow.json")) as f:
            rel = json.load(f).get("work_dir", ".work")
    except (OSError, ValueError, AttributeError):
        rel = ".work"
    d, r = os.path.realpath(os.path.join(wt, rel)), os.path.realpath(wt)
    return d if d != r and d.startswith(r + os.sep) else None


def read(path, limit=200_000):
    try:
        with open(path, errors="replace") as f:
            return f.read(limit)
    except OSError:
        return None


def events(wdir):
    """Events of the worktree grouped by task, from the tail of the log. A line that is not
    an object with a numeric t is skipped, so one corrupt line cannot take the page down."""
    path, by_work = os.path.join(wdir, "events.jsonl"), {}
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - EVENTS_TAIL_BYTES))
            data = f.read()
    except OSError:
        return by_work
    lines = data.decode(errors="replace").splitlines()
    if size > EVENTS_TAIL_BYTES and lines:
        lines = lines[1:]  # cut in the middle by the seek
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or not isinstance(e.get("t"), (int, float)) or not isinstance(e.get("work"), str):
            continue
        e["agent"] = str(e.get("agent") or "main")
        by_work.setdefault(e["work"], []).append(e)
    return by_work


def agents(evs, now):
    """One entry per agent instance, in the order they were last seen."""
    lanes = {}
    for e in evs:
        # A stop with no type from an agent never seen is not a flow agent (measured on a real
        # task: 26 of those, each shown as "main finished, took 0s").
        if e.get("ev") == "stop" and not e.get("type") and (e.get("agent") or "main") not in lanes:
            continue
        a = lanes.setdefault(e.get("agent") or "main", {
            "agent": e.get("agent") or "main", "type": e.get("type") or "main",
            "state": "", "tools": 0, "start": e.get("t", 0), "ran": False})
        ev = e.get("ev")
        if ev == "start":
            # ran: a start event was seen, so "start" is when the agent began and not merely the
            # first event left in the log tail; the fix round trusts only those.
            a.update(state="running", start=e.get("t", 0), summary=None, end=None, ran=True)
        elif ev == "tool":
            # A blocked agent was sent back to write its file: a tool call means it is at it.
            if a["state"] == "blocked":
                a["state"] = "running"
            a["tools"] += 1
            a["last"] = f"{e.get('tool') or ''}  {e.get('detail') or ''}".strip()
        elif ev == "stop":
            a.update(state="failed" if e.get("gate") == "failed" else "ok", summary=e.get("detail"), end=e.get("t"))
        elif ev == "blocked":
            a.update(state="blocked", summary=e.get("detail"))
        a["seen"] = e.get("t", 0)
    for a in lanes.values():
        # main has no start event: it is "running" while it keeps calling tools.
        if a["agent"] == "main":
            a["state"] = "running" if now - a["seen"] < STALE_AFTER else "idle"
        elif a["state"] in ("running", "blocked") and now - a["seen"] >= STALE_AFTER:
            a["state"] = "stale"
    return sorted(lanes.values(), key=lambda a: a["seen"], reverse=True)


NOT_APPLICABLE_RX = re.compile(r"^##\s+Not applicable\s*$", re.M | re.I)
# The contract reviewer re-runs every fix round; the other lenses only when their last review had
# findings.
ALWAYS_RERUN = "reviewer"


def stages(flow, tdir):
    """Each stage of flow.json with its file state; a review also says whether it is "not
    applicable" (a PASS with a `## Not applicable` section; a FAIL stays a FAIL) and whether it needs no re-run
    in a fix round (`settled`: a lens other than the contract reviewer whose last review is a PASS,
    so it had no finding to fix)."""
    out = []
    for agent, st in (flow.get("stages") or {}).items():
        text = read(os.path.join(tdir, st.get("file", "")))
        m = WORD_RX.search(text or "")
        word = m.group(1) if m else None
        review = bool(st.get("verdict"))
        verdict = word if review else None
        out.append({"agent": agent, "file": st.get("file"), "written": text is not None,
                    "needs_verdict": review, "verdict": verdict, "status": None if review else word,
                    "open": len(MARKER_RX.findall(text or "")),
                    "na": review and verdict == "PASS" and bool(text and NOT_APPLICABLE_RX.search(text)),
                    "settled": review and agent != ALWAYS_RERUN and verdict == "PASS"})
    return out


def status(stage_list, lanes, tdir=None):
    """FAIL > findings | fixing | ready-for-mr > blocked > clarify > running > the last stage
    written. "done" is not here: only a PR/MR, a merge or `work.sh done` closes a task
    (task_state). A stage let through on the retry with its gate failed is FAIL, as `work.sh
    status` says."""
    verdicts = [s for s in stage_list if s["needs_verdict"]]
    # Only the latest run of each stage counts: a re-run that passed the gate clears the old one.
    latest = {}
    for a in sorted(lanes, key=lambda a: a["seen"]):
        latest[a["type"]] = a
    if any(a["state"] == "failed" for a in latest.values()):
        return "FAIL"
    header = (read(os.path.join(tdir, "findings-decision.md"), 200) or "").startswith("Decided:") if tdir else False
    if any(s["verdict"] == "FAIL" for s in stage_list):
        # A FAIL waits on the dev while its findings are undecided, and comes back when a re-review
        # fails again (the new review reopens the decision). Decided, with a fix pending or a flow
        # agent at work, it is the fix round: the page said FAIL while the executor fixed.
        failed = [s for s in verdicts if s["verdict"] == "FAIL" and s["written"]]
        items = decided_findings(tdir, stage_list)
        if items and header and not any(f["decision"] is None for f in items) and findings_decided(tdir, failed) and (
                any(f["state"] == "fix-pending" for f in items) or flow_agent_running(stage_list, lanes)):
            return "fixing"
        return "FAIL"
    if verdicts and all(s["verdict"] for s in verdicts):
        # Decided means every finding the page shows has a decision line, not only that a
        # decision file is newer than the reviews: one line must not settle three findings.
        with_findings = any(s["verdict"] == "PASS_WITH_FINDINGS" for s in verdicts)
        items = decided_findings(tdir, stage_list) if with_findings else []
        # Something to decide: a finding with no decision line, or findings the dev never dated a
        # decision for. A lens re-run that brings no undecided finding (a PASS, or findings already
        # decided) asks nothing: the page asked to decide with 0 open after two lenses re-ran clean.
        if with_findings and (any(f["decision"] is None for f in items) or not header):
            return "findings"
        # A fix round is not ready for a PR/MR: a finding decided fix and not yet fixed, an agent
        # of the flow at work, or a stage whose file an earlier stage's agent will rewrite (the
        # page read ready-for-mr while the executor of the fix round ran).
        why = fix_round(stage_list, lanes, tdir, items)
        if why in ("pending", "agent"):
            return "fixing"
        if why == "stale":
            # Every fix is in and nothing runs, but a lens has not re-read the round's work: the
            # task is paused before its reviews, as the terminal shows, not running.
            before = [s["agent"] for s in stage_list if not s["needs_verdict"] and s["written"] and s["agent"] != "retrospective"]
            return before[-1] if before else "fixing"
        return "ready-for-mr"
    # Only subagents can be blocked: agents() sets main to running or idle.
    if any(a["state"] == "blocked" for a in lanes) or any(s["status"] in ("BLOCKED", "NEEDS_CONTEXT") for s in stage_list):
        return "blocked"
    if any(s["open"] for s in stage_list):
        return "clarify"
    if any(a["state"] == "running" for a in lanes):
        return "running"
    written = [s["agent"] for s in stage_list if s["written"]]
    return written[-1] if written else "purpose"


def flow_agent_running(stage_list, lanes):
    """An agent of a flow stage at work: a general-purpose subagent the session runs for itself
    is no stage of the task and does not hold it."""
    types = {s["agent"] for s in stage_list}
    return any(a["type"] in types and a["state"] == "running" for a in lanes)


def fix_round(stage_list, lanes, tdir, items):
    """Why the task is not ready for a PR/MR after its reviews, or None: "pending" while a finding
    decided fix is not fixed, "agent" while a flow agent runs, "stale" when a stage file is older
    than the start of an agent of an earlier stage before the reviews (that round will write it
    again)."""
    if any(f["state"] == "fix-pending" for f in items):
        return "pending"
    if flow_agent_running(stage_list, lanes):
        return "agent"
    since = 0.0
    for s in stage_list:
        # A lens with no finding in its last review is not re-run by the fix round: its file being
        # older than the round is no sign of a review to come.
        if s.get("settled") or s["agent"] == "retrospective":
            continue
        if s.get("file") and s["written"] and tdir:
            written = (mtime_ns(os.path.join(tdir, s["file"])) or 0) / 1e9
            if written < since:
                return "stale"
        # Only the stages before the reviews move the mark: lenses run side by side, and one
        # lens re-run alone says nothing about the others.
        if not s["needs_verdict"]:
            starts = [a.get("start") or 0 for a in lanes if a["type"] == s["agent"] and a.get("ran")]
            since = max([since, *starts])
    return None


def mtime_ns(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def findings_decided(tdir, reviews):
    """True when findings-decision.md (written by the orchestrator after the dev picked the
    findings to fix) is at least as new as every review: a lens re-run reopens the decision.
    Nanoseconds and >=, so a decision written in the same second as the review still counts."""
    if not tdir:
        return False
    path = os.path.join(tdir, "findings-decision.md")
    if not (read(path, 200) or "").startswith("Decided:"):
        return False
    decided = mtime_ns(path)
    return all((mtime_ns(os.path.join(tdir, s["file"])) or 0) <= decided for s in reviews)


STEP_OF_STAGE = {"refiner": "contract", "executor": "change", "tester": "proof"}


def next_step(st, stage_list):
    """A key, not prose: the page says it in the dev's language."""
    fixed = {"done": "clean-up", "FAIL": "fix-fail", "findings": "decide-findings", "clarify": "clarify",
             "ready-for-mr": "open-mr", "blocked": "unblock", "running": "wait", "fixing": "wait-fixes"}
    if st in fixed:
        return fixed[st]
    # The retrospective runs after the reviews: it is never the next step before them.
    pending = next((s for s in stage_list if not s["written"] and s["agent"] != "retrospective"), None)
    if pending is None or pending["needs_verdict"]:
        return "reviews"
    return STEP_OF_STAGE.get(pending["agent"], "reviews")


def task_state(stage_list, lanes, tdir, mr, lookup_possible=True):
    """status plus what closes it: an open or merged MR, or the `closed` marker of `work.sh done`.
    A FAIL that an MR or the marker closed stays visible as a warning. lookup_possible is False
    on a base, detached or empty branch, where only `work.sh done` (or the page's "Mark as done")
    can close the task."""
    st = status(stage_list, lanes, tdir)
    warning = "fail-unresolved" if st == "FAIL" else None
    closed_by = None
    if isinstance(mr, dict) and mr.get("state") in ("opened", "merged"):
        closed_by = "mr"
    elif tdir and os.path.isfile(os.path.join(tdir, "closed")):
        closed_by = "work.sh"
    if closed_by:
        st = "done"
    else:
        warning = None
    step = next_step(st, stage_list)
    if st == "ready-for-mr":
        error = mr.get("error") if isinstance(mr, dict) else None
        # A lookup that failed is not "no MR": opening one again is the wrong advice.
        if error in TOKEN_ERRORS:
            step = "fix-mr-check"
        elif not lookup_possible or error == "unsupported-remote":
            step = "mark-done"
    return {"status": st, "warning": warning, "closed_by": closed_by, "mr": mr,
            "next_step": step, "cleanable": st == "done", "closable": st != "done"}


FINDING_ITEM = re.compile(r"^(?:[-*]|\d+[.)])\s+(.*)$")
FINDING_HEADING = re.compile(r"^(#{3,})\s+(.*)$")
FINDING_DETAILS_MAX = 6000
SEVERITY_NAMES = r"critical|blocker|high|major|important|medium|low|minor|info"
SEVERITY_WORD = re.compile(r"\b(" + SEVERITY_NAMES + r")\b", re.I)
# Forms the reviews use, most explicit first: "Severity: medium", "[medium]" or "(medium)",
# "**Medium**"; then the word near the start ("High: ...", "1. **High: ...**").
SEVERITY_FORMS = [re.compile(r"\bseverity\s*[:=]\s*\**\s*(" + SEVERITY_NAMES + r")\b", re.I),
                  re.compile(r"[\[(]\s*(" + SEVERITY_NAMES + r")\s*[\])]", re.I),
                  re.compile(r"\*\*\s*(" + SEVERITY_NAMES + r")\b", re.I)]
SEVERITY_OF = {"blocker": "critical", "major": "high", "important": "high", "minor": "low"}
SEVERITY_LEAD = re.compile(r"^(?:\d+[.)]\s*)?[\[(]?\s*(?:" + SEVERITY_NAMES + r")\s*[\])]?(?:\s*[:,]|\s*[.-](?=\s))?\s*", re.I)
# `- fix|fixed|accepted <severity> <lens> [<key>]: <summary>`. The key names one finding; a line
# without it is the older form, read only where it cannot name the wrong finding.
DECISION_LINE = re.compile(
    r"^-\s+(fixed|fix|accepted)\s+([A-Za-z]+)\s+([A-Za-z0-9_-]+)(?:\s+\[([0-9a-f]{12})\])?\s*:\s*(.*)$", re.I)


def severity(text):
    """The finding's severity, or "unknown"; the form the reviewer agents are told to use is
    `- [medium] ...` or `### N. (medium) ...`, the other forms are read for older reviews."""
    for rx in SEVERITY_FORMS:
        m = rx.search(text)
        if m:
            break
    else:
        m = SEVERITY_WORD.search(text[:60])
    if not m:
        return "unknown"
    word = m.group(1).lower()
    return SEVERITY_OF.get(word, word)


def summary_of(text, limit=200):
    text = re.sub(r"\s+", " ", re.sub(r"[*`]", "", text)).strip()
    text = re.sub(r"^\d+[.)]\s+", "", text)
    # The kit's route tag says who fixes it (scripts/decisions.py), not what the finding is.
    return re.sub(r"^\[route:\s*\w+\]\s*", "", SEVERITY_LEAD.sub("", text, count=1), flags=re.I)[:limit]


def review_findings(tdir, stage_list):
    """[{lens, severity, summary, full_text, details}] from the "## Findings" section of every
    review that is not a plain PASS. A finding is a top-level list item, or a `###` heading that is
    numbered or starts with its severity; what follows it (indented lines, a deeper heading, the
    list items under a heading) is its details, and a "Severity:" line there gives the severity
    the heading did not."""
    out = []
    for s in stage_list:
        if not s["needs_verdict"] or s["verdict"] not in ("PASS_WITH_FINDINGS", "FAIL"):
            continue
        m = re.match(r"^04-review-(.+)\.md$", s["file"] or "")
        lens = m.group(1) if m else s["agent"]
        text = read(os.path.join(tdir, s["file"])) or ""
        sec = re.search(r"^##\s+Findings\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
        heading = None  # the finding a heading opened, while its details follow
        level = 0  # that heading's depth: a deeper heading (#### Fix) is part of its details
        current = None  # the finding the next indented or blank lines belong to
        for line in (sec.group(1) if sec else "").splitlines():
            h = FINDING_HEADING.match(line)
            if h and heading is not None and len(h.group(1)) > level:
                heading["_details"].append(line)
                continue
            if h:
                body = h.group(2).strip()
                if re.match(r"^\d+[.)]\s", body) or SEVERITY_LEAD.match(body) and SEVERITY_WORD.match(
                        re.sub(r"^[\[(*\s]+", "", body)):
                    heading = {"lens": lens, "severity": severity(body), "summary": summary_of(body),
                               "full_text": summary_of(body, None), "route": decisions.route_of(body),
                               "verdict": s["verdict"], "_details": []}
                    level = len(h.group(1))
                    out.append(heading)
                else:
                    heading = None
                current = heading
                continue
            if heading is not None:
                if heading["severity"] == "unknown" and SEVERITY_FORMS[0].search(line):
                    heading["severity"] = severity(line)
                heading["_details"].append(line)
                continue
            item = FINDING_ITEM.match(line)
            if not item:
                # A list finding's details: its indented sub-items and paragraphs.
                if current is not None and (not line.strip() or line[:1] in " \t"):
                    current["_details"].append(line)
                else:
                    current = None
                continue
            body = item.group(1).strip()
            if re.fullmatch(r"(none|nothing|n/?a)\.?", body, re.I):
                current = None
                continue
            current = {"lens": lens, "severity": severity(body), "summary": summary_of(body),
                       "full_text": summary_of(body, None), "route": decisions.route_of(body),
                       "verdict": s["verdict"], "_details": []}
            out.append(current)
    # The dev decides on the whole finding (scenario, fix), not on a title cut at 200 characters:
    # the details go to the page, masked with the hooks' mask like every alert text. The key and
    # the match of an older keyless line read the raw words, so a decision recorded before the
    # mask still lands; every text the page gets is masked.
    for f in out:
        f["key"] = finding_key(f)
        f["_raw"] = norm_summary(f["summary"])
        f["summary"] = _FLOW.mask(f["summary"])
        f["full_text"] = _FLOW.mask(f["full_text"])
        f["details"] = _FLOW.mask(textwrap.dedent("\n".join(f.pop("_details"))).strip())[:FINDING_DETAILS_MAX]
    return out


def findings_view(tdir, stage_list, st):
    """findings with the dev's decision (or None, open), and why the task reads "findings".
    A decision line names (severity, lens); one line deciding several lenses' findings at once
    counts once, so what it did not name stays open, visible instead of silently accepted."""
    items = decided_findings(tdir, stage_list)
    path = os.path.join(tdir, "findings-decision.md") if tdir else ""
    text = read(path) if tdir else None
    reason, newer = None, []
    if st == "findings":
        if text is None:
            reason = "no-decision"
        elif not text.startswith("Decided:"):
            reason = "decision-without-date"
        elif any(f["decision"] is None for f in items):
            decided = mtime_ns(path) or 0
            newer = [re.sub(r"^04-review-(.+)\.md$", r"\1", s["file"]) for s in stage_list
                     if s["needs_verdict"] and s["written"] and (mtime_ns(os.path.join(tdir, s["file"])) or 0) > decided]
            # A current, dated decision that leaves some findings without a line: partly decided.
            reason = "review-newer" if newer else "some-open"
    return {"findings": items, "findings_open": sum(f["decision"] is None for f in items),
            "findings_reason": reason, "findings_review_newer": newer}


def decided_findings(tdir, stage_list, pins=None):
    """The reviews' findings, each with the decision line that names it (state "open" when none).

    A line with a key decides the finding with that key, and only it: a lens re-run that words a
    finding differently gives it a new key, so an old decision never lands on a new finding. A
    line without a key (the older form) decides a finding only when that cannot be wrong: its
    summary is the finding's, or, in a file with no keyed line at all and not older than that
    lens's review, it is the only line for a (lens, severity) that has only one finding.
    pins, when a dict, receives {line number: key} for each keyless line that decided a finding."""
    items = review_findings(tdir, stage_list) if tdir else []
    path = os.path.join(tdir, "findings-decision.md") if tdir else ""
    text = read(path) if tdir and items else None
    keyed, legacy = {}, []
    for n, line in enumerate((text or "").splitlines()):
        m = DECISION_LINE.match(line.strip())
        if not m:
            continue
        word, sev, lens, key, summary = m.groups()
        entry = [word.lower(), SEVERITY_OF.get(sev.lower(), sev.lower()), lens.lower(), norm_summary(summary), n, key]
        if key:
            keyed[key] = entry
        else:
            legacy.append(entry)

    def decide(f, entry):
        f["state"] = STATE_OF_DECISION[entry[0]]
        f["decision"] = "accepted" if entry[0] == "accepted" else "fix"
        if pins is not None and not entry[5]:
            pins[entry[4]] = f["key"]
    for f in items:
        f["key"] = f.get("key") or finding_key(f)
        f["decision"] = f["state"] = None
        if f["key"] in keyed:
            decide(f, keyed[f["key"]])
    for f in items:
        hit = next((e for e in legacy if f["state"] is None and e[2] == f["lens"].lower()
                    and e[3] in (f.get("_raw"), norm_summary(f["summary"]))), None)
        if hit:
            legacy.remove(hit)
            decide(f, hit)
    if not keyed and legacy:
        decided_at = mtime_ns(path) or 0
        fresh = {re.sub(r"^04-review-(.+)\.md$", r"\1", s["file"]).lower() for s in stage_list
                 if s["needs_verdict"] and s["written"] and (mtime_ns(os.path.join(tdir, s["file"])) or 0) <= decided_at}
        for f in items:
            lens, sev = f["lens"].lower(), f["severity"]
            if f["state"] is not None or lens not in fresh:
                continue
            same = [g for g in items if g["lens"].lower() == lens and g["severity"] == sev]
            lines = [e for e in legacy if e[2] == lens and e[1] == sev]
            if len(same) == 1 and len(lines) == 1:
                legacy.remove(lines[0])
                decide(f, lines[0])
    # A fix is proven by the lens re-running without the finding. A review newer than the decision
    # file that still lists a finding decided `fix` or marked `fixed` says the problem is still
    # there: the finding opens again, never "ready for PR/MR" on an old line. `accepted` carries
    # over: it was the dev's call on that very text.
    decided_at = mtime_ns(path) if text is not None else None
    if decided_at:
        newer = {re.sub(r"^04-review-(.+)\.md$", r"\1", s["file"]).lower() for s in stage_list
                 if s["needs_verdict"] and s["written"] and (mtime_ns(os.path.join(tdir, s["file"])) or 0) > decided_at}
        for f in items:
            if f["decision"] == "fix" and f["lens"].lower() in newer:
                f["decision"] = f["state"] = None
                f["reopened"] = True
    for f in items:
        f["state"] = f["state"] or "open"
        f.pop("_raw", None)
    return items


def norm_summary(text):
    return re.sub(r"\s+", " ", re.sub(r"[*`]", "", text or "")).strip().rstrip(".").lower()


def finding_key(f):
    """Stable while the reviewer keeps the finding's words: lens and normalized summary."""
    return hashlib.sha1(f"{f['lens'].lower()}|{norm_summary(f['summary'])}".encode()).hexdigest()[:12]


_decision_lock = threading.Lock()


def decide_finding(root, body, now=None):
    """(status, answer) for one finding the dev decides on the page: appends its decision line,
    keeps every other line and dates the header. Only an open finding of an existing task."""
    now = time.time() if now is None else now
    if not isinstance(body, dict) or body.get("decision") not in ("fix", "accepted") \
            or not isinstance(body.get("key"), str):
        return 400, failure("bad-body", "body needs wt, id, key and decision fix|accepted")
    wi, tid = body.get("wt"), body.get("id")
    wts = worktrees(root)
    if not isinstance(wi, int) or isinstance(wi, bool) or not 0 <= wi < len(wts) or not isinstance(tid, str) \
            or not ID_RX.fullmatch(tid):
        return 404, failure("no-such-task", "no such task")
    wdir = work_dir(wts[wi][0])
    if not wdir or tid not in task_ids(wdir):
        return 404, failure("no-such-task", "no such task")
    tdir = os.path.join(wdir, tid)
    with _decision_lock:
        pins = {}
        found = next((f for f in decided_findings(tdir, stages(flow_of(wts[wi][0]), tdir), pins)
                      if hmac.compare_digest(f["key"], body["key"])), None)
        if found is None:
            return 404, failure("no-such-finding", "no such finding: the review changed")
        if found["state"] != "open":
            return 409, failure("not-open", "the finding already has a decision")
        path = os.path.join(tdir, "findings-decision.md")
        lines = (read(path) or "").splitlines()
        # A keyless line that decides a finding now gets that finding's key: once the file has
        # keyed lines a keyless one matches only its exact summary, and the decision the dev saw
        # must not reopen.
        for n, key in pins.items():
            lines[n] = re.sub(r"^(\s*-\s+\S+\s+\S+\s+[A-Za-z0-9_-]+)(\s*:)", rf"\1 [{key}]\2", lines[n], count=1)
        if lines and lines[0].startswith("Decided:"):
            lines = lines[1:]
        line = f"- {body['decision']} {found['severity']} {found['lens']} [{found['key']}]: {found['summary']}"
        text = "\n".join([f"Decided: {time.strftime('%Y-%m-%d', time.gmtime(now))}", *lines, line]) + "\n"
        tmp = os.path.join(tdir, f".tmp-{secrets.token_hex(6)}")
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    return 200, {"ok": True, "state": STATE_OF_DECISION[body["decision"]]}


def branch_kind(branch, flow):
    if not branch:
        return "none"
    if branch == "(detached)":
        return "detached"
    return "base" if branch in base_branches(flow) else "feature"


def step_command(state, tid, wt, root, remote_host=None, forge=None):
    """(command, kind) for the task's next step: what the dev runs, with no secret in it."""
    step, q = state["next_step"], shlex.quote
    if step == "fix-mr-check":
        host = f"export FORGE_HOST={q(remote_host)} && " if state["mr"].get("error") == "host-not-allowed" and remote_host else ""
        var = "GITHUB_TOKEN" if forge == "github" else "GITLAB_TOKEN"
        # The token is read without echo, never typed into the command line: a command line goes
        # to the shell history, and this one is copied from the page.
        return (f"cd {q(root)} && {host}read -rs {var} && export {var} && "
                f"{WORK_SH} ui stop && {WORK_SH} ui start", "shell")
    if step == "open-mr":
        # ship.py pushes and opens the PR/MR when flow.json "ship" is on, and otherwise prints the
        # push and the command for the dev's forge tool: either way one line to run.
        return f"cd {q(wt)} && python3 .claude/kit/scripts/ship.py {q(tid)}", "shell"
    if step == "mark-done":
        return f"cd {q(wt)} && {WORK_SH} done {tid}", "shell"
    if step == "decide-findings":
        return f"cd {q(wt)} && claude {q('decide the findings of ' + tid)}", "shell"
    if step == "clean-up":
        return f"cd {q(wt)} && {WORK_SH} cleanup {tid} --state", "shell"
    return None, None


def parse_remote(url):
    """(host, project path, token in the URL or None) of a remote URL; None when it is not one."""
    url = (url or "").strip()
    m = re.match(r"^([a-z][a-z0-9+.-]*)://(?:([^@/]*)@)?([^/:@]+)(?::\d+)?/(.+)$", url)
    if m:
        scheme, user, host, path = m.groups()
        token = None
        if scheme in ("http", "https") and user and ":" in user:
            token = urllib.parse.unquote(user.split(":", 1)[1]) or None
    else:
        m = re.match(r"^(?:[^@/]+@)?([^:/]+):(.+)$", url)
        if not m:
            return None
        (host, path), token = m.groups(), None
    path = path.strip("/").removesuffix(".git")
    return (host.lower(), path, token) if host and path else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib forwards custom headers on a redirect: the token would go to whatever host
    # the redirect names.
    def redirect_request(self, *a, **k):
        return None


# Some proxies in front of a forge (a Cloudflare, measured) answer 403 to urllib's default
# "Python-urllib/x.y" User-Agent, so every lookup would read "unavailable".
USER_AGENT = "agent-flow-dashboard/1.0"


def http_fetch(url, headers, timeout):
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    with opener.open(request, timeout=timeout) as r:
        return json.loads(r.read())


# Replaced by the tests: no forge is called from a test.
FETCH = http_fetch


def base_branches(flow):
    got = flow.get("base_branches")
    if isinstance(got, list) and all(isinstance(b, str) for b in got):
        return tuple(got)
    return BASE_BRANCHES


def forge_hosts(flow):
    """Hosts an environment token may be sent to: github.com, `forge_hosts` in flow.json and
    FORGE_HOST (comma-separated); `gitlab_hosts` and GITLAB_HOST are read too. Any other host
    gets only a token written in its own remote URL, so a token exported for one forge never
    reaches another repository's."""
    hosts = []
    for key in ("forge_hosts", "gitlab_hosts"):
        if isinstance(flow.get(key), list):
            hosts += [h for h in flow[key] if isinstance(h, str)]
    for var in ("FORGE_HOST", "GITLAB_HOST"):
        hosts += (os.environ.get(var) or "").split(",")
    return {h.strip().lower() for h in hosts if h.strip()} | {"github.com"}


def forge_of(host, flow):
    """"github", "gitlab" or None. flow.json "forge" wins (a GitHub Enterprise or a GitLab on
    its own domain); otherwise github.com is GitHub and any host not known as another forge is
    taken as a GitLab."""
    named = flow.get("forge")
    if named in ("github", "gitlab"):
        return named
    if named == "none" or any(host == f or host.endswith("." + f) for f in OTHER_FORGES):
        return None
    return "github" if host == "github.com" else "gitlab"


def env_token(forge):
    return next((os.environ[v] for v in TOKEN_VARS.get(forge, ()) if os.environ.get(v)), None)


def forge_mrs(host, path, branch, forge, token):
    """{"mrs": [{iid, state opened|merged, web_url, title, source_branch, created}]} of the
    branch, or {"error": "unavailable"} on any failure, never "no PR"."""
    if forge == "gitlab":
        url = (f"https://{host}/api/v4/projects/{urllib.parse.quote(path, safe='')}/merge_requests"
               f"?source_branch={urllib.parse.quote(branch, safe='')}&state=all&per_page=20")
        headers = {"PRIVATE-TOKEN": token}
    else:
        api = "https://api.github.com" if host == "github.com" else f"https://{host}/api/v3"
        owner = path.split("/")[0]
        url = (f"{api}/repos/{path}/pulls?head={urllib.parse.quote(owner + ':' + branch, safe='')}"
               f"&state=all&per_page=20")
        headers = {"Accept": "application/vnd.github+json", **({"Authorization": f"Bearer {token}"} if token else {})}
    if token:
        _seen_tokens.add(token)
    try:
        data = FETCH(url, headers, MR_TIMEOUT)
        if not isinstance(data, list):
            raise ValueError("not a list")
        out = []
        for m in data:
            if not isinstance(m, dict):
                continue
            if forge == "gitlab":
                iid, state, web = m.get("iid"), m.get("state"), m.get("web_url")
                src, created = m.get("source_branch"), m.get("created_at")
                # An MR from a fork with the same branch name is not this branch's delivery.
                same = m.get("source_project_id") is not None and m.get("source_project_id") == m.get("target_project_id")
            else:
                iid, web, created = m.get("number"), m.get("html_url"), m.get("created_at")
                state = "merged" if m.get("merged_at") else ("opened" if m.get("state") == "open" else "closed")
                head, base = m.get("head"), m.get("base")
                src = head.get("ref") if isinstance(head, dict) else None
                repo = lambda side: (side.get("repo") or {}).get("full_name") if isinstance(side, dict) else None
                same = repo(head) is not None and repo(head) == repo(base)
            # The page renders web_url as a link: only http(s), never javascript:.
            if same and state in ("opened", "merged") and isinstance(iid, int) and isinstance(web, str) \
                    and re.match(r"^https?://", web):
                out.append({"iid": iid, "state": state, "web_url": web, "title": str(m.get("title") or ""),
                            "source_branch": str(src or ""), "created": str(created or "")})
        out.sort(key=lambda m: (m["created"], m["iid"]), reverse=True)
        return {"mrs": out}
    except Exception:  # noqa: BLE001  any failure is "unavailable"; the text may carry the token
        return {"error": "unavailable"}


def default_base(wt):
    ref = git_out(wt, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    return (ref or "").strip() or "origin/main"


def merged_by_git(wt, branch):
    """True when the branch came into the base (as last fetched) through a merge commit: its
    tip is in the base but off the base's first-parent line. A branch whose tip is on that
    line was just cut from the base, which a fast-forward merge cannot be told apart from."""
    tip = (git_out(wt, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}") or "").strip()
    base = default_base(wt)
    if not tip or git_out(wt, "merge-base", "--is-ancestor", tip, base) is None:
        return False
    return tip not in (git_out(wt, "rev-list", "--first-parent", base) or "").split()



def mr_lookup(wt, branch, flow, now, network=True):
    """PRs/MRs of the worktree's branch, opened or merged, newest first: {"mrs": [...]} or
    {"error": key}, or None when no lookup is made (detached, empty or base branch). A
    failure is a fixed key and is cached like a success: an exception text can carry the URL
    or the header, and retrying every poll would storm the API. A merge git can see counts
    even without a forge (web_url None); network=False asks git only."""
    if not branch or branch == "(detached)" or branch in base_branches(flow):
        return None
    remote = parse_remote(git_out(wt, "remote", "get-url", "origin"))
    forge = forge_of(remote[0], flow) if remote else None
    key = (remote[:2] if remote else (wt, ""), branch, network)
    with _mr_lock:
        hit = _mr_cache.get(key)
        if hit and 0 <= now - hit[0] < MR_TTL:
            return hit[1]
    if not network or flow.get("forge") == "none":
        result = {"mrs": []}
    elif remote is None or forge is None:
        result = {"error": "unsupported-remote"}
    else:
        host, path, url_token = remote
        env = env_token(forge)
        token = url_token or (env if host in forge_hosts(flow) else None)
        if not token and forge == "gitlab":
            result = {"error": "host-not-allowed" if env else "no-token"}
        elif not token and env:
            result = {"error": "host-not-allowed"}
        else:
            # GitHub answers a public repository without a token.
            result = forge_mrs(host, path, branch, forge, token)
    if not result.get("mrs") and merged_by_git(wt, branch):
        result = {"mrs": [{"iid": None, "state": "merged", "web_url": None, "title": "",
                           "source_branch": branch, "created": ""}]}
    with _mr_lock:
        _mr_cache[key] = (now, result)
    return result


def mentions(text, tid):
    return bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(tid) + r"(?![A-Za-z0-9])", text or "", re.I))


def mr_for(tid, current, lookup):
    """The PR/MR that delivers this task: the worktree's branch serves the current task and any
    task named in the branch or title, never every task of the worktree."""
    if lookup is None:
        return None
    if "error" in lookup:
        return {"error": lookup["error"]}
    for m in lookup["mrs"]:
        if tid == current or mentions(m["source_branch"], tid) or mentions(m["title"], tid):
            return {"iid": m["iid"], "state": m["state"], "web_url": m["web_url"]}
    return None


# A task's branch and PR/MR are its own, not the worktree's HEAD: a session that moved on to the
# next part in the same worktree switched HEAD, and the finished part read ready-for-mr again.
# `work.sh init` records the branch; the first PR/MR found is kept.
TASK_BRANCH_RX = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/-]{1,200}$")


def task_branch(tdir, head):
    """The branch the task started on (.work/<ID>/branch), else the worktree's HEAD (a task
    from before the file existed)."""
    got = (read(os.path.join(tdir, "branch"), 300) or "").strip() if tdir else ""
    return got if TASK_BRANCH_RX.match(got) and got != "HEAD" else head


def task_mr(tid, tdir, current, found, branch):
    """The task's PR/MR: the lookup's, kept in .work/<ID>/mr.json once opened or merged; the kept
    one when the lookup finds none or fails, so done survives a branch switch or an outage."""
    mr = mr_for(tid, current, found)
    path = os.path.join(tdir, "mr.json") if tdir else None
    if isinstance(mr, dict) and mr.get("state") in ("opened", "merged") and isinstance(mr.get("iid"), int) \
            and isinstance(mr.get("web_url"), str) and path:
        keep = {"iid": mr["iid"], "url": mr["web_url"], "state": mr["state"], "branch": branch}
        if read_json(path) != keep:
            try:
                tmp = path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(keep, f)
                os.replace(tmp, path)
            except OSError:
                pass
        return mr
    if isinstance(mr, dict) and mr.get("state") in ("opened", "merged"):
        return mr
    kept = read_json(path) if path else None
    if isinstance(kept, dict) and kept.get("state") in ("opened", "merged") and isinstance(kept.get("iid"), int) \
            and isinstance(kept.get("url"), str) and re.match(r"^https?://", kept["url"]):
        return {"iid": kept["iid"], "state": kept["state"], "web_url": kept["url"]}
    return mr


def title(tdir, tid):
    first = (read(os.path.join(tdir, "00-purpose.md"), 2000) or "").split("\n", 1)[0]
    return first.lstrip("# ").removeprefix(tid).strip()


def flow_of(wt):
    try:
        with open(os.path.join(wt, ".claude", "flow.json")) as f:
            flow = json.load(f)
        return flow if isinstance(flow, dict) else {}
    except (OSError, ValueError):
        return {}


def task_ids(wdir):
    try:
        names = sorted(os.listdir(wdir))
    except OSError:
        return []
    return [n for n in names if ID_RX.fullmatch(n) and os.path.isfile(os.path.join(wdir, n, "00-purpose.md"))]


def full_state(wt, branch, flow, tid, tdir, lanes, st_list, found, current, root):
    """task_state plus the next step's command, the branch kind and the findings to decide."""
    kind = branch_kind(branch, flow)
    state = task_state(st_list, lanes, tdir, task_mr(tid, tdir, current, found, branch), lookup_possible=kind == "feature")
    remote = parse_remote(git_out(wt, "remote", "get-url", "origin")) if state["next_step"] == "fix-mr-check" else None
    command, command_kind = step_command(state, tid, wt, root or wt, remote[0] if remote else None,
                                         forge_of(remote[0], flow) if remote else None)
    full = {**state, "command": command, "command_kind": command_kind, "branch_kind": kind,
            **findings_view(tdir, st_list, state["status"])}
    rounds = max(0, sum(1 for l in lanes if l["type"] == "executor") - 1)
    return decisions.enrich(full, tdir, st_list, rounds, flow.get("max_fix_rounds", 3), work_dir(wt), tid)


def worktree_tasks(wt, branch, now, lookup=True, root=None):
    """[task dict] of one worktree, with status, PR/MR and next step. lookup=False asks git
    only, never the forge."""
    wdir = work_dir(wt)
    ids = task_ids(wdir) if wdir else []
    if not ids:
        triage = triage_task(wt, wdir, now)
        return [triage] if triage else []
    flow, evs = flow_of(wt), events(wdir)
    current = (read(os.path.join(wdir, "current"), 200) or "").strip()
    lookups = {}
    tasks = []
    for tid in ids:
        mine, tdir = evs.get(tid, []), os.path.join(wdir, tid)
        tb = task_branch(tdir, branch)
        if tb not in lookups:
            lookups[tb] = mr_lookup(wt, tb, flow, now, network=lookup)
        state = full_state(wt, tb, flow, tid, tdir, agents(mine, now), stages(flow, tdir), lookups[tb], current, root)
        tasks.append({"id": tid, "title": title(tdir, tid), **state, "current": tid == current,
                      "updated": max([e.get("t", 0) for e in mine] or [os.path.getmtime(tdir)])})
    tasks.sort(key=lambda t: (not t["current"], -t["updated"]))
    return tasks


TRIAGE_STAGE = {"agent": "triage", "file": None, "written": False, "needs_verdict": False, "verdict": None}
# A task KEY (ABC-12): written in <work dir>/attention/.task-key by `work.sh triage <ID>`, or the
# name of a worktree opened for that key.
KEY_RX = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")
TASK_KEY_FILE = ".task-key"


def triage_id(wt):
    """The task KEY of a worktree opened for a task: the one recorded in it, else its name when the
    name is a key (abc-12 gives ABC-12), else None. Only a linked worktree (its .git is a file): the
    main checkout, or a worktree with neither, often has a session doing anything but a triage."""
    if not os.path.isfile(os.path.join(wt, ".git")):
        return None
    wdir = work_dir(wt)
    recorded = (read(os.path.join(wdir, "attention", TASK_KEY_FILE), 100) or "").strip() if wdir else ""
    if KEY_RX.match(recorded):
        return recorded
    name = os.path.basename(wt).upper()
    return name if KEY_RX.match(name) else None


def triage_task(wt, wdir, now):
    """The synthetic task of a worktree whose session is still in the task skill's triage: the
    session asks language, lane and the interview before `work.sh init` writes 00-purpose.md, and
    without this the worktree showed nothing for minutes, read as broken. None when the worktree has
    a task or no live session: the real task replaces this one."""
    tid = triage_id(wt) if wdir and not task_ids(wdir) else None
    if not tid or not live_session(wdir, now):
        return None
    adir, marks = os.path.join(wdir, "attention"), []
    try:
        sessions = os.listdir(adir)
    except OSError:
        sessions = []
    for session in sessions:
        if safe_id(session):
            marks.append(mtime_ns(os.path.join(adir, session, SESSION_MARKER)) or 0)
    # Live through an alert or an event only: no marker to date it, so it is as recent as now.
    updated = max(marks) / 1e9 if any(marks) else now
    st = [dict(TRIAGE_STAGE)] + [dict(x) for x in stages(flow_of(wt), os.path.join(wdir, tid))]
    for x in st:
        x["written"], x["verdict"], x["na"], x["settled"] = False, None, False, False
    # Nothing to close, clean or decide: the next step is answering the session, which the
    # worktree's alerts already say.
    return {"id": tid, "title": "triage", "triage": True, "status": "triage", "warning": None,
            "closed_by": None, "mr": None, "next_step": None, "cleanable": False, "closable": False,
            "command": None, "command_kind": None, "branch_kind": None, "findings": [], "findings_open": 0,
            "findings_reason": None, "findings_review_newer": [], "current": False, "updated": updated, "stages": st}


MERGE_TTL = 30
_MERGE_CACHE = {}


def merge_positions(root, now=None):
    """{branch: {pos, total, after, pending, independent}} from merge-order.py, which counts each
    worktree's uncommitted work too (running tasks keep theirs uncommitted); {} when the script
    fails, so the sidebar shows nothing rather than a guessed order. Cached a few seconds."""
    now = time.time() if now is None else now
    ref = default_base(root)
    hit = _MERGE_CACHE.get((root, ref))
    if hit and 0 <= now - hit[0] < MERGE_TTL:
        return hit[1]
    try:
        p = subprocess.run([sys.executable, os.path.join(KIT, "scripts", "merge-order.py"), "--base", ref, "--json"],
                           capture_output=True, text=True, cwd=root, timeout=30)
        got = json.loads(p.stdout) if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        got = None
    out = {}
    if isinstance(got, dict):
        order = got.get("order") or []
        for n, b in enumerate(order, 1):
            out[b] = {"pos": n, "total": len(order), "pending": b in (got.get("pending") or []),
                      "independent": b in (got.get("independent") or []),
                      "after": [e["first"] for e in got.get("edges") or [] if e.get("then") == b]}
    _MERGE_CACHE[(root, ref)] = (now, out)
    return out


def list_tasks(root, now=None):
    now = time.time() if now is None else now
    groups = []
    merge = merge_positions(root, now)
    for i, (wt, branch) in enumerate(worktrees(root)):
        tasks = worktree_tasks(wt, branch, now, root=root)
        if not tasks:
            continue
        # The path lets the page write "cd <path> && claude '<prompt>'" for a step done in a session.
        groups.append({"wt": i, "name": os.path.basename(wt), "branch": branch, "path": wt,
                       "here": os.path.realpath(wt) == os.path.realpath(root), "tasks": tasks,
                       "switches": switches_of(wt), "merge": merge.get(branch)})
    return groups


def task_detail(root, wt_index, tid, now=None):
    now = time.time() if now is None else now
    wts = worktrees(root)
    if not ID_RX.fullmatch(tid or "") or not 0 <= wt_index < len(wts):
        return None
    wt, branch = wts[wt_index]
    wdir = work_dir(wt)
    if not wdir:
        return None
    if tid not in task_ids(wdir):
        triage = triage_task(wt, wdir, now)
        if not triage or triage["id"] != tid:
            return None
        return {**triage, "worktree": os.path.basename(wt), "path": wt, "branch": branch, "purpose": None, "lane": None,
                "clarified": 0, "rounds": 0, "max_rounds": flow_of(wt).get("max_fix_rounds", 3), "rulings": [],
                "files": {}, "agents": [], "events": []}
    tdir, flow = os.path.join(wdir, tid), flow_of(wt)
    mine = events(wdir).get(tid, [])
    lanes, st = agents(mine, now), stages(flow, tdir)
    current = (read(os.path.join(wdir, "current"), 200) or "").strip()
    branch = task_branch(tdir, branch)
    state = full_state(wt, branch, flow, tid, tdir, lanes, st, mr_lookup(wt, branch, flow, now), current, root)
    # Review and stage text goes to the page through the hooks' mask, like every alert text: a
    # reviewer quoting a credential must not put it on the screen.
    files = {s["file"]: mask_text(read(os.path.join(tdir, s["file"]))) for s in st if s["written"]}
    purpose = mask_text(read(os.path.join(tdir, "00-purpose.md")) or "")
    lane = re.search(r"^Lane:\s*(\w+)", purpose, re.M | re.I)
    # The template's example ruling sits inside an HTML comment: it is not a decision.
    ledger = re.sub(r"<!--.*?-->", "", read(os.path.join(tdir, "ledger.md")) or "", flags=re.S)
    return {"id": tid, "title": title(tdir, tid), "worktree": os.path.basename(wt), "path": wt, "branch": branch,
            "purpose": purpose, **state, "lane": lane.group(1) if lane else None,
            "clarified": len(re.findall(r"^- Q:", purpose, re.M)),
            "rounds": max(0, sum(1 for e in mine if e.get("ev") == "start" and e.get("type") == "executor") - 1),
            "max_rounds": flow.get("max_fix_rounds", 3),
            "rulings": [line[2:] for line in ledger.splitlines() if line.startswith("- Ruling:")],
            "stages": st, "files": files, "agents": lanes, "events": mine[-EVENTS_PER_TASK:]}


def safe_id(i):
    return isinstance(i, str) and bool(ALERT_ID_RX.fullmatch(i)) and not i.endswith(".answer")


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError):
        return True
    return True


def live(alert, now):
    """False for an alert whose session is gone: dead Claude Code pid, or no pid and 24 h old."""
    if not isinstance(alert, dict) or alert.get("kind") not in ALERT_KINDS:
        return False
    since = alert.get("since")
    if not isinstance(since, (int, float)):
        return False
    pid = alert.get("claude_pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 1:
        return pid_alive(pid)
    return now - since < NO_PID_MAX_AGE


def held_now(alert, now):
    beat = alert.get("heartbeat")
    return bool(alert.get("held")) and isinstance(beat, (int, float)) and now - beat < HOLDER_STALE


def alerts(wdir, now):
    adir, out = os.path.join(wdir, "attention"), []
    try:
        sessions = sorted(os.listdir(adir))
    except OSError:
        return out
    for session in sessions:
        sdir = os.path.join(adir, session)
        if not safe_id(session) or not os.path.isdir(sdir):
            continue
        try:
            names = sorted(os.listdir(sdir))
        except OSError:
            continue
        for name in names:
            agent = name[:-len(".json")]
            if not name.endswith(".json") or not safe_id(agent):
                continue
            a = read_json(os.path.join(sdir, name))
            if not live(a, now):
                continue
            # Masked again with the server's own mask: the file was written by the worktree's
            # hook, whose masking may be older or edited.
            item = {"session": session, "agent": agent, "kind": a["kind"], "since": a["since"],
                    "detail": _FLOW.mask(str(a.get("detail") or "")), "tool": a.get("tool"), "qid": a.get("qid"),
                    "held": held_now(a, now)}
            if a["kind"] == "turn":
                item["message"] = _FLOW.mask(str(a.get("message") or ""))[-TURN_MAX:]
            if a["kind"] == "question":
                item.update(questions=a.get("questions") or [])
            if a["kind"] in ("question", "turn"):
                item.update(release_of(a, item["held"]))
            out.append(item)
    return out


# Why a hold the page could answer no longer can: attention.py writes the reason when it lets go;
# a hold killed or gone without writing it reads as "stopped" at its last heartbeat. None for a
# question or turn that was never held (the dev answers in the terminal as always).
RELEASE_REASONS = ("timeout", "page-gone", "switch-off", "released", "bad-answer", "agent-running", "stopped")


def release_of(a, held):
    if held:
        return {"released": None, "released_at": None}
    if a.get("released") is not None:
        reason, at = a.get("released"), a.get("released_at")
    elif a.get("held"):
        # Still marked held, heartbeat stale: the hook was killed or its session went away.
        reason, at = "stopped", a.get("heartbeat")
    else:
        return {"released": None, "released_at": None}
    return {"released": reason if reason in RELEASE_REASONS else "stopped",
            "released_at": at if isinstance(at, (int, float)) else None}


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# When a page last said it is in view (document.visibilityState "visible"). The holds wait an
# hour for an open page; a forgotten background tab must not keep them, so only a page in view
# marks `.page` fresh, by its polls and through the event stream.
VISIBLE_FOR = 15
_last_visible = [0.0]


def list_attention(root, now=None, visible=True):
    """Alerts per worktree, including a worktree with no task: the session in the triage is
    the case that waits on the dev most. A call from a page in view marks the page alive for the
    hold; one from a hidden tab (visible False) does not."""
    now = time.time() if now is None else now
    if visible:
        _last_visible[0] = time.time()
    groups = []
    for i, (wt, branch) in enumerate(worktrees(root)):
        wdir = work_dir(wt)
        if not wdir:
            continue
        adir = os.path.join(wdir, "attention")
        if visible and os.path.isdir(adir):
            try:
                with open(os.path.join(adir, ".page"), "a"):
                    pass
                os.utime(os.path.join(adir, ".page"))
            except OSError:
                pass
        found, pending = alerts(wdir, now), memory_requests(wdir)
        answers_on = os.path.exists(os.path.join(adir, "answers-on"))
        for a in found:
            a.update(alert_action(a, answers_on, wt))
            if a["kind"] == "turn" and not a.get("message"):
                a["message"] = transcript_tail(wt, a["session"], os.path.expanduser("~")) or a["detail"]
            a["awaits"] = awaits(a, wdir, now)
            a["idle"] = a["awaits"] and idle_only(a)
        q = shlex.quote
        memory = [{"id": tid, "action": "open-session", "command": f"cd {q(wt)} && claude {q('review the auto memory of ' + tid)}", "command_kind": "shell"}
                  for tid in pending]
        check = mr_check(wt, branch, now, root)
        # A memory request nobody consumed and a failed MR lookup wait on the dev too. A live session
        # with no task and no alert up (a triage between two questions) is listed as well: it
        # carries the switches, and turning one off used to hide the row and the way back.
        if found or pending or check or live_session(wdir, now):
            groups.append({"wt": i, "name": os.path.basename(wt), "branch": branch, "path": wt,
                           "here": os.path.realpath(wt) == os.path.realpath(root),
                           "answers": answers_on, "turns": os.path.exists(os.path.join(adir, "turns-on")), "alerts": found,
                           "memory_requests": pending, "memory_actions": memory, "mr_check": check})
    return groups


# Whether a "your turn" waits on the dev. A session that stops while its subagents run, or whose
# last message is a progress note, is not asking anything: shown as pending, it taught the dev to
# read every turn as noise. The words miss some asks, so a turn nothing resumed for AWAIT_AFTER
# seconds waits on the dev whatever it says, as "idle": shown, neutral, never counted or beeped.
AWAIT_AFTER = 120
RUNNING = ".running"


def subagents_running(wdir, session, now):
    """hooks/attention.py keeps <session>/.running/<agent> between SubagentStart and SubagentStop,
    touched on each of its tool calls. A marker older than STALE_AFTER is an agent whose stop never
    came (a killed session), unless its agent still logs tool events: an older hook wrote the
    marker once, and a long stage read as gone while it worked."""
    rdir = os.path.join(wdir, "attention", session, RUNNING)
    try:
        names = os.listdir(rdir)
    except OSError:
        return False
    if any(now - (mtime_ns(os.path.join(rdir, n)) or 0) / 1e9 < STALE_AFTER for n in names):
        return True
    last = {}
    for evs in events(wdir).values():
        for e in evs:
            last[e["agent"]] = max(last.get(e["agent"], 0), e["t"])
    return any(now - last.get(n, 0) < STALE_AFTER for n in names)


def awaits(alert, wdir, now):
    """True when the alert waits on the dev: a question or a permission always; a turn while the
    hook holds it (its session is blocked until answered), or when no subagent of its session runs
    and either the end of its message asks something or it has stood AWAIT_AFTER seconds."""
    if alert.get("kind") != "turn":
        return True
    if alert.get("held"):
        return True
    if subagents_running(wdir, alert.get("session") or "", now):
        return False
    if _FLOW.asks(alert.get("message") or alert.get("detail")):
        return True
    since = alert.get("since")
    return isinstance(since, (int, float)) and now - since >= AWAIT_AFTER


def idle_only(alert):
    """A turn that awaits only by the AWAIT_AFTER rule: its message asks nothing and no hook holds
    it. The page lists it under "Idle sessions", neutral and uncounted, never under Answer: the
    dev looked for a question in that terminal and found just the prompt."""
    return alert.get("kind") == "turn" and not alert.get("held") and not _FLOW.asks(alert.get("message") or alert.get("detail"))


TURN_MAX = 8000
_tail_cache = {}


def transcript_tail(wt, session, home_dir):
    """The last assistant text of a session, masked, from its transcript under
    <config>/projects/<key of the worktree>/<session>.jsonl. Fallback for a "your turn" alert
    written by an older hook, which kept only the first 200 characters: the ask ("ok?") is at
    the end of the message, and the dev had to read it in the terminal."""
    if not safe_id(session):
        return ""
    # Claude Code keys a project by the path it was started in, which may be the symlinked one
    # (/tmp against /private/tmp): both spellings are tried.
    keys = list(dict.fromkeys(re.sub(r"[^A-Za-z0-9]", "-", p) for p in (wt, os.path.realpath(wt))))
    configs = [os.environ.get("CLAUDE_CONFIG_DIR") or ""]
    try:
        configs += [os.path.join(home_dir, n) for n in sorted(os.listdir(home_dir))
                    if n == ".claude" or n.startswith(".claude-")]
    except OSError:
        pass
    for config, key in ((c, k) for c in filter(None, configs) for k in keys):
        path = os.path.join(config, "projects", key, session + ".jsonl")
        try:
            st = os.stat(path)
        except OSError:
            continue
        hit = _tail_cache.get(path)
        if hit and hit[0] == st.st_mtime:
            return hit[1]
        text = ""
        try:
            with open(path, "rb") as f:
                f.seek(max(0, st.st_size - 1_000_000))
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            lines = []
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            msg = entry.get("message") if isinstance(entry, dict) and entry.get("type") == "assistant" else None
            parts = msg.get("content") if isinstance(msg, dict) else None
            texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"] \
                if isinstance(parts, list) else []
            if any(t.strip() for t in texts):
                text = _FLOW.mask("\n".join(texts))[-TURN_MAX:]
                break
        _tail_cache[path] = (st.st_mtime, text)
        return text
    return ""


def alert_action(alert, answers_on, wt):
    """What the dev does about an alert, as a key the page translates and a command if any."""
    if alert["kind"] == "question":
        if alert.get("held"):
            return {"action": "answer-on-page", "command": None, "command_kind": None}
        if answers_on:
            return {"action": "answer-in-terminal", "command": None, "command_kind": None}
        return {"action": "answer-in-terminal-or-enable", "command_kind": "shell",
                "command": f"cd {shlex.quote(wt)} && {WORK_SH} ui answers on"}
    if alert["kind"] == "permission":
        if alert.get("held"):
            return {"action": "approve-on-page", "command": None, "command_kind": None}
        return {"action": "approve-in-terminal", "command": None, "command_kind": None}
    if alert.get("held"):
        return {"action": "reply-on-page", "command": None, "command_kind": None}
    return {"action": "prompt-session", "command": None, "command_kind": None}


def mr_check(wt, branch, now, root):
    """The failed MR lookup of a worktree, as an alert: the tasks it leaves undecided and the
    command that gives the server a token. None when no task waits on it."""
    failed = [t for t in worktree_tasks(wt, branch, now, root=root)
              if t["status"] != "done" and isinstance(t["mr"], dict) and t["mr"].get("error") in TOKEN_ERRORS]
    if not failed:
        return None
    remote = parse_remote(git_out(wt, "remote", "get-url", "origin"))
    command = step_command({"next_step": "fix-mr-check", "mr": failed[0]["mr"]}, failed[0]["id"], wt, root,
                           remote[0] if remote else None,
                           forge_of(remote[0], flow_of(wt)) if remote else None)[0]
    return {"error": failed[0]["mr"]["error"], "tasks": [t["id"] for t in failed], "action": "fix-mr-check",
            "command": command, "command_kind": "shell"}


def request_path(wdir, tid):
    return os.path.join(wdir, "attention", REQUESTS, f"memory-{tid}.json")


def memory_requests(wdir):
    try:
        names = sorted(os.listdir(os.path.join(wdir, "attention", REQUESTS)))
    except OSError:
        return []
    ids = [n[len("memory-"):-len(".json")] for n in names if n.startswith("memory-") and n.endswith(".json")]
    return [i for i in ids if ID_RX.fullmatch(i)]


# Written by hooks/attention.py on SessionStart and UserPromptSubmit: a session that is working
# in a worktree without an alert up still has its Claude process recorded here.
SESSION_MARKER = ".session"


def live_session(wdir, now):
    """True when a Claude session may still be using the worktree: a live alert, a session marker
    whose Claude process is alive (or, without a pid, younger than STALE_AFTER), or an event of
    any task in the last STALE_AFTER seconds."""
    if alerts(wdir, now):
        return True
    adir = os.path.join(wdir, "attention")
    try:
        sessions = os.listdir(adir)
    except OSError:
        sessions = []
    for session in sessions:
        m = read_json(os.path.join(adir, session, SESSION_MARKER)) if safe_id(session) else None
        if not isinstance(m, dict):
            continue
        pid = m.get("claude_pid")
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 1:
            if pid_alive(pid):
                return True
        elif isinstance(m.get("since"), (int, float)) and now - m["since"] < STALE_AFTER:
            return True
    return any(now - e["t"] < STALE_AFTER for evs in events(wdir).values() for e in evs)


def sensitive(rel, work_rel, cleaned_ids, extra_dirs=()):
    """True for an ignored file that nobody can recreate from the remote: a local secret, or the
    state of a task this cleanup does not name."""
    parts = rel.split("/")
    if SENSITIVE_NAME.match(parts[-1]) or (SENSITIVE_DIRS | set(extra_dirs)) & set(parts[:-1]):
        return True
    wparts = work_rel.split("/")
    if parts[:len(wparts)] == wparts and len(parts) > len(wparts) + 1:
        owner = parts[len(wparts)]
        return ID_RX.fullmatch(owner) is not None and owner not in cleaned_ids and owner != "attention"
    return False


def ignored_files(wt, wdir, cleaned_ids):
    """(every git-ignored file of the worktree, the sensitive ones, or None if git failed).
    `git worktree remove` without --force deletes ignored files without a word: they have to be
    in the confirmation, and a secret among them refuses the removal."""
    out = git_out(wt, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    if out is None:
        return None, None
    names = sorted(n for n in out.split("\0") if n)
    work_rel = os.path.relpath(wdir, os.path.realpath(wt)) if wdir else ".work"
    extra = flow_of(wt).get("sensitive_ignored")
    extra = [x.strip("/") for x in extra if isinstance(x, str)] if isinstance(extra, list) else []
    return names, [n for n in names if sensitive(n, work_rel, cleaned_ids, extra)]


def worktree_refusal(wt, branch, main, served, tasks, wdir, now, sensitive_found=(), ignored_total=0):
    """Why the worktree may not be removed, or None. Only local refs: no fetch, so a stale
    remote-tracking ref can only refuse more, never allow more."""
    real = os.path.realpath(wt)
    if not branch or branch == "(detached)":
        return "detached"
    if real == os.path.realpath(main):
        return "main-worktree"
    if real == os.path.realpath(served):
        return "serving-worktree"
    porcelain = git_out(wt, "status", "--porcelain")
    if porcelain is None or porcelain.strip():
        return "dirty"
    unpushed = git_out(wt, "rev-list", "HEAD", "--not", "--remotes")
    if unpushed is None or unpushed.strip():
        return "unpushed"
    remote_refs = git_out(wt, "for-each-ref", "--format=%(refname)", f"refs/remotes/*/{branch}")
    if remote_refs is None or not remote_refs.strip():
        return "no-remote-branch"
    if any(t["status"] != "done" for t in tasks):
        return "other-task-not-done"
    if wdir and live_session(wdir, now):
        return "live-session"
    if sensitive_found is None or sensitive_found:
        return "sensitive-files"
    if ignored_total > IGNORED_SHOWN:
        return "too-many-ignored"
    return None


def file_list(path):
    """What a listed directory holds, for the plan hash: a file that appears changes the plan."""
    if os.path.isfile(path):
        return [[os.path.basename(path), os.path.getsize(path)]]
    out = []
    for dp, _, fs in os.walk(path):
        for f in fs:
            fp = os.path.join(dp, f)
            try:
                out.append([os.path.relpath(fp, path), os.path.getsize(fp), mtime_ns(fp)])
            except OSError:
                continue
    return sorted(out)


def cleanup_plan(root, targets, now=None):
    """{"plan": hash, "targets": [...]} for [(wt index, ID)] or "all-done". Pure on the disk:
    it deletes nothing. The hash covers each target with all three items plus what the listed
    directories hold and the worktree's HEAD, so a confirmation of an older plan is refused."""
    now = time.time() if now is None else now
    wts, main = worktrees(root), (listed_worktrees(root) or [(root, "")])[0][0]
    by_wt = {}

    def tasks_of(i):
        if i not in by_wt:
            by_wt[i] = worktree_tasks(wts[i][0], wts[i][1], now, root=root)
        return by_wt[i]

    if targets == "all-done":
        targets = [(i, t["id"]) for i in range(len(wts)) for t in tasks_of(i) if t["status"] == "done"]
    targets = sorted(set(targets), key=lambda t: (str(t[0]), str(t[1])))
    out, fingerprint = [], []
    for i, tid in targets:
        entry = {"wt": i, "id": tid, "refused": None, "items": {}}
        if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(wts) \
                or not ID_RX.fullmatch(tid if isinstance(tid, str) else ""):
            out.append({**entry, "refused": "no-such-task"})
            continue
        wt, branch = wts[i]
        wdir = work_dir(wt)
        task = next((t for t in tasks_of(i) if t["id"] == tid), None)
        entry.update(worktree=os.path.basename(wt), branch=branch, status=task["status"] if task else None)
        if task is None:
            out.append({**entry, "refused": "no-such-task"})
            continue
        if task["status"] != "done":
            out.append({**entry, "refused": "not-done"})
            continue
        paths = [os.path.join(wdir, tid)]
        if (read(os.path.join(wdir, "current"), 200) or "").strip() == tid:
            paths.append(os.path.join(wdir, "current"))
        pending = [request_path(d, tid) for d in sorted({wdir, work_dir(root)} - {None})
                   if os.path.isfile(request_path(d, tid))]
        paths += pending
        mine = tasks_of(i)
        cleaned = {t for w, t in targets if w == i}
        ignored, secrets_found = ignored_files(wt, wdir, cleaned)
        refusal = worktree_refusal(wt, branch, main, root, mine, wdir, now, secrets_found, len(ignored or []))
        safe_branch = branch if branch and branch != "(detached)" else "<branch>"
        base = branch in base_branches(flow_of(wt))
        q = shlex.quote
        recreate = f"git worktree add {q(wt)} {q(safe_branch)}" if base else \
            f"git worktree add {q(wt)} -b {q(safe_branch)} {q('origin/' + safe_branch)}"
        entry["retro"] = os.path.isfile(os.path.join(wdir, tid, "05-retro.md"))
        entry["items"] = {
            "state": {"paths": paths, "cancels_memory_request": bool(pending), "refused": None},
            "worktree": {"path": wt, "branch": branch, "tasks": [t["id"] for t in mine],
                         # A base branch is shared with the main checkout's history: only the
                         # worktree goes.
                         "delete_branch": not base, "recreate": recreate,
                         "ignored": (ignored or [])[:IGNORED_SHOWN], "ignored_count": len(ignored or []),
                         "sensitive": secrets_found or [], "refused": refusal},
            "memory": {"request": f"memory-{tid}.json", "session_worktree": os.path.basename(wt),
                       "session_worktree_if_removed": os.path.basename(root), "refused": None}}
        fingerprint.append([i, tid, [file_list(p) for p in paths], git_out(wt, "rev-parse", "HEAD"),
                            git_out(wt, "status", "--porcelain"), ignored])
        out.append(entry)
    digest = hashlib.sha256(json.dumps([out, fingerprint], sort_keys=True).encode()).hexdigest()
    return {"plan": digest, "targets": out}


def remove(path):
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)
    except FileNotFoundError:
        pass


def write_request(wdir, tid, now):
    path = request_path(wdir, tid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = os.path.join(os.path.dirname(path), f".tmp-{secrets.token_hex(6)}")
    with open(tmp, "w") as f:
        json.dump({"id": tid, "since": now}, f)
    os.replace(tmp, path)


def failure(code, msg):
    return {"error": msg, "code": code}


def cleanup_execute(root, body, now=None):
    """(status, answer). Recomputes the plan for the targets the request names, refuses when its
    hash is not the one the dev confirmed, and deletes from the recomputed plan, never from the
    request's paths (the request only carries ids and item names). A target with no item picked
    is part of the hashed set and is left alone."""
    now = time.time() if now is None else now
    if not isinstance(body, dict) or not isinstance(body.get("targets"), list) or not isinstance(body.get("plan"), str):
        return 400, failure("bad-body", "body must carry targets and plan")
    if not body["targets"]:
        return 400, failure("empty", "nothing to clean")
    picks = {}
    for t in body["targets"]:
        if not isinstance(t, dict) or not isinstance(t.get("items"), list) \
                or any(x not in CLEANUP_ITEMS for x in t["items"]):
            return 400, failure("bad-body", "each target needs wt, id and items among " + ", ".join(CLEANUP_ITEMS))
        key = (t.get("wt"), t.get("id"))
        if not isinstance(key[0], int) or isinstance(key[0], bool) or not isinstance(key[1], str) or key in picks:
            return 400, failure("bad-body", "bad or repeated target")
        picks[key] = list(dict.fromkeys(t["items"]))
    plan = cleanup_plan(root, list(picks), now)
    if not hmac.compare_digest(plan["plan"], body["plan"]):
        return 409, failure("plan-changed", "plan changed")
    for t in plan["targets"]:
        picked = picks[(t["wt"], t["id"])]
        if picked and (t["refused"] or any(t["items"][x]["refused"] for x in picked)):
            return 409, failure("refused", "refused")
    results, served = [], work_dir(root)
    pick = lambda t: picks[(t["wt"], t["id"])]
    removed_wts = {t["items"]["worktree"]["path"] for t in plan["targets"] if "worktree" in pick(t)}
    for t in plan["targets"]:
        if "state" in pick(t):
            ok = True
            for path in t["items"]["state"]["paths"]:
                try:
                    remove(path)
                except OSError:
                    ok = False
            results.append({"wt": t["wt"], "id": t["id"], "item": "state", "ok": ok,
                            "error": None if ok else "delete-failed"})
    # After the state: the state lists a pending request of the task, and deleting it after
    # writing it would report "memory ok" for a request nobody will read. Before the worktree:
    # a removed worktree's request waits in the served one, whose session the dev is in.
    for t in plan["targets"]:
        if "memory" in pick(t):
            wt = t["items"]["worktree"]["path"]
            target_dir = served if wt in removed_wts else work_dir(wt)
            try:
                write_request(target_dir, t["id"], now)
                results.append({"wt": t["wt"], "id": t["id"], "item": "memory", "ok": True, "error": None})
            except OSError:
                results.append({"wt": t["wt"], "id": t["id"], "item": "memory", "ok": False, "error": "write-failed"})
    done_wts = set()
    for t in plan["targets"]:
        if "worktree" not in pick(t):
            continue
        w = t["items"]["worktree"]
        if w["path"] in done_wts:
            results.append({"wt": t["wt"], "id": t["id"], "item": "worktree", "ok": True, "error": None})
            continue
        done_wts.add(w["path"])
        results.append({"wt": t["wt"], "id": t["id"], "item": "worktree", **remove_worktree(root, w)})
    return 200, {"ok": all(r["ok"] for r in results), "results": results}


def close_task(root, body, now=None):
    """(status, answer) for "Mark as done": writes the same `closed` marker as `work.sh done`."""
    now = time.time() if now is None else now
    if not isinstance(body, dict):
        return 400, failure("bad-body", "body must be an object")
    wi, tid = body.get("wt"), body.get("id")
    wts = worktrees(root)
    if not isinstance(wi, int) or isinstance(wi, bool) or not 0 <= wi < len(wts) or not isinstance(tid, str) \
            or not ID_RX.fullmatch(tid):
        return 404, failure("no-such-task", "no such task")
    task = next((t for t in worktree_tasks(wts[wi][0], wts[wi][1], now, root=root) if t["id"] == tid), None)
    # A triage task has no directory to write `closed` into: it ends when `work.sh init` runs.
    if task is None or task.get("triage"):
        return 404, failure("no-such-task", "no such task")
    if task["status"] == "done":
        return 409, failure("already-done", "already done")
    tdir = os.path.join(work_dir(wts[wi][0]), tid)
    tmp = os.path.join(tdir, f".tmp-{secrets.token_hex(6)}")
    with open(tmp, "w") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)) + "\n")
    os.replace(tmp, os.path.join(tdir, "closed"))
    warnings = (["fail"] if task["status"] == "FAIL" else []) + \
        (["open-findings"] if task["findings_open"] or task["status"] == "findings" else [])
    return 200, {"ok": True, "warnings": warnings}


SWITCHES = {"answers": "answers-on", "turns": "turns-on"}


def list_worktrees(root, now=None):
    """Every worktree of the repository, with or without a task, for the page's chat: where a
    message to a session goes, whether one is alive there, and its switches."""
    now = time.time() if now is None else now
    out = []
    for i, (wt, branch) in enumerate(worktrees(root)):
        wdir = work_dir(wt)
        out.append({"wt": i, "name": os.path.basename(wt), "branch": branch, "path": wt,
                    "here": os.path.realpath(wt) == os.path.realpath(root),
                    "live": bool(wdir) and live_session(wdir, now), "switches": switches_of(wt)})
    return out


def switches_of(wt):
    wdir = work_dir(wt)
    adir = os.path.join(wdir, "attention") if wdir else None
    return {k: bool(adir) and os.path.exists(os.path.join(adir, f)) for k, f in SWITCHES.items()}


def set_switch(root, body):
    """(status, answer): turns a worktree's switch on or off, as `work.sh ui answers|turns` does."""
    if not isinstance(body, dict) or body.get("name") not in SWITCHES or not isinstance(body.get("on"), bool):
        return 400, failure("bad-body", "body needs wt, name answers|turns and on true|false")
    wts, wi = worktrees(root), body.get("wt")
    if not isinstance(wi, int) or isinstance(wi, bool) or not 0 <= wi < len(wts) or not work_dir(wts[wi][0]):
        return 404, failure("no-such-task", "no such worktree")
    adir = os.path.join(work_dir(wts[wi][0]), "attention")
    flag = os.path.join(adir, SWITCHES[body["name"]])
    if body["on"]:
        os.makedirs(adir, exist_ok=True)
        open(flag, "a").close()
    else:
        try:
            os.unlink(flag)
        except FileNotFoundError:
            pass
    return 200, {"ok": True, "name": body["name"], "on": body["on"]}


def notify_session(root, body):
    """After a decision on the page, the request the task's session applies on its next prompt."""
    wt, branch = worktrees(root)[body["wt"]]
    task = next((t for t in worktree_tasks(wt, branch, time.time(), root=root) if t["id"] == body["id"]), None)
    if task is not None:
        decisions.notify(work_dir(wt), task)


def remove_worktree(root, w):
    """git worktree remove without --force: git refuses a dirty tree on its own, a second
    guard behind the plan. The branch goes only if, checked again right now, every commit of it
    is on a remote: -D is never used on a branch with a commit outside the remotes."""
    if git_out(root, "worktree", "remove", w["path"]) is None:
        return {"ok": False, "error": "worktree-remove-failed"}
    if not w["delete_branch"]:
        return {"ok": True, "error": None}
    unpushed = git_out(root, "rev-list", w["branch"], "--not", "--remotes")
    if unpushed is None or unpushed.strip():
        return {"ok": False, "error": "branch-kept-unpushed"}
    if git_out(root, "branch", "-D", w["branch"]) is None:
        return {"ok": False, "error": "branch-delete-failed"}
    return {"ok": True, "error": None}


def valid_picks(questions, picks):
    if not isinstance(picks, list) or len(picks) != len(questions):
        return False
    for q, p in zip(questions, picks):
        if not isinstance(q, dict) or not isinstance(p, dict):
            return False
        idx, other = p.get("options", []), p.get("other", "")
        if not isinstance(idx, list) or not isinstance(other, str) or len(other) > MAX_OTHER:
            return False
        n = len(q.get("options") or [])
        if any(not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < n for i in idx) or len(set(idx)) != len(idx):
            return False
        count = len(idx) + bool(other.strip())
        if count == 0 or (not q.get("multiSelect") and count != 1):
            return False
    return True


FINDINGS_DECIDED = "findings decided on the dashboard for {id}: read findings-decision.md and go on"


def answer(root, body, now=None):
    """(status, message). Writes <agent>.answer.json or <agent>.release next to a question the
    hook is holding right now; anything else writes nothing."""
    now = time.time() if now is None else now
    if not isinstance(body, dict):
        return 400, "body must be an object"
    wt, session, agent, qid = body.get("wt"), body.get("session"), body.get("agent"), body.get("qid")
    wts = worktrees(root)
    if not isinstance(wt, int) or isinstance(wt, bool) or not 0 <= wt < len(wts):
        return 404, "no such worktree"
    if not safe_id(session) or not safe_id(agent) or not isinstance(qid, str):
        return 400, "bad session, agent or qid"
    wdir = work_dir(wts[wt][0])
    if not wdir:
        return 404, "no work directory"
    base = os.path.join(wdir, "attention", session, agent)
    a = read_json(base + ".json")
    if not isinstance(a, dict) or a.get("kind") not in ALERT_KINDS or not isinstance(a.get("qid"), str) \
            or not hmac.compare_digest(a["qid"], qid) or not live(a, now) or not held_now(a, now):
        return 409, "no question with that id is being held"
    if body.get("release") is True:
        target, content = base + ".release", ""
    elif a["kind"] == "permission":
        if body.get("behavior") not in ("allow", "deny"):
            return 400, "behavior must be allow or deny"
        target, content = base + ".answer.json", json.dumps({"qid": qid, "behavior": body["behavior"], "via": "web"})
    elif a["kind"] == "turn":
        if body.get("kind") is not None:
            # A message the server builds, never one the page wrote: the findings decided.
            tid = body.get("id")
            if body.get("kind") != "findings-decided" or not isinstance(tid, str) or not ID_RX.fullmatch(tid):
                return 400, "kind findings-decided and a task id"
            message = FINDINGS_DECIDED.format(id=tid)
        else:
            message = body.get("message")
        if not isinstance(message, str) or not message.strip() or len(message) > MAX_OTHER:
            return 400, "message must be a non-empty text of at most 4000 characters"
        target, content = base + ".answer.json", json.dumps({"qid": qid, "message": message, "via": "web"})
    else:
        if not valid_picks(a.get("questions") or [], body.get("answers")):
            return 400, "answers do not fit the question"
        target, content = base + ".answer.json", json.dumps({"qid": qid, "answers": body["answers"], "via": "web"})
    tmp = os.path.join(os.path.dirname(base), f".tmp-{secrets.token_hex(6)}")
    with open(tmp, "w") as f:
        f.write(content)
    os.replace(tmp, target)
    return 200, "ok"


def redact(text):
    """An error text sent to the page with every token the server knows masked."""
    for tok in _seen_tokens | {os.environ.get(v) or "" for vs in TOKEN_VARS.values() for v in vs}:
        if tok:
            text = text.replace(tok, "[masked]")
    text = re.sub(r"://[^/\s@]+@", "://[masked]@", text)
    text = re.sub(r"(?i)(bearer\s+)[^\s'\",}]+", r"\1[masked]", text)
    return re.sub(r"(?i)(private-token['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+", r"\1[masked]", text)


# Server-Sent Events. A tick re-reads the work dirs' file listing (names, sizes, mtimes): no
# git status, no GitLab, so a tick per second per client is cheap.
EVENTS_TICK = 1.0
EVENTS_HEARTBEAT = 15.0
# A stream ends after this long and the browser's EventSource reconnects (after `retry`), so a
# stream whose client vanished without a FIN never holds a thread for good.
EVENTS_MAX_SECONDS = 600.0
MAX_STREAMS = 8
# Files whose change says nothing to the page: the page heartbeat this stream itself touches,
# temp files of an atomic write, and the dashboard's own pid and log.
SIGNATURE_SKIP = {".page", "ui.pid", "ui.url", "ui.log"}
_streams_lock = threading.Lock()


def change_signature(root):
    """A hash of what the page shows that lives on disk: alerts, memory requests, task files,
    events. Changes when any of them does; never reads their content."""
    h = hashlib.sha256()
    for wt, branch in worktrees(root):
        h.update(f"{wt}\0{branch}\n".encode())
        wdir = work_dir(wt)
        if not wdir:
            continue
        for dp, dns, fs in os.walk(wdir):
            dns.sort()
            for f in sorted(fs):
                if f in SIGNATURE_SKIP or f.startswith(".tmp-"):
                    continue
                path = os.path.join(dp, f)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                h.update(f"{os.path.relpath(path, wdir)}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def touch_pages(root):
    """While a stream is open the page is open: the hold (attention.py) waits for the web only
    while `.page` is fresh, and a throttled background tab may poll /api/attention rarely. Only
    while a page said it is in view in the last VISIBLE_FOR seconds."""
    if time.time() - _last_visible[0] > VISIBLE_FOR:
        return
    for wt, _ in worktrees(root):
        wdir = work_dir(wt)
        adir = os.path.join(wdir, "attention") if wdir else None
        if adir and os.path.isdir(adir):
            try:
                with open(os.path.join(adir, ".page"), "a"):
                    pass
                os.utime(os.path.join(adir, ".page"))
            except OSError:
                pass


class Handler(http.server.BaseHTTPRequestHandler):
    root = "."
    # Set by serve(); with None every write is refused.
    token = None
    # Open event streams of this server class; over MAX_STREAMS a new one gets 503.
    streams = None

    def send(self, code, body, ctype):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        # The page carries the token and the answer buttons: a site that framed it could
        # overlay them and borrow the dev's click.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            self.route()
        except Exception as e:  # an error must read as an error on the page, never as "no task"
            self.send(500, json.dumps({"error": redact(f"{type(e).__name__}: {e}")}), "application/json")

    def do_POST(self):
        try:
            self.post()
        except Exception as e:
            self.send(500, json.dumps({"error": redact(f"{type(e).__name__}: {e}")}), "application/json")

    def refuse_method(self):
        self.send(405, "method not allowed", "text/plain")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = refuse_method

    def local_host(self):
        # Refuses any Host other than localhost: a DNS-rebinding page in the browser could
        # otherwise read the contracts and the event log.
        return (self.headers.get("Host") or "").rsplit(":", 1)[0] in LOCAL_HOSTS

    def local_origin(self):
        origin = urllib.parse.urlparse(self.headers.get("Origin") or "")
        return origin.scheme == "http" and origin.hostname in LOCAL_HOSTS and origin.netloc == self.headers.get("Host")

    def post(self):
        err = lambda code, msg: self.send(code, json.dumps({"error": msg}), "application/json")
        if not self.local_host():
            return err(403, "forbidden")
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/api/answer", "/api/cleanup", "/api/close", "/api/findings-decision", "/api/switch"):
            return err(404, "not found")
        # Every check below runs before the dispatch, for both writes.
        sent = self.headers.get("X-Kit-Token") or ""
        if not self.token or not hmac.compare_digest(sent.encode(), self.token.encode()):
            return err(403, "bad token")
        origin = urllib.parse.urlparse(self.headers.get("Origin") or "")
        if origin.scheme != "http" or origin.hostname not in LOCAL_HOSTS or origin.netloc != self.headers.get("Host"):
            return err(403, "bad origin")
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return err(415, "content type must be application/json")
        try:
            length = int(self.headers.get("Content-Length") or "-1")
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            return err(413, "body missing or over 64 KB")
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            return err(400, "body is not JSON")
        if path == "/api/cleanup":
            code, out = cleanup_execute(self.root, body)
            return self.send(code, json.dumps(out), "application/json")
        if path == "/api/close":
            code, out = close_task(self.root, body)
            return self.send(code, json.dumps(out), "application/json")
        if path == "/api/switch":
            code, out = set_switch(self.root, body)
            return self.send(code, json.dumps(out), "application/json")
        if path == "/api/findings-decision":
            code, out = decide_finding(self.root, body)
            if code == 200:
                notify_session(self.root, body)
            return self.send(code, json.dumps(out), "application/json")
        code, msg = answer(self.root, body)
        if code != 200:
            return err(code, msg)
        return self.send(200, json.dumps({"ok": True}), "application/json")

    def route(self):
        if not self.local_host():
            return self.send(403, "forbidden", "text/plain")
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if url.path in ("/", "/index.html"):
            page = read(os.path.join(KIT, "dashboard", "index.html")) or ""
            return self.send(200, page.replace("__KIT_TOKEN__", self.token or ""), "text/html; charset=utf-8")
        if url.path == "/api/events":
            # The stream holds a thread and one of MAX_STREAMS slots: only the page (its token,
            # as ?t= since EventSource cannot set headers) or a localhost Origin may open one, so
            # another site open in the browser cannot take the slots.
            sent = (q.get("t") or [""])[0] or self.headers.get("X-Kit-Token") or ""
            token_ok = bool(self.token) and hmac.compare_digest(sent.encode(), self.token.encode())
            if not token_ok and not self.local_origin():
                return self.send(403, json.dumps({"error": "the event stream needs the page's token"}), "application/json")
            return self.event_stream()
        if url.path == "/api/attention":
            visible = q.get("visible") == ["1"]
            return self.send(200, json.dumps(list_attention(self.root, visible=visible)), "application/json")
        if url.path == "/api/worktrees":
            return self.send(200, json.dumps(list_worktrees(self.root)), "application/json")
        if url.path in ("/api/answer", "/api/cleanup", "/api/close", "/api/findings-decision", "/api/switch"):
            return self.refuse_method()
        if url.path == "/api/cleanup-plan":
            if (q.get("all") or [""])[0] == "done":
                return self.send(200, json.dumps(cleanup_plan(self.root, "all-done")), "application/json")
            try:
                wt = int((q.get("wt") or ["-1"])[0])
            except ValueError:
                wt = -1
            plan = cleanup_plan(self.root, [(wt, (q.get("id") or [""])[0])])
            if plan["targets"][0]["refused"] == "no-such-task":
                return self.send(404, json.dumps({"error": "no such task"}), "application/json")
            return self.send(200, json.dumps(plan), "application/json")
        if url.path == "/flow.json":
            return self.send(200, json.dumps(flow_of(self.root)), "application/json")
        if url.path == "/api/tasks":
            return self.send(200, json.dumps(list_tasks(self.root)), "application/json")
        if url.path == "/api/task":
            try:
                wt = int((q.get("wt") or ["-1"])[0])
            except ValueError:
                wt = -1
            detail = task_detail(self.root, wt, (q.get("id") or [""])[0])
            if detail is None:
                return self.send(404, json.dumps({"error": "no such task"}), "application/json")
            return self.send(200, json.dumps(detail), "application/json")
        return self.send(404, "not found", "text/plain")

    def client_gone(self):
        """True when the browser closed the connection: readable with nothing to read is EOF."""
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            return bool(ready) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def event_stream(self):
        """text/event-stream: `hello` at once, `change` when change_signature moves, a `: ping`
        comment every EVENTS_HEARTBEAT. Read-only, like every GET: no token, the Host check in
        route() only. Events carry a counter, never paths or content."""
        cls = type(self)
        with _streams_lock:
            if cls.streams is None:
                cls.streams = threading.BoundedSemaphore(MAX_STREAMS)
        if not cls.streams.acquire(blocking=False):
            return self.send(503, json.dumps({"error": "too many event streams"}), "application/json")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.end_headers()
            seq, last = 0, change_signature(self.root)
            start = beat = time.monotonic()
            self.wfile.write(f"retry: 2000\nevent: hello\ndata: {json.dumps({'seq': seq})}\n\n".encode())
            self.wfile.flush()
            while time.monotonic() - start < EVENTS_MAX_SECONDS:
                time.sleep(EVENTS_TICK)
                if self.client_gone():
                    return
                touch_pages(self.root)
                sig = change_signature(self.root)
                if sig != last:
                    seq, last = seq + 1, sig
                    self.wfile.write(f"event: change\ndata: {json.dumps({'seq': seq})}\n\n".encode())
                    self.wfile.flush()
                elif time.monotonic() - beat >= EVENTS_HEARTBEAT:
                    beat = time.monotonic()
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return  # the client went away: nothing to answer
        finally:
            cls.streams.release()

    def log_message(self, *a):
        pass


def serve(root, port):
    # A new token per server: the page carries it, and nothing else that the server returns does.
    handler = type("BoundHandler", (Handler,), {"root": root, "token": secrets.token_urlsafe(32)})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    # Event streams live for minutes: their threads must not hold the process at exit.
    server.daemon_threads = True
    server.serve_forever()


def print_plan(plan, picked):
    for t in plan["targets"]:
        print(f"{t['id']}  (worktree {t.get('worktree', '?')}, {t.get('status')})"
              f"{'' if t['refused'] or t.get('retro') else '  no retrospective yet: its learnings go with it'}")
        if t["refused"]:
            print(f"  refused: {t['refused']}")
            continue
        for item in picked:
            it = t["items"][item]
            if it["refused"]:
                print(f"  {item}: refused ({it['refused']})")
                for name in it.get("sensitive") or []:
                    print(f"    sensitive: {name}")
                if item == "worktree":
                    print(f"    to recreate it later for review fixes: {it['recreate']}")
            elif item == "state":
                for p in it["paths"]:
                    print(f"  state: delete {p}")
                if it["cancels_memory_request"]:
                    print("  state: this cancels the pending memory cleanup of this task (pick --memory to ask again)")
            elif item == "worktree":
                branch = f"; git branch -D {it['branch']}" if it["delete_branch"] else f" (branch {it['branch']} kept)"
                print(f"  worktree: git worktree remove {it['path']}{branch} (tasks in it: {', '.join(it['tasks'])})")
                print(f"    git-ignored files that go with it ({it['ignored_count']}):")
                for name in it["ignored"]:
                    print(f"      {name}")
                if it["ignored_count"] > len(it["ignored"]):
                    print(f"      ... and {it['ignored_count'] - len(it['ignored'])} more")
                print(f"    recreate: {it['recreate']}")
            else:
                print(f"  memory: ask the session in {it['session_worktree']} to review its auto memory of "
                      f"{t['id']} (in {it['session_worktree_if_removed']} if that worktree is removed)")
    print(f"plan {plan['plan']}")


def findings_cli(argv):
    """dashboard.py findings <root> <ID>: each finding of the task with its key and state, so
    whoever writes findings-decision.md can write keyed lines (PROTOCOL.md)."""
    root, tid = argv[0], argv[1]
    wdir = work_dir(root)
    if not wdir or not ID_RX.fullmatch(tid) or tid not in task_ids(wdir):
        print(f"no task {tid} in {root}", file=sys.stderr)
        return 1
    tdir = os.path.join(wdir, tid)
    for f in decided_findings(tdir, stages(flow_of(root), tdir)):
        print(f"{f['state']:11} - <fix|accepted> {f['severity']} {f['lens']} [{f['key']}]: {f['summary']}")
    return 0


def cleanup_cli(argv):
    """dashboard.py cleanup <root> <ID>|--all-done [--state] [--worktree] [--memory]. The plan
    is the endpoint's; without a terminal to confirm on, it prints it and deletes nothing."""
    root, what, flags = argv[0], argv[1], argv[2:]
    picked = [x for x in CLEANUP_ITEMS if f"--{x}" in flags]
    unknown = [f for f in flags if f.lstrip("-") not in CLEANUP_ITEMS]
    if unknown:
        print(f"unknown option: {' '.join(unknown)}", file=sys.stderr)
        return 1
    if what == "--all-done":
        plan = cleanup_plan(root, "all-done")
    else:
        # A task ID names a task of this worktree: worktrees() lists root's own first.
        plan = cleanup_plan(root, [(0, what)])
        if plan["targets"][0]["refused"] == "no-such-task":
            print(f"no task {what} in {root}", file=sys.stderr)
            return 1
    print_plan(plan, picked or list(CLEANUP_ITEMS))
    if not sys.stdin.isatty():
        print("not a terminal: nothing deleted (run it in a terminal to confirm)", file=sys.stderr)
        return 2
    if not picked:
        print("pick what to delete: --state, --worktree, --memory", file=sys.stderr)
        return 1
    # Same as the page: a refused item is left out, so one refusal does not refuse the rest.
    body = {"plan": plan["plan"], "targets": [
        {"wt": t["wt"], "id": t["id"],
         "items": [] if t["refused"] else [x for x in picked if not t["items"][x]["refused"]]}
        for t in plan["targets"]]}
    if not any(t["items"] for t in body["targets"]):
        print("nothing to clean")
        return 0
    for t in body["targets"]:
        print(f"will clean {t['id']}: {', '.join(t['items']) or 'nothing (refused)'}")
    if input("Delete exactly what is listed above? Type yes: ").strip() != "yes":
        print("nothing deleted")
        return 1
    code, out = cleanup_execute(root, body)
    print(json.dumps(out, indent=2))
    return 0 if code == 200 and out.get("ok") else 1


LANGUAGE_NAMES = {"en": "English", "pt": "Portuguese", "pt-br": "Brazilian Portuguese", "pt-pt": "European Portuguese",
                  "es": "Spanish", "fr": "French", "de": "German", "it": "Italian"}


def language_line(root):
    """The dev's language from flow.json, as context for the session; None for English."""
    lang = str(flow_of(root).get("language") or "en").strip()
    if lang.lower() in ("en", "en-us", "en-gb", ""):
        return None
    name = LANGUAGE_NAMES.get(lang.lower(), lang)
    return (f"foreman: the dev reads {name}. Write in {name} everything they read: your messages and "
            "questions, and every file under the work directory (purpose, contract, change, proof, reviews, "
            "retrospective, rulings). Keep in English only the keywords the scripts read: the Status: and "
            "Verdict: lines, the ## section headings flow.json lists, the [severity] and [route: ...] tags, "
            "and Ruling: / why: / cost if wrong:. Repository files follow the project's conventions.")


def hook_cli(root):
    """SessionStart: the dev's language, and the finished tasks of every worktree so the session
    offers the cleanup. Git and the `closed` marker only, no forge call: a session start must stay
    fast, and it must never fail."""
    try:
        line = language_line(root)
        if line:
            print(line)
    except Exception:  # noqa: BLE001
        pass
    try:
        now, found = time.time(), []
        for wt, branch in worktrees(root):
            found += [f"{t['id']} ({os.path.basename(wt)})" for t in worktree_tasks(wt, branch, now, lookup=False, root=root)
                      if t["status"] == "done"]
    except Exception:  # noqa: BLE001
        return 0
    if found:
        print(f"foreman: finished task(s) not cleaned up yet: {', '.join(found)}. Offer the dev the cleanup "
              "(task skill, 'Close and clean up'), with AskUserQuestion.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["findings"] and len(sys.argv) == 4:
        sys.exit(findings_cli(sys.argv[2:]))
    if sys.argv[1:2] == ["cleanup"]:
        if len(sys.argv) < 4:
            print("usage: dashboard.py cleanup <root> <ID>|--all-done [--state] [--worktree] [--memory]", file=sys.stderr)
            sys.exit(1)
        sys.exit(cleanup_cli(sys.argv[2:]))
    if sys.argv[1:2] == ["hook"]:
        sys.exit(hook_cli(sys.argv[2] if len(sys.argv) > 2 else os.environ.get("CLAUDE_PROJECT_DIR", ".")))
    serve(sys.argv[1], int(sys.argv[2]))
