#!/usr/bin/env python3
"""Attention alerts: which Claude Code session waits on the dev, and why.

State: <work_dir>/attention/<session_id>/<agent>.json, one file per (session, agent), so
parallel hooks of different agents never write the same file and need no lock. It does not
depend on a current work: a session in the triage, before `work.sh init`, raises alerts too.

  question    AskUserQuestion pending (written by `attention.py hold`, the PreToolUse entry
              with matcher AskUserQuestion)
  permission  permission dialog pending (PermissionRequest; Notification(permission_prompt)
              only when nothing was raised, since it fires ~6 s later and has no tool)
  turn        the main session finished its turn (Stop)

<session>/.session (SessionStart, UserPromptSubmit) records the session's Claude process, so
the dashboard's cleanup can tell a worktree some session still works in.

`flow.py` calls handle() for the raise and clear events; the per-tool PreToolUse path stays in
flow.py and costs one unlink. Every path exits 0 with empty stdout, except a web answer (on
PermissionRequest, Notification and PreToolUse a stdout line is read as a decision) and a
memory cleanup request: <work_dir>/attention/.requests/memory-<ID>.json, written by the
dashboard's cleanup, is consumed once by the next UserPromptSubmit or SessionStart of a session
in that worktree, which prints it as additional context (stdout there is context, not a decision).

Web answers (opt-in, `work.sh ui answers on`): the hold keeps the AskUserQuestion for the
dashboard while the page is open, and answers it through the documented PreToolUse contract
(permissionDecision "allow" + updatedInput with an `answers` map). It releases to the terminal
on the "answer in the terminal" button, when the page goes away, when answers are turned off,
and before the hook timeout (3600 s in settings.json), because a hook killed at its timeout
shows an error instead of the dialog.
"""
import json
import os
import secrets
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import flow as F  # noqa: E402  (flow.py registers itself under this name when it runs the hook)


