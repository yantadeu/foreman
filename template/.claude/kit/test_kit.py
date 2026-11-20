#!/usr/bin/env python3
"""Proves the kit's checks bite: run with `python3 .claude/kit/test_kit.py`."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

KIT = os.path.dirname(os.path.abspath(__file__))
CLAUDE = os.path.dirname(KIT)
HOOK = os.path.join(KIT, "hooks", "flow.py")
WORK_SH = os.path.join(KIT, "scripts", "work.sh")
LEARN = os.path.join(KIT, "scripts", "learnings.py")
EXPORT = os.path.join(KIT, "scripts", "export.py")
EVALS = os.path.join(KIT, "evals", "run.py")
DASH = os.path.join(KIT, "scripts", "dashboard.py")
ATTN = os.path.join(KIT, "hooks", "attention.py")
WIRE = os.path.join(KIT, "scripts", "wire_hooks.py")
FLOW = os.path.join(CLAUDE, "flow.json")

PURPOSE = "# p\n\nLane: {lane}\n\n## Acceptance criteria\n- AC-001: a\n- AC-002: b\n"
CONTRACT = """Status: DONE

## The request in one sentence
x
## How it is proven
{proven}
## What stays out
x
## Where it touches
x
## What can fail silently
{silent}
## Sources read
x
"""
GOOD = CONTRACT.format(proven="- AC-001: x\n- AC-002: y", silent="- the wait closes green at zero")


def run(root, args, payload=None):
    env = dict(os.environ, CLAUDE_PROJECT_DIR=root)
    cmd = ["bash", *args] if args[0].endswith(".sh") else [sys.executable, *args]
    p = subprocess.run(cmd, input=json.dumps(payload or {}), capture_output=True, text=True, env=env, cwd=root)
    return p.returncode, p.stdout + p.stderr


def stop(root, agent_type, active=False, transcript="handed work dir .work/TASK-1"):
    t = os.path.join(root, "transcript.jsonl")
    open(t, "w").write(json.dumps({"prompt": transcript}) + "\n")
    return run(root, [HOOK], {"hook_event_name": "SubagentStop", "agent_id": "a1", "agent_type": agent_type,
                              "stop_hook_active": active, "agent_transcript_path": t,
                              "last_assistant_message": "done"})[0]


def launch(root, agent_type, prompt="work dir .work/TASK-1"):
    """Output of the hook when the orchestrator launches a stage: a deny JSON, or empty."""
    return run(root, [HOOK], {"hook_event_name": "PreToolUse", "tool_name": "Agent",
                              "tool_input": {"subagent_type": agent_type, "prompt": prompt}})[1]


def main():
    # The registry of projects (one dashboard serves them all) goes to a temporary home: the
    # checks never touch the dev's ~/.foreman.
    home = tempfile.mkdtemp()
    os.environ["FOREMAN_HOME"] = home
    try:
        checks()
    finally:
        shutil.rmtree(home, ignore_errors=True)


def checks():
    root = tempfile.mkdtemp()
    try:
        shutil.copytree(CLAUDE, os.path.join(root, ".claude"), ignore=shutil.ignore_patterns("__pycache__"))
        for cmd in (["git", "init", "-q"], ["git", "-c", "user.name=t", "-c", "user.email=t@localhost",
                                            "commit", "-q", "--allow-empty", "-m", "base"]):
            subprocess.run(cmd, cwd=root, check=True, capture_output=True)
        work = os.path.join(root, ".work")
        w = os.path.join(work, "TASK-1")
        write = lambda name, text: open(os.path.join(w, name), "w").write(text)

        # Stage gate on SubagentStop
        assert stop(root, "refiner") == 0, "outside the flow (no current) must not gate"
        assert run(root, [WORK_SH, "init", "PROJ 1"])[0] != 0 and not os.path.exists(os.path.join(work, "current")), \
            "an ID the hook would ignore must be refused at init"
        assert run(root, [WORK_SH, "init", "TASK-1", "t"])[0] == 0
        assert "Base: " in open(os.path.join(w, "ledger.md")).read(), "init must record the base commit"
        os.remove(os.path.join(w, "00-purpose.md"))
        assert stop(root, "refiner") == 2, "no purpose must block"
        write("00-purpose.md", "# p\n\n## Acceptance criteria\n- the user is happy\n")
        write("01-contract.md", GOOD)
        assert stop(root, "refiner") == 2, "a purpose without AC-### ids must block"
        write("00-purpose.md", PURPOSE.format(lane="Light"))
        os.remove(os.path.join(w, "01-contract.md"))
        assert stop(root, "refiner") == 2, "no contract must block"
        write("01-contract.md", "Status: DONE\n\n## The request in one sentence\nx\n")
        assert stop(root, "refiner") == 2, "missing sections must block"
        write("01-contract.md", GOOD.replace("Status: DONE\n", ""))
        assert stop(root, "refiner") == 2, "a stage file without a Status line must block"
        write("01-contract.md", CONTRACT.format(proven="- AC-001: x", silent="- x"))
        assert stop(root, "refiner") == 2, "a contract that does not cite AC-002 must block"
        write("01-contract.md", CONTRACT.format(proven="- AC-001: x\n- AC-002: y", silent="- none"))
        assert stop(root, "refiner") == 2, "'none' as silent failure must block"
        write("01-contract.md", GOOD)
        assert stop(root, "refiner") == 0, "a complete contract must pass"

        write("04-review-qa.md", "## Findings\n- none\n")
        assert stop(root, "qa-reviewer") == 2, "a review without a verdict must block"
        write("04-review-qa.md", "Verdict: PASS\n\n## Findings\n- none\n")
        assert stop(root, "qa-reviewer") == 0, "a review with a verdict must pass"
        assert stop(root, "executor", active=True) == 0, "second stop attempt must not loop"
        assert stop(root, "Explore") == 0, "an agent that is not a stage must not gate"
        assert stop(root, "executor", transcript="review MR 57") == 0, \
            "a stale current must not capture an agent launched for something else"
        assert stop(root, "executor") == 2, "the same agent handed the work dir must be gated"

        # Readiness on launch (PreToolUse of the Agent tool)
        assert launch(root, "executor") == "", "executor may start once the contract passes"
        write("01-contract.md", GOOD.replace("- AC-002: y\n", "- AC-002: y [NEEDS CLARIFICATION: CSV or XLSX?]\n"))
        assert '"deny"' in launch(root, "executor") and "NEEDS CLARIFICATION" in launch(root, "executor"), \
            "an open clarification must stop the executor"
        assert launch(root, "executor", prompt="review MR 57") == "", "a launch for something else is not gated"
        code, out = run(root, [WORK_SH, "status"])
        assert code == 1 and "OPEN" in out, "status must report open clarifications"
        write("01-contract.md", GOOD)
        assert '"deny"' in launch(root, "tester"), "the tester must not start before the executor's file"
        write("01-contract.md", GOOD.replace("Status: DONE", "Status: BLOCKED"))
        assert '"deny"' in launch(root, "executor"), "a BLOCKED refiner must stop the executor"
        write("01-contract.md", GOOD)
        assert '"deny"' in launch(root, "reviewer"), "Light lane: reviewers need the stages before them"
        write("00-purpose.md", PURPOSE.format(lane="None"))
        assert launch(root, "reviewer") == "", "None lane: reviewers run on the change directly"
        write("00-purpose.md", PURPOSE.format(lane="Light"))
        for _ in range(4):
            run(root, [HOOK], {"hook_event_name": "SubagentStart", "agent_id": "e", "agent_type": "executor"})
        assert '"deny"' in launch(root, "executor") and "fix rounds" in launch(root, "executor"), \
            "the executor must not run past max_fix_rounds"

        # Event log
        secret = "psql cpf=123.456.789-09 pan=4111 1111 1111 1111 PGPASSWORD=hunter2 Bearer abc.def"
        run(root, [HOOK], {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": secret}})
        log = open(os.path.join(work, "events.jsonl")).read()
        for leak in ("123.456.789-09", "4111 1111", "hunter2", "abc.def"):
            assert leak not in log, f"{leak} leaked into the event log"
        assert all(isinstance(json.loads(line)["t"], float) for line in log.splitlines()), "masking broke the JSON"
        assert '"gate": "failed"' in log, "a stop let through on retry must be logged as failed"

        write("02-change.md", "Status: DONE\n\n## What changed\nx\n")
        code, out = run(root, [WORK_SH, "status"])
        assert code == 1 and "FAILED" in out, "status must report a stage that failed its gate"

        # Review package
        open(os.path.join(root, "app.py"), "w").write("print('changed')\n")
        code, out = run(root, [WORK_SH, "review-package"])
        diff = open(os.path.join(w, "review.diff")).read()
        assert code == 0 and "app.py" in diff, "review-package must hold the diff since the base"

        os.remove(os.path.join(work, "current"))
        before = open(os.path.join(work, "events.jsonl")).read()
        run(root, [HOOK], {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {"file_path": "x"}})
        assert open(os.path.join(work, "events.jsonl")).read() == before, "nothing is logged outside a work"

        # Configuration errors
        flow_path = os.path.join(root, ".claude", "flow.json")
        good = open(flow_path).read()
        open(flow_path, "w").write(json.dumps({**json.loads(good), "work_dir": ".."}))
        open(os.path.join(work, "current"), "w").write("TASK-1")
        code, out = run(root, [HOOK], {"hook_event_name": "SubagentStop", "agent_type": "executor"})
        assert code == 0 and "must be a subdirectory" in out, "a work_dir outside the repo must be refused, loudly"
        open(flow_path, "w").write("{bad")
        code, out = run(root, [HOOK], {"hook_event_name": "SubagentStop", "agent_type": "executor"})
        assert code == 0 and "invalid" in out, "a broken flow.json must say the gate is off"
        assert run(root, [HOOK, "status", "TASK-1"])[0] == 2, "a configuration error exits 2"
        open(flow_path, "w").write(good)

        # Projections for Cursor, Codex and Copilot
        code, out = run(root, [EXPORT, "--tools", "cursor,codex,copilot"])
        assert code == 0 and os.path.exists(os.path.join(root, ".github", "agents", "refiner.agent.md")) \
            and os.path.exists(os.path.join(root, ".github", "prompts", "task.prompt.md")) \
            and os.path.exists(os.path.join(root, ".cursor", "rules", "task.mdc")), out
        assert run(root, [EXPORT, "--tools", "cursor,codex,copilot", "--check"])[0] == 0, "export must be stable"
        assert run(root, [EXPORT, "--tools", "nope"])[0] == 2, "an unknown tool is a usage error"

        # Eval graders, offline: they must fail a bad review and pass a good one
        ev = os.path.join(root, "eval")
        shutil.copytree(os.path.join(KIT, "evals", "cases", "qa-vacuous-test", "work"), os.path.join(ev, ".work", "EVAL-3"))
        os.makedirs(os.path.join(ev, ".claude"))
        shutil.copy(flow_path, os.path.join(ev, ".claude", "flow.json"))
        review = os.path.join(ev, ".work", "EVAL-3", "04-review-qa.md")
        open(review, "w").write("Verdict: PASS\n\n## Findings\n- none\n")
        assert run(root, [EVALS, "--grade", "qa-vacuous-test", ev])[0] == 1, "the grader must fail a PASS on a vacuous test"
        open(review, "w").write("Verdict: FAIL\n\n## Findings\n- [critical] [route: tester] AC-002: no test\n")
        code, out = run(root, [EVALS, "--grade", "qa-vacuous-test", ev])
        assert code == 0, "the grader must pass a correct review: " + out

        # Dashboard server: tasks of every worktree, status per task, and its refusals
        import importlib.util
        spec = importlib.util.spec_from_file_location("dashboard", DASH)
        dash = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dash)
        g = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@localhost", *a],
                                      cwd=root, check=True, capture_output=True)
        g("add", "-A", ".claude")
        g("commit", "-qm", "kit")
        wt2 = root + "-wt2"
        g("worktree", "add", "-q", wt2, "-b", "feature/other")
        try:
            w2 = os.path.join(wt2, ".work", "OTHER-1")
            os.makedirs(w2)
            open(os.path.join(w2, "00-purpose.md"), "w").write(PURPOSE.format(lane="Light"))
            open(os.path.join(w2, "01-contract.md"), "w").write(GOOD.replace("- AC-002: y", "- AC-002: [NEEDS CLARIFICATION: which?]"))
            done = os.path.join(work, "DONE-1")
            os.makedirs(done)
            open(os.path.join(done, "00-purpose.md"), "w").write(PURPOSE.format(lane="None"))
            for lens in ("contract", "security", "qa", "product"):
                open(os.path.join(done, f"04-review-{lens}.md"), "w").write("Verdict: PASS\n\n## Findings\n- none\n")
            open(os.path.join(done, "ledger.md"), "w").write("Base: x\n<!-- example:\n- Ruling: <decision> | why: <reason> -->\n- Ruling: CSV | why: AC-001 | cost if wrong: one writer\n")
            groups = dash.list_tasks(root)
            found = {(grp["name"], t["id"]): t["status"] for grp in groups for t in grp["tasks"]}
            assert found.get((os.path.basename(wt2), "OTHER-1")) == "clarify", f"another worktree's task must be listed: {found}"
            assert found.get((os.path.basename(root), "DONE-1")) == "ready-for-mr", \
                f"four verdicts are not done: only a PR/MR, a merge or work.sh done closes a task: {found}"
            here = next(grp["wt"] for grp in groups if grp["here"])
            detail = dash.task_detail(root, here, "DONE-1")
            assert detail["rulings"] == ["Ruling: CSV | why: AC-001 | cost if wrong: one writer"], \
                "the template's commented example is not a ruling"
            assert dash.task_detail(root, here, "../etc") is None, "an ID outside the pattern is refused"
            assert dash.task_detail(root, 99, "DONE-1") is None, "a worktree index outside the list is refused"
            open(os.path.join(done, "01-contract.md"), "w").write(GOOD.replace("Status: DONE", "Status: BLOCKED"))
            for lens in ("contract", "security", "qa", "product"):
                os.remove(os.path.join(done, f"04-review-{lens}.md"))
            assert dash.task_detail(root, here, "DONE-1")["status"] == "blocked", "a BLOCKED stage blocks the task"
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", wt2], cwd=root, capture_output=True)
            shutil.rmtree(wt2, ignore_errors=True)

        # Learnings by tag and by lens
        open(os.path.join(root, "L.md"), "w").write(
            "# L\n```\n## <slug>\n```\n## a\n- **Tags**: qa\nA\n\n## b\n- **Tags**: security\nB\n\n## c\nC\n")
        flow = json.loads(good)
        flow["learnings"] = "L.md"
        open(flow_path, "w").write(json.dumps(flow))
        code, out = run(root, [LEARN, "security"])
        assert code == 0 and "## b" in out and "## a" not in out, out
        code, out = run(root, [LEARN, "--lens", "security"])
        assert code == 0 and "## b" in out and "## a" not in out, "--lens must use the lens tags from flow.json"
        assert run(root, [LEARN, "--lens", "nope"])[0] != 0, "an unknown lens must fail, not print nothing"
        code, out = run(root, [LEARN, "--untagged"])
        assert code == 1 and "## c" in out and "<slug>" not in out, out
    finally:
        shutil.rmtree(root)
    merge_order()
    merge_order_uncommitted()
    ship()
    attention_hooks()
    attention_server()
    wiring()
    hooks_via_main()
    closing_status()
    fix_round_status()
    partial_rerun_status()
    reopened_findings()
    closing_mr()
    task_owns_branch()
    mr_fetch_http()
    work_done()
    closing_cleanup()
    close_and_actions()
    event_stream()
    findings_view()
    whole_findings()
    closing_git()
    findings_decision_endpoint()
    decision_keys()
    page_holds()
    turn_holds()
    page_visibility()
    alert_text()
    turn_awaits()
    findings_decided_message()
    triage_task()
    language_hook()
    rooms_and_agents()
    switches()
    closing_docs()
    projects_server()
    print("ok: gates, readiness, fix-round cap, event log, review package, exports, eval graders, dashboard,"
          " learnings, merge order, attention alerts, web answers, hook wiring, closing and cleanup, one dashboard for every project")


def projects_server():
    """One dashboard serves every registered project: /api/projects lists them with their alert
    counts, ?p=<index> picks one for every other endpoint, an unknown index is a 404, and only the
    project on screen counts as an open page for the holds."""
    import http.client
    import threading
    d = load_dashboard("dashboard_projects")
    base = tempfile.mkdtemp()
    old_home = os.environ.get("FOREMAN_HOME")
    os.environ["FOREMAN_HOME"] = os.path.join(base, "home")
    try:
        roots = []
        for name in ("alpha", "beta"):
            r = os.path.join(base, name)
            os.makedirs(os.path.join(r, ".claude"))
            shutil.copy(FLOW, os.path.join(r, ".claude", "flow.json"))
            subprocess.run(["git", "init", "-q", r], check=True, capture_output=True)
            subprocess.run(["git", "-C", r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i"], check=True, capture_output=True)
            roots.append(os.path.realpath(r))
        alpha, beta = roots
        assert not d.register(base), "a directory without Foreman is not registered"
        assert d.register(beta) and d.register(beta), "registering twice keeps one entry"
        assert [x["root"] for x in d.read_registry()] == [beta], d.read_registry()
        assert d.project_roots(alpha) == [alpha, beta], "the server's own project and the registry's, by path"
        os.makedirs(os.path.join(beta, ".work", "TASK-B"))
        open(os.path.join(beta, ".work", "TASK-B", "00-purpose.md"), "w").write("# TASK-B beta work\n")
        adir = os.path.join(beta, ".work", "attention", "s1")
        os.makedirs(adir)
        now = time.time()
        open(os.path.join(adir, "main.json"), "w").write(json.dumps({"kind": "turn", "since": now, "detail": "", "claude_pid": os.getpid()}))
        listed = d.list_projects(alpha, now)
        assert [(x["name"], x["here"], x["pending"]) for x in listed] == [("alpha", True, 0), ("beta", False, 1)], listed
        assert not os.path.exists(os.path.join(beta, ".work", "attention", ".page")), "counting alerts must not open the page"
        handler = type("H", (d.Handler,), {"root": alpha, "token": "t" * 24})
        server = d.http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            def get(path):
                c = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                c.request("GET", path, headers={"Host": "localhost"})
                r = c.getresponse()
                return r.status, r.read().decode()
            code, body = get("/api/tasks")
            assert code == 200 and json.loads(body) == [], "without ?p: the server's own project"
            code, body = get("/api/tasks?p=1")
            assert code == 200 and [t["id"] for g in json.loads(body) for t in g["tasks"]] == ["TASK-B"], body
            assert get("/api/tasks?p=7")[0] == 404 and get("/flow.json?p=7")[0] == 404, "an unknown project is a 404"
            code, body = get("/api/projects")
            assert code == 200 and [x["name"] for x in json.loads(body)] == ["alpha", "beta"], body
            assert get("/api/attention?p=1&visible=1")[0] == 200 and os.path.exists(os.path.join(beta, ".work", "attention", ".page")), \
                "the project on screen counts as an open page"
        finally:
            server.shutdown()
    finally:
        if old_home is None:
            os.environ.pop("FOREMAN_HOME", None)
        else:
            os.environ["FOREMAN_HOME"] = old_home
        shutil.rmtree(base, ignore_errors=True)


def merge_order():
    """Stacked branches merge after their parent, overlapping ones after the one that started
    first, and a branch that touches nothing shared is independent."""
    repo = tempfile.mkdtemp()
    try:
        def git(*a, t=None):
            env = dict(os.environ, GIT_AUTHOR_DATE=f"{t} +0000", GIT_COMMITTER_DATE=f"{t} +0000") if t else None
            subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True, env=env)
        def commit(branch, path, t, start="develop"):
            git("checkout", "-q", "-B", branch, start)
            open(os.path.join(repo, path), "a").write(branch + "\n")
            git("add", "-A")
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", branch, t=t)
        git("init", "-q", "-b", "develop")
        for f in ("shared", "a2", "solo"):
            open(os.path.join(repo, f), "w").write("x\n")
        git("add", "-A")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base", t=1_700_000_000)
        commit("late-overlap", "shared", 1_700_000_300)           # started after early, same file
        commit("early", "shared", 1_700_000_100)
        commit("stacked", "a2", 1_700_000_200, start="early")     # cut from early
        commit("solo", "solo", 1_700_000_050)
        commit("merged", "a2", 1_700_000_060)
        # A child shares its parent's first commit; here its name sorts before the parent's.
        commit("zz-parent", "zfile", 1_700_000_400)
        commit("aa-child", "zchild", 1_700_000_500, start="zz-parent")
        git("checkout", "-q", "develop")
        git("merge", "-q", "--ff-only", "merged")                  # already in the base
        def run(names):
            p = subprocess.run([sys.executable, os.path.join(KIT, "scripts", "merge-order.py"), "--base", "develop",
                                "--json", *names], capture_output=True, text=True, cwd=repo)
            assert p.returncode == 0, p.stderr
            return json.loads(p.stdout)
        names = ["stacked", "late-overlap", "solo", "early", "merged", "aa-child", "zz-parent"]
        got = run(names)
        # The answer cannot depend on the order the branches were named in.
        assert run(names[::-1]) == got, "the order changed with the order of the arguments"
        order = got["order"]
        assert "merged" not in order, "a branch already in the base has nothing to merge"
        assert order.index("early") < order.index("stacked"), f"stacked goes after its parent: {order}"
        assert order.index("early") < order.index("late-overlap"), f"overlap goes after the one that started first: {order}"
        assert got["independent"] == ["solo"], got
        assert order.index("zz-parent") < order.index("aa-child"), f"a child goes after its parent: {order}"
        kinds = {(e["first"], e["then"]): e["kind"] for e in got["edges"]}
        # stacked carries early's change to shared, so it overlaps late-overlap too.
        assert kinds == {("early", "stacked"): "stacked", ("early", "late-overlap"): "overlap",
                         ("stacked", "late-overlap"): "overlap", ("zz-parent", "aa-child"): "stacked"}, kinds
    finally:
        shutil.rmtree(repo)


def merge_order_uncommitted():
    """Running tasks keep their work uncommitted: the worktree's changed files count. A and B
    create the same new files, B and C change the same four files, A and C are independent: A
    and C merge first, B rebases once onto both. The sidebar gets the same order per branch."""
    base = tempfile.mkdtemp()
    try:
        repo = os.path.join(base, "repo")
        os.makedirs(repo)
        g = lambda *a, cwd=repo: subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                                check=True, capture_output=True, text=True).stdout
        g("init", "-q", "-b", "develop")
        for f in ("w1", "w2", "w3", "w4", "solo", "c-only"):
            open(os.path.join(repo, f), "w").write("x\n")
        g("add", "-A")
        g("commit", "-qm", "base")
        wt = {}
        for i, name in enumerate(("task-b", "task-a", "task-c", "task-d")):
            wt[name] = os.path.join(base, name)
            g("worktree", "add", "-q", "-b", name, wt[name])
            # B's worktree is the newest; the rule must not lean on it to put B last.
            os.utime(os.path.join(wt[name], ".git"), (1_700_000_000 + 100 * i,) * 2)
        def write(name, path, text="y\n"):
            full = os.path.join(wt[name], path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            open(full, "w").write(text)
        shared = [f"src/ports/{p}.py" for p in ("orders", "ledger", "notify", "phase", "billing", "export")]
        for p in shared:
            write("task-a", p)
            write("task-b", p)
        for f in ("w1", "w2", "w3", "w4"):
            write("task-b", f)
            write("task-c", f)
        write("task-c", "c-only")
        # D committed its work and left one more file uncommitted: both count, and it overlaps no one.
        write("task-d", "solo")
        g("add", "-A", cwd=wt["task-d"])
        g("commit", "-qm", "d", cwd=wt["task-d"])
        write("task-d", "d-extra")
        script = os.path.join(KIT, "scripts", "merge-order.py")
        run = lambda *a: subprocess.run([sys.executable, script, "--base", "develop", *a], capture_output=True, text=True, cwd=repo)
        p = run("--json")
        assert p.returncode == 0, p.stderr
        got = json.loads(p.stdout)
        order = got["order"]
        assert set(order) == {"task-a", "task-b", "task-c", "task-d"}, f"uncommitted work counts: {got}"
        assert order.index("task-b") > order.index("task-a") and order.index("task-b") > order.index("task-c"), \
            f"B overlaps both: it merges after A and C and rebases once: {order}"
        kinds = {(e["first"], e["then"]): e["kind"] for e in got["edges"]}
        assert kinds == {("task-a", "task-b"): "overlap", ("task-c", "task-b"): "overlap"}, kinds
        assert "src/ports/orders.py" in next(e["detail"] for e in got["edges"] if e["first"] == "task-a"), got
        assert got["pending"] == ["task-a", "task-b", "task-c"] and got["independent"] == ["task-d"], got
        text = run().stdout
        assert "task-b (not committed yet)" in text and "task-d (not committed yet)" not in text \
            and "task-d  independent, any order" in text, text
        # The sidebar's per-branch place, from the server, against the remote's default branch.
        g("remote", "add", "origin", repo)
        g("fetch", "-q", "origin")
        g("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop")
        d = load_dashboard("dash_merge")
        pos = d.merge_positions(repo)
        assert pos["task-b"]["pos"] > max(pos["task-a"]["pos"], pos["task-c"]["pos"]) and pos["task-b"]["total"] == 4 \
            and sorted(pos["task-b"]["after"]) == ["task-a", "task-c"] and pos["task-b"]["pending"] \
            and pos["task-d"]["independent"] and not pos["task-d"]["pending"], pos
    finally:
        shutil.rmtree(base)


def ship():
    """ship.py: off by default, it prints the push and the PR/MR command; on, it pushes the task's
    own branch and opens the PR/MR with the tool picked from the origin (gh for github.com, glab for a
    GitLab), keeps the URL in mr.json, refuses a base branch, names a missing or logged-out tool, and
    shell-quotes each value of a custom template."""
    base = os.path.realpath(tempfile.mkdtemp())
    try:
        remote, repo, bin_dir = os.path.join(base, "remote.git"), os.path.join(base, "repo"), os.path.join(base, "bin")
        os.makedirs(bin_dir)
        g = lambda *a: subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                      check=True, capture_output=True, text=True).stdout
        subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
        os.makedirs(os.path.join(repo, ".claude"))
        flow = json.load(open(FLOW))
        json.dump(flow, open(os.path.join(repo, ".claude", "flow.json"), "w"))
        open(os.path.join(repo, ".gitignore"), "w").write(".work/\n")
        g("init", "-q", "-b", "main")
        g("add", "-A")
        g("commit", "-qm", "init")
        g("remote", "add", "origin", remote)
        g("push", "-q", "origin", "main")
        g("fetch", "-q", "origin")
        g("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        g("remote", "set-url", "origin", "https://github.com/acme/app.git")
        g("remote", "set-url", "--push", "origin", remote)
        g("checkout", "-q", "-b", "feat/abc-12")
        tdir = os.path.join(repo, ".work", "ABC-12")
        os.makedirs(tdir)
        open(os.path.join(tdir, "00-purpose.md"), "w").write("# ABC-12 CSV export\n\nThe purpose.\n")
        open(os.path.join(tdir, "branch"), "w").write("feat/abc-12\n")
        log = os.path.join(base, "calls.log")

        def tool(name, body):
            path = os.path.join(bin_dir, name)
            open(path, "w").write(f"#!/bin/sh\necho \"{name} $*\" >> {log}\n{body}\n")
            os.chmod(path, 0o755)
        script = os.path.join(KIT, "scripts", "ship.py")

        def run(*a, path=None, bare=False):
            env = dict(os.environ, CLAUDE_PROJECT_DIR=repo, PATH=(path or bin_dir) + ("" if bare else os.pathsep + "/usr/bin:/bin"))
            return subprocess.run([sys.executable, script, *a], capture_output=True, text=True, cwd=repo, env=env)

        def set_ship(**cfg):
            json.dump({**flow, "ship": cfg}, open(os.path.join(repo, ".claude", "flow.json"), "w"))

        # Off by default: the commands to run, nothing pushed, nothing opened.
        p = run("ABC-12")
        assert p.returncode == 0 and "git push -u origin feat/abc-12" in p.stdout and "gh pr create" in p.stdout, p.stdout + p.stderr
        assert not os.path.exists(log), "off: no tool is called"
        # On, auto picks gh for github.com; a missing gh is named, never a crash.
        set_ship(enabled=True)
        # A PATH with git only: a runner may have a real gh in /usr/bin.
        only_git = os.path.join(base, "only-git")
        os.makedirs(only_git)
        os.symlink(shutil.which("git"), os.path.join(only_git, "git"))
        p = run("ABC-12", path=only_git, bare=True)
        assert p.returncode == 1 and "gh is not installed" in p.stderr, p.stderr
        tool("gh", 'case "$1" in auth) exit 1;; esac')
        p = run("ABC-12")
        assert p.returncode == 1 and "gh auth login" in p.stderr, f"logged out: says what to run: {p.stderr}"
        tool("gh", 'case "$1" in auth) exit 0;; pr) echo "https://github.com/acme/app/pull/42";; esac')
        p = run("ABC-12")
        assert p.returncode == 0 and p.stdout.strip() == "https://github.com/acme/app/pull/42", p.stdout + p.stderr
        kept = json.load(open(os.path.join(tdir, "mr.json")))
        assert kept == {"iid": 42, "url": "https://github.com/acme/app/pull/42", "state": "opened", "branch": "feat/abc-12"}, kept
        assert "feat/abc-12" in subprocess.run(["git", "-C", remote, "branch"], capture_output=True, text=True).stdout, \
            "the branch was pushed"
        calls = open(log).read()
        assert "--base main --head feat/abc-12" in calls and "--draft" in calls, calls
        # A GitLab origin picks glab.
        g("remote", "set-url", "origin", "https://gitlab.example.com/acme/app.git")
        g("remote", "set-url", "--push", "origin", remote)
        tool("glab", 'case "$1" in auth) exit 0;; mr) echo "https://gitlab.example.com/acme/app/-/merge_requests/7";; esac')
        p = run("ABC-12")
        assert p.returncode == 0 and p.stdout.strip().endswith("/merge_requests/7") and "glab mr create" in open(log).read(), \
            p.stdout + p.stderr
        # A custom template: each value is one quoted word, never split or run as shell.
        open(os.path.join(tdir, "00-purpose.md"), "w").write("# ABC-12 Export; rm -rf $HOME `x`\n")
        tool("pr-tool", 'printf "%s\\n" "$@" > ' + os.path.join(base, "args.txt") + '\necho "https://forge.example.com/pr/9"')
        set_ship(enabled=True, tool="custom", command="pr-tool --title {title} --into {base}")
        p = run("ABC-12")
        args = open(os.path.join(base, "args.txt")).read().splitlines()
        assert p.returncode == 0 and args == ["--title", "Export; rm -rf $HOME `x`", "--into", "main"], (args, p.stderr)
        # A base branch never ships.
        open(os.path.join(tdir, "branch"), "w").write("main\n")
        g("checkout", "-q", "main")
        p = run("ABC-12")
        assert p.returncode == 1 and "base branch" in p.stderr, p.stderr
    finally:
        shutil.rmtree(base)


def hook(root, payload, *args, env=None, script=None, raw=None):
    """Runs a hook as Claude Code does; returns (code, stdout, stderr, seconds)."""
    e = dict(os.environ, CLAUDE_PROJECT_DIR=root, **(env or {}))
    t = time.monotonic()
    p = subprocess.run([sys.executable, script or HOOK, *args], capture_output=True, text=True, env=e,
                       input=raw if raw is not None else json.dumps(payload))
    return p.returncode, p.stdout, p.stderr, time.monotonic() - t


def kit_root():
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, ".claude"))
    shutil.copy(FLOW, os.path.join(root, ".claude", "flow.json"))
    return root


QUESTIONS = [
    {"question": "Which track? cpf 123.456.789-09", "header": "Track", "multiSelect": False,
     "options": [{"label": "Light Spec (Recommended)", "description": "adapter"},
                 {"label": "No Spec", "description": "reversible"}]},
    {"question": "Which layers?", "header": "Layers", "multiSelect": True,
     "options": [{"label": "unit", "description": ""}, {"label": "integration", "description": ""},
                 {"label": "semana", "description": ""}]},
]


def ask(session="s1", agent=None):
    e = {"hook_event_name": "PreToolUse", "session_id": session, "tool_name": "AskUserQuestion",
         "tool_input": {"questions": QUESTIONS}}
    return {**e, "agent_id": agent} if agent else e


def attention_hooks():
    """Alerts are raised and cleared per (session, agent) with or without a current work, the
    hold answers through the PreToolUse contract only when opted in and a page is open, and
    no path breaks a session or prints a stray decision."""
    root = kit_root()
    try:
        att = os.path.join(root, ".work", "attention")
        alert = lambda s, a="main": os.path.join(att, s, a + ".json")
        load = lambda s, a="main": json.load(open(alert(s, a)))
        assert not os.path.exists(os.path.join(root, ".work", "current"))

        # 1. Raise and clear, no current task.
        code, out, err, secs = hook(root, ask(), "hold", script=ATTN)
        assert code == 0 and out == "" and secs < 4, (code, out, err, secs)
        q = load("s1")
        assert q["kind"] == "question" and q["held"] is False and q["qid"], q
        assert q["questions"][0]["options"][0]["label"] == "Light Spec (Recommended)", q
        assert "123.456.789-09" not in json.dumps(q), "a question text must be masked on disk"
        # The "*" entry runs in parallel with the hold: it must not erase the question.
        hook(root, ask())
        assert os.path.exists(alert("s1")), "the '*' PreToolUse entry erased the held question"
        hook(root, {"hook_event_name": "PostToolUse", "session_id": "s1", "tool_name": "AskUserQuestion"})
        assert not os.path.exists(alert("s1")), "PostToolUse AskUserQuestion must clear the question"

        secret = ("psql cpf=123.456.789-09 pan=4111 1111 1111 1111 PGPASSWORD=hunter2 && mysql --password s3cr3tA "
                  "&& gpg --passphrase=s3cr3tB && curl -u bob:s3cr3tC https://x && aws s3 ls AKIAABCDEFGHIJKLMNOP "
                  "&& curl 'https://b/o?X-Amz-Signature=s3cr3tD&y=1'")
        perm = {"hook_event_name": "PermissionRequest", "session_id": "s1", "agent_id": "a1",
                "tool_name": "Bash", "tool_input": {"command": secret}}
        # The permission hold entry raises it; with no page open it returns at once and prints
        # nothing, so the dialog stays in the terminal.
        code, out, _, secs = hook(root, perm, "permission", script=ATTN)
        assert code == 0 and out == "" and secs < 5, "PermissionRequest without a page must not print: stdout is a decision"
        p = load("s1", "a1")
        assert p["kind"] == "permission" and p["tool"] == "Bash" and "psql" in p["detail"], p
        sys.path.insert(0, os.path.dirname(HOOK))
        import flow as flow_mod
        sys.path.remove(os.path.dirname(HOOK))
        p_text = json.dumps(p) + flow_mod.mask(secret)
        for leak in ("123.456.789-09", "4111 1111", "hunter2", "s3cr3t", "AKIAABCDEFGHIJKLMNOP"):
            assert leak not in p_text, f"{leak} leaked into the alert"
        # A Notification arrives ~6 s later for the same prompt: it must not replace the tool.
        hook(root, {"hook_event_name": "Notification", "session_id": "s1", "agent_id": "a1",
                    "notification_type": "permission_prompt", "message": "Claude needs your permission"})
        assert load("s1", "a1")["tool"] == "Bash", "the fallback overwrote the PermissionRequest alert"
        # Another agent's tool call leaves a1's alert alone.
        hook(root, {"hook_event_name": "PreToolUse", "session_id": "s1", "agent_id": "a2", "tool_name": "Read"})
        assert os.path.exists(alert("s1", "a1")), "a PreToolUse of a2 cleared a1's permission alert"
        hook(root, {"hook_event_name": "PreToolUse", "session_id": "s1", "agent_id": "a1", "tool_name": "Bash"})
        assert not os.path.exists(alert("s1", "a1")), "the agent's next tool call must clear its alert"
        code, out, _, _ = hook(root, {"hook_event_name": "Notification", "session_id": "s1", "agent_id": "a3",
                                      "notification_type": "permission_prompt", "message": "needs permission"})
        assert out == "" and load("s1", "a3")["kind"] == "permission", "the fallback must raise when nothing did"
        hook(root, {"hook_event_name": "SubagentStop", "session_id": "s1", "agent_id": "a3", "agent_type": "Explore"})
        assert not os.path.exists(alert("s1", "a3")), "SubagentStop must clear the agent's alert"
        for kind in ("idle_prompt", "elicitation_dialog"):
            hook(root, {"hook_event_name": "Notification", "session_id": "s9", "notification_type": kind, "message": "m"})
            assert not os.path.exists(os.path.join(att, "s9")), f"Notification {kind} must raise nothing"
        # C2: without agent_id the prompt may be a running subagent's; nothing would clear a
        # main alert until the next prompt. Once the subagent stopped, the fallback raises again.
        nmain = {"hook_event_name": "Notification", "session_id": "s8", "notification_type": "permission_prompt",
                 "message": "Claude needs your permission"}
        hook(root, {"hook_event_name": "SubagentStart", "session_id": "s8", "agent_id": "a5", "agent_type": "Explore"})
        hook(root, nmain)
        assert not os.path.exists(alert("s8")), "a subagent runs: the fallback must not raise on main"
        hook(root, {"hook_event_name": "UserPromptSubmit", "session_id": "s8"})
        hook(root, nmain)
        assert not os.path.exists(alert("s8")), "a prompt does not stop a background subagent: the marker stays"
        hook(root, {"hook_event_name": "SubagentStop", "session_id": "s8", "agent_id": "a5", "agent_type": "Explore"})
        hook(root, nmain)
        assert load("s8")["kind"] == "permission", "no subagent runs: the fallback raises on main"
        hook(root, {"hook_event_name": "PermissionRequest", "session_id": "s1", "tool_name": "AskUserQuestion"}, "permission", script=ATTN)
        assert not os.path.exists(alert("s1")), "a permission alert must not stand in for a question"

        hook(root, {"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": "done, token=abc123"})
        t = load("s1")
        assert t["kind"] == "turn" and "abc123" not in t["detail"], t
        long = "word " * 400 + "THE END"
        hook(root, {"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": long})
        assert load("s1")["detail"].endswith("THE END"), "a turn keeps the whole message: the chat shows it"
        hook(root, {**perm, "agent_id": "a1"})
        hook(root, {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "go"})
        # Every alert goes; what stays is the session marker, which says a session lives there.
        assert os.listdir(os.path.join(att, "s1")) == [".session"], "UserPromptSubmit must clear the whole session"
        marker = json.load(open(os.path.join(att, "s1", ".session")))
        assert isinstance(marker["since"], float) and "claude_pid" in marker, marker
        # A session just opened waits in its terminal: the page cannot reach it until its first Stop,
        # and the chat says so instead of queueing a message that never goes.
        dash = load_dashboard("dash_fresh")
        wdir = os.path.join(root, ".work")
        assert marker["fresh"] is False, "a prompted session is not fresh"
        others = [x for x in os.listdir(att) if x != "s1" and os.path.isdir(os.path.join(att, x))]
        saved = {x: json.load(open(os.path.join(att, x, ".session"))) for x in others
                 if os.path.exists(os.path.join(att, x, ".session"))}
        for x in saved:  # another live session that took a prompt is not fresh: it hides s1's state
            os.remove(os.path.join(att, x, ".session"))
        def alive_marker():  # the hook's parent is gone by now: this process stands in for Claude
            mp = os.path.join(att, "s1", ".session")
            m = json.load(open(mp)); m["claude_pid"] = os.getpid(); json.dump(m, open(mp, "w"))
        hook(root, {"hook_event_name": "SessionStart", "session_id": "s1", "source": "startup"})
        alive_marker()
        assert dash.fresh_session(wdir, time.time()), "an opened session that took no prompt is fresh"
        hook(root, {"hook_event_name": "SessionStart", "session_id": "s1", "source": "compact"})
        alive_marker()
        assert not dash.fresh_session(wdir, time.time()), "a compaction restarts mid-turn: not fresh"
        hook(root, {"hook_event_name": "SessionStart", "session_id": "s1", "source": "startup"})
        hook(root, {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "go"})
        alive_marker()
        assert not dash.fresh_session(wdir, time.time()), "a prompt ends fresh"
        for x, m in saved.items():
            json.dump(m, open(os.path.join(att, x, ".session"), "w"))
        for ev in ("SessionEnd", "SessionStart"):
            hook(root, {"hook_event_name": "Stop", "session_id": "s2"})
            hook(root, {**perm, "session_id": "s2"})
            hook(root, {"hook_event_name": ev, "session_id": "s2"})
            left = os.listdir(os.path.join(att, "s2")) if os.path.exists(os.path.join(att, "s2")) else []
            assert left == ([".session"] if ev == "SessionStart" else []), f"{ev} must clear the session: {left}"
        def plant():
            for s_, a_ in (("s1", "a1"), ("s1", "a2"), ("s1", "main"), ("s2", "a1"), ("s2", "main")):
                os.makedirs(os.path.join(att, s_), exist_ok=True)
                json.dump({"kind": "permission", "since": time.time(), "detail": ""}, open(alert(s_, a_), "w"))
        agent_events = [{"hook_event_name": "SubagentStop", "agent_type": "Explore"},
                        {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion"},
                        {"hook_event_name": "PreToolUse", "tool_name": "Read"},
                        {"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "ls"}}]
        for ev in agent_events:
            plant()
            hook(root, {**ev, "session_id": "s1", "agent_id": "a1"})
            kept = [p_ for p_ in (("s1", "a2"), ("s1", "main"), ("s2", "a1"), ("s2", "main")) if os.path.exists(alert(*p_))]
            assert len(kept) == 4, f"{ev['hook_event_name']} of s1/a1 touched another alert: kept {kept}"
        for name in ("UserPromptSubmit", "SessionStart", "SessionEnd"):
            plant()
            hook(root, {"hook_event_name": name, "session_id": "s1"})
            assert not any(os.path.exists(alert("s1", a_)) for a_ in ("a1", "a2", "main")), f"{name} must clear s1"
            assert os.path.exists(alert("s2", "a1")) and os.path.exists(alert("s2", "main")), f"{name} of s1 cleared s2"
        shutil.rmtree(att)
        assert not os.path.exists(os.path.join(root, ".work", "events.jsonl")), "alerts must not log without a work"

        # 2. Off is today's behavior: answers off, or on with no page or a stale page.
        # The flag goes through work.sh, so the file it writes is the one the hold reads.
        def answers(*arg):
            p = subprocess.run(["bash", os.path.join(KIT, "scripts", "work.sh"), "ui", "answers", *arg],
                               capture_output=True, text=True, env=dict(os.environ, CLAUDE_PROJECT_DIR=root))
            assert p.returncode == 0, p.stderr
            return p.stdout.strip()
        assert answers() == "off", "web answers must default to off"
        answers("on")
        assert answers() == "on" and os.path.exists(os.path.join(att, "answers-on")), "work.sh ui answers on"
        page = os.path.join(att, ".page")
        # A hold that wrongly started lasts at least one 8 s poll; a correct return is a Python
        # start-up, under 4 s even on a loaded runner.
        slow = {"KIT_ATTENTION_PAGE_GONE": "600", "KIT_ATTENTION_HOLD": "10", "KIT_ATTENTION_POLL": "8"}
        for label in ("no page", "stale page"):
            if label == "stale page":
                open(page, "w").close()
                os.utime(page, (time.time() - 60, time.time() - 60))
            code, out, _, secs = hook(root, ask(), "hold", script=ATTN, env=slow)
            assert code == 0 and out == "" and secs < 4, f"{label}: the hold must return at once ({secs:.2f}s)"
            assert load("s1")["held"] is False, label
        answers("off")
        assert answers() == "off", "work.sh ui answers off"
        open(page, "w").close()
        code, out, _, secs = hook(root, ask(), "hold", script=ATTN, env=slow)
        assert out == "" and secs < 4 and load("s1")["held"] is False, "answers off must not hold"
        # Q3: a question list that is empty, or that the page could not show whole, is not held.
        answers("on")
        open(page, "w").close()
        for label, qs in (("no questions", []), ("a question that is not an object", [QUESTIONS[0], "x"])):
            code, out, _, secs = hook(root, {**ask(), "tool_input": {"questions": qs}}, "hold", script=ATTN, env=slow)
            assert code == 0 and out == "" and secs < 4 and load("s1")["held"] is False, f"{label}: must not hold"
        # S3: the hold answers only AskUserQuestion, whatever the matcher in settings says.
        os.remove(alert("s1"))
        code, out, _, secs = hook(root, {**ask(), "tool_name": "mcp__x__ask"}, "hold", script=ATTN, env=slow)
        assert out == "" and secs < 4 and not os.path.exists(alert("s1")), "the hold must ignore any other tool"
        answers("off")

        # 3. Hold and answer, release, page gone, timeout, session died.
        answers("on")
        def held(env=None, payload=None):
            open(page, "w").close()
            e = dict(os.environ, CLAUDE_PROJECT_DIR=root, **(env or {}))
            proc = subprocess.Popen([sys.executable, ATTN, "hold"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, env=e)
            proc.stdin.write(json.dumps(payload or ask()))
            proc.stdin.close()
            for _ in range(200):
                a = json.load(open(alert("s1"))) if os.path.exists(alert("s1")) else {}
                if a.get("held"):
                    return proc, a
                time.sleep(0.05)
            raise AssertionError("the hold did not raise a held question")
        # 15 s: a hold that ignores its exit condition runs to the page timeout (30 s) or the
        # hold (570 s); a correct one ends within one 0.5 s poll plus a start-up.
        def finish(proc, limit=15):
            # Bounded: a hold that ignores its exit condition would otherwise block the run for 570 s.
            try:
                proc.wait(timeout=limit)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise AssertionError(f"the hold did not end within {limit}s: it ignored its exit condition")
            return proc.returncode, proc.stdout.read()
        def put(name, obj):
            tmp = os.path.join(att, "s1", ".tmp-test")
            open(tmp, "w").write(json.dumps(obj))
            os.replace(tmp, os.path.join(att, "s1", name))

        proc, a = held()
        put("main.answer.json", {"qid": a["qid"], "via": "web",
                                 "answers": [{"options": [0]}, {"options": [2, 0], "other": "kms"}]})
        code, out = finish(proc)
        got = json.loads(out)
        assert got == {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                       "updatedInput": {"questions": QUESTIONS, "answers": {
                           QUESTIONS[0]["question"]: "Light Spec (Recommended)",
                           QUESTIONS[1]["question"]: "unit, semana, kms"}}}}, got
        assert not os.listdir(os.path.join(att, "s1")), "the alert and the answer must be gone after an answer"

        proc, a = held()
        put("main.answer.json", {"qid": "stale", "via": "web", "answers": [{"options": [0]}, {"options": [0]}]})
        t0 = time.monotonic()
        code, out = finish(proc)
        # Timed: a hold that kept going would still end empty, 30 s later, when the page goes stale.
        assert time.monotonic() - t0 < 10, "an answer for another qid must release at once, not at the page timeout"
        assert code == 0 and out == "" and load("s1")["held"] is False, "an answer for another qid must go to the terminal"

        # S1: an answer the server did not write (no tag) is not the dev's: back to the terminal.
        # Q3: so is one with fewer answers than questions.
        for label, bad in (("untagged", {"answers": [{"options": [0]}, {"options": [0]}]}),
                           ("one answer for two questions", {"via": "web", "answers": [{"options": [0]}]})):
            proc, a = held()
            put("main.answer.json", {"qid": a["qid"], **bad})
            code, out = finish(proc)
            assert out == "" and load("s1")["held"] is False, f"{label}: the hold must not take it"

        proc, a = held()
        open(os.path.join(att, "s1", "main.release"), "w").close()
        t0 = time.monotonic()
        code, out = finish(proc)
        # "Answer in the terminal" releases at once; while the page is open it never goes stale.
        assert time.monotonic() - t0 < 10, "release must hand the question back at once, not at the page timeout"
        assert code == 0 and out == "" and load("s1")["held"] is False, "release must hand the question to the terminal"
        assert not os.path.exists(os.path.join(att, "s1", "main.release")), "the release file must be consumed"

        # A hold longer than the dashboard's 15 s staleness needs a fresh heartbeat, or the page
        # stops offering the answer while the hook is still waiting for it.
        proc, a = held()
        for _ in range(14):
            time.sleep(0.25)
            os.utime(page)
        beat = load("s1").get("heartbeat", 0)
        open(os.path.join(att, "s1", "main.release"), "w").close()
        finish(proc)
        assert beat > a["heartbeat"] + 1, "the holder must refresh its heartbeat while it holds"

        # Turning answers off while a question is held hands it back to the terminal at once.
        proc, a = held()
        answers("off")
        t0 = time.monotonic()
        code, out = finish(proc)
        assert time.monotonic() - t0 < 10 and out == "" and load("s1")["held"] is False, \
            "answers off during a hold must release the question"
        answers("on")

        proc, a = held(env={"KIT_ATTENTION_PAGE_GONE": "1"})
        t0 = time.monotonic()
        code, out = finish(proc)
        assert out == "" and time.monotonic() - t0 < 10 and load("s1")["held"] is False, "a closed page must release"

        proc, a = held(env={"KIT_ATTENTION_HOLD": "1"})
        started = time.monotonic()
        while proc.poll() is None and time.monotonic() - started < 4:
            os.utime(page)
            time.sleep(0.2)
        code, out = finish(proc)
        assert out == "" and load("s1")["held"] is False, "the hold must time out to the terminal"

        sleeper = subprocess.Popen(["sleep", "30"])
        proc, a = held(env={"KIT_ATTENTION_PID": str(sleeper.pid)})
        assert a["claude_pid"] == sleeper.pid, a
        sleeper.kill()
        sleeper.wait()
        code, out = finish(proc)
        assert out == "" and not os.path.exists(alert("s1")), "a holder whose session died must drop its alert"

        spec = __import__("importlib.util").util.spec_from_file_location("attention_defaults", ATTN)
        env_saved = {k: os.environ.pop(k, None) for k in ("KIT_ATTENTION_HOLD", "KIT_ATTENTION_PAGE_GONE", "KIT_ATTENTION_POLL")}
        sys.path.insert(0, os.path.dirname(ATTN))
        m = __import__("importlib.util").util.module_from_spec(spec)
        spec.loader.exec_module(m)
        sys.path.remove(os.path.dirname(ATTN))
        os.environ.update({k: v for k, v in env_saved.items() if v is not None})
        assert (m.HOLD_SECONDS, m.PAGE_GONE, m.PAGE_FRESH, m.POLL) == (3570, 30, 10, 0.5), "the hold defaults changed"

        # 7. Never breaks a session: no stdout, exit 0, on every broken input.
        cases = [("invalid JSON", {}, "{bad", HOOK), ("invalid JSON hold", {}, "{bad", ATTN),
                 ("array", {}, "[]", HOOK), ("array hold", {}, "[]", ATTN),
                 ("slash session", {**perm, "session_id": "a/b"}, None, HOOK),
                 ("dotdot session", {**perm, "session_id": ".."}, None, HOOK),
                 ("slash session hold", {**ask(), "session_id": "../x"}, None, ATTN)]
        for label, payload, raw, script in cases:
            code, out, _, _ = hook(root, payload, *(["hold"] if script == ATTN else []), raw=raw, script=script)
            assert code == 0 and out == "", f"{label}: exit {code}, stdout {out!r}"
        assert not os.path.exists(os.path.join(root, ".work", "x")), "a session id escaped the attention dir"
        # A regular file where the directory should be: unwritable for root too (CI runs as root,
        # where chmod 0500 changes nothing).
        shutil.rmtree(att)
        open(att, "w").close()
        try:
            for payload in (perm, {"hook_event_name": "Stop", "session_id": "s1"},
                            {"hook_event_name": "Notification", "session_id": "s1", "message": "m"},
                            {"hook_event_name": "UserPromptSubmit", "session_id": "s1"}):
                code, out, _, _ = hook(root, payload)
                assert code == 0 and out == "", f"unwritable dir: {payload['hook_event_name']} exit {code} {out!r}"
            for payload in ({"hook_event_name": "SubagentStart", "session_id": "s1", "agent_id": "a1"},
                            {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Read"},
                            {"hook_event_name": "SessionStart", "session_id": "s1"}):
                code, out, _, _ = hook(root, payload)
                assert code == 0 and out == "", f"unwritable dir: {payload['hook_event_name']} exit {code} {out!r}"
            code, out, _, _ = hook(root, ask(), "hold", script=ATTN)
            assert code == 0 and out == "", "unwritable dir: the hold must fall back to the terminal"
            assert os.path.isfile(att), "the hook replaced the blocking file"
        finally:
            os.remove(att)
        os.remove(os.path.join(root, ".claude", "flow.json"))
        for payload, script in ((perm, HOOK), (ask(), ATTN)):
            code, out, _, _ = hook(root, payload, *(["hold"] if script == ATTN else []), script=script)
            assert code == 0 and out == "", "a missing flow.json must not break a session"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def attention_server():
    """GET /api/attention lists live alerts of every worktree, task or not, and POST
    /api/answer writes only an answer to a held question, with the token, a local Origin and
    Host and a JSON body."""
    import importlib.util
    import http.client
    import threading
    spec = importlib.util.spec_from_file_location("dashboard_attn", os.path.join(KIT, "scripts", "dashboard.py"))
    d = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(d)
    base = tempfile.mkdtemp()
    try:
        main_wt, other_wt = os.path.join(base, "repo"), os.path.join(base, "repo-two")
        git = lambda *a: subprocess.run(["git", "-C", main_wt, *a], check=True, capture_output=True)
        os.makedirs(os.path.join(main_wt, ".claude"))
        shutil.copy(FLOW, os.path.join(main_wt, ".claude", "flow.json"))
        git("init", "-q", "-b", "develop")
        git("add", ".")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        git("worktree", "add", "-q", "-b", "feat/two", other_wt)
        now = time.time()
        dead = subprocess.Popen(["true"])
        dead.wait()

        def put(wt, session, agent, alert):
            p = os.path.join(wt, ".work", "attention", session)
            os.makedirs(p, exist_ok=True)
            open(os.path.join(p, agent + ".json"), "w").write(json.dumps(alert))
        q = lambda **k: {"kind": "question", "since": now, "detail": "Which track?", "claude_pid": None, "qid": "QID",
                         "questions": [{"question": "Which track?", "multiSelect": False,
                                        "options": [{"label": "A"}, {"label": "B"}]},
                                       {"question": "Which?", "multiSelect": True, "options": [{"label": "x"}]}],
                         "held": True, "heartbeat": now, **k}
        # Only the second worktree has alerts, and it has no task directory at all.
        put(other_wt, "s1", "main", q())
        put(other_wt, "s1", "a1", {"kind": "permission", "since": now, "detail": "Bash: ls", "claude_pid": os.getpid(), "tool": "Bash"})
        put(other_wt, "s2", "main", {"kind": "turn", "since": now, "detail": "", "claude_pid": dead.pid})
        put(other_wt, "s3", "main", {"kind": "turn", "since": now - 25 * 3600, "detail": "", "claude_pid": None})
        put(other_wt, "s4", "main", {"kind": "turn", "since": now - 23 * 3600, "detail": "", "claude_pid": None})
        # A held question whose Claude Code died: hidden, and an answer to it is refused.
        put(other_wt, "s5", "main", q(claude_pid=dead.pid))

        assert not d.list_tasks(main_wt, now), "precondition: no worktree has a task"
        groups = d.list_attention(main_wt, now)
        assert [g["wt"] for g in groups] == [1] and groups[0]["name"] == "repo-two", groups
        got = {(a["session"], a["agent"]): a for a in groups[0]["alerts"]}
        assert set(got) == {("s1", "main"), ("s1", "a1"), ("s4", "main")}, \
            f"dead pid and 24 h without pid must be hidden, the rest shown: {sorted(got)}"
        assert got[("s1", "main")]["held"] and got[("s1", "a1")]["kind"] == "permission", got
        assert all(k in got[("s1", "a1")] for k in ("session", "agent", "kind", "since", "detail")), got
        assert os.path.exists(os.path.join(other_wt, ".work", "attention", ".page")), "a poll must mark the page alive"
        put(other_wt, "s1", "main", q(heartbeat=now - 60))
        held = {a["agent"]: a for a in d.list_attention(main_wt, now)[0]["alerts"] if a["session"] == "s1"}
        assert held["main"]["held"] is False, "a holder without a heartbeat is not holding"
        put(other_wt, "s1", "main", q())

        token = "tok-" + "x" * 20
        handler = type("H", (d.Handler,), {"root": main_wt, "token": token})
        server = d.http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_port
        sdir = os.path.join(other_wt, ".work", "attention", "s1")
        try:
            def call(method, path, body=None, headers=None, raw=None):
                h = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}",
                     "Content-Type": "application/json", "X-Kit-Token": token}
                for k, v in (headers or {}).items():
                    if v is None:
                        h.pop(k, None)
                    else:
                        h[k] = v
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
                c.putrequest(method, path, skip_host=True)
                for k, v in h.items():
                    c.putheader(k, v)
                if data is not None:
                    c.putheader("Content-Length", str(len(data)))
                c.endheaders(data)
                r = c.getresponse()
                return r.status, r.read()

            code, page = call("GET", "/")
            assert code == 200 and token.encode() in page and b"__KIT_TOKEN__" not in page, "the page must carry the token"
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", "/", headers={"Host": f"127.0.0.1:{port}"})
            r = c.getresponse()
            r.read()
            assert r.getheader("X-Frame-Options") == "DENY" and \
                r.getheader("Content-Security-Policy") == "frame-ancestors 'none'", "the page must refuse to be framed"
            # The page's own strings and wiring are checked in test_page.py (wiring()).
            code, body = call("GET", "/api/attention")
            assert code == 200 and token.encode() not in body, "the token must not be in /api/attention"
            assert json.loads(body)[0]["alerts"], body
            for path in ("/api/tasks", "/api/task?wt=0&id=X"):
                assert call("POST", path, {})[0] in (404, 405), f"POST {path} must not be served"

            good = {"wt": 1, "session": "s1", "agent": "main", "qid": "QID",
                    "answers": [{"options": [1]}, {"options": [0], "other": "y"}]}
            # (label, body, headers, raw body, the exact status): each refusal for its own reason.
            refused = [
                ("wrong token", good, {"X-Kit-Token": "nope"}, None, 403),
                ("no token", good, {"X-Kit-Token": None}, None, 403),
                ("foreign Origin", good, {"Origin": "http://evil.example"}, None, 403),
                ("Origin on another port", good, {"Origin": "http://127.0.0.1:1"}, None, 403),
                ("no Origin", good, {"Origin": None}, None, 403),
                ("foreign Host", good, {"Host": "evil.example"}, None, 403),
                ("form content type", good, {"Content-Type": "application/x-www-form-urlencoded"}, None, 415),
                # Valid in every other way, so only the size cap can refuse it.
                ("body over 64 KB", None, None, json.dumps({**good, "pad": "a" * 70_000}).encode(), 413),
                ("not JSON", None, None, b"{bad", 400),
                ("stale qid", {**good, "qid": "OLD"}, None, None, 409),
                ("permission alert", {**good, "agent": "a1"}, None, None, 409),
                ("index out of range", {**good, "answers": [{"options": [2]}, {"options": [0]}]}, None, None, 400),
                ("two picks on a single select", {**good, "answers": [{"options": [0, 1]}, {"options": [0]}]}, None, None, 400),
                ("unanswered question", {**good, "answers": [{"options": [0]}, {"options": []}]}, None, None, 400),
                ("fewer answers than questions", {**good, "answers": [{"options": [0]}]}, None, None, 400),
                ("more answers than questions", {**good, "answers": [{"options": [0]}, {"options": [0]}, {"options": [0]}]}, None, None, 400),
                ("session with ../", {**good, "session": "../s1"}, None, None, 400),
                # A held question planted outside the attention dir: only the id rule refuses it.
                ("session escaping to a planted question", {**good, "session": "..", "agent": "planted/main"}, None, None, 400),
                ("session ../planted", {**good, "session": "../planted"}, None, None, 400),
                ("worktree out of range", {**good, "wt": 9}, None, None, 404),
                ("dead session", {**good, "session": "s5"}, None, None, 409),
            ]
            adir = os.path.dirname(sdir)
            # Every file under the attention dir but the page heartbeat: a refusal writes nowhere.
            tree = lambda: sorted(os.path.relpath(os.path.join(dp, f), adir) for dp, _, fs in os.walk(adir)
                                  for f in fs if f != ".page")
            before_all = tree()
            planted = os.path.join(other_wt, ".work", "planted")
            os.makedirs(planted)
            open(os.path.join(planted, "main.json"), "w").write(json.dumps(q()))
            before = sorted(os.listdir(sdir))
            for label, body, headers, raw, want in refused:
                code, _ = call("POST", "/api/answer", body, headers, raw)
                assert code == want, f"{label}: got {code}, want {want}"
                assert sorted(os.listdir(sdir)) == before and tree() == before_all \
                    and os.listdir(planted) == ["main.json"], f"{label}: a file was written"
            # A valid answer body, token and Origin, sent to another path: only /api/answer writes.
            for path in ("/api/tasks", "/api/task?wt=0&id=X", "/api/attention", "/"):
                code, _ = call("POST", path, good)
                assert code == 404 and tree() == before_all, f"POST {path}: {code}"
            for method in ("GET", "PUT", "DELETE"):
                code, _ = call(method, "/api/answer", good)
                assert code == 405 and sorted(os.listdir(sdir)) == before, f"{method} /api/answer: {code}"
            put(other_wt, "s1", "main", q(held=False))
            assert call("POST", "/api/answer", good)[0] == 409, "a question not held must refuse an answer"
            assert sorted(os.listdir(sdir)) == before
            put(other_wt, "s1", "main", q())

            assert call("POST", "/api/answer", good)[0] == 200
            written = json.load(open(os.path.join(sdir, "main.answer.json")))
            assert written["qid"] == "QID" and written["via"] == "web", "the server must tag what it writes"
            os.remove(os.path.join(sdir, "main.answer.json"))
            assert call("POST", "/api/answer", {k: v for k, v in good.items() if k != "answers"} | {"release": True})[0] == 200
            assert os.path.exists(os.path.join(sdir, "main.release"))
            os.remove(os.path.join(sdir, "main.release"))

            # End to end: the real hold, opted in, answered through the endpoint.
            shutil.rmtree(sdir)
            open(os.path.join(other_wt, ".work", "attention", "answers-on"), "w").close()
            call("GET", "/api/attention")
            e = dict(os.environ, CLAUDE_PROJECT_DIR=other_wt)
            proc = subprocess.Popen([sys.executable, ATTN, "hold"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, env=e)
            proc.stdin.write(json.dumps(ask()))
            proc.stdin.close()
            alert = None
            for _ in range(200):
                alerts = json.loads(call("GET", "/api/attention")[1])
                alert = next((a for g in alerts for a in g["alerts"] if a.get("held")), None)
                if alert:
                    break
                time.sleep(0.05)
            assert alert, "the held question never reached /api/attention"
            code, _ = call("POST", "/api/answer", {"wt": 1, "session": alert["session"], "agent": alert["agent"],
                                                   "qid": alert["qid"], "answers": [{"options": [1]}, {"options": [1]}]})
            assert code == 200
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                raise AssertionError("the hold did not take the answer the endpoint wrote")
            out = proc.stdout.read()
            answers = json.loads(out)["hookSpecificOutput"]["updatedInput"]["answers"]
            assert answers == {QUESTIONS[0]["question"]: "No Spec", QUESTIONS[1]["question"]: "integration"}, answers
        finally:
            server.shutdown()
    finally:
        shutil.rmtree(base)


def wiring():
    """install.sh wires per (event, matcher), adds the new entries next to an old install's,
    is idempotent, and the repo's settings.json is exactly what it produces."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("wire_hooks", WIRE)
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    flow_cmd = {"type": "command", "command": w.FLOW, "timeout": 10}
    old_hold = {"type": "command", "command": w.HOLD, "timeout": 600}
    guard = {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 guard.py", "timeout": 600}]}
    gone = {"type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/kit/scripts/cleanup.py\" --hook"}
    old = {"hooks": {"SessionStart": [{"hooks": [gone]}], "PreToolUse": [guard, {"matcher": "*", "hooks": [flow_cmd]},
                                                                      {"matcher": "AskUserQuestion", "hooks": [old_hold]}],
                     "SubagentStart": [{"hooks": [flow_cmd]}], "SubagentStop": [{"hooks": [flow_cmd]}],
                     # An older kit wired the turn with 600 s: the upgrade must raise it, not add a second one.
                     "Stop": [{"hooks": [{"type": "command", "command": w.TURN, "timeout": 600}]}]}}
    d = tempfile.mkdtemp()
    try:
        path = os.path.join(d, "settings.json")
        open(path, "w").write(json.dumps(old))
        subprocess.run([sys.executable, WIRE, path], check=True)
        once = open(path).read()
        s = json.loads(once)["hooks"]
        def entry(event, matcher):
            return [h for g in s.get(event, []) if g.get("matcher") == matcher for h in g["hooks"]]
        assert s["PreToolUse"][0] == guard, "an existing hook must be kept as it was"
        assert len(entry("PreToolUse", "*")) == 1, "the old '*' entry must not be duplicated"
        hold = entry("PreToolUse", "AskUserQuestion")
        assert len(hold) == 1 and hold[0]["timeout"] == 3600 and hold[0]["command"].endswith("attention.py\" hold"), \
            f"an older install's 600 s hold is raised to the hour the hold now waits: {hold}"
        for event, matcher in (("PostToolUse", "AskUserQuestion"), ("Notification", "permission_prompt"),
                               ("UserPromptSubmit", None), ("SessionEnd", None)):
            assert len(entry(event, matcher)) == 1, f"{event} {matcher} was not wired"
        # Permission dialogs and the main session's turn are held for the page: their own entry,
        # with the 3600 s timeout the 3570 s hold stays under; the turn's 8 h lets chat reach an idle session.
        for event, arg, limit in (("PermissionRequest", "permission", 3600), ("Stop", "turn", 8 * 3600)):
            got = entry(event, None)
            held = [h for h in got if h["command"].endswith(f'attention.py\" {arg}')]
            assert len(got) == 2 and len(held) == 1 and held[0]["timeout"] == limit, f"{event}: {got}"
        starts = [h["command"] for h in entry("SessionStart", None)]
        assert len(starts) == 2 and any("dashboard.py\" hook" in c for c in starts), starts
        assert not any("cleanup.py" in h["command"] for g in s.values() for x in g for h in x["hooks"]), \
            "an entry of a script the kit no longer has must be removed"
        assert all(x["hooks"] for g in s.values() for x in g), "removing it must not leave an empty hook group"
        subprocess.run([sys.executable, WIRE, path], check=True)
        assert open(path).read() == once, "a second install changed settings.json"
    finally:
        shutil.rmtree(d)


def hooks_via_main():
    """From a linked worktree the hooks run the main checkout's copy of a script (a worktree on an
    older branch ran old hooks); outside git, or when the main checkout lacks it, the project's own
    copy runs; an old-form entry is upgraded in place, never duplicated."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("wire_hooks_vm", WIRE)
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    base = os.path.realpath(tempfile.mkdtemp())
    try:
        main_repo, wt, plain = os.path.join(base, "main"), os.path.join(base, "wt"), os.path.join(base, "plain")
        for d in (main_repo, plain):
            os.makedirs(os.path.join(d, ".claude", "kit", "hooks"))
        open(os.path.join(main_repo, ".claude", "kit", "hooks", "flow.py"), "w").write("import os\nprint('MAIN', os.environ['CLAUDE_PROJECT_DIR'])\n")
        open(os.path.join(plain, ".claude", "kit", "hooks", "flow.py"), "w").write("print('OWN')\n")
        g = lambda *a: subprocess.run(["git", "-C", main_repo, "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True)
        g("init", "-q"); g("add", "-A"); g("commit", "-qm", "init"); g("worktree", "add", "-q", wt)
        open(os.path.join(wt, ".claude", "kit", "hooks", "flow.py"), "w").write("print('WORKTREE')\n")
        run = lambda proj: subprocess.run(["bash", "-c", w.FLOW], env={**os.environ, "CLAUDE_PROJECT_DIR": proj},
                                          capture_output=True, text=True).stdout.strip()
        assert run(wt) == f"MAIN {wt}", f"a linked worktree runs the main checkout's copy, on its own project: {run(wt)!r}"
        assert run(plain) == "OWN", "outside git, the project's own copy runs"
        os.remove(os.path.join(main_repo, ".claude", "kit", "hooks", "flow.py"))
        assert run(wt) == "WORKTREE", "the main checkout without the script: the worktree's own copy runs"
        # An old-form entry (a plain path) is replaced by the new command, not duplicated.
        path = os.path.join(base, "settings.json")
        oldform = "python3 \"$CLAUDE_PROJECT_DIR/.claude/kit/hooks/attention.py\" hold"
        open(path, "w").write(json.dumps({"hooks": {"PreToolUse": [{"matcher": "AskUserQuestion", "hooks": [
            {"type": "command", "command": oldform, "timeout": 600}]}]}}))
        subprocess.run([sys.executable, WIRE, path], check=True)
        got = [h["command"] for x in json.load(open(path))["hooks"]["PreToolUse"] if x.get("matcher") == "AskUserQuestion" for h in x["hooks"]]
        assert got == [w.HOLD], f"the old-form hold is upgraded in place: {got}"
    finally:
        shutil.rmtree(base)


def load_dashboard(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, os.path.join(KIT, "scripts", "dashboard.py"))
    d = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(d)
    return d


REVIEWS = ("04-review-contract.md", "04-review-security.md", "04-review-qa.md", "04-review-product.md")


def fix_round_status():
    """A fix round never reads ready-for-mr: the page said ready-for-mr while the executor of the
    fix round ran, and the dev nearly opened the PR. Ready only once every fix is marked fixed,
    nothing of the flow runs and the lenses wrote after the round's agents started. Only an agent
    whose type is a stage of flow.json holds the task."""
    d = load_dashboard("dash_fix_round")
    base = tempfile.mkdtemp()
    try:
        tdir = os.path.join(base, "T-1")
        os.makedirs(tdir)
        flow = json.load(open(FLOW))
        s0 = 1_700_000_000

        def put(name, text, t):
            open(os.path.join(tdir, name), "w").write(text)
            os.utime(os.path.join(tdir, name), (t, t))
        for i, f in enumerate(("01-contract.md", "02-change.md", "03-proof.md")):
            put(f, "x\n", s0 + i)
        for f in REVIEWS:
            put(f, "Verdict: PASS_WITH_FINDINGS\n\n## Findings\n- [high] the hold drops the answer\n", s0 + 10)
        keys = [x["key"] for x in d.decided_findings(tdir, d.stages(flow, tdir))]
        lens = lambda f: f[len("04-review-"):-3]
        put("findings-decision.md", "Decided: 2026-11-17\n" + "".join(
            f"- fix high {lens(f)} [{k}]: the hold drops the answer\n" for f, k in zip(REVIEWS, keys)), s0 + 20)
        executor = lambda state, start: {"agent": "e2", "type": "executor", "state": state, "start": start, "ran": True,
                                         "seen": start + 5, "tools": 3}
        state = lambda lanes: d.task_state(d.stages(flow, tdir), lanes, tdir, None)
        got = state([executor("running", s0 + 30)])
        assert got["status"] == "fixing" and got["next_step"] == "wait-fixes", \
            f"a decided fix with a running executor is a fix round, not ready for a PR/MR: {got}"
        assert state([])["status"] == "fixing", "a finding still fix-pending is a fix round even with no agent seen"
        # The executor marks the fixes; the lenses have not re-run since it started.
        text = open(os.path.join(tdir, "findings-decision.md")).read().replace("- fix high", "- fixed high")
        put("findings-decision.md", text, s0 + 60)
        done = executor("ok", s0 + 30)
        got = state([done])
        assert got["status"] == "tester" and got["next_step"] == "reviews", \
            f"fixed and nothing runs, the lens files older than the round's executor: paused after the tester: {got}"
        put("02-change.md", "x\n", s0 + 70)
        put("03-proof.md", "x\n", s0 + 80)
        assert state([done])["status"] == "tester", "the proof is new, the lens files still older than the round"
        for f in REVIEWS:
            put(f, "Verdict: PASS\n", s0 + 90)
        got = state([done])
        assert got["status"] == "ready-for-mr" and got["next_step"] == "open-mr", f"all fixed and lenses re-run: {got}"
        assert state([done, {**executor("running", s0 + 100), "agent": "r3", "type": "reviewer"}])["status"] == "fixing", \
            "a flow agent running (a lens re-running alone) is never ready for a PR/MR"
        helper = {**executor("running", s0 + 100), "agent": "g1", "type": "general-purpose"}
        assert state([done, helper])["status"] == "ready-for-mr", "a subagent of no flow stage does not hold the task"

        # A FAIL during its fix round: the page said FAIL while the executor fixed.
        put("04-review-product.md", "Verdict: FAIL\n\n## Findings\n- [high] the export drops rows\n", s0 + 200)
        assert state([])["status"] == "FAIL", "a FAIL with its finding undecided waits on the dev"
        items = d.decided_findings(tdir, d.stages(flow, tdir))
        fail_key = next(f["key"] for f in items if f["lens"] == "product")
        put("findings-decision.md", open(os.path.join(tdir, "findings-decision.md")).read()
            + f"- fix high product [{fail_key}]: the export drops rows\n", s0 + 210)
        got = state([executor("running", s0 + 220)])
        assert got["status"] == "fixing" and got["next_step"] == "wait-fixes", f"a decided FAIL being fixed is the fix round: {got}"
        assert state([])["status"] == "fixing", "decided, its fix still pending: the fix round too"
        put("04-review-product.md", "Verdict: FAIL\n\n## Findings\n- [high] the export still drops rows\n", s0 + 300)
        assert state([executor("ok", s0 + 220)])["status"] == "FAIL", "a re-review that fails again shows FAIL"
    finally:
        shutil.rmtree(base)


def partial_rerun_status():
    """The fix round re-runs the contract reviewer and only the lenses that had findings. A lens
    whose last review had no finding (a PASS) and is older than the round needs no re-run: the
    task is ready for its PR/MR once the others re-ran. The contract reviewer is never settled."""
    d = load_dashboard("dash_partial_rerun")
    base = tempfile.mkdtemp()
    try:
        tdir = os.path.join(base, "T-1")
        os.makedirs(tdir)
        flow = json.load(open(FLOW))
        s0 = 1_700_000_000

        def put(name, text, t):
            open(os.path.join(tdir, name), "w").write(text)
            os.utime(os.path.join(tdir, name), (t, t))
        for i, f in enumerate(("01-contract.md", "02-change.md", "03-proof.md")):
            put(f, "x\n", s0 + i)
        finding = "Verdict: PASS_WITH_FINDINGS\n\n## Findings\n- [high] the hold drops the answer\n"
        put("04-review-contract.md", "Verdict: PASS\n\n## Findings\n- none\n", s0 + 10)
        put("04-review-security.md", "Verdict: PASS\n\n## Findings\n- none\n", s0 + 10)
        put("04-review-qa.md", "Verdict: PASS\n\n## Findings\n- none\n", s0 + 10)
        put("04-review-product.md", finding, s0 + 10)
        put("04-review-security.md", "Verdict: PASS\n\n## Not applicable\nNo input crosses a trust boundary.\n", s0 + 10)
        st = d.stages(flow, tdir)
        by = {x["file"]: x for x in st}
        assert by["04-review-security.md"]["na"] and not by["04-review-qa.md"]["na"], "a Not applicable section is said"
        put("04-review-product.md", "Verdict: FAIL\n\n## Not applicable\nx\n\n## Findings\n- [high] y\n", s0 + 10)
        fail = {x["file"]: x for x in d.stages(flow, tdir)}["04-review-product.md"]
        assert not fail["na"] and not fail["settled"], f"a FAIL is never not applicable nor settled: {fail}"
        put("04-review-product.md", finding, s0 + 10)
        assert by["04-review-qa.md"]["settled"] and by["04-review-security.md"]["settled"] \
            and not by["04-review-product.md"]["settled"] and not by["04-review-contract.md"]["settled"], \
            f"settled: the lenses with no finding, never the contract reviewer, PASS as it is: {st}"
        keys = {x["lens"]: x["key"] for x in d.decided_findings(tdir, st)}
        put("findings-decision.md", "Decided: 2026-11-18\n" + "".join(
            f"- fixed high {lens} [{k}]: the hold drops the answer\n" for lens, k in keys.items()), s0 + 60)
        done = {"agent": "e2", "type": "executor", "state": "ok", "start": s0 + 30, "ran": True, "seen": s0 + 35, "tools": 3}
        state = lambda: d.task_state(d.stages(flow, tdir), [done], tdir, None)
        put("02-change.md", "x\n", s0 + 70)
        put("03-proof.md", "x\n", s0 + 80)
        assert state()["status"] == "tester", "nothing re-ran yet: paused before the reviews"
        put("04-review-product.md", "Verdict: PASS\n", s0 + 90)
        assert state()["status"] == "tester", "the contract reviewer always re-runs: still paused before it"
        put("04-review-contract.md", "Verdict: PASS\n", s0 + 95)
        got = state()
        assert got["status"] == "ready-for-mr", \
            f"the lenses with findings re-ran; qa and security, clean and older than the round, need no re-run: {got}"
        # Control: the same old qa file, had it carried a finding, would hold the round.
        put("04-review-qa.md", finding, s0 + 10)
        assert state()["status"] != "ready-for-mr", "a lens with a finding older than the round is not settled"
    finally:
        shutil.rmtree(base)


def reopened_findings():
    """A lens that re-runs and still lists a finding decided fix or marked fixed, in the same words
    (so the same key): the problem is still there, the finding opens again and the task asks the
    dev. An accepted one carries over. Nothing undecided asks nothing: "some-open" agrees with the
    open count, never with 0 open."""
    d = load_dashboard("dash_reopened")
    base = tempfile.mkdtemp()
    try:
        flow = json.load(open(FLOW))
        s0 = 1_700_000_000

        def make(name):
            t = os.path.join(base, name)
            os.makedirs(t)
            for i, f in enumerate(("01-contract.md", "02-change.md", "03-proof.md")):
                put(t, f, "x\n", s0 + i)
            return t

        def put(t, name, text, at):
            open(os.path.join(t, name), "w").write(text)
            os.utime(os.path.join(t, name), (at, at))

        def decide_all(t, word, at):
            items = d.decided_findings(t, d.stages(flow, t))
            put(t, "findings-decision.md", "Decided: 2026-11-17\n" + "".join(
                f"- {word} {f['severity']} {f['lens']} [{f['key']}]: {f['summary']}\n" for f in items), at)

        t = make("T-2")
        for f in REVIEWS:
            put(t, f, "Verdict: PASS_WITH_FINDINGS\n\n## Findings\n- [high] the hold drops the answer\n", s0 + 10)
        decide_all(t, "fixed", s0 + 60)
        for f in REVIEWS:
            os.utime(os.path.join(t, f), (s0 + 90, s0 + 90))  # re-ran after the round, same finding
        st = d.stages(flow, t)
        got = d.task_state(st, [], t, None)
        assert got["status"] == "findings", f"a re-review still listing a fixed finding opens it again: {got}"
        items = d.decided_findings(t, st)
        assert all(f["state"] == "open" and f.get("reopened") for f in items), items
        full = d.full_state(base, "feat/x", flow, "T-2", t, [], st, None, "", base)
        assert all(f["view"] == "needs-you" for f in full["findings"]), \
            f"a reopened finding is the dev's, not the fix loop's: {full['findings']}"
        decide_all(t, "accepted", s0 + 60)
        for f in REVIEWS:
            os.utime(os.path.join(t, f), (s0 + 90, s0 + 90))
        assert all(f["state"] == "accepted" for f in d.decided_findings(t, d.stages(flow, t))), "accepted stays accepted"

        # Two lenses re-ran clean after the decision, every finding fixed: nothing to decide.
        t = make("T-3")
        for f in REVIEWS:
            put(t, f, "Verdict: PASS\n", s0 + 10)
        put(t, "04-review-product.md", "Verdict: PASS_WITH_FINDINGS\n\n## Findings\n" +
            "".join(f"- [medium] product finding {n}\n" for n in range(4)), s0 + 10)
        decide_all(t, "fixed", s0 + 20)
        put(t, "04-review-contract.md", "Verdict: PASS\n", s0 + 30)
        put(t, "04-review-qa.md", "Verdict: PASS\n", s0 + 30)
        st = d.stages(flow, t)
        status = d.status(st, [], t)
        view = d.findings_view(t, st, status)
        assert status == "ready-for-mr" and view["findings_open"] == 0 and view["findings_reason"] is None, \
            f"nothing to decide after clean re-runs: {status}, {view['findings_reason']}"

        # A FAIL lens, every finding decided fix, the executor done and the tester next.
        t = make("T-4")
        for f in REVIEWS:
            put(t, f, "Verdict: PASS_WITH_FINDINGS\n\n## Findings\n- [high] one\n- [medium] two\n", s0 + 10)
        put(t, "04-review-security.md", "Verdict: FAIL\n\n## Findings\n- [high] the token leaks\n", s0 + 10)
        decide_all(t, "fix", s0 + 20)
        executor = {"agent": "e2", "type": "executor", "state": "ok", "start": s0 + 30, "end": s0 + 100, "ran": True,
                    "seen": s0 + 100, "tools": 9}
        st = d.stages(flow, t)
        state = d.task_state(st, [executor], t, None)
        view = d.findings_view(t, st, state["status"])
        assert state["status"] == "fixing", f"between executor and tester: the fix round: {state}"
        assert view["findings_open"] == 0 and view["findings_reason"] is None, f"no 'some-open' with 0 open: {view}"
        put(t, "04-review-qa.md", "Verdict: PASS_WITH_FINDINGS\n\n## Findings\n- [high] one\n- [low] brand new\n", s0 + 200)
        st = d.stages(flow, t)
        status = d.status(st, [executor], t)
        view = d.findings_view(t, st, status)
        # Two open: "brand new", and "one", which the re-run lens still lists after its fix.
        assert status == "FAIL" and view["findings_open"] == 2, f"a finding not decided waits on the dev: {status}, {view}"
    finally:
        shutil.rmtree(base)


def task_owns_branch():
    """Two tasks in one worktree on different branches (a split story's parts): part 1's PR/MR is
    found on its recorded branch and kept in mr.json; HEAD moves to part 2's branch: part 1 stays
    done, part 2 is not; a later failed lookup keeps part 1 done; init records the branch. Both
    forges: GitLab here, GitHub with the same rule."""
    d = load_dashboard("dash_owns")
    base = tempfile.mkdtemp()
    saved = {k: os.environ.get(k) for k in ("GITLAB_TOKEN", "FORGE_HOST", "GITHUB_TOKEN", "GH_TOKEN")}
    try:
        for forge, origin in (("gitlab", "https://gitlab.example/g/p.git"), ("github", "https://github.com/o/p.git")):
            for k in saved:
                os.environ.pop(k, None)
            os.environ["GITLAB_TOKEN"], os.environ["FORGE_HOST"] = "tok-x", "gitlab.example"
            d._mr_cache.clear()
            top = os.path.join(base, forge)
            os.makedirs(top)
            repo = new_repo(top, origin=origin)
            wt = os.path.join(top, "wt")
            git_in(repo, "worktree", "add", "-q", "-b", "feat/T-1-port", wt)
            pass_all = [(n, "Verdict: PASS\n") for n in REVIEWS]
            add_task(wt, "T-1", pass_all, current=True)
            open(os.path.join(wt, ".work", "T-1", "branch"), "w").write("feat/T-1-port\n")
            found = mr(7, "opened", "T-1 the port", "feat/T-1-port")
            if forge == "github":
                found = {"number": 7, "state": "open", "merged_at": None, "html_url": "https://github.com/o/p/pull/7",
                         "title": "T-1 the port", "created_at": "2026-11-17T00:00:00Z",
                         "head": {"ref": "feat/T-1-port", "repo": {"full_name": "o/p"}}, "base": {"repo": {"full_name": "o/p"}}}
            d.FETCH = FetchStub(by_branch={"feat/T-1-port": [found]})
            listed = lambda now: {k["id"]: k for g in d.list_tasks(repo, now) for k in g["tasks"] if os.path.basename(g["path"]) == "wt"}
            now = time.time()
            got = listed(now)
            assert got["T-1"]["status"] == "done" and json.load(open(os.path.join(wt, ".work", "T-1", "mr.json")))["iid"] == 7, (forge, got["T-1"])
            git_in(wt, "checkout", "-q", "-b", "feat/T-2-requester")
            p = subprocess.run(["bash", WORK_SH, "init", "T-2", "part 2"], capture_output=True, text=True,
                               env=dict(os.environ, CLAUDE_PROJECT_DIR=wt))
            assert p.returncode == 0, p.stderr
            assert open(os.path.join(wt, ".work", "T-2", "branch")).read().strip() == "feat/T-2-requester", "init records the task's branch"
            for n, text in pass_all:
                open(os.path.join(wt, ".work", "T-2", n), "w").write(text)
            d._mr_cache.clear()
            got = listed(now + 1)
            assert got["T-1"]["status"] == "done" and got["T-2"]["status"] != "done", \
                f"{forge}: part 1 stays done on its own branch, part 2 is not: {got['T-1']['status']}, {got['T-2']['status']}"
            os.remove(os.path.join(wt, ".work", "T-1", "mr.json"))
            d._mr_cache.clear()
            assert listed(now + 1.5)["T-1"]["status"] == "done", "the lookup uses the task's recorded branch, not HEAD"
            d._mr_cache.clear()
            d.FETCH = FetchStub(error=lambda url, h: RuntimeError("down"))
            assert listed(now + 2)["T-1"]["status"] == "done", f"{forge}: a failed lookup never undoes a kept PR/MR"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(base)


def closing_status():
    """A1, A8: "done" only from an MR (opened or merged) or the closed marker; before that,
    "findings" until findings-decision.md is at least as new as every review."""
    d = load_dashboard("dash_status")
    base = tempfile.mkdtemp()
    try:
        tdir = os.path.join(base, "T-1")
        os.makedirs(tdir)
        open(os.path.join(tdir, "00-purpose.md"), "w").write("# T-1 x\n")
        flow = json.load(open(FLOW))
        t0 = 1_700_000_000 * 10**9

        def put(name, text, t=t0):
            path = os.path.join(tdir, name)
            open(path, "w").write(text)
            os.utime(path, ns=(t, t))

        def verdicts(*vs):
            for name, v in zip(REVIEWS, vs):
                put(name, f"Verdict: {v}\n\n## Findings\n- [low] x\n")
        state = lambda mr=None: d.task_state(d.stages(flow, tdir), [], tdir, mr)
        opened = {"iid": 7, "state": "opened", "web_url": "https://gitlab.example/g/p/-/merge_requests/7"}
        merged = {**opened, "state": "merged"}

        verdicts("PASS", "PASS", "PASS_WITH_FINDINGS", "PASS")
        assert state()["status"] == "findings", state()
        assert state()["next_step"] == "decide-findings" and not state()["cleanable"], state()
        put("findings-decision.md", "Decided: 2026-11-16\n- accepted minor qa: x\n", t0 + 10**9)
        assert state()["status"] == "ready-for-mr", "a decision newer than every review is decided"
        put("findings-decision.md", "Decided: 2026-11-16\n- accepted minor qa: x\n", t0)
        assert state()["status"] == "ready-for-mr", "same mtime counts as decided (>=, nanoseconds)"
        put("findings-decision.md", "- accepted minor qa: x\n", t0 + 10**9)
        assert state()["status"] == "findings", "a decision file without its 'Decided:' line is not a decision"
        put("findings-decision.md", "Decided: 2026-11-16\n- accepted minor qa: x\n", t0 + 10**9)
        put("04-review-qa.md", "Verdict: PASS_WITH_FINDINGS\n\n## Findings\n- [low] x\n", t0 + 2 * 10**9)
        assert state()["status"] == "ready-for-mr", "a lens re-run that brings only decided findings asks nothing"
        put("04-review-qa.md", "Verdict: PASS_WITH_FINDINGS\n\n## Findings\n- [low] x\n- [medium] a new one\n", t0 + 2 * 10**9)
        assert state()["status"] == "findings", "a lens re-run with a finding not decided reopens it"
        os.remove(os.path.join(tdir, "findings-decision.md"))

        verdicts("PASS", "PASS", "PASS", "PASS")
        assert state()["status"] == "ready-for-mr" and state()["next_step"] == "open-mr", state()
        assert state()["closed_by"] is None and state()["mr"] is None, state()
        # A failed lookup is not "no MR": the next step says to fix the check, not to open one.
        for error in ("no-token", "unavailable", "host-not-allowed"):
            got = state({"error": error})
            assert got["status"] == "ready-for-mr" and got["next_step"] == "fix-mr-check", got
        assert state({"error": "unsupported-remote"})["next_step"] == "mark-done", \
            "no MR can ever be found on a forge that is not GitLab: close it"
        got = d.task_state(d.stages(flow, tdir), [], tdir, None, lookup_possible=False)
        assert got["next_step"] == "mark-done", "a base or detached branch closes by Mark as done, not by an MR"
        got = d.full_state(base, "develop", flow, "T-1", tdir, [], d.stages(flow, tdir), None, "", "/srv/root")
        assert got["branch_kind"] == "base" and got["next_step"] == "mark-done" \
            and got["command"] == f"cd {base} && bash .claude/kit/scripts/work.sh done T-1" \
            and got["command_kind"] == "shell", got
        got = d.full_state(base, "feat/x", flow, "T-1", tdir, [], d.stages(flow, tdir), {"error": "no-token"}, "", "/srv/root")
        assert got["branch_kind"] == "feature" and got["command"] == (
            "cd /srv/root && read -rs GITLAB_TOKEN && export GITLAB_TOKEN && "
            "bash .claude/kit/scripts/work.sh ui stop && bash .claude/kit/scripts/work.sh ui start"), got
        assert "GITLAB_TOKEN=" not in got["command"], "the token is never written into the command line"

        verdicts("PASS", "PASS", "PASS", "FAIL")
        assert state() == {"status": "FAIL", "warning": None, "closed_by": None, "mr": None,
                           "next_step": "fix-fail", "cleanable": False, "closable": True}, state()
        got = state(opened)
        assert got["status"] == "done" and got["warning"] == "fail-unresolved" and got["closed_by"] == "mr" \
            and got["cleanable"] and got["next_step"] == "clean-up", got

        verdicts("PASS", "PASS", "PASS", "PASS")
        open(os.path.join(tdir, "closed"), "w").write("2026-11-16T00:00:00Z\n")
        got = state()
        assert got["status"] == "done" and got["closed_by"] == "work.sh" and got["warning"] is None, got
        assert not got["closable"], "a done task is not offered Mark as done again"
        os.remove(os.path.join(tdir, "closed"))

        for name in REVIEWS:
            os.remove(os.path.join(tdir, name))
        got = state(opened)
        assert got["status"] == "done" and got["mr"]["state"] == "opened", "an open MR closes a task with lenses missing"
        assert state(merged)["status"] == "done" and state(merged)["mr"]["state"] == "merged"
        closed_mr = {**opened, "state": "closed"}
        assert state(closed_mr)["status"] == "purpose" and state(closed_mr)["closed_by"] is None, \
            "A1: an MR only closed (not merged) does not close the task"
        got = state({"error": "unavailable"})
        assert got["status"] == "purpose" and got["mr"] == {"error": "unavailable"}, got
        assert set(d.next_step(s, []) for s in d.STATUSES) <= set(d.NEXT_STEPS), "every next step is a known key"
    finally:
        shutil.rmtree(base)
    findings_view()


def git_in(path, *a):
    p = subprocess.run(["git", "-C", path, "-c", "user.name=t", "-c", "user.email=t@t", *a],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"git {' '.join(a)}: {p.stderr}"
    return p.stdout


def new_repo(base, origin=None):
    repo = os.path.join(base, "repo")
    os.makedirs(os.path.join(repo, ".claude"))
    shutil.copy(FLOW, os.path.join(repo, ".claude", "flow.json"))
    open(os.path.join(repo, ".gitignore"), "w").write("/.work/\n*.pem\n/bulk/\nenvios-sftp/\n")
    git_in(repo, "init", "-q", "-b", "develop")
    git_in(repo, "add", ".")
    git_in(repo, "commit", "-qm", "init")
    if origin:
        git_in(repo, "remote", "add", "origin", origin)
    return repo


def add_task(wt, tid, files=(), current=False, closed=False):
    w = os.path.join(wt, ".work", tid)
    os.makedirs(w, exist_ok=True)
    open(os.path.join(w, "00-purpose.md"), "w").write(f"# {tid} title\n")
    for name, text in files:
        open(os.path.join(w, name), "w").write(text)
    if current:
        open(os.path.join(wt, ".work", "current"), "w").write(tid)
    if closed:
        open(os.path.join(w, "closed"), "w").write("2026-11-16T00:00:00Z\n")


class FetchStub:
    def __init__(self, by_branch=None, error=None):
        self.calls, self.by_branch, self.error = [], by_branch or {}, error

    def __call__(self, url, headers, timeout):
        import urllib.parse as up
        # GitLab names the branch in source_branch, GitHub in head=owner:branch.
        q = up.parse_qs(up.urlparse(url).query)
        branch = q["source_branch"][0] if "source_branch" in q else q["head"][0].split(":", 1)[1]
        self.calls.append(branch)
        if self.error:
            raise self.error(url, headers)
        return self.by_branch.get(branch, [])


def mr(iid, state, title="", branch="", source_project=5):
    return {"iid": iid, "state": state, "web_url": f"https://gitlab.example/g/p/-/merge_requests/{iid}",
            "title": title, "source_branch": branch, "created_at": f"2026-11-{10 + iid:02d}T00:00:00Z",
            "source_project_id": source_project, "target_project_id": 5,
            "author": {"username": "someone"}, "description": "secret"}


def closing_mr():
    """A2-A6, A16: the MR lookup is per task, never on a base branch or detached HEAD, a failure
    is visible and cached, and the token never reaches a response."""
    import urllib.error
    d = load_dashboard("dash_mr")
    base = tempfile.mkdtemp()
    saved_env = os.environ.pop("GITLAB_TOKEN", None)
    saved_host = os.environ.pop("GITLAB_HOST", None)
    try:
        repo = new_repo(base, origin="https://gitlab.example/g/p.git")
        # GITLAB_TOKEN goes only to a host the project allows (flow.json gitlab_hosts or GITLAB_HOST).
        os.environ["GITLAB_HOST"] = "gitlab.example"
        feat, det, rel = (os.path.join(base, n) for n in ("feat", "det", "rel"))
        git_in(repo, "worktree", "add", "-q", "-b", "feat/t-2-x", feat)
        git_in(repo, "worktree", "add", "-q", "--detach", det)
        git_in(repo, "worktree", "add", "-q", "-b", "release", rel)
        # A repo can name its own base branches; the default keeps main/master/develop.
        flow = json.load(open(FLOW))
        open(os.path.join(rel, ".claude", "flow.json"), "w").write(json.dumps({**flow, "base_branches": ["release"]}))
        pass_all = [(n, "Verdict: PASS\n") for n in REVIEWS]
        add_task(repo, "T-1", pass_all, current=True)
        add_task(det, "T-3", pass_all, current=True)
        add_task(rel, "T-4", pass_all, current=True)
        add_task(feat, "T-2", pass_all)
        add_task(feat, "T-22", pass_all)
        add_task(feat, "T-7", pass_all, current=True)
        add_task(feat, "T-9", pass_all)
        add_task(feat, "T-5", pass_all)
        os.environ["GITLAB_TOKEN"] = "tok-x"
        b = "feat/t-2-x"
        stub = FetchStub({b: [mr(1, "closed", "T-9 old attempt", b), mr(2, "opened", "Fix T-5 too", b),
                              mr(3, "merged", "unrelated", b), mr(4, "opened", "T-9 from a fork", b, source_project=9)]})
        d.FETCH = stub
        now = time.time()
        tasks = lambda at=now: {t["id"]: t for g in d.list_tasks(repo, at) for t in g["tasks"]}
        got = tasks()
        assert stub.calls == ["feat/t-2-x"], f"A3: a base branch, a detached HEAD or a flow base branch was looked up: {stub.calls}"
        assert got["T-2"]["status"] == "done" and got["T-2"]["mr"] == {
            "iid": 3, "state": "merged", "web_url": "https://gitlab.example/g/p/-/merge_requests/3"}, \
            f"A2: the ID in the branch (any case) applies, newest MR first: {got['T-2']['mr']}"
        assert got["T-5"]["mr"]["iid"] == 2, "the ID in the title of an older MR still applies"
        assert got["T-7"]["status"] == "done", "A2: the worktree's current task takes the branch's MR"
        assert got["T-5"]["status"] == "done", "A2: the ID in the MR title applies"
        assert got["T-9"]["status"] == "ready-for-mr" and got["T-9"]["mr"] is None, \
            "A2: a closed MR naming the task, an MR from a fork or an MR of another task does not close it"
        assert got["T-22"]["mr"] is None, "A2: T-2 in the branch is not T-22"
        assert set(got["T-2"]["mr"]) == {"iid", "state", "web_url"}, f"A6: only iid, state, web_url: {got['T-2']['mr']}"
        for tid in ("T-1", "T-3", "T-4"):
            assert got[tid]["mr"] is None and got[tid]["status"] == "ready-for-mr" \
                and got[tid]["next_step"] == "mark-done", got[tid]
        assert d.mr_lookup(feat, "", flow, now) is None and stub.calls == ["feat/t-2-x"], "A3: an empty branch is not looked up"

        # A5: one fetch per minute per branch, failures included.
        tasks(now + 30)
        assert stub.calls == ["feat/t-2-x"], f"A5: a second poll 30 s later fetched again: {stub.calls}"
        tasks(now + 61)
        assert stub.calls == ["feat/t-2-x"] * 2, f"A5: after 60 s the cache must expire: {stub.calls}"

        # A4: a failure is a fixed key on every task of the worktree, never "no MR".
        def boom_timeout(url, headers):
            return TimeoutError(f"timed out {url} {headers}")
        def boom_401(url, headers):
            return urllib.error.HTTPError(url, 401, f"Unauthorized {headers}", {}, None)
        def boom_404(url, headers):
            return urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        # The PR/MR found above is kept in mr.json (task_owns_branch): these are lookups of a task
        # that never had one.
        kept = os.path.join(feat, ".work", "T-2", "mr.json")
        for boom in (boom_timeout, boom_401, boom_404):
            if os.path.exists(kept):
                os.remove(kept)
            d._mr_cache.clear()
            d.FETCH = stub = FetchStub(error=boom)
            got = tasks()
            assert got["T-2"]["mr"] == {"error": "unavailable"} and got["T-2"]["status"] == "ready-for-mr", got["T-2"]
            tasks(now + 30)
            assert len(stub.calls) == 1, f"A5: a failure must be cached too: {stub.calls}"
        d._mr_cache.clear()
        if os.path.exists(kept):
            os.remove(kept)
        d.FETCH = stub = FetchStub(by_branch={"feat/t-2-x": {"message": "401"}})
        assert tasks()["T-2"]["mr"] == {"error": "unavailable"}, "a body that is not a list is a failure"
        d._mr_cache.clear()
        del os.environ["GITLAB_TOKEN"]
        d.FETCH = stub = FetchStub()
        got = tasks()["T-2"]
        assert got["mr"] == {"error": "no-token"} and not stub.calls, "A4: no token says so"
        assert got["next_step"] == "fix-mr-check" and "read -rs GITLAB_TOKEN" in got["command"], got
        attn = d.list_attention(repo, now)
        check = next(g["mr_check"] for g in attn if g["branch"] == b)
        assert check["error"] == "no-token" and "T-2" in check["tasks"] and check["action"] == "fix-mr-check" \
            and check["command"] == got["command"], f"a failed lookup is an alert of its worktree: {check}"
        # A token exported for one GitLab never goes to another host.
        d._mr_cache.clear()
        os.environ["GITLAB_TOKEN"] = "tok-x"
        del os.environ["GITLAB_HOST"]
        got = tasks()["T-2"]
        assert got["mr"] == {"error": "host-not-allowed"} and not stub.calls, f"the token went to a host not allowed: {stub.calls}"
        assert "export FORGE_HOST=gitlab.example && read -rs GITLAB_TOKEN" in got["command"] \
            and "tok-x" not in got["command"] and "GITLAB_TOKEN=" not in got["command"], got
        flow_hosts = {**json.load(open(FLOW)), "gitlab_hosts": ["gitlab.example"]}
        open(os.path.join(feat, ".claude", "flow.json"), "w").write(json.dumps(flow_hosts))
        d._mr_cache.clear()
        tasks()
        assert stub.calls == [b], "a host listed in flow.json gitlab_hosts receives the token"
        git_in(feat, "checkout", "-q", ".claude/flow.json")
        os.environ["GITLAB_HOST"] = "gitlab.example"
        del os.environ["GITLAB_TOKEN"]
        stub.calls.clear()

        # A16: GitLab only; another forge says so instead of failing the page.
        d._mr_cache.clear()
        git_in(repo, "remote", "set-url", "origin", "git@bitbucket.org:o/p.git")
        os.environ["GITLAB_TOKEN"] = "tok-x"
        assert tasks()["T-2"]["mr"] == {"error": "unsupported-remote"} and not stub.calls

        # GitHub: pulls by head=owner:branch, no token needed for a public repository; a PR
        # closed without merging does not count, a merged one does.
        d._mr_cache.clear()
        git_in(repo, "remote", "set-url", "origin", "git@github.com:o/p.git")
        gh = lambda n, state, merged, title="": {"number": n, "state": state, "merged_at": merged, "title": title,
                                                  "html_url": f"https://github.com/o/p/pull/{n}",
                                                  "head": {"ref": b, "repo": {"full_name": "o/p"}},
                                                  "base": {"repo": {"full_name": "o/p"}}, "created_at": f"2026-11-{10 + n:02d}T00:00:00Z"}
        fork = dict(gh(6, "closed", "2026-11-17T00:00:00Z"), head={"ref": b, "repo": {"full_name": "someone/p"}})
        d.FETCH = stub = FetchStub({b: [gh(4, "closed", None, "T-9 abandoned"), gh(5, "closed", "2026-11-16T00:00:00Z"), fork]})
        got = tasks()
        assert stub.calls == [b] and got["T-2"]["mr"] == {"iid": 5, "state": "merged",
                                                          "web_url": "https://github.com/o/p/pull/5"}, got["T-2"]
        assert got["T-9"]["mr"] is None, "a PR closed without merge does not close its task"
        assert got["T-2"]["mr"]["iid"] == 5, "a newer PR from a fork with the same branch name is not this branch's"
        d._mr_cache.clear()
        git_in(repo, "remote", "set-url", "origin", "https://ghe.example/o/p.git")
        open(os.path.join(feat, ".claude", "flow.json"), "w").write(json.dumps({**flow, "forge": "github"}))
        seen = []
        d.FETCH = lambda url, headers, timeout: seen.append(url) or []
        tasks()
        assert seen and seen[0].startswith("https://ghe.example/api/v3/repos/o/p/pulls?"), \
            f"flow.json forge names a GitHub Enterprise host: {seen}"
        open(os.path.join(feat, ".claude", "flow.json"), "w").write(json.dumps(flow))
        assert d.parse_remote("ssh://git@gitlab.example:2222/g/sub/p.git") == ("gitlab.example", "g/sub/p", None)
        assert d.parse_remote("https://u:p%40ss@gitlab.example/g/p.git") == ("gitlab.example", "g/p", "p@ss")

        # A6: the token, from the environment or from the origin URL, never reaches a response.
        sentinel = "tok-SENTINEL"
        leak = lambda url, headers: RuntimeError(f"GET {url} with {headers} PRIVATE-TOKEN: {sentinel}")
        import http.client
        import threading
        for env_token, origin in ((sentinel, "https://gitlab.example/g/p.git"),
                                  (None, f"https://user:{sentinel}@gitlab.example/g/p.git")):
            if os.path.exists(kept):
                os.remove(kept)  # the GitHub PR above was kept: this task's lookup must fail here
            d._mr_cache.clear()
            d._seen_tokens.clear()
            os.environ.pop("GITLAB_TOKEN", None)
            if env_token:
                os.environ["GITLAB_TOKEN"] = env_token
            git_in(repo, "remote", "set-url", "origin", origin)
            d.FETCH = FetchStub(error=leak)
            handler = type("H", (d.Handler,), {"root": repo, "token": "page-token"})
            server = d.http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                def get(path):
                    c = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                    c.request("GET", path, headers={"Host": "127.0.0.1"})
                    r = c.getresponse()
                    return r.status, r.read()
                wt_feat = next(g["wt"] for g in d.list_tasks(repo) if g["branch"] == "feat/t-2-x")
                bodies = [get("/api/tasks"), get(f"/api/task?wt={wt_feat}&id=T-2"),
                          get(f"/api/cleanup-plan?wt={wt_feat}&id=T-2"), get("/api/cleanup-plan?all=done"),
                          get("/api/task?wt=99&id=T-2")]
                assert json.loads(bodies[0][1]) and json.loads(bodies[1][1])["mr"] == {"error": "unavailable"}, bodies[1]
                real = d.list_tasks
                d.list_tasks = lambda *a: d.mr_lookup(feat, "feat/t-2-x", flow, 0) and (_ for _ in ()).throw(
                    RuntimeError(f"failed with {os.environ.get('GITLAB_TOKEN') or origin} PRIVATE-TOKEN: {sentinel}"))
                bodies.append(get("/api/tasks"))
                d.list_tasks = real
                assert bodies[-1][0] == 500, bodies[-1]
                for code, body in bodies:
                    assert sentinel.encode() not in body, f"A6: the token reached a response ({code}): {body[:300]}"
            finally:
                server.shutdown()
    finally:
        os.environ.pop("GITLAB_TOKEN", None)
        os.environ.pop("GITLAB_HOST", None)
        if saved_env is not None:
            os.environ["GITLAB_TOKEN"] = saved_env
        if saved_host is not None:
            os.environ["GITLAB_HOST"] = saved_host
        shutil.rmtree(base)


def mr_fetch_http():
    """The real fetch (not the stub): a User-Agent that is not urllib's default, which the
    Cloudflare in front of GitLab answers with 403 (so every lookup read "unavailable"), the
    PRIVATE-TOKEN kept, and a redirect not followed, so the token never reaches another host.
    Two local servers, no network."""
    import http.server
    import threading
    import urllib.error
    d = load_dashboard("dash_fetch")
    assert d.FETCH is d.http_fetch, "the lookup must use http_fetch outside the tests"
    seen = {"a": [], "b": []}

    def server(name, redirect_to=None):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen[name].append((self.path, dict(self.headers)))
                if self.path == "/redir" and redirect_to:
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{redirect_to()}/list")
                    self.end_headers()
                    return
                ua = self.headers.get("User-Agent", "")
                # What the Cloudflare does: urllib's default agent is refused.
                body, code = (b'{"message": "403"}', 403) if ua.startswith("Python-urllib") else (b"[]", 200)
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=s.serve_forever, daemon=True).start()
        return s
    b = server("b")
    a = server("a", redirect_to=lambda: b.server_port)
    try:
        got = d.http_fetch(f"http://127.0.0.1:{a.server_port}/list", {"PRIVATE-TOKEN": "tok-UA"}, 5)
        assert got == [], f"the fetch must get the list: {got}"
        headers = {k.lower(): v for k, v in seen["a"][-1][1].items()}
        assert headers.get("user-agent") == d.USER_AGENT and not d.USER_AGENT.startswith("Python-urllib"), \
            f"the request must carry the kit's User-Agent, not urllib's default: {headers.get('user-agent')}"
        assert headers.get("private-token") == "tok-UA", f"the token header must be kept: {sorted(headers)}"
        try:
            d.http_fetch(f"http://127.0.0.1:{a.server_port}/redir", {"PRIVATE-TOKEN": "tok-UA"}, 5)
            raise AssertionError("a redirect must not be followed")
        except urllib.error.HTTPError as e:
            assert e.code == 302, e
        assert seen["a"][-1][0] == "/redir" and seen["b"] == [], \
            f"a redirect was followed: the token went to another host: {seen['b']}"
    finally:
        a.shutdown()
        b.shutdown()


def work_done():
    """A7: work.sh done writes the closed marker of the current task or of any task by ID; an
    unknown ID writes nothing and fails."""
    root = kit_root()
    try:
        ws = lambda *a: subprocess.run(["bash", os.path.join(KIT, "scripts", "work.sh"), *a], capture_output=True,
                                       text=True, env=dict(os.environ, CLAUDE_PROJECT_DIR=root))
        work = os.path.join(root, ".work")
        assert ws("init", "T-OLD").returncode == 0 and ws("init", "T-1").returncode == 0
        p = ws("done")
        assert p.returncode == 0 and os.path.isfile(os.path.join(work, "T-1", "closed")), p.stdout + p.stderr
        assert not os.path.exists(os.path.join(work, "current")), "done unmarks the current task"
        assert ws("init", "T-2").returncode == 0
        p = ws("done", "T-OLD")
        assert p.returncode == 0 and os.path.isfile(os.path.join(work, "T-OLD", "closed")), p.stderr
        assert open(os.path.join(work, "current")).read().strip() == "T-2", "closing another task keeps the current one"
        before = sorted(os.listdir(work))
        for bad in ("NOPE", "../T-2", "T 2"):
            p = ws("done", bad)
            assert p.returncode != 0 and sorted(os.listdir(work)) == before, f"done {bad!r} must fail and write nothing"
        assert not os.path.exists(os.path.join(work, "T-2", "closed"))
    finally:
        shutil.rmtree(root)


def snapshot(base):
    """Every file under base except git's internals, with its content hash."""
    import hashlib
    out = {}
    for dp, dns, fs in os.walk(base):
        dns[:] = [n for n in dns if n not in (".git", "remote.git")]
        for f in fs:
            if f == ".git":
                continue
            fp = os.path.join(dp, f)
            out[os.path.relpath(fp, base)] = hashlib.sha256(open(fp, "rb").read()).hexdigest()
    return out


def cleanup_repo(base):
    """A repo with a bare remote and worktrees in every state the cleanup must tell apart."""
    remote = os.path.join(base, "remote.git")
    subprocess.run(["git", "init", "-q", "--bare", remote], check=True, capture_output=True)
    repo = new_repo(base, origin=remote)
    git_in(repo, "push", "-q", "origin", "develop")
    wts = {}
    for name, branch in (("done", "feat/done"), ("dirty", "feat/dirty"), ("unp", "feat/unp"),
                         ("nor", "feat/nor"), ("mixed", "feat/mixed")):
        path = os.path.join(base, name)
        git_in(repo, "worktree", "add", "-q", "-b", branch, path)
        if name != "nor":
            open(os.path.join(path, f"{name}.txt"), "w").write(name)
            git_in(path, "add", ".")
            git_in(path, "commit", "-qm", name)
            git_in(path, "push", "-q", "origin", branch)
        wts[name] = path
    # A linked worktree on a branch the repo declares as base: removable, its branch kept.
    rel = os.path.join(base, "rel")
    git_in(repo, "worktree", "add", "-q", "-b", "release", rel)
    flow = json.load(open(os.path.join(rel, ".claude", "flow.json")))
    open(os.path.join(rel, ".claude", "flow.json"), "w").write(json.dumps({**flow, "base_branches": ["release"]}))
    git_in(rel, "commit", "-qam", "release is a base branch")
    git_in(rel, "push", "-q", "origin", "release")
    wts["rel"] = rel
    open(os.path.join(wts["dirty"], "scratch.txt"), "w").write("x")
    open(os.path.join(wts["unp"], "more.txt"), "w").write("x")
    git_in(wts["unp"], "add", ".")
    git_in(wts["unp"], "commit", "-qm", "not pushed")
    pwf = [(n, "Verdict: PASS_WITH_FINDINGS\n") for n in REVIEWS]
    add_task(repo, "M1", closed=True)
    add_task(wts["done"], "D1", closed=True, current=True)
    add_task(wts["dirty"], "D2", closed=True)
    add_task(wts["unp"], "D3", closed=True)
    add_task(wts["nor"], "D4", closed=True)
    add_task(wts["mixed"], "D5", closed=True, current=True)
    add_task(wts["mixed"], "F5", pwf)
    add_task(wts["mixed"], "R5")
    add_task(rel, "B1", closed=True)
    with open(os.path.join(wts["mixed"], ".work", "events.jsonl"), "w") as f:
        f.write(json.dumps({"ev": "start", "agent": "a1", "type": "executor", "t": time.time(), "work": "R5"}) + "\n")
    return repo, wts


def closing_cleanup():
    """A9-A14: the cleanup plan lists exactly what goes and refuses what must stay; the endpoint
    deletes only a confirmed, recomputed plan behind the same checks as /api/answer; work.sh
    cleanup prints the same plan; a memory request reaches the next prompt of a session once."""
    import http.client
    import threading
    d = load_dashboard("dash_cleanup")
    d.FETCH = FetchStub()  # the bare remote is not GitLab; nothing may go out anyway
    # git reports real paths: /var is a link to /private/var on macOS.
    base = os.path.realpath(tempfile.mkdtemp())
    try:
        repo, wts = cleanup_repo(base)
        idx = {os.path.basename(p): i for i, (p, _) in enumerate(d.worktrees(repo))}
        dead = subprocess.Popen(["true"])
        dead.wait()
        adir = os.path.join(wts["done"], ".work", "attention")
        for session, pid in (("s-dead", dead.pid), ("s-live", os.getpid())):
            os.makedirs(os.path.join(adir, session))
            open(os.path.join(adir, session, "main.json"), "w").write(
                json.dumps({"kind": "turn", "since": time.time(), "detail": "", "claude_pid": pid}))
        plan = lambda targets, root=repo: d.cleanup_plan(root, targets)
        one = lambda name, tid, root=repo: plan([(idx[name], tid)], root)["targets"][0]

        # A9: refusals, one assertion each.
        assert one("mixed", "F5")["refused"] == "not-done" and one("mixed", "F5")["items"] == {}, "a findings task is not done"
        assert one("mixed", "R5")["refused"] == "not-done", "a running task is not done"
        assert one("repo", "NOPE")["refused"] == "no-such-task"
        wt_refusal = lambda name, tid, root=repo: one(name, tid, root)["items"]["worktree"]["refused"]
        assert wt_refusal("repo", "M1") == "main-worktree", "the main worktree is never removed"
        idx_from_done = {os.path.basename(p): i for i, (p, _) in enumerate(d.worktrees(wts["done"]))}
        assert d.cleanup_plan(wts["done"], [(idx_from_done["done"], "D1")])["targets"][0]["items"]["worktree"]["refused"] \
            == "serving-worktree", "the worktree the server serves is never removed"
        assert wt_refusal("dirty", "D2") == "dirty", "untracked changes refuse"
        assert wt_refusal("unp", "D3") == "unpushed", "commits not on a remote refuse"
        assert wt_refusal("nor", "D4") == "no-remote-branch", "a branch with no remote ref refuses"
        assert wt_refusal("mixed", "D5") == "other-task-not-done", "a worktree holding a task not done refuses"
        assert wt_refusal("done", "D1") == "live-session", "removing a worktree would delete a live alert"
        d1 = one("done", "D1")
        state = d1["items"]["state"]["paths"]
        w_done = os.path.join(wts["done"], ".work")
        assert sorted(state) == [os.path.join(w_done, "D1"), os.path.join(w_done, "current")] \
            and not d1["items"]["state"]["cancels_memory_request"], \
            f"state lists the task and current that holds it; alerts go only with their worktree: {state}"
        ignored = d1["items"]["worktree"]["ignored"]
        assert ".work/attention/s-dead/main.json" in ignored and ".work/D1/00-purpose.md" in ignored \
            and d1["items"]["worktree"]["ignored_count"] == len(ignored), f"what goes with the worktree is listed: {ignored}"
        # A live session without an alert up: its marker (live Claude process) or a recent event.
        parked = os.path.join(base, "parked-s-live")
        shutil.move(os.path.join(adir, "s-live"), parked)
        assert wt_refusal("done", "D1") is None, "precondition: nothing else refuses this worktree"
        for pid, want in ((os.getpid(), "live-session"), (dead.pid, None)):
            os.makedirs(os.path.join(adir, "s-run"), exist_ok=True)
            open(os.path.join(adir, "s-run", ".session"), "w").write(json.dumps({"claude_pid": pid, "since": time.time()}))
            assert wt_refusal("done", "D1") == want, f"session marker with pid {pid}: {wt_refusal('done', 'D1')}"
        shutil.rmtree(os.path.join(adir, "s-run"))
        log = os.path.join(w_done, "events.jsonl")
        open(log, "w").write(json.dumps({"ev": "tool", "agent": "main", "t": time.time(), "work": "D1"}) + "\n")
        assert wt_refusal("done", "D1") == "live-session", "an event in the last 15 minutes is a live session"
        os.remove(log)
        # Ignored files go with the worktree: a secret among them refuses it, and so does the
        # state of a task this cleanup does not name.
        open(os.path.join(wts["done"], "deploy.pem"), "w").write("not a real key\n")
        got = one("done", "D1")["items"]["worktree"]
        assert got["refused"] == "sensitive-files" and got["sensitive"] == ["deploy.pem"], got
        os.remove(os.path.join(wts["done"], "deploy.pem"))
        # A directory the project names in flow.json (copies of PCI files here) is a secret too.
        real_flow = d.flow_of
        d.flow_of = lambda wt: {**real_flow(wt), "sensitive_ignored": ["envios-sftp"]}
        os.makedirs(os.path.join(wts["done"], "envios-sftp"))
        open(os.path.join(wts["done"], "envios-sftp", "batch.txt"), "w").write("x")
        got = one("done", "D1")["items"]["worktree"]
        assert got["refused"] == "sensitive-files" and got["sensitive"] == ["envios-sftp/batch.txt"], got
        shutil.rmtree(os.path.join(wts["done"], "envios-sftp"))
        d.flow_of = real_flow
        # More ignored files than the confirmation can list: refused, never removed unlisted.
        os.makedirs(os.path.join(wts["done"], "bulk"))
        for n in range(d.IGNORED_SHOWN + 1):
            open(os.path.join(wts["done"], "bulk", f"f{n:03}"), "w").close()
        got = one("done", "D1")["items"]["worktree"]
        assert got["refused"] == "too-many-ignored" and got["ignored_count"] > d.IGNORED_SHOWN \
            and len(got["ignored"]) == d.IGNORED_SHOWN, (got["refused"], got["ignored_count"])
        shutil.rmtree(os.path.join(wts["done"], "bulk"))
        assert one("done", "D1")["items"]["worktree"]["refused"] is None, "back to removable"
        for rel, want in (("docs/staging-develop.env", True), (".env", True), (".env.local", True),
                          (".envrc", True), ("a/b/prod.env", True), ("keys/x", True), ("notes/envelope.txt", False),
                          ("src/environment.go", False)):
            assert d.sensitive(rel, ".work", set()) == want, rel
        assert d.sensitive("x/pci/a", ".work", set(), ["pci"]) and not d.sensitive("x/pci/a", ".work", set()), \
            "flow.json sensitive_ignored adds directories"
        add_task(wts["done"], "D1B", closed=True)
        got = one("done", "D1")["items"]["worktree"]
        assert got["refused"] == "sensitive-files" and got["sensitive"] == [".work/D1B/00-purpose.md", ".work/D1B/closed"], got
        both = plan([(idx["done"], "D1"), (idx["done"], "D1B")])["targets"]
        assert all(t["items"]["worktree"]["refused"] is None for t in both), "a task the same cleanup names is not a secret"
        shutil.rmtree(os.path.join(w_done, "D1B"))
        shutil.move(parked, os.path.join(adir, "s-live"))
        b1 = one("rel", "B1")["items"]["worktree"]
        assert b1["refused"] is None and b1["delete_branch"] is False \
            and b1["recreate"] == f"git worktree add {wts['rel']} release", f"a base branch keeps its branch: {b1}"
        open(os.path.join(wts["mixed"], ".work", "current"), "w").write("R5")
        assert os.path.join(wts["mixed"], ".work", "current") not in one("mixed", "D5")["items"]["state"]["paths"], \
            "current is listed only when it holds that task"
        open(os.path.join(wts["mixed"], ".work", "current"), "w").write("D5")
        w = d1["items"]["worktree"]
        assert w["path"] == wts["done"] and w["branch"] == "feat/done" and w["tasks"] == ["D1"] \
            and w["recreate"] == f"git worktree add {wts['done']} -b feat/done origin/feat/done", w
        mem = d1["items"]["memory"]
        assert mem["request"] == "memory-D1.json" and not any(isinstance(v, str) and os.path.isabs(v) for v in mem.values()), \
            f"memory is a request to the session, never a path: {mem}"

        # A12: every done task of every worktree, and only those.
        alld = plan("all-done")
        assert sorted(t["id"] for t in alld["targets"]) == ["B1", "D1", "D2", "D3", "D4", "D5", "M1"], alld["targets"]
        assert all(t["refused"] is None for t in alld["targets"])

        # A14: work.sh cleanup prints the same plan, and without a terminal deletes nothing.
        before = snapshot(base)
        p = subprocess.run(["bash", os.path.join(KIT, "scripts", "work.sh"), "cleanup", "D5", "--state"],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           env=dict(os.environ, CLAUDE_PROJECT_DIR=wts["mixed"]))
        want = d.cleanup_plan(wts["mixed"], [(0, "D5")])["plan"]
        assert p.returncode == 2 and f"plan {want}" in p.stdout, f"A14: {p.returncode} {p.stdout} {p.stderr}"
        assert "state: delete " + os.path.join(wts["mixed"], ".work", "D5") in p.stdout, p.stdout
        p = subprocess.run(["bash", os.path.join(KIT, "scripts", "work.sh"), "cleanup", "--all-done",
                            "--state", "--worktree", "--memory"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, env=dict(os.environ, CLAUDE_PROJECT_DIR=repo))
        assert p.returncode == 2 and f"plan {alld['plan']}" in p.stdout, f"A14 all-done: {p.stdout} {p.stderr}"
        assert snapshot(base) == before, "A14: work.sh cleanup without a terminal deleted something"
        # In a terminal: a refused item is dropped before the question, as the page does; with
        # every worktree refused, nothing is asked and nothing is deleted.
        import io
        from contextlib import redirect_stdout

        class Tty(io.StringIO):
            def isatty(self):
                return True
        asked, real_stdin = [], sys.stdin
        d.input = lambda prompt: asked.append(prompt) or "yes"
        b1_closed = os.path.join(wts["rel"], ".work", "B1", "closed")
        os.rename(b1_closed, b1_closed + ".off")  # B1's worktree is the one removable: not done now
        sys.stdin, out = Tty(), io.StringIO()
        try:
            with redirect_stdout(out):
                code = d.cleanup_cli([repo, "--all-done", "--worktree"])
        finally:
            sys.stdin = real_stdin
            os.rename(b1_closed + ".off", b1_closed)
        assert code == 0 and "nothing to clean" in out.getvalue() and not asked, \
            f"CLI with every item refused: {code} {out.getvalue()[-300:]}"
        assert snapshot(base) == before, "the CLI deleted something with every item refused"

        # A fake auto memory tree that nothing may touch.
        home_mem = os.path.join(base, "home", ".claude-x", "projects", "p", "memory")
        os.makedirs(home_mem)
        open(os.path.join(home_mem, "MEMORY.md"), "w").write("- [D1](d1.md)\n")
        open(os.path.join(home_mem, "d1.md"), "w").write("D1 memory\n")

        token = "tok-" + "y" * 20
        handler = type("H", (d.Handler,), {"root": repo, "token": token})
        server = d.http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_port
        try:
            def call(method, path, body=None, headers=None, raw=None, srv_port=port, tok=token):
                h = {"Host": f"127.0.0.1:{srv_port}", "Origin": f"http://127.0.0.1:{srv_port}",
                     "Content-Type": "application/json", "X-Kit-Token": tok}
                for k, v in (headers or {}).items():
                    if v is None:
                        h.pop(k, None)
                    else:
                        h[k] = v
                c = http.client.HTTPConnection("127.0.0.1", srv_port, timeout=10)
                data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
                c.putrequest(method, path, skip_host=True)
                for k, v in h.items():
                    c.putheader(k, v)
                if data is not None:
                    c.putheader("Content-Length", str(len(data)))
                c.endheaders(data)
                r = c.getresponse()
                return r.status, r.read()

            code, body = call("GET", f"/api/cleanup-plan?wt={idx['mixed']}&id=D5")
            got = json.loads(body)
            assert code == 200 and got["plan"] == plan([(idx["mixed"], "D5")])["plan"], "GET serves cleanup_plan"
            assert call("GET", f"/api/cleanup-plan?wt={idx['mixed']}&id=NOPE")[0] == 404
            assert call("GET", "/api/cleanup-plan?all=done", headers={"Host": "evil.example"})[0] == 403, \
                "the plan is localhost-only"
            assert call("POST", "/api/cleanup-plan", {})[0] == 404, "the plan is read-only"

            good = {"plan": got["plan"], "targets": [{"wt": idx["mixed"], "id": "D5", "items": ["state", "memory"]}]}
            # A11: the same protection as /api/answer; each refusal deletes nothing.
            refused = [
                ("wrong token", good, {"X-Kit-Token": "nope"}, None, 403),
                ("no token", good, {"X-Kit-Token": None}, None, 403),
                ("foreign Origin", good, {"Origin": "http://evil.example"}, None, 403),
                ("foreign Host", good, {"Host": "evil.example"}, None, 403),
                ("text/plain", good, {"Content-Type": "text/plain"}, None, 415),
                ("form", good, {"Content-Type": "application/x-www-form-urlencoded"}, None, 415),
                ("body over 64 KB", None, None, json.dumps({**good, "pad": "a" * 70_000}).encode(), 413),
                ("stale plan", {**good, "plan": "0" * 64}, None, None, 409),
                ("item not in the plan", {**good, "targets": [{**good["targets"][0], "items": ["everything"]}]}, None, None, 400),
                ("refused item", {"plan": plan([(idx["dirty"], "D2")])["plan"],
                                  "targets": [{"wt": idx["dirty"], "id": "D2", "items": ["state", "worktree"]}]}, None, None, 409),
                ("task not done", {"plan": plan([(idx["mixed"], "F5")])["plan"],
                                   "targets": [{"wt": idx["mixed"], "id": "F5", "items": ["state"]}]}, None, None, 409),
            ]
            before = snapshot(base)
            for label, body, headers, raw, want in refused:
                code, _ = call("POST", "/api/cleanup", body, headers, raw)
                assert code == want, f"A11 {label}: got {code}, want {want}"
                assert snapshot(base) == before, f"A11 {label}: something changed"
            assert call("GET", "/api/cleanup")[0] == 405 and snapshot(base) == before, "GET /api/cleanup is 405"
            tokenless = type("H0", (d.Handler,), {"root": repo, "token": None})
            server0 = d.http.server.ThreadingHTTPServer(("127.0.0.1", 0), tokenless)
            threading.Thread(target=server0.serve_forever, daemon=True).start()
            try:
                code, _ = call("POST", "/api/cleanup", good, srv_port=server0.server_port, tok="")
                assert code == 403 and snapshot(base) == before, "a handler without a token refuses"
            finally:
                server0.shutdown()

            # A10: a plan that changed after the confirmation is refused, and nothing goes: a
            # commit landed, the task stopped being done, a file appeared.
            git_in(wts["mixed"], "commit", "-q", "--allow-empty", "-m", "late")
            before = snapshot(base)
            code, body = call("POST", "/api/cleanup", good)
            assert code == 409 and json.loads(body)["code"] == "plan-changed" and snapshot(base) == before, \
                "A10: a commit after the confirmation must refuse the cleanup"
            git_in(wts["mixed"], "reset", "-q", "--hard", "HEAD~1")
            closed = os.path.join(wts["mixed"], ".work", "D5", "closed")
            os.rename(closed, closed + ".off")
            code, body = call("POST", "/api/cleanup", good)
            assert code == 409 and json.loads(body)["code"] == "plan-changed", "A10: a task no longer done refuses"
            os.rename(closed + ".off", closed)
            # A file of the task changes content (same name): only the listed files' sizes and
            # mtimes in the hash can see it.
            purpose = os.path.join(wts["mixed"], ".work", "D5", "00-purpose.md")
            open(purpose, "a").write("edited after the confirmation\n")
            code, body = call("POST", "/api/cleanup", good)
            assert code == 409 and json.loads(body)["code"] == "plan-changed", \
                "A10: a task file edited after the confirmation must refuse the cleanup"
            open(os.path.join(wts["mixed"], ".work", "D5", "late.md"), "w").write("x")
            before = snapshot(base)
            code, body = call("POST", "/api/cleanup", good)
            assert code == 409 and json.loads(body)["code"] == "plan-changed" and snapshot(base) == before, \
                "A10: a file that appeared after the confirmation must refuse the cleanup"
            good["plan"] = plan([(idx["mixed"], "D5")])["plan"]
            code, body = call("POST", "/api/cleanup", good)
            after = snapshot(base)
            assert code == 200 and json.loads(body)["ok"], body
            gone = {k for k in before if k not in after}
            added = {k for k in after if k not in before}
            assert gone == {k for k in before if k.startswith("mixed/.work/D5/")} | {"mixed/.work/current"}, \
                f"A10: exactly the listed paths go: {sorted(gone)}"
            assert added == {"mixed/.work/attention/.requests/memory-D5.json"}, f"A13: the request lands in its worktree: {added}"
            assert all(after[k] == before[k] for k in after if k in before), "A10: a remaining file changed"
            attn = json.loads(call("GET", "/api/attention")[1])
            g = next(g for g in attn if g["memory_requests"] == ["D5"])
            assert g["memory_actions"] == [{"id": "D5", "action": "open-session", "command": f"cd {wts['mixed']} && claude 'review the auto memory of D5'",
                                            "command_kind": "shell"}], f"the pending request says what to run: {g}"

            # Memory picked with state: the pending request the state deletes is written again,
            # never reported ok and gone. A target with nothing picked is accepted and left alone.
            # F5 is not done (refused as a target): with nothing picked it rides along in the set.
            m1 = lambda: plan([(idx["repo"], "M1"), (idx["dirty"], "D2"), (idx["mixed"], "F5")])["plan"]
            code, _ = call("POST", "/api/cleanup", {"plan": m1(), "targets": [
                {"wt": idx["repo"], "id": "M1", "items": ["memory"]}, {"wt": idx["dirty"], "id": "D2", "items": []},
                {"wt": idx["mixed"], "id": "F5", "items": []}]})
            rq_m1 = os.path.join(repo, ".work", "attention", ".requests", "memory-M1.json")
            assert code == 200 and os.path.isfile(rq_m1), code
            assert plan([(idx["repo"], "M1")])["targets"][0]["items"]["state"]["cancels_memory_request"], \
                "the plan says the state deletes a pending memory request"
            before = snapshot(base)
            code, out = call("POST", "/api/cleanup", {"plan": m1(), "targets": [
                {"wt": idx["repo"], "id": "M1", "items": ["state", "memory"]}, {"wt": idx["dirty"], "id": "D2", "items": []},
                {"wt": idx["mixed"], "id": "F5", "items": []}]})
            after = snapshot(base)
            assert code == 200 and json.loads(out)["ok"], out
            assert os.path.isfile(rq_m1), "memory + state: the fresh request must survive"
            assert {k for k in before if k not in after} == {k for k in before if k.startswith("repo/.work/M1/")}, \
                "only M1's state goes; D2, with nothing picked, stays"
            os.remove(rq_m1)
            code, body = call("POST", "/api/cleanup", {"plan": m1(), "targets": []})
            assert code == 400 and json.loads(body)["code"] == "empty", "an empty cleanup is refused, by code"

            # A base-branch linked worktree goes; its branch stays.
            code, out = call("POST", "/api/cleanup", {"plan": plan([(idx["rel"], "B1")])["plan"],
                                                      "targets": [{"wt": idx["rel"], "id": "B1", "items": ["worktree"]}]})
            assert code == 200 and json.loads(out)["ok"] and not os.path.exists(wts["rel"]), out
            assert "release" in git_in(repo, "branch", "--list", "release"), "the base branch is never deleted"

            # The whole worktree: state, worktree and memory; the request moves to the served one.
            shutil.rmtree(os.path.join(adir, "s-live"))
            body = {"plan": plan([(idx["done"], "D1")])["plan"],
                    "targets": [{"wt": idx["done"], "id": "D1", "items": ["state", "worktree", "memory"]}]}
            before = snapshot(base)
            code, out = call("POST", "/api/cleanup", body)
            after = snapshot(base)
            assert code == 200 and json.loads(out)["ok"], out
            assert {k for k in before if k not in after} == {k for k in before if k.startswith("done/")}, \
                "A10: only the removed worktree's files go"
            assert {k for k in after if k not in before} == {"repo/.work/attention/.requests/memory-D1.json"}, \
                "A13: a removed worktree's request waits in the served one"
            assert not os.path.exists(wts["done"]) and "feat/done" not in git_in(repo, "branch", "--list"), \
                "the worktree and its local branch are gone"
            assert "feat/done" in git_in(repo, "branch", "-r"), "the remote branch stays, to recreate it"
            assert sorted(os.listdir(home_mem)) == ["MEMORY.md", "d1.md"], "the auto memory is never touched"
        finally:
            server.shutdown()

        # A13: the next prompt of a session in that worktree consumes the request once.
        e = dict(os.environ, CLAUDE_PROJECT_DIR=repo)
        prompt = lambda name="UserPromptSubmit", s="s1": subprocess.run(
            [sys.executable, HOOK], input=json.dumps({"hook_event_name": name, "session_id": s}),
            capture_output=True, text=True, env=e)
        rq = os.path.join(repo, ".work", "attention", ".requests")
        p = prompt("SessionEnd")
        assert p.stdout == "" and os.listdir(rq) == ["memory-D1.json"], "SessionEnd neither prints nor clears requests"
        p = prompt()
        ctx = json.loads(p.stdout)["hookSpecificOutput"]
        assert ctx["hookEventName"] == "UserPromptSubmit" and "D1" in ctx["additionalContext"] \
            and "AskUserQuestion" in ctx["additionalContext"] and "multiSelect" in ctx["additionalContext"] \
            and "Delete nothing without that pick" in ctx["additionalContext"], ctx
        assert home_mem not in ctx["additionalContext"] and "/" not in ctx["additionalContext"].replace("auto-memory", ""), \
            "the context names no path to delete"
        assert prompt().stdout == "" and os.listdir(rq) == [], "A13: a second prompt prints nothing"
        d.write_request(os.path.join(repo, ".work"), "D9", time.time())
        p = prompt("SessionStart", "s2")
        assert "D9" in json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"], "SessionStart consumes too"
        for _ in range(5):
            d.write_request(os.path.join(repo, ".work"), "D8", time.time())
            procs = [subprocess.Popen([sys.executable, HOOK], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, env=e) for _ in range(4)]
            outs = [pr.communicate(json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": f"p{i}"}))[0]
                    for i, pr in enumerate(procs)]
            assert sum("D8" in o for o in outs) == 1, f"A13: two sessions both took one request: {outs}"
    finally:
        shutil.rmtree(base)


def close_and_actions():
    """POST /api/close is "Mark as done" (the closed marker of work.sh done) behind the same
    checks as the other writes; every alert says what the dev does; a hook-internal agent is not
    a lane, and an agent id is never masked into another's lane."""
    import http.client
    import threading
    d = load_dashboard("dash_close")
    d.FETCH = FetchStub()
    base = os.path.realpath(tempfile.mkdtemp())
    try:
        repo = new_repo(base)
        pwf = [(n, "Verdict: PASS_WITH_FINDINGS\n\n## Findings\n- [low] x\n") for n in REVIEWS]
        add_task(repo, "C1", pwf, current=True)
        add_task(repo, "C2", [(n, "Verdict: PASS\n") for n in REVIEWS])
        token = "tok-" + "z" * 20
        handler = type("H", (d.Handler,), {"root": repo, "token": token})
        server = d.http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_port

        def call(method, path, body=None, headers=None):
            h = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}",
                 "Content-Type": "application/json", "X-Kit-Token": token, **(headers or {})}
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            data = json.dumps(body).encode() if body is not None else None
            c.putrequest(method, path, skip_host=True)
            for k, v in h.items():
                c.putheader(k, v)
            if data is not None:
                c.putheader("Content-Length", str(len(data)))
            c.endheaders(data)
            r = c.getresponse()
            return r.status, r.read()
        closed = os.path.join(repo, ".work", "C1", "closed")
        try:
            good = {"wt": 0, "id": "C1"}
            for label, headers, want in (("wrong token", {"X-Kit-Token": "nope"}, 403),
                                         ("foreign Origin", {"Origin": "http://evil.example"}, 403),
                                         ("foreign Host", {"Host": "evil.example"}, 403),
                                         ("text/plain", {"Content-Type": "text/plain"}, 415)):
                code, _ = call("POST", "/api/close", good, headers)
                assert code == want and not os.path.exists(closed), f"close {label}: {code}"
            assert call("GET", "/api/close")[0] == 405 and not os.path.exists(closed)
            code, body = call("POST", "/api/close", {"wt": 0, "id": "NOPE"})
            assert code == 404 and json.loads(body)["code"] == "no-such-task"
            code, body = call("POST", "/api/close", good)
            assert code == 200 and json.loads(body) == {"ok": True, "warnings": ["open-findings"]}, body
            assert os.path.isfile(closed) and open(closed).read().strip().endswith("Z"), "the marker of work.sh done"
            task = next(t for t in d.list_tasks(repo)[0]["tasks"] if t["id"] == "C1")
            assert task["status"] == "done" and task["closed_by"] == "work.sh", task
            code, body = call("POST", "/api/close", good)
            assert code == 409 and json.loads(body)["code"] == "already-done"
            code, body = call("POST", "/api/close", {"wt": 0, "id": "C2"})
            assert code == 200 and json.loads(body)["warnings"] == [], body
        finally:
            server.shutdown()

        # Every alert ends with the dev's action, and a command where there is one.
        now = time.time()
        att = os.path.join(repo, ".work", "attention", "s1")
        os.makedirs(att)
        alert = lambda agent, a: open(os.path.join(att, agent + ".json"), "w").write(json.dumps(
            {"since": now, "detail": "", "claude_pid": None, **a}))
        alert("main", {"kind": "question", "qid": "Q", "questions": [], "held": False})
        alert("a1", {"kind": "permission", "tool": "Bash"})
        alert("a2", {"kind": "turn"})
        got = {a["agent"]: a for a in d.list_attention(repo, now)[0]["alerts"]}
        enable = "bash .claude/kit/scripts/work.sh ui " + "answers on"
        assert got["main"]["action"] == "answer-in-terminal-or-enable" \
            and got["main"]["command"] == f"cd {repo} && {enable}", got["main"]
        assert got["a1"]["action"] == "approve-in-terminal" and got["a1"]["command"] is None
        assert got["a2"]["action"] == "prompt-session"
        open(os.path.join(repo, ".work", "attention", "answers-" + "on"), "w").close()
        got = {a["agent"]: a for a in d.list_attention(repo, now)[0]["alerts"]}
        assert got["main"]["action"] == "answer-in-terminal" and got["main"]["command"] is None, got["main"]
        alert("main", {"kind": "question", "qid": "Q", "questions": [], "held": True, "heartbeat": now})
        got = {a["agent"]: a for a in d.list_attention(repo, now)[0]["alerts"]}
        assert got["main"]["action"] == "answer-on-page", got["main"]
        assert {a["action"] for a in got.values()} <= set(d.ALERT_ACTIONS)
    finally:
        shutil.rmtree(base)

    # Lanes: a stop without a type from an agent never started is not a lane.
    now = time.time()
    lanes = d.agents([{"ev": "stop", "agent": "ab12", "type": "", "t": now - 5},
                      {"ev": "start", "agent": "ac34", "type": "tester", "t": now - 4},
                      {"ev": "stop", "agent": "ac34", "type": "", "t": now - 3}], now)
    assert [a["agent"] for a in lanes] == ["ac34"] and lanes[0]["state"] == "ok", lanes
    root = kit_root()
    try:
        os.makedirs(os.path.join(root, ".work", "T"))
        open(os.path.join(root, ".work", "current"), "w").write("T")
        agent_id = "a1234567890123456"
        hook(root, {"hook_event_name": "SubagentStart", "agent_id": agent_id, "agent_type": "Explore"})
        logged = [json.loads(l) for l in open(os.path.join(root, ".work", "events.jsonl"))]
        assert logged[-1]["agent"] == agent_id, f"the agent id must not be masked into a shared lane: {logged[-1]}"
    finally:
        shutil.rmtree(root)


def findings_view():
    """The findings a task waits on, from the review files, each decided or open, and the reason
    it reads "findings". A decision file that names fewer findings than the reviews keeps the rest
    open and visible: nothing passes as accepted without being shown."""
    d = load_dashboard("dash_findings")
    base = tempfile.mkdtemp()
    try:
        tdir = os.path.join(base, "T-1")
        os.makedirs(tdir)
        flow = json.load(open(FLOW))
        t0 = 1_700_000_000 * 10**9

        def put(name, text, t=t0):
            open(os.path.join(tdir, name), "w").write(text)
            os.utime(os.path.join(tdir, name), ns=(t, t))
        put("04-review-contract.md", "Verdict: PASS_WITH_FINDINGS\n\n## Findings\n\n1. **High: a failed lookup reads as "
            "ready.** Scenario...\n   more text\n2. **Low: wrong message.**\n\n### Not implemented yet\n\n## Contract coverage\n- High: not a finding\n")
        put("04-review-security.md", "Verdict: PASS_WITH_FINDINGS\n\n## Findings\n- [medium] dashboard.py:782 deletes ignored files\n- [low] token host\n")
        put("04-review-qa.md", "Verdict: PASS\n\n## Findings\n- [low] a remark of a passing lens\n")
        put("04-review-product.md", "Verdict: PASS_WITH_FINDINGS\n\n## Findings\n- none\n")
        view = lambda: d.findings_view(tdir, d.stages(flow, tdir), d.status(d.stages(flow, tdir), [], tdir))
        got = view()
        assert [(f["lens"], f["severity"]) for f in got["findings"]] == [
            ("contract", "high"), ("contract", "low"), ("security", "medium"), ("security", "low")], got["findings"]
        assert got["findings"][0]["summary"].startswith("a failed lookup reads as ready.") \
            and got["findings"][2]["summary"] == "dashboard.py:782 deletes ignored files", got["findings"]
        assert got["findings_open"] == 4 and got["findings_reason"] == "no-decision", got
        put("findings-decision.md", "- fix high contract: x\n", t0 + 10**9)
        assert view()["findings_reason"] == "decision-without-date", view()
        put("findings-decision.md", "Decided: 2026-11-16\n- fix high contract: x\n- accepted medium security: y\n", t0 + 10**9)
        got = view()
        assert got["findings_open"] == 2 and got["findings_reason"] == "some-open", \
            "a dated decision newer than every review that leaves two findings without a line is partly decided"
        assert [f["decision"] for f in got["findings"]] == ["fix", None, "accepted", None], got["findings"]
        assert d.status(d.stages(flow, tdir), [], tdir) == "findings", \
            "status follows the per-finding match: an open finding keeps the task in findings"
        put("findings-decision.md", "Decided: 2026-11-16\n- fix high contract: x\n- accepted low contract: y\n"
            "- accepted medium security: y\n- fix low security: z\n", t0 + 10**9)
        got = view()
        assert got["findings_open"] == 0 and got["findings_reason"] is None \
            and d.status(d.stages(flow, tdir), [], tdir) == "fixing", \
            f"every finding decided, two of them fix-pending: the fix round, never ready for a PR/MR: {got}"
        put("04-review-security.md", "Verdict: PASS_WITH_FINDINGS\n\n## Findings\n- [medium] again\n", t0 + 2 * 10**9)
        got = view()
        assert got["findings_reason"] == "review-newer" and got["findings_review_newer"] == ["security"], got

        # The shapes of a real review (KIT-ATTENTION round 3, contract lens): numbered `###`
        # headings with the severity in parentheses and detail bullets under them, a heading
        # with no severity, a "Severity:" line, a bold severity, and a plain subsection whose
        # bullets are findings of their own.
        put("04-review-contract.md", "Verdict: PASS_WITH_FINDINGS\n\n## Findings\n\n"
            "### 1. (medium) Two plain redirections still write into the dir undetected\n\n"
            "Shown with these commands:\n"
            "- `echo '{...}' >| target` (noclobber override): split at `|`, allowed.\n"
            "- `echo '{...}' >& target`: split at `&`, allowed.\n\n"
            "### 2. (low) Other writes that pass (best effort, as agreed; for the record)\n\n"
            "- subshell `(cd dir && cp a b)`: the word is `(cd`;\n"
            "- High: a detail that only looks like a finding\n\n"
            "### 3. Read-only commands still refused\n\nSeverity: low\n\n"
            "### 4. **Medium** Page: background tabs delay the alert\n\n"
            "### 5. (open, carried from round 1) M1 still not measured\n\n"
            "### Not implemented yet\n\n"
            "- [high] a decision added after the contract\n"
            "- [medium] .claude/kit/x.py:12 keeps its leading dot\n\n"
            "## Contract coverage\n- High: not a finding\n", t0 + 3 * 10**9)
        got = [(f["severity"], f["summary"]) for f in view()["findings"] if f["lens"] == "contract"]
        assert got == [
            ("medium", "Two plain redirections still write into the dir undetected"),
            ("low", "Other writes that pass (best effort, as agreed; for the record)"),
            ("low", "Read-only commands still refused"),
            ("medium", "Page: background tabs delay the alert"),
            ("unknown", "(open, carried from round 1) M1 still not measured"),
            ("high", "a decision added after the contract"),
            ("medium", ".claude/kit/x.py:12 keeps its leading dot"),
        ], got
        assert [d.severity(x) for x in ("Severity: Medium", "(low) x", "**Medium** x", "[high] x", "1. **High: x**",
                                        "Minor: x", "nothing here")] == \
            ["medium", "low", "medium", "high", "high", "low", "unknown"]
        assert d.severity("Low-level parser detail that breaks the whole build [high]") == "high", \
            "an explicit severity marker wins over a severity word used as an adjective"
    finally:
        shutil.rmtree(base)



def whole_findings():
    """Each finding reaches the page whole: its line uncut (full_text) and its details (indented
    lines, a deeper heading, the items under a heading), masked with the hooks' mask; the summary
    and the key stay as they were, so a decision recorded before the mask still lands. Task files
    and the purpose reach the page masked too."""
    d = load_dashboard("dash_whole")
    base = tempfile.mkdtemp()
    try:
        tdir = os.path.join(base, "T-1")
        os.makedirs(tdir)
        flow = json.load(open(FLOW))
        long = "the export " + "drops rows " * 30 + "at the end"
        open(os.path.join(tdir, "04-review-qa.md"), "w").write(
            "Verdict: PASS_WITH_FINDINGS\n\n## Findings\n- [high] " + long + "\n  Scenario: an empty report.\n"
            "  Fix: PASSWORD=hunter2 in the fixture must go.\n\n### 2. (medium) A heading finding\n\n"
            "Why it matters.\n\n#### Fix\n- write the test\n\n## Contract coverage\n- x\n")
        items = d.decided_findings(tdir, d.stages(flow, tdir))
        assert len(items) == 2, items
        a, b = items
        assert len(a["summary"]) == 200 and a["full_text"].endswith("at the end"), a
        assert "Scenario: an empty report." in a["details"] and "hunter2" not in a["details"], a["details"]
        assert "Why it matters." in b["details"] and "#### Fix" in b["details"] and "write the test" in b["details"], \
            f"a deeper heading stays in its finding: {b}"
        open(os.path.join(tdir, "00-purpose.md"), "w").write("# T-1 x\nAPI_TOKEN=abc123\n")
        root = new_repo(base)
        w = os.path.join(root, ".work", "T-1")
        shutil.copytree(tdir, w)
        detail = d.task_detail(root, 0, "T-1")
        assert "abc123" not in detail["purpose"] and "hunter2" not in detail["files"]["04-review-qa.md"], \
            "the purpose and the review text reach the page masked"
    finally:
        shutil.rmtree(base)


def closing_docs():
    """The protocol and the task skill say the closing steps, and the kit names no project."""
    import re
    flat = lambda t: " ".join(t.split())
    p = flat(open(os.path.join(KIT, "PROTOCOL.md")).read())
    assert "The task is done when every lens that applies has a verdict and none is" not in p, "the old done rule is gone"
    for want in ("`findings`", "`ready-for-mr`", "`done`", "findings-decision.md", "work.sh done", "Mark as done",
                 "work.sh cleanup", "memory-<ID>.json", "/clear", "never `--force`", "host-not-allowed", "Learnings first"):
        assert want in p, f"PROTOCOL.md must say {want}"
    text = open(os.path.join(CLAUDE, "skills", "task", "SKILL.md")).read()
    section = lambda title: flat(re.search(r"^## " + re.escape(title) + r".*?\n(.*?)(?=^## |\Z)", text, re.M | re.S).group(1))
    sec = section("7. Findings decision")
    for want in ("AskUserQuestion", "multiSelect", "severity", "(Recommended)", "more than 4 findings", "00-purpose.md",
                 "findings-decision.md", "Never send findings to be fixed without this question",
                 "open or merged", "work.sh done", "Mark as done", "/clear", "never worked around"):
        assert want in sec, f"task skill step 7 must say {want!r}"
    sec = section("8. Close and clean up")
    for want in ("work.sh cleanup", "retrospective", "AskUserQuestion", "--force", "never delete the listed paths yourself"):
        assert want in sec, f"task skill step 8 must say {want!r}"
    sec = section("1. Read before asking")
    assert "plain request" in sec and "slug" in sec and "Do not ask for a ticket" in sec, "a task without a tracker must be clear"
    # Names that must never reach the kit (projects, hosts, clients) come from the environment,
    # so the list itself is not published: FOREMAN_FORBIDDEN_WORDS="name1,name2".
    forbidden = [w.strip() for w in os.environ.get("FOREMAN_FORBIDDEN_WORDS", "").split(",") if w.strip()]
    hits = []
    for dp, dns, fs in os.walk(CLAUDE):
        dns[:] = [n for n in dns if n != "__pycache__"]
        for f in fs:
            if f in ("test_kit.py", "test_page.py"):
                continue
            body = open(os.path.join(dp, f), errors="replace").read()
            hits += [f"{f}: {w}" for w in forbidden if w.lower() in body.lower()]
    assert not hits, f"the kit names a project: {hits}"


def findings_on_page():
    """Each finding says who acts: the dev (needs-you), the fix loop (agent), a fix in progress or
    an accepted one; the page's decision is written, reaches the next prompt once, and a re-review
    tells fixed from still reported. Behind the same token as every write."""
    import http.client
    import threading
    d = load_dashboard("dash_findings")
    d.FETCH = FetchStub()
    base = os.path.realpath(tempfile.mkdtemp())
    try:
        repo = new_repo(base)
        w = os.path.join(repo, ".work", "F-1")
        reviews = {"contract": "Verdict: PASS\n\n## Findings\n- none\n",
                   "security": "Verdict: PASS\n\n## Findings\n- none\n",
                   "qa": "Verdict: PASS_WITH_FINDINGS\n\n## Findings\n- [important] [route: tester] t.py:3: no test for an empty report\n"
                         "- [minor] [route: executor] e.py:9: header order differs from the spec\n",
                   "product": "Verdict: PASS\n\n## Findings\n- none\n"}
        add_task(repo, "F-1", [(f"04-review-{k}.md", v) for k, v in reviews.items()], current=True)
        task = lambda: next(t for g in d.list_tasks(repo) for t in g["tasks"] if t["id"] == "F-1")
        t = task()
        assert t["status"] == "findings" and [f["state"] for f in t["findings"]] == ["needs-you", "needs-you"], t["findings"]
        assert [f["route"] for f in t["findings"]] == ["tester", "executor"], "each finding names the stage that fixes it"
        assert t["findings_counts"]["needs-you"] == 2 and not t["findings_sent"]
        fids = [f["fid"] for f in t["findings"]]

        code, out = d.decide_findings(repo, {"wt": 0, "id": "F-1", "decisions": [{"fid": fids[0], "decision": "fix"}]})
        assert code == 409 and out["code"] == "undecided", "a finding left undecided refuses the decision"
        code, _ = d.decide_findings(repo, {"wt": 0, "id": "F-1", "decisions": [{"fid": "nope", "decision": "fix"}]})
        assert code == 400, "an id the task does not have is refused"
        code, _ = d.decide_findings(repo, {"wt": 0, "id": "F-1", "decisions": [{"fid": fids[0], "decision": "maybe"}, {"fid": fids[1], "decision": "fix"}]})
        assert code == 400 and not os.path.exists(os.path.join(w, "findings-decision.md")), "nothing is written on a refusal"

        token = "tok-" + "f" * 20
        handler = type("H", (d.Handler,), {"root": repo, "token": token})
        server = d.http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            def post(body, tok=token):
                c = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                data = json.dumps(body).encode()
                c.request("POST", "/api/findings", data, {"Host": f"127.0.0.1:{server.server_port}", "Origin": f"http://127.0.0.1:{server.server_port}",
                                                         "Content-Type": "application/json", "X-Kit-Token": tok})
                r = c.getresponse()
                return r.status, json.loads(r.read() or b"{}")
            body = {"wt": 0, "id": "F-1", "decisions": [{"fid": fids[0], "decision": "fix"},
                                                        {"fid": fids[1], "decision": "accepted", "reason": "the spec\nis wrong"}]}
            assert post(body, "nope")[0] == 403 and not os.path.exists(os.path.join(w, "findings-decision.md")), "a write without the token"
            code, out = post(body)
            assert code == 200 and out == {"ok": True, "fix": 1, "accepted": 1}, out
        finally:
            server.shutdown()
        text = open(os.path.join(w, "findings-decision.md")).read()
        assert text.startswith("Decided: ") and "- fix high qa: t.py:3: no test for an empty report" in text \
            and "- accepted low qa: e.py:9: header order differs from the spec — the spec is wrong" in text, text
        t = task()
        assert [f["state"] for f in t["findings"]] == ["fixing", "accepted"] and t["status"] == "fixing" \
            and t["next_step"] == "wait-fixes" and t["findings_sent"], (t["status"], t["findings"])

        # The next prompt in that worktree gets the decision once, as context.
        e = dict(os.environ, CLAUDE_PROJECT_DIR=repo)
        prompt = lambda: subprocess.run([sys.executable, HOOK], input=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s1"}),
                                        capture_output=True, text=True, env=e).stdout
        ctx = json.loads(prompt())["hookSpecificOutput"]["additionalContext"]
        assert "F-1" in ctx and "do not ask the findings question again" in ctx and "no test for an empty report (route: tester)" in ctx \
            and "the spec is wrong" in ctx, ctx
        assert prompt() == "" and not task()["findings_sent"], "the request is consumed once"

        # Re-review: the fixed one is gone (fixed); a fix still reported is the dev's again.
        time.sleep(0.01)
        qa = os.path.join(w, "04-review-qa.md")
        open(qa, "w").write("Verdict: PASS_WITH_FINDINGS\n\n## Findings\n- [minor] [route: executor] e.py:9: header order differs from the spec\n")
        t = task()
        assert [x["summary"] for x in t["findings_fixed"]] == ["t.py:3: no test for an empty report"], t["findings_fixed"]
        open(qa, "w").write(reviews["qa"])
        t = task()
        assert t["findings"][0]["state"] == "needs-you" and t["findings"][0]["reopened"] and t["status"] == "findings", t["findings"]

        # A FAIL finding is the fix loop's, until the round cap hands it to the dev.
        open(os.path.join(w, "04-review-security.md"), "w").write("Verdict: FAIL\n\n## Findings\n- [critical] [route: executor] a.py:1: token in the log\n")
        sec = lambda: next(f for f in task()["findings"] if f["lens"] == "security")
        assert sec()["state"] == "agent" and sec()["actor"] == "executor", sec()
        with open(os.path.join(repo, ".work", "events.jsonl"), "w") as f:
            for i in range(4):
                f.write(json.dumps({"ev": "start", "agent": f"x{i}", "type": "executor", "t": time.time() - 100 + i, "work": "F-1"}) + "\n")
        assert sec()["state"] == "needs-you" and task()["findings_capped"], "after max_fix_rounds the FAIL is the dev's"
    finally:
        shutil.rmtree(base)


def event_stream():
    """GET /api/events pushes a change within seconds of a new alert (a background tab throttles
    the 2 s poll), sends a heartbeat, keeps the page alive for the hold, refuses a foreign Host,
    caps the streams, and ends its thread when the client goes away."""
    import socket
    import threading
    d = load_dashboard("dash_events")
    d.FETCH = FetchStub()
    d.EVENTS_TICK, d.EVENTS_HEARTBEAT, d.MAX_STREAMS = 0.1, 0.5, 2
    base = os.path.realpath(tempfile.mkdtemp())
    try:
        repo = new_repo(base)
        add_task(repo, "E1")
        adir = os.path.join(repo, ".work", "attention")
        os.makedirs(adir)
        handler = type("H", (d.Handler,), {"root": repo, "token": "t"})
        server = d.http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_port

        def open_stream(host="127.0.0.1", path="/api/events?t=t", origin=None):
            c = socket.create_connection(("127.0.0.1", port), timeout=5)
            extra = f"Origin: {origin}\r\n" if origin else ""
            c.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n{extra}Accept: text/event-stream\r\n\r\n".encode())
            return c

        def read_until(c, needle, seconds):
            buf, end = b"", time.monotonic() + seconds
            c.settimeout(0.2)
            while needle not in buf and time.monotonic() < end:
                try:
                    chunk = c.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
            return buf

        try:
            c = open_stream("evil.example")
            head = read_until(c, b"\r\n\r\n", 3)
            c.close()
            assert head.startswith(b"HTTP/1.0 403"), f"a foreign Host must be refused: {head[:80]}"
            # Another site cannot hold the slots: no token and no localhost Origin is refused.
            for label, path, origin in (("no token", "/api/events", None), ("wrong token", "/api/events?t=x", None),
                                        ("foreign Origin", "/api/events", "http://evil.example")):
                c = open_stream(path=path, origin=origin)
                head = read_until(c, b"\r\n\r\n", 3)
                c.close()
                assert head.startswith(b"HTTP/1.0 403"), f"{label}: {head[:60]}"
            c = open_stream(path="/api/events", origin=f"http://127.0.0.1:{port}")
            assert b"event: hello" in read_until(c, b"event: hello", 3), "a localhost Origin opens the stream"
            c.close()
            time.sleep(0.3)

            d._last_visible[0] = time.time() + 600  # a page in view (page_visibility tests the rule)
            c = open_stream()
            got = read_until(c, b"event: hello", 3)
            assert b" 200 " in got and b"text/event-stream" in got and b"data: {\"seq\": 0}" in got, got
            # The page heartbeat is kept while the stream is open.
            page = os.path.join(adir, ".page")
            got = read_until(c, b": ping", 3)
            assert b": ping" in got, f"a heartbeat comment within the heartbeat period: {got}"
            assert os.path.exists(page) and time.time() - os.path.getmtime(page) < 2, "the stream keeps the page alive"
            os.makedirs(os.path.join(adir, "s1"))
            t = time.monotonic()
            open(os.path.join(adir, "s1", "main.json"), "w").write(json.dumps(
                {"kind": "turn", "since": time.time(), "detail": "", "claude_pid": None}))
            got = read_until(c, b"event: change", 3)
            assert b"event: change" in got and b'"seq": 1' in got, f"a new alert must be pushed: {got}"
            assert time.monotonic() - t < 2, "pushed within two seconds"
            assert repo.encode() not in got and b"main.json" not in got, "events carry no paths"

            # Streams are capped; a closed client frees its slot.
            c2 = open_stream()
            assert b"event: hello" in read_until(c2, b"event: hello", 3)
            c3 = open_stream()
            assert read_until(c3, b"\r\n\r\n", 3).startswith(b"HTTP/1.0 503"), "over MAX_STREAMS: 503"
            c3.close()
            # No write is due now, so only noticing the closed socket can end these streams.
            d.EVENTS_HEARTBEAT = 60
            time.sleep(0.6)
            c.close()
            c2.close()

            def free_slots():
                taken = 0
                while taken < d.MAX_STREAMS and handler.streams.acquire(blocking=False):
                    taken += 1
                for _ in range(taken):
                    handler.streams.release()
                return taken
            for _ in range(30):
                if free_slots() == d.MAX_STREAMS:
                    break
                time.sleep(0.1)
            assert free_slots() == d.MAX_STREAMS, "a stream whose client went away must end and free its slot"
        finally:
            server.shutdown()
            server.server_close()
    finally:
        shutil.rmtree(base)


def turn_holds():
    """One classifier (flow.asks) decides whether a turn asks. Only a turn that asks is held, never
    one whose session has a subagent running (a fresh .running marker; a stale one is ignored), and
    a held turn lets go when a subagent starts. A hold that lets go records why and when. Every tool
    call of a subagent refreshes its marker."""
    sys.path.insert(0, os.path.join(KIT, "hooks"))
    import flow as F
    assert F.asks("Contract written. Go on?") and F.asks("Pick one and let me know.") and F.asks("Responda aqui")
    assert not F.asks("Executor launched; waiting for it to finish.")
    assert not F.asks("Is this right? " + "x" * 700), "only the last 600 characters count"
    root = kit_root()
    try:
        adir = os.path.join(root, ".work", "attention")
        os.makedirs(adir)
        page = os.path.join(adir, ".page")
        open(os.path.join(adir, "turns-on"), "w").close()
        env = dict(os.environ, CLAUDE_PROJECT_DIR=root, KIT_ATTENTION_PID=str(os.getpid()), KIT_ATTENTION_POLL="0.1",
                   KIT_ATTENTION_HOLD="20")
        alert = lambda sid: json.load(open(os.path.join(adir, sid, "main.json")))

        def quick(sid, text):
            open(page, "w").close()
            t0 = time.monotonic()
            p = subprocess.run([sys.executable, ATTN, "turn"], input=json.dumps(
                {"hook_event_name": "Stop", "session_id": sid, "last_assistant_message": text}),
                capture_output=True, text=True, env=env)
            return p.stdout, time.monotonic() - t0

        out, took = quick("i1", "Executor launched; waiting for it to finish.")
        a = alert("i1")
        assert out == "" and took < 5 and a["held"] is False and a["kind"] == "turn" and "released" not in a, \
            f"a turn that asks nothing is not held, its alert still raised: {a}"
        marker = os.path.join(adir, "i2", ".running", "agent-x")
        os.makedirs(os.path.dirname(marker))
        open(marker, "w").close()
        out, took = quick("i2", "Contract written. Go on?")
        assert out == "" and took < 5 and not os.path.exists(os.path.join(adir, "i2", "main.json")), \
            "a session with a running subagent does not raise a turn alert"
        old = time.time() - 20 * 60
        os.utime(marker, (old, old))
        # A subagent's tool call refreshes its marker; the main session's does not create one.
        p = subprocess.run([sys.executable, HOOK], input=json.dumps(
            {"hook_event_name": "PostToolUse", "session_id": "i2", "agent_id": "agent-x", "tool_name": "Read"}),
            capture_output=True, text=True, env=env)
        assert time.time() - os.path.getmtime(marker) < 60, "a subagent's tool call keeps its marker fresh"
        os.utime(marker, (old, old))

        proc = subprocess.Popen([sys.executable, ATTN, "turn"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env)
        open(page, "w").close()
        proc.stdin.write(json.dumps({"hook_event_name": "Stop", "session_id": "i2", "last_assistant_message": "Go on?"}))
        proc.stdin.close()
        for _ in range(100):
            if os.path.exists(os.path.join(adir, "i2", "main.json")) and alert("i2").get("held"):
                break
            os.utime(page)
            time.sleep(0.05)
        assert alert("i2")["held"], "a stale marker (a killed subagent) does not stop the hold"
        os.utime(marker)  # a subagent of the session starts working
        proc.wait(10)
        a = alert("i2")
        assert proc.stdout.read() == "" and a["held"] is False and a["released"] == "agent-running" \
            and isinstance(a.get("released_at"), float), f"a held turn lets go when a subagent starts, and says why: {a}"

        # A question released from the page says so.
        open(os.path.join(adir, "answers-on"), "w").close()
        proc = subprocess.Popen([sys.executable, ATTN, "hold"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env)
        open(page, "w").close()
        proc.stdin.write(json.dumps(ask("q1")))
        proc.stdin.close()
        path = os.path.join(adir, "q1", "main.json")
        for _ in range(100):
            if os.path.exists(path) and json.load(open(path)).get("held"):
                break
            os.utime(page)
            time.sleep(0.05)
        open(path[:-len(".json")] + ".release", "w").close()
        proc.wait(10)
        a = json.load(open(path))
        assert a["held"] is False and a["released"] == "released" and a["released_at"], a
        d = load_dashboard("dash_released")
        got = {x["session"]: x for x in d.alerts(os.path.join(root, ".work"), time.time())}
        assert got["q1"]["released"] == "released" and got["i2"]["released"] == "agent-running" \
            and got["i1"]["released"] is None, "the server says why a hold let go, and nothing for one never held"
        # A hold killed without a word (still marked held, heartbeat stale) reads "stopped".
        json.dump({**{k: v for k, v in a.items() if k not in ("released", "released_at")}, "held": True,
                   "heartbeat": time.time() - 60}, open(path, "w"))
        assert {x["session"]: x for x in d.alerts(os.path.join(root, ".work"), time.time())}["q1"]["released"] == "stopped"
    finally:
        shutil.rmtree(root)


def page_visibility():
    """Only a page in view keeps a hold: a poll from a hidden tab does not mark the page alive, nor
    does the event stream once no page said it is in view for VISIBLE_FOR seconds."""
    d = load_dashboard("dash_visible")
    root = kit_root()
    try:
        adir = os.path.join(root, ".work", "attention")
        os.makedirs(adir)
        page = os.path.join(adir, ".page")
        open(page, "w").close()
        old = time.time() - 120
        os.utime(page, (old, old))
        d._last_visible[0] = 0.0
        d.list_attention(root, visible=False)
        assert os.path.getmtime(page) < time.time() - 60, "a hidden tab's poll does not keep the holds"
        d.touch_pages(root)
        assert os.path.getmtime(page) < time.time() - 60, "nor the event stream, with no page in view"
        d.list_attention(root, visible=True)
        assert os.path.getmtime(page) > time.time() - 5, "a page in view keeps them"
        os.utime(page, (old, old))
        d.touch_pages(root)
        assert os.path.getmtime(page) > time.time() - 5, "the stream keeps them while a page was in view lately"
    finally:
        shutil.rmtree(root)


def alert_text():
    """The page gets a turn's whole last message (its last 8000 characters), masked by the
    server with its own copy of the hooks' mask whatever the worktree's hook wrote; an alert of an
    older hook without the message falls back to the session's transcript."""
    d = load_dashboard("dash_alert_text")
    root = os.path.realpath(kit_root())
    config = tempfile.mkdtemp()
    saved = os.environ.get("CLAUDE_CONFIG_DIR")
    try:
        sdir = os.path.join(root, ".work", "attention", "s-long")
        os.makedirs(sdir)
        long = "x" * 9000 + " PASSWORD=hunter2 card 4111 1111 1111 1111. Shall I go on?"
        json.dump({"kind": "turn", "since": time.time(), "detail": "token=abc123", "message": long},
                  open(os.path.join(sdir, "main.json"), "w"))
        old = os.path.join(root, ".work", "attention", "s-old")
        os.makedirs(old)
        json.dump({"kind": "turn", "since": time.time(), "detail": "Done with the contract."},
                  open(os.path.join(old, "main.json"), "w"))
        key = re.sub(r"[^A-Za-z0-9]", "-", root)
        os.makedirs(os.path.join(config, "projects", key))
        with open(os.path.join(config, "projects", key, "s-old.jsonl"), "w") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Done with the contract. Secret=s3cr3t. Which option, A or B?"}]}}) + "\n")
        os.environ["CLAUDE_CONFIG_DIR"] = config
        got = {a["session"]: a for a in d.list_attention(root)[0]["alerts"]}
        msg = got["s-long"]["message"]
        assert len(msg) <= 8000 and msg.endswith("Shall I go on?") and "hunter2" not in msg and "4111" not in msg, msg[-120:]
        assert "abc123" not in got["s-long"]["detail"], "the detail is masked again by the server"
        assert got["s-old"]["message"].endswith("Which option, A or B?") and "s3cr3t" not in got["s-old"]["message"], \
            f"an older hook's alert reads the transcript's last text: {got['s-old']['message']}"
    finally:
        if saved is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = saved
        shutil.rmtree(root)
        shutil.rmtree(config)


def turn_awaits():
    """A "your turn" waits on the dev (awaits) when held, or when no subagent of its session runs and
    its message's end asks something or it has stood 2 minutes; one that waits only by those 2
    minutes is idle. A question or permission always waits. A running marker counts while it is
    fresh, or while its agent logs tool events (an older hook's marker); a stale one is ignored."""
    d = load_dashboard("dash_awaits")
    root = kit_root()
    try:
        w = os.path.join(root, ".work")
        now = time.time()
        sdir = os.path.join(w, "attention", "s1")
        os.makedirs(sdir)
        base = {"kind": "turn", "session": "s1", "since": now - 10}
        assert d.awaits({**base, "message": "Contract written. Go on?"}, w, now)
        assert not d.awaits({**base, "message": "Executor launched."}, w, now), "a progress note does not wait"
        old = {**base, "since": now - 200, "message": "Executor finished."}
        assert d.awaits(old, w, now) and d.idle_only(old), "two minutes with nothing resuming it: idle"
        assert not d.idle_only({**base, "message": "Pick A or B?"}), "a turn that asks is never idle"
        assert d.awaits({**base, "held": True, "message": "Executor launched."}, w, now), "a held turn always waits"
        assert d.awaits({"kind": "question", "session": "s1", "since": now}, w, now)
        marker = os.path.join(sdir, ".running", "agent-1")
        os.makedirs(os.path.dirname(marker))
        open(marker, "w").close()
        assert not d.awaits({**base, "message": "Go on?"}, w, now), "a fresh running marker: the session works"
        os.utime(marker, (now - 3600, now - 3600))
        assert d.awaits({**base, "message": "Go on?"}, w, now), "a stale marker is ignored"
        open(os.path.join(w, "events.jsonl"), "w").write(json.dumps(
            {"t": now - 30, "work": "T-1", "agent": "agent-1", "ev": "tool", "tool": "Read"}) + "\n")
        assert not d.awaits({**base, "message": "Go on?"}, w, now), "a marker whose agent logs tool events counts"
        os.makedirs(os.path.join(w, "attention", "s2"))
        json.dump({"kind": "turn", "since": now - 300, "detail": "Report written.", "message": "Report written."},
                  open(os.path.join(w, "attention", "s2", "main.json"), "w"))
        got = {a["session"]: a for a in d.list_attention(root)[0]["alerts"]}
        assert got["s2"]["awaits"] and got["s2"]["idle"], got["s2"]
    finally:
        shutil.rmtree(root)


def findings_decided_message():
    """A held turn told that the findings are decided gets the server's own message, never one the
    page built; any other kind or a bad task id is refused and writes nothing."""
    d = load_dashboard("dash_decided_msg")
    root = kit_root()
    try:
        sdir = os.path.join(root, ".work", "attention", "s1")
        os.makedirs(sdir)
        json.dump({"kind": "turn", "since": time.time(), "detail": "x", "qid": "Q1", "held": True, "heartbeat": time.time()},
                  open(os.path.join(sdir, "main.json"), "w"))
        body = {"wt": 0, "session": "s1", "agent": "main", "qid": "Q1"}
        assert d.answer(root, {**body, "kind": "anything", "id": "T-1"})[0] == 400
        assert d.answer(root, {**body, "kind": "findings-decided", "id": "../x"})[0] == 400
        assert not os.path.exists(os.path.join(sdir, "main.answer.json")), "a refused body writes nothing"
        assert d.answer(root, {**body, "kind": "findings-decided", "id": "T-1", "message": "rm -rf /"})[0] == 200
        got = json.load(open(os.path.join(sdir, "main.answer.json")))
        assert got["message"] == d.FINDINGS_DECIDED.format(id="T-1"), got
    finally:
        shutil.rmtree(root)


def triage_task():
    """A linked worktree named after a KEY (or with `work.sh triage <ID>` recorded), a live session
    and no task directory lists one synthetic triage task; the main checkout or another name does
    not; Mark as done refuses it; a dead session lists nothing; the real task replaces it."""
    d = load_dashboard("dash_triage")
    base = tempfile.mkdtemp()
    try:
        repo = new_repo(base)
        wt, odd = os.path.join(base, "abc-12"), os.path.join(base, "spike")
        git_in(repo, "worktree", "add", "-q", "-b", "w-abc-12", wt)
        git_in(repo, "worktree", "add", "-q", "-b", "w-spike", odd)
        now = time.time()

        def session(w, pid):
            adir = os.path.join(w, ".work", "attention", "s1")
            os.makedirs(adir, exist_ok=True)
            open(os.path.join(adir, ".session"), "w").write(json.dumps({"claude_pid": pid, "since": now}))

        listed = lambda: {g["name"]: g["tasks"] for g in d.list_tasks(repo, now)}
        assert listed() == {}, "no session, no task: nothing listed"
        for w in (wt, odd, repo):
            session(w, os.getpid())
        got = listed()
        assert set(got) == {"abc-12"}, got
        [k] = got["abc-12"]
        assert k["id"] == "ABC-12" and k["triage"] and k["status"] == "triage" and k["stages"][0]["agent"] == "triage", k
        assert not any(x["written"] for x in k["stages"]) and not k["closable"] and not k["cleanable"]
        wi = next(i for i, (p, _) in enumerate(d.worktrees(repo)) if os.path.basename(p) == "abc-12")
        assert d.task_detail(repo, wi, "ABC-12", now)["status"] == "triage"
        assert d.close_task(repo, {"wt": wi, "id": "ABC-12"}, now)[0] == 404
        p = subprocess.run(["bash", WORK_SH, "triage", "SPK-7"], capture_output=True, text=True, env=dict(os.environ, CLAUDE_PROJECT_DIR=odd))
        assert p.returncode == 0, p.stderr
        assert [k["id"] for k in listed()["spike"]] == ["SPK-7"], "a recorded key names the triage"
        assert subprocess.run(["bash", WORK_SH, "triage", "bad"], capture_output=True, env=dict(os.environ, CLAUDE_PROJECT_DIR=odd)).returncode != 0
        dead = subprocess.Popen(["true"])
        dead.wait()
        session(wt, dead.pid)
        assert "abc-12" not in listed(), "a dead session lists nothing"
        session(wt, os.getpid())
        add_task(wt, "ABC-12")
        [k] = listed()["abc-12"]
        assert k["status"] != "triage" and "triage" not in k, k
    finally:
        shutil.rmtree(base)


def page_holds():
    """A permission dialog is held while the page is open and answered there, allow or deny; the
    main session's turn is held only with `work.sh ui turns on`, and the page's message continues
    it. The server takes only a held alert's id, a known behavior and a non-empty message."""
    d = load_dashboard("dash_holds")
    root = kit_root()
    try:
        adir = os.path.join(root, ".work", "attention")
        os.makedirs(adir)
        page = os.path.join(adir, ".page")
        env = dict(os.environ, CLAUDE_PROJECT_DIR=root, KIT_ATTENTION_PID=str(os.getpid()), KIT_ATTENTION_POLL="0.1",
                   KIT_ATTENTION_HOLD="20")

        def start(arg, payload):
            open(page, "w").close()
            proc = subprocess.Popen([sys.executable, ATTN, arg], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, env=env)
            proc.stdin.write(json.dumps(payload))
            proc.stdin.close()
            path = os.path.join(adir, payload["session_id"], "main.json")
            for _ in range(100):
                a = json.load(open(path)) if os.path.exists(path) else {}
                if a.get("held"):
                    return proc, a
                os.utime(page)
                time.sleep(0.05)
            raise AssertionError(f"{arg} was not held: {a}")

        def finish(proc):
            out = proc.stdout.read()
            proc.wait(10)
            return out

        perm = {"hook_event_name": "PermissionRequest", "session_id": "p1", "tool_name": "Bash", "tool_input": {"command": "rm -rf build"}}
        for behavior in ("allow", "deny"):
            proc, a = start("permission", perm)
            assert a["kind"] == "permission" and "rm -rf build" in a["detail"], a
            assert d.answer(root, {"wt": 0, "session": "p1", "agent": "main", "qid": a["qid"], "behavior": "maybe"})[0] == 400
            assert d.answer(root, {"wt": 0, "session": "p1", "agent": "main", "qid": "nope", "behavior": "allow"})[0] == 409
            assert d.answer(root, {"wt": 0, "session": "p1", "agent": "main", "qid": a["qid"], "behavior": behavior})[0] == 200
            out = json.loads(finish(proc))["hookSpecificOutput"]
            assert out["hookEventName"] == "PermissionRequest" and out["decision"]["behavior"] == behavior, out

        # Released from the page: the dialog goes back to the terminal, nothing printed.
        proc, a = start("permission", perm)
        assert d.answer(root, {"wt": 0, "session": "p1", "agent": "main", "qid": a["qid"], "release": True})[0] == 200
        assert finish(proc) == "", "a released permission prints no decision"

        stop = {"hook_event_name": "Stop", "session_id": "t1", "last_assistant_message": "Contract written. Go on?"}
        t0 = time.monotonic()
        p = subprocess.run([sys.executable, ATTN, "turn"], input=json.dumps(stop), capture_output=True, text=True, env=env)
        assert p.stdout == "" and time.monotonic() - t0 < 5, "turns off: the Stop is not held"
        open(os.path.join(adir, "turns-on"), "w").close()
        proc, a = start("turn", stop)
        assert a["kind"] == "turn" and "Go on?" in a["detail"]
        assert d.answer(root, {"wt": 0, "session": "t1", "agent": "main", "qid": a["qid"], "message": "  "})[0] == 400
        assert d.answer(root, {"wt": 0, "session": "t1", "agent": "main", "qid": a["qid"], "message": "yes, go on"})[0] == 200
        out = json.loads(finish(proc))
        assert out["decision"] == "block" and out["reason"].endswith("yes, go on"), out
        assert not os.path.exists(os.path.join(adir, "t1", "main.json")), "an answered turn clears its alert"
        # A turn that ends while a background subagent runs waits for that subagent, not the dev:
        # a hold would keep its "done" notice from waking the session, and the flow would stall.
        os.makedirs(os.path.join(adir, "t1", ".running"), exist_ok=True)
        open(os.path.join(adir, "t1", ".running", "ag1"), "w").close()
        t0 = time.monotonic()
        p = subprocess.run([sys.executable, ATTN, "turn"], input=json.dumps(stop), capture_output=True, text=True, env=env)
        assert p.stdout == "" and time.monotonic() - t0 < 5, "a Stop with a subagent running must not be held"
        assert not os.path.exists(os.path.join(adir, "t1", "main.json")), "nor raise a turn alert"
        shutil.rmtree(os.path.join(adir, "t1", ".running"))
        # A subagent's stop is never held.
        p = subprocess.run([sys.executable, ATTN, "turn"], input=json.dumps({**stop, "agent_id": "a9"}), capture_output=True, text=True, env=env)
        assert p.stdout == ""

        # A decision taken on the page reaches the session at once. Working: after the main
        # session's next tool call (never a subagent's, whose context is not the session's).
        os.remove(os.path.join(adir, "turns-on"))
        rdir = os.path.join(adir, ".requests")
        os.makedirs(rdir, exist_ok=True)
        req = {"id": "F-1", "since": time.time(), "fix": [{"lens": "qa", "severity": "high", "summary": "no empty-report test", "route": "tester"}],
               "accepted": [], "open": 1}
        open(os.path.join(rdir, "findings-F-1.json"), "w").write(json.dumps(req))
        post_tool = {"hook_event_name": "PostToolUse", "session_id": "w1", "tool_name": "Read"}
        p = subprocess.run([sys.executable, HOOK], input=json.dumps({**post_tool, "agent_id": "a3"}), capture_output=True, text=True, env=env)
        assert p.stdout == "" and os.path.exists(os.path.join(rdir, "findings-F-1.json")), "a subagent's tool call does not take it"
        p = subprocess.run([sys.executable, HOOK], input=json.dumps(post_tool), capture_output=True, text=True, env=env)
        ctx = json.loads(p.stdout)["hookSpecificOutput"]
        assert ctx["hookEventName"] == "PostToolUse" and "no empty-report test (route: tester)" in ctx["additionalContext"] \
            and "Still open on the page: 1" in ctx["additionalContext"], ctx
        assert not os.listdir(rdir), "delivered once"

        # Stopped for the findings decision: the Stop is held while the page is open, even with
        # turns off, and the decision continues the session the moment it is taken.
        tdir = os.path.join(root, ".work", "F-1")
        os.makedirs(tdir)
        open(os.path.join(tdir, "00-purpose.md"), "w").write("# F-1\n")
        open(os.path.join(tdir, "04-review-qa.md"), "w").write("Verdict: PASS_WITH_FINDINGS\n\n## Findings\n- [important] x\n")
        open(os.path.join(root, ".work", "current"), "w").write("F-1")
        proc, a = start("turn", {**stop, "session_id": "t2"})
        assert a.get("waits_for") == "findings", a
        open(os.path.join(rdir, "findings-F-1.json"), "w").write(json.dumps(req))
        out = json.loads(finish(proc))
        assert out["decision"] == "block" and "findings-decision.md" in out["reason"] and "route: tester" in out["reason"], out
        open(os.path.join(tdir, "findings-decision.md"), "w").write("Decided: 2026-11-16\n- fix high qa: x\n")
        t0 = time.monotonic()
        p = subprocess.run([sys.executable, ATTN, "turn"], input=json.dumps({**stop, "session_id": "t3"}), capture_output=True, text=True, env=env)
        assert p.stdout == "" and time.monotonic() - t0 < 5, "decided findings and turns off: the Stop is not held"
    finally:
        shutil.rmtree(root)


def language_hook():
    """A session starts told the dev's language from flow.json; English says nothing."""
    root = kit_root()
    try:
        run = lambda: subprocess.run([sys.executable, DASH, "hook", root], capture_output=True, text=True).stdout
        assert "reads" not in run(), "English is the default: no line"
        path = os.path.join(root, ".claude", "flow.json")
        flow = json.load(open(path))
        open(path, "w").write(json.dumps({**flow, "language": "pt-BR"}))
        out = run()
        assert "Brazilian Portuguese" in out and "Verdict:" in out and "[route: ...]" in out, out
    finally:
        shutil.rmtree(root)


def rooms_and_agents():
    """Every stage's agent has a desk in a room, every room's agent is a stage or the main
    session, and an agent added as a review lens shows up as a stage of the task with its
    findings, with no other change."""
    flow = json.load(open(FLOW))
    seated = [a for r in flow["rooms"] for a in r["agents"]]
    assert sorted(set(seated)) == sorted(seated), "an agent sits in two rooms"
    assert set(flow["stages"]) <= set(seated), f"no desk for {set(flow['stages']) - set(seated)}"
    assert set(seated) - set(flow["stages"]) == {"main"}, "a room seats an agent that is not a stage"
    for r in flow["rooms"]:
        assert r["id"] and r["name"]["en"] and r["name"]["pt"], r
    d = load_dashboard("dash_rooms")
    d.FETCH = FetchStub()
    base = os.path.realpath(tempfile.mkdtemp())
    try:
        repo = new_repo(base)
        custom = dict(flow)
        custom["stages"] = {**flow["stages"], "perf-reviewer": {"file": "04-review-perf.md", "verdict": True,
                            "requires": ["refiner", "executor", "tester"], "sections": ["Findings"]}}
        custom["rooms"] = flow["rooms"] + [{"id": "lab", "name": {"en": "Performance lab", "pt": "Laboratório"}, "agents": ["perf-reviewer"]}]
        custom["agents"] = {"perf-reviewer": {"label": {"en": "performance reviewer", "pt": "revisor de desempenho"}, "color": "#ff9900", "emblem": "gauge"}}
        open(os.path.join(repo, ".claude", "flow.json"), "w").write(json.dumps(custom))
        add_task(repo, "P-1", [("04-review-perf.md", "Verdict: PASS_WITH_FINDINGS\n\n## Findings\n- [high] [route: executor] q.py:9: N+1 query on the report\n")], current=True)
        t = next(t for g in d.list_tasks(repo) for t in g["tasks"] if t["id"] == "P-1")
        assert [f["lens"] for f in t["findings"]] == ["perf"] and t["findings"][0]["view"] == "needs-you", t["findings"]
        assert any(s["agent"] == "perf-reviewer" and s["needs_verdict"] for s in d.task_detail(repo, 0, "P-1")["stages"])
    finally:
        shutil.rmtree(base)


def switches():
    """The page turns a worktree's switches on and off, behind the token like every write: the
    same files the terminal command writes, and the task list says their state."""
    import http.client
    import threading
    d = load_dashboard("dash_switch")
    d.FETCH = FetchStub()
    base = os.path.realpath(tempfile.mkdtemp())
    try:
        repo = new_repo(base)
        add_task(repo, "S-1", current=True)
        token = "tok-" + "s" * 20
        server = d.http.server.ThreadingHTTPServer(("127.0.0.1", 0), type("H", (d.Handler,), {"root": repo, "token": token}))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            def post(body, tok=token):
                c = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                h = {"Host": f"127.0.0.1:{server.server_port}", "Origin": f"http://127.0.0.1:{server.server_port}",
                     "Content-Type": "application/json", "X-Kit-Token": tok}
                c.request("POST", "/api/switch", json.dumps(body).encode(), h)
                return c.getresponse().status
            flag = lambda name: os.path.exists(os.path.join(repo, ".work", "attention", name))
            state = lambda: next(g for g in d.list_tasks(repo) if g["wt"] == 0)["switches"]
            assert state() == {"answers": False, "turns": False}
            assert post({"wt": 0, "name": "answers", "on": True}, "nope") == 403 and not flag("answers-on"), "a write without the token"
            assert post({"wt": 0, "name": "everything", "on": True}) == 400 and post({"wt": 0, "name": "turns", "on": "yes"}) == 400
            assert post({"wt": 9, "name": "turns", "on": True}) == 404
            assert post({"wt": 0, "name": "turns", "on": True}) == 200 and flag("turns-on") and state()["turns"]
            assert post({"wt": 0, "name": "answers", "on": True}) == 200 and flag("answers-on")
            assert post({"wt": 0, "name": "turns", "on": False}) == 200 and not flag("turns-on") and state() == {"answers": True, "turns": False}
            assert post({"wt": 0, "name": "turns", "on": False}) == 200, "turning off twice is fine"
        finally:
            server.shutdown()
    finally:
        shutil.rmtree(base)


def findings_decision_endpoint():
    """Each finding reads open, fix-pending, fixed or accepted; POST /api/findings-decision
    decides one open finding by its key, behind the write checks, keeping every other line of
    findings-decision.md and dating its header."""
    import http.client
    import threading
    d = load_dashboard("dash_fd")
    d.FETCH = FetchStub()
    base = os.path.realpath(tempfile.mkdtemp())
    try:
        repo = new_repo(base)
        add_task(repo, "F1", [
            ("04-review-contract.md", "Verdict: PASS_WITH_FINDINGS\n\n## Findings\n- [low] first thing\n- [low] second thing\n"),
            ("04-review-security.md", "Verdict: PASS_WITH_FINDINGS\n\n## Findings\n- [medium] third thing\n"),
            ("04-review-qa.md", "Verdict: PASS\n"), ("04-review-product.md", "Verdict: PASS\n")])
        tdir = os.path.join(repo, ".work", "F1")
        path = os.path.join(tdir, "findings-decision.md")
        flow = json.load(open(FLOW))
        items = lambda: d.decided_findings(tdir, d.stages(flow, tdir))
        first, second, third = items()
        assert [f["state"] for f in (first, second, third)] == ["open"] * 3 and len({first["key"], second["key"], third["key"]}) == 3
        task = d.task_detail(repo, 0, "F1")
        assert [f["key"] for f in task["findings"]] == [first["key"], second["key"], third["key"]] and task["status"] == "findings"
        # A decision the orchestrator wrote before: it stays, by position for its (lens, severity).
        open(path, "w").write("Decided: 2026-01-01\n- fix medium security: the orchestrator's own words\n")
        assert [f["state"] for f in items()] == ["open", "open", "fix-pending"]

        token = "tok-" + "f" * 20
        handler = type("H", (d.Handler,), {"root": repo, "token": token})
        server = d.http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_port

        def call(method, body=None, headers=None):
            h = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}",
                 "Content-Type": "application/json", "X-Kit-Token": token, **(headers or {})}
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            data = json.dumps(body).encode() if body is not None else None
            c.putrequest(method, "/api/findings-decision", skip_host=True)
            for k, v in h.items():
                c.putheader(k, v)
            if data is not None:
                c.putheader("Content-Length", str(len(data)))
            c.endheaders(data)
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"{}") if r.getheader("Content-Type", "").startswith("application/json") else {}
        try:
            good = {"wt": 0, "id": "F1", "key": second["key"], "decision": "accepted"}
            before = open(path).read()
            for label, body, headers, want in (
                    ("wrong token", good, {"X-Kit-Token": "nope"}, 403),
                    ("foreign Origin", good, {"Origin": "http://evil.example"}, 403),
                    ("foreign Host", good, {"Host": "evil.example"}, 403),
                    ("text/plain", good, {"Content-Type": "text/plain"}, 415),
                    ("bad decision", {**good, "decision": "ignore"}, None, 400),
                    ("unknown task", {**good, "id": "NOPE"}, None, 404),
                    ("unknown key", {**good, "key": "0" * 12}, None, 404),
                    ("decided finding", {**good, "key": third["key"]}, None, 409)):
                code, out = call("POST", body, headers)
                assert code == want and open(path).read() == before, f"{label}: {code} {out}"
            assert call("GET")[0] == 405 and open(path).read() == before
            code, out = call("POST", good)
            assert code == 200 and out == {"ok": True, "state": "accepted"}, out
            text = open(path).read().splitlines()
            assert text[0] == "Decided: " + time.strftime("%Y-%m-%d", time.gmtime()) \
                and text[1] == f"- fix medium security [{third['key']}]: the orchestrator's own words" \
                and text[2] == f"- accepted low contract [{second['key']}]: second thing", \
                f"the page writes a keyed line and pins the keyless line it kept: {text}"
            assert [f["state"] for f in items()] == ["open", "accepted", "fix-pending"], \
                "the line carrying the finding's summary names that finding, not the first of its lens and severity"
            assert call("POST", good)[0] == 409, "a finding decided once is not open"
            code, out = call("POST", {**good, "key": first["key"], "decision": "fix"})
            assert code == 200 and out["state"] == "fix-pending"
            # Foreman: with fixes asked and no review since, the task is being fixed, not ready for a PR.
            assert d.task_detail(repo, 0, "F1")["status"] == "fixing", "nothing open, fixes pending: fixing"
        finally:
            server.shutdown()
        # The fix round ends: the executor turns "fix" into "fixed" on the lines it fixed.
        text = open(path).read().replace("- fix low contract [", "- fixed low contract [")
        text = text.replace("- fix medium security [", "- fixed medium security [")
        open(path, "w").write(text)
        assert [f["state"] for f in items()] == ["fixed", "accepted", "fixed"], items()
        assert [f["decision"] for f in items()] == ["fix", "accepted", "fix"], "decision stays fix for a fixed finding"
        assert d.task_detail(repo, 0, "F1")["status"] == "ready-for-mr"
        assert set(d.FINDING_STATES) == {"open", "fix-pending", "fixed", "accepted"}
    finally:
        shutil.rmtree(base)


def decision_keys():
    """A decision belongs to one finding, by key: a lens re-run that words its findings anew never
    inherits the old round's decisions; a keyless (older) line decides only where it cannot be
    wrong. work.sh findings prints the keys."""
    d = load_dashboard("dash_keys")
    root = kit_root()
    try:
        tdir = os.path.join(root, ".work", "K1")
        os.makedirs(tdir)
        open(os.path.join(tdir, "00-purpose.md"), "w").write("# K1\n")
        flow = json.load(open(FLOW))
        t0 = 1_700_000_000 * 10**9

        def put(name, text, t):
            open(os.path.join(tdir, name), "w").write(text)
            os.utime(os.path.join(tdir, name), ns=(t, t))
        review = lambda *items: "Verdict: PASS_WITH_FINDINGS\n\n## Findings\n" + "".join(f"- [low] {i}\n" for i in items)
        for lens in ("contract", "security", "product"):
            put(f"04-review-{lens}.md", "Verdict: PASS\n", t0)
        put("04-review-qa.md", review("empty cleanup is not tested"), t0)
        states = lambda: [(f["summary"], f["state"]) for f in d.decided_findings(tdir, d.stages(flow, tdir))]
        # An older file: one keyless line in the orchestrator's own words, newer than the review.
        put("findings-decision.md", "Decided: 2026-11-16\n- fix low qa: empty sets\n", t0 + 10**9)
        assert states() == [("empty cleanup is not tested", "fix-pending")], "a 1:1 keyless line still decides"
        # The lens re-runs with a different finding: the old line must not decide it.
        put("04-review-qa.md", review("A10 misses the commit case"), t0 + 2 * 10**9)
        assert states() == [("A10 misses the commit case", "open")], states()
        # Two findings, one keyless line: ambiguous, so neither is decided.
        put("04-review-qa.md", review("one", "two"), t0)
        assert [s_ for _, s_ in states()] == ["open", "open"], states()
        # A keyless line with the finding's own summary decides that finding, whatever the order.
        put("findings-decision.md", "Decided: 2026-11-16\n- accepted low qa: two\n", t0 + 10**9)
        assert states() == [("one", "open"), ("two", "accepted")], states()
        # Keyed lines: a key decides its finding only, and survives a re-run that keeps the words.
        one, two = d.decided_findings(tdir, d.stages(flow, tdir))
        put("findings-decision.md", f"Decided: 2026-11-16\n- fix low qa [{one['key']}]: whatever\n"
            f"- fix low qa: an old keyless line\n", t0 + 10**9)
        assert states() == [("one", "fix-pending"), ("two", "open")], \
            "with keyed lines, a keyless line matches only its exact summary"
        put("04-review-security.md", review("token in history"), t0)
        put("findings-decision.md", f"Decided: 2026-11-16\n- fix low qa [{one['key']}]: whatever\n"
            f"- fix low security: an old keyless line\n", t0 + 10**9)
        assert ("token in history", "open") in states(), \
            "with keyed lines, even a 1:1 keyless line no longer decides by position"
        put("04-review-security.md", "Verdict: PASS\n", t0)
        put("04-review-qa.md", review("one", "three"), t0 + 2 * 10**9)
        put("findings-decision.md", open(os.path.join(tdir, "findings-decision.md")).read(), t0 + 3 * 10**9)
        assert states() == [("one", "fix-pending"), ("three", "open")], \
            "a re-run keeps the decision of an unchanged finding and none lands on the new one"
        assert one["key"] == d.finding_key({"lens": "QA", "summary": " one. "}), "the key ignores case, spaces, a final dot"
        p = subprocess.run(["bash", os.path.join(KIT, "scripts", "work.sh"), "findings", "K1"], capture_output=True,
                           text=True, env=dict(os.environ, CLAUDE_PROJECT_DIR=root))
        assert p.returncode == 0 and f"fix-pending - <fix|accepted> low qa [{one['key']}]: one" in p.stdout, p.stdout + p.stderr
    finally:
        shutil.rmtree(root)


def closing_git():
    """Without a forge, git closes a task whose branch came into the base through a merge
    commit; a branch just cut from the base does not. The SessionStart hook names done tasks,
    with no forge call, and prints nothing when there are none."""
    d = load_dashboard("dash_git")
    base = os.path.realpath(tempfile.mkdtemp())
    try:
        remote = os.path.join(base, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "develop", remote], check=True, capture_output=True)
        repo = new_repo(base, origin=remote)
        git_in(repo, "push", "-q", "-u", "origin", "develop")
        git_in(repo, "remote", "set-head", "origin", "develop")
        merged, fresh = os.path.join(base, "merged"), os.path.join(base, "fresh")
        git_in(repo, "worktree", "add", "-q", "-b", "feat/m", merged)
        open(os.path.join(merged, "m.txt"), "w").write("m")
        git_in(merged, "add", ".")
        git_in(merged, "commit", "-qm", "m")
        git_in(repo, "merge", "-q", "--no-ff", "-m", "merge feat/m", "feat/m")
        git_in(repo, "push", "-q", "origin", "develop")
        git_in(repo, "worktree", "add", "-q", "-b", "feat/f", fresh)
        add_task(merged, "G-1", current=True)
        add_task(fresh, "G-2", current=True)
        d.FETCH = FetchStub(error=lambda u, h: AssertionError("no forge call"))
        flow = dict(json.load(open(FLOW)), forge="none")
        for wt in (merged, fresh):
            open(os.path.join(wt, ".claude", "flow.json"), "w").write(json.dumps(flow))
        got = {t["id"]: t for g in d.list_tasks(repo) for t in g["tasks"]}
        assert got["G-1"]["status"] == "done" and got["G-1"]["mr"] == {"iid": None, "state": "merged", "web_url": None}, got["G-1"]
        assert got["G-2"]["status"] == "purpose", f"a branch just cut from the base is not merged: {got['G-2']}"
        # The hook's lookup is git only: a reachable forge is never asked at a session start.
        d._mr_cache.clear()
        open(os.path.join(merged, ".claude", "flow.json"), "w").write(json.dumps(dict(flow, forge="gitlab")))
        os.environ["GITLAB_TOKEN"] = "tok-git"
        os.environ["FORGE_HOST"] = "gitlab.example"
        git_in(repo, "remote", "set-url", "origin", "https://gitlab.example/g/p.git")
        try:
            d.FETCH = stub = FetchStub()
            assert [t["status"] for t in d.worktree_tasks(merged, "feat/m", time.time(), lookup=False)] == ["done"]
            assert stub.calls == [], f"network=False asked the forge: {stub.calls}"
        finally:
            os.environ.pop("GITLAB_TOKEN", None)
            os.environ.pop("FORGE_HOST", None)
            git_in(repo, "remote", "set-url", "origin", remote)
        open(os.path.join(merged, ".claude", "flow.json"), "w").write(json.dumps(flow))
        p = subprocess.run([sys.executable, DASH, "hook", repo], capture_output=True, text=True)
        assert p.returncode == 0 and "G-1 (merged)" in p.stdout and "G-2" not in p.stdout, p.stdout + p.stderr
        # Learnings first: the plan says when the retrospective has not run.
        wi = next(i for i, (p, _) in enumerate(d.worktrees(repo)) if os.path.basename(p) == "merged")
        assert d.cleanup_plan(repo, [(wi, "G-1")])["targets"][0]["retro"] is False
        open(os.path.join(merged, ".work", "G-1", "05-retro.md"), "w").write("x\n")
        assert d.cleanup_plan(repo, [(wi, "G-1")])["targets"][0]["retro"] is True
        shutil.rmtree(os.path.join(merged, ".work", "G-1"))
        p = subprocess.run([sys.executable, DASH, "hook", repo], capture_output=True, text=True)
        assert p.returncode == 0 and p.stdout == "", f"nothing done, nothing printed: {p.stdout!r}"
    finally:
        shutil.rmtree(base)


if __name__ == "__main__":
    main()