def _env_seconds(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


# 30 s under the 3600 s hook timeout (wire_hooks.py, measured): the kill would leave a hook error
# instead of a fallback. While a page stays open and in view the hold waits that long; PAGE_GONE
# lets go 30 s after the page closes or hides, so a long hold never keeps a dev who left the page.
HOLD_SECONDS = _env_seconds("KIT_ATTENTION_HOLD", 3570)
# The main session's turn waits for the page as long as the page stays open, so the chat reaches
# it at any time: 30 s under its own 8 h hook timeout. Waiting costs no tokens (a local poll).
TURN_HOLD_SECONDS = _env_seconds("KIT_ATTENTION_TURN_HOLD", 8 * 3600 - 30)
# The page polls every 2 s; a heartbeat older than this means no page is open.
PAGE_FRESH = 10
# During a hold, how long without a poll before the question goes back to the terminal.
PAGE_GONE = _env_seconds("KIT_ATTENTION_PAGE_GONE", 30)
POLL = _env_seconds("KIT_ATTENTION_POLL", 0.5)
HEARTBEAT_EVERY = 2
DETAIL_MAX = 200
# A turn's detail is the session's whole last message: the page shows it in the chat, so it stays readable.
TURN_MAX = 6000


def attention_dir(flow):
    return os.path.join(F.work_dir(flow), "attention")


def write_json(path, obj):
    """Atomic: the dashboard reads these files while hooks write them."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = os.path.join(os.path.dirname(path), f".tmp-{secrets.token_hex(6)}")
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def unlink(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def short(text, limit=DETAIL_MAX):
    return F.mask(str(text or ""))[:limit]


# The whole last message of a turn, masked, for the page: its question or "ok?" is at the end,
# past the 200 characters of the detail, and the dev read the terminal to see it.
TURN_MAX = 8000


def turn_message(text):
    return F.mask(str(text or ""))[-TURN_MAX:]


def claude_pid():
    """The Claude Code process this hook runs under, so the dashboard can hide the alerts of a
    session that was killed without SessionEnd. Walks parent pids with ps; only called on the
    raising events, never on the per-tool path. None when not found: the 24 h cap is the guard."""
    forced = os.environ.get("KIT_ATTENTION_PID")
    if forced:
        return int(forced) if forced.isdigit() else None
    pid = os.getppid()
    for _ in range(8):
        if pid <= 1:
            return None
        try:
            out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=2).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        if not out:
            return None
        ppid, _, comm = out.partition(" ")
        if "claude" in os.path.basename(comm.strip()).lower():
            return pid
        try:
            pid = int(ppid)
        except ValueError:
            return None
    return None


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def raise_alert(flow, event, kind, detail, **extra):
    path = F.alert_path(flow, event.get("session_id"), event.get("agent_id") or "main")
    if not path:
        return None
    alert = {"kind": kind, "since": time.time(), "detail": short(detail, TURN_MAX if kind == "turn" else DETAIL_MAX), "claude_pid": claude_pid(), **extra}
    write_json(path, alert)
    return path, alert


def clear_session(flow, session, keep_running=False):
    """Removes the session's alerts. keep_running leaves the running-subagent markers: a prompt
    submitted while background subagents run does not stop them."""
    if not F.ALERT_ID_RX.fullmatch(session or ""):
        return
    sdir = os.path.join(attention_dir(flow), session)
    try:
        names = os.listdir(sdir)
    except (FileNotFoundError, NotADirectoryError):
        return
    if not keep_running or RUNNING not in names:
        shutil.rmtree(sdir, ignore_errors=True)
        return
    for name in names:
        if name != RUNNING:
            unlink(os.path.join(sdir, name))


# <session>/.running/<agent_id>: subagents between SubagentStart and SubagentStop. A
# Notification carries no agent_id, so while one runs a permission_prompt may be the
# subagent's; raising it on main would stay up until the next prompt, often 20 minutes.
RUNNING = ".running"


def running_marker(flow, session, agent):
    if F.alert_path(flow, session, agent) is None or agent == "main":
        return None
    return os.path.join(attention_dir(flow), session, RUNNING, agent)


# A marker older than this is an agent whose stop never came (a killed session): every tool call of
# a running subagent touches its marker (PostToolUse), so a long stage stays fresh.
RUNNING_STALE = 15 * 60


def subagent_running(flow, session):
    rdir = os.path.join(attention_dir(flow), session, RUNNING)
    try:
        names = os.listdir(rdir)
    except (FileNotFoundError, NotADirectoryError):
        return False
    now = time.time()
    for n in names:
        try:
            if now - os.path.getmtime(os.path.join(rdir, n)) < RUNNING_STALE:
                return True
        except OSError:
            continue
    return False


def handle(event, flow):
    """Raise or clear for every event except PreToolUse, which flow.py handles inline."""
    name = event.get("hook_event_name")
    session, agent = event.get("session_id"), event.get("agent_id") or "main"
    if name in ("PostToolUse", "SubagentStop"):
        path = F.alert_path(flow, session, agent)
        if path:
            unlink(path)
        if name == "PostToolUse" and not event.get("agent_id"):
            memory_requests(flow, "PostToolUse", only="findings-")
        marker = running_marker(flow, session, agent)
        if marker and name == "SubagentStop":
            unlink(marker)
        elif marker and os.path.exists(marker):
            # A subagent's tool call says it is still working: a marker is aged by its mtime, and
            # a stage of 15 to 40 minutes would otherwise read as gone.
            try:
                os.utime(marker)
            except OSError:
                pass
    elif name == "SubagentStart":
        marker = running_marker(flow, session, agent)
        if marker:
            os.makedirs(os.path.dirname(marker), exist_ok=True)
            open(marker, "w").close()
    elif name == "PermissionRequest":
        tool = event.get("tool_name") or ""
        # The question dialog is the hold's alert; a permission alert would overwrite it.
        if tool == "AskUserQuestion":
            return
        return  # the permission hold entry (attention.py permission) raises it
    elif name == "Notification":
        if event.get("notification_type") not in (None, "permission_prompt"):
            return
        # Without agent_id the prompt may be a running subagent's: PermissionRequest is the
        # source that carries the agent, so the fallback stays quiet rather than raise a
        # main-session alert nothing would clear until the next prompt.
        if not event.get("agent_id") and subagent_running(flow, session):
            return
        path = F.alert_path(flow, session, agent)
        # Fallback only: PermissionRequest already raised it with the tool, or a question is up.
        if path and not os.path.exists(path):
            raise_alert(flow, event, "permission", event.get("message") or "permission prompt")
    elif name == "Stop":
        if not turns_on(attention_dir(flow)):
            raise_alert(flow, event, "turn", event.get("last_assistant_message") or "",
                        message=turn_message(event.get("last_assistant_message")))
    elif name == "UserPromptSubmit":
        clear_session(flow, session, keep_running=True)
        mark_session(flow, session)
        memory_requests(flow, name)
    elif name in ("SessionEnd", "SessionStart"):
        # SessionStart covers a resume after a kill, which never ran SessionEnd.
        clear_session(flow, session)
        if name == "SessionStart":
            # Opened, resumed or cleared: at its prompt, with no Stop yet for the page to hold.
            # A compaction restarts mid-turn, so it is not fresh.
            mark_session(flow, session, fresh=event.get("source") in ("startup", "resume", "clear"))
            memory_requests(flow, name)


# <session>/.session: the session's Claude process, so the dashboard never removes a worktree a
# session is working in while it has no alert up. No .json suffix: it is not an alert.
SESSION_MARKER = ".session"


def mark_session(flow, session, fresh=False):
    """fresh: the session has not taken a prompt since it opened, so it waits in its terminal and
    the page cannot reach it until its first Stop."""
    if not F.ALERT_ID_RX.fullmatch(session or "") or session.endswith(".answer"):
        return
    write_json(os.path.join(attention_dir(flow), session, SESSION_MARKER),
               {"claude_pid": claude_pid(), "since": time.time(), "fresh": bool(fresh)})


# Same name as the dashboard's REQUESTS: the leading dot keeps it out of ALERT_ID_RX, so
# clear_session never removes it as a session directory.
REQUESTS = ".requests"
MEMORY_CONTEXT = (
    "The dev cleaned up the finished task {tid} from the Foreman dashboard and asked to clear "
    "Claude's auto memory about it. Before anything else: find the auto-memory entries (the memory "
    "index and the files it links) that are about {tid} only. List them with AskUserQuestion, "
    "multiSelect, one option per entry with its one-line summary, at most 4 options per question, "
    "split across questions so none is dropped. Delete only the entries the dev checks, and update "
    "the index for them. Delete nothing without that pick; an entry that also serves other work "
    "stays unless the dev checks it. If no entry is about {tid}, say so in one line."
)


FINDINGS_CONTEXT = (
    "The dev is deciding the review findings of {tid} on the Foreman dashboard; each decision is "
    "already written in the task's findings-decision.md. Apply them now and do not ask about these "
    "findings again. To fix, each by the stage its route names, then the tester and the lens that "
    "raised it: {fix}. Accepted as they are, to record in 00-purpose.md under \"Decisions from the "
    "interview\" with today's date: {accepted}. Still open on the page: {open}; the dev decides "
    "those there. After the re-review, change the line of each finding it no longer reports from "
    "\"- fix\" to \"- fixed\"."
)


def findings_context(rdir, name, tid):
    """The context of one findings request, or None when the file is not one."""
    try:
        with open(os.path.join(rdir, name)) as f:
            req = json.load(f)
    except (OSError, ValueError):
        return None
    item = lambda f: f"[{f.get('severity')}] {f.get('lens')}: {f.get('summary')}" + \
        (f" (route: {f['route']})" if f.get("route") else "")
    fix = "; ".join(item(f) for f in req.get("fix") or []) or "none"
    accepted = "; ".join(item(f) for f in req.get("accepted") or []) or "none"
    return FINDINGS_CONTEXT.format(tid=tid, fix=fix, accepted=accepted, open=int(req.get("open") or 0))


def memory_requests(flow, event_name, only=None):
    """Consumes the pending requests of this worktree (memory cleanups, findings decisions; only
    the ones whose name starts with `only` when given): the rename claims each one, so two
    sessions prompting at once never both act on it."""
    rdir = os.path.join(attention_dir(flow), REQUESTS)
    try:
        names = sorted(os.listdir(rdir))
    except (FileNotFoundError, NotADirectoryError):
        return
    ids, texts = [], []
    for name in names:
        if only and not name.startswith(only):
            continue
        if name.startswith("findings-") and name.endswith(".json"):
            tid = name[len("findings-"):-len(".json")]
            text = findings_context(rdir, name, tid) if F.ALERT_ID_RX.fullmatch(tid) else None
            claimed = os.path.join(rdir, f".claimed-{secrets.token_hex(6)}")
            try:
                os.rename(os.path.join(rdir, name), claimed)
            except FileNotFoundError:
                continue  # another session took it
            unlink(claimed)
            if text:
                texts.append(text)
            continue
        tid = name[len("memory-"):-len(".json")] if name.startswith("memory-") and name.endswith(".json") else ""
        if not F.ALERT_ID_RX.fullmatch(tid):
            continue
        claimed = os.path.join(rdir, f".claimed-{secrets.token_hex(6)}")
        try:
            os.rename(os.path.join(rdir, name), claimed)
        except FileNotFoundError:
            continue  # another session took it
        unlink(claimed)
        ids.append(tid)
    texts += [MEMORY_CONTEXT.format(tid=t) for t in ids]
    if texts:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": event_name, "additionalContext": "\n\n".join(texts)}}))


def display_questions(questions):
    """Masked copy for the page; the original stays in the hook's memory for the answer."""
    out = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        out.append({"question": short(q.get("question")), "header": short(q.get("header")),
                    "multiSelect": bool(q.get("multiSelect")),
                    "options": [{"label": short(o.get("label")), "description": short(o.get("description"))}
                                for o in q.get("options") or [] if isinstance(o, dict)]})
    return out


def build_answers(questions, picked):
    """{original question text: label or comma-joined labels}, from the indexes the page sent.
    None when anything does not fit: a wrong answer would be worse than the terminal dialog."""
    if not isinstance(picked, list) or len(picked) != len(questions):
        return None
    answers = {}
    for q, p in zip(questions, picked):
        if not isinstance(q, dict) or not isinstance(p, dict):
            return None
        options = q.get("options") or []
        idx = p.get("options") or []
        other = p.get("other") or ""
        if not isinstance(idx, list) or not isinstance(other, str):
            return None
        if any(not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(options) for i in idx):
            return None
        labels = [str(options[i].get("label", "")) for i in sorted(set(idx))]
        if other.strip():
            labels.append(other.strip())
        if not labels or (not q.get("multiSelect") and len(labels) != 1):
            return None
        answers[str(q.get("question", ""))] = ", ".join(labels)
    return answers


def page_age(adir):
    try:
        return time.time() - os.path.getmtime(os.path.join(adir, ".page"))
    except OSError:
        return float("inf")


def answers_on(adir):
    return os.path.exists(os.path.join(adir, "answers-on"))


def turns_on(adir):
    return os.path.exists(os.path.join(adir, "turns-on"))


MAX_MESSAGE = 4000


def wait_page(path, alert, adir, pid, still_on, extra=None, let_go=None, hold=None):
    """Holds while the page is open: the answer the page wrote ({qid, via: web, ...}), or None
    when it was released, the page went away, the switch went off, the session died or time ran
    out; alert["released"] then says why, for the page. let_go, when given, returns a reason to
    stop holding (a subagent of the session started). The alert carries a heartbeat so the page
    knows it is held."""
    answer_path, release_path = path[:-len(".json")] + ".answer.json", path[:-len(".json")] + ".release"
    deadline, beat = time.monotonic() + (hold if hold is not None else HOLD_SECONDS), time.monotonic()
    alert["released"] = "timeout"
    while time.monotonic() < deadline:
        time.sleep(POLL)
        got = extra() if extra else None
        if got:
            return {"context": got}
        picked = read_json(answer_path)
        if picked is not None:
            unlink(answer_path)
            if isinstance(picked, dict) and picked.get("qid") == alert["qid"] and picked.get("via") == "web":
                return picked
            alert["released"] = "bad-answer"
            return None
        if os.path.exists(release_path):
            unlink(release_path)
            alert["released"] = "released"
            return None
        if pid and not pid_alive(pid):
            unlink(path)
            return None
        why = let_go() if let_go else None
        if why:
            alert["released"] = why
            return None
        if not still_on() or page_age(adir) > PAGE_GONE:
            alert["released"] = "switch-off" if not still_on() else "page-gone"
            return None
        if time.monotonic() - beat >= HEARTBEAT_EVERY:
            beat = time.monotonic()
            alert["heartbeat"] = time.time()
            if os.path.exists(path):
                write_json(path, alert)
    return None


def hold_alert(flow, event, kind, detail, still_on, extra=None, holdable=True, let_go=None, hold=None, **fields):
    """Raises the alert, held for the page when it is open and holdable; returns (path, alert,
    answer). A hold that lets go records why and when (released, released_at) for the page."""
    path = F.alert_path(flow, event.get("session_id"), event.get("agent_id") or "main")
    if not path:
        return None, None, None
    adir = attention_dir(flow)
    for suffix in (".answer.json", ".release"):
        unlink(path[:-len(".json")] + suffix)
    held = holdable and still_on() and page_age(adir) < PAGE_FRESH
    pid = claude_pid()
    alert = {"kind": kind, "since": time.time(), "detail": short(detail, TURN_MAX if kind == "turn" else DETAIL_MAX), "claude_pid": pid,
             "qid": secrets.token_urlsafe(16), "held": held, "heartbeat": time.time(), **fields}
    write_json(path, alert)
    picked = wait_page(path, alert, adir, pid, still_on, extra, let_go, hold) if held else None
    if picked is None and os.path.exists(path):
        alert["held"] = False
        if held:
            alert["released_at"] = time.time()
        else:
            alert.pop("released", None)
        write_json(path, alert)
    return path, alert, picked


def hold_permission(event, flow):
    """PermissionRequest: allow or deny from the page while it is open (always on). Released,
    the dialog stays in the terminal."""
    tool = event.get("tool_name") or ""
    if tool == "AskUserQuestion":
        return 0  # the question's own hold shows it
    detail = F.tool_detail_full(event)
    path, alert, picked = hold_alert(flow, event, "permission", f"{tool}: {detail}" if detail else tool,
                                     lambda: True, tool=tool)
    if picked is None or picked.get("behavior") not in ("allow", "deny"):
        return 0
    unlink(path)
    decision = {"behavior": "allow"} if picked["behavior"] == "allow" else \
        {"behavior": "deny", "message": "The dev denied this on the Foreman dashboard."}
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}))
    return 0


def awaiting_findings(flow, tid):
    """True while the task has PASS_WITH_FINDINGS reviews newer than any decision: its session
    stopped to have the findings decided."""
    if not tid:
        return False
    tdir = os.path.join(F.work_dir(flow), tid)
    newest = 0
    for st in (flow.get("stages") or {}).values():
        if not st.get("verdict"):
            continue
        path = os.path.join(tdir, st.get("file", ""))
        try:
            with open(path, errors="replace") as fh:
                if "Verdict: PASS_WITH_FINDINGS" in fh.read(4000):
                    newest = max(newest, os.path.getmtime(path))
        except OSError:
            continue
    if not newest:
        return False
    try:
        return os.path.getmtime(os.path.join(tdir, "findings-decision.md")) < newest
    except OSError:
        return True


def claim_findings(flow, tid):
    """The context of the task's findings request, claimed (so only one session gets it), or None."""
    rdir = os.path.join(attention_dir(flow), REQUESTS)
    name = f"findings-{tid}.json"
    claimed = f".claimed-{secrets.token_hex(6)}"
    try:
        os.rename(os.path.join(rdir, name), os.path.join(rdir, claimed))
    except (FileNotFoundError, NotADirectoryError):
        return None
    text = findings_context(rdir, claimed, tid)
    unlink(os.path.join(rdir, claimed))
    return text


def hold_turn(event, flow):
    """Stop of the main session, with `work.sh ui turns on`: the dev's next message from the page
    continues the session (decision "block" with the message as the reason)."""
    adir = attention_dir(flow)
    tid = F.current_id(flow) if hasattr(F, "current_id") else None
    if event.get("agent_id") or not (turns_on(adir) or awaiting_findings(flow, tid)):
        return 0
    session, text = event.get("session_id"), event.get("last_assistant_message") or ""
    awaiting = awaiting_findings(flow, tid)
    # Only a turn that asks something is held, never one whose session has a subagent running:
    # while the Stop is held the session cannot take its subagent's completion and wake by itself,
    # and it sat blocked on a progress note while the page showed nothing to answer.
    if subagent_running(flow, session):
        return 0
    holdable = awaiting or F.asks(text)
    path_now = F.alert_path(flow, session, "main")

    def let_go():
        if subagent_running(flow, session):
            return "agent-running"
        # The dev typed in the terminal: UserPromptSubmit cleared the alert, the text goes in now.
        if path_now and not os.path.exists(path_now):
            return "prompted"
        return None
    path, alert, picked = hold_alert(flow, event, "turn", text,
                                     lambda: turns_on(adir) or awaiting_findings(flow, tid),
                                     extra=lambda: claim_findings(flow, tid), holdable=holdable, let_go=let_go,
                                     hold=TURN_HOLD_SECONDS,
                                     waits_for="findings" if awaiting else None, message=turn_message(text))
    if isinstance(picked, dict) and picked.get("context"):
        if path:
            unlink(path)
        print(json.dumps({"decision": "block", "reason": picked["context"]}, ensure_ascii=False))
        return 0
    message = picked.get("message") if isinstance(picked, dict) else None
    if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE:
        return 0
    unlink(path)
    print(json.dumps({"decision": "block", "reason": f"The dev answered on the Foreman dashboard: {message.strip()}"},
                     ensure_ascii=False))
    return 0


def hold(event, flow):
    # The matcher in settings.json already limits this entry to AskUserQuestion; checked here
    # too, because an "allow" printed for any other tool would skip its permission dialog.
    if event.get("tool_name") != "AskUserQuestion":
        return 0
    path = F.alert_path(flow, event.get("session_id"), event.get("agent_id") or "main")
    if not path:
        return 0
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    questions = tool_input.get("questions") if isinstance(tool_input.get("questions"), list) else []
    adir = attention_dir(flow)
    answer_path, release_path = path[:-len(".json")] + ".answer.json", path[:-len(".json")] + ".release"
    unlink(answer_path)
    unlink(release_path)
    shown = display_questions(questions)
    # Every question must be shown, or the page could not answer them all.
    held = bool(shown) and len(shown) == len(questions) and answers_on(adir) and page_age(adir) < PAGE_FRESH
    first = shown[0]["question"] if shown else ""
    pid = claude_pid()
    alert = {"kind": "question", "since": time.time(), "detail": first, "claude_pid": pid,
             "qid": secrets.token_urlsafe(16), "questions": shown, "held": held, "heartbeat": time.time()}
    write_json(path, alert)
    if not held:
        return 0

    deadline, beat = time.monotonic() + HOLD_SECONDS, time.monotonic()
    # Why the hold let go, for the page: "timeout" unless something else ended it first.
    released = "timeout"
    while time.monotonic() < deadline:
        time.sleep(POLL)
        picked = read_json(answer_path)
        if picked is not None:
            unlink(answer_path)
            answers = build_answers(questions, picked.get("answers")) \
                if isinstance(picked, dict) and picked.get("qid") == alert["qid"] and picked.get("via") == "web" else None
            if answers is not None:
                unlink(path)
                # Every other key of the input is kept: updatedInput replaces the whole input.
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "allow",
                    "updatedInput": {**tool_input, "questions": questions, "answers": answers}}},
                    ensure_ascii=False))
                return 0
            released = "bad-answer"
            break
        if os.path.exists(release_path):
            unlink(release_path)
            released = "released"
            break
        if pid and not pid_alive(pid):
            # The session died: nobody will read an answer, and the alert would show forever.
            unlink(path)
            return 0
        if not answers_on(adir) or page_age(adir) > PAGE_GONE:
            released = "switch-off" if not answers_on(adir) else "page-gone"
            break
        if time.monotonic() - beat >= HEARTBEAT_EVERY:
            beat = time.monotonic()
            alert["heartbeat"] = time.time()
            if os.path.exists(path):
                write_json(path, alert)
    if os.path.exists(path):
        alert.update(held=False, released=released, released_at=time.time())
        write_json(path, alert)
    return 0


def main(argv):
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            return 0
        flow = F.load_flow()
        if flow is None:
            return 0
        if argv[1:2] == ["hold"]:
            return hold(event, flow)
        if argv[1:2] == ["permission"]:
            return hold_permission(event, flow)
        if argv[1:2] == ["turn"]:
            return hold_turn(event, flow)
        handle(event, flow)
    except Exception as e:  # a hook must never break the session; the reason goes to stderr
        print(f"foreman attention: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
