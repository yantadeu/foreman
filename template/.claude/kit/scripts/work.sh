#!/usr/bin/env bash
# Work directory of Foreman (.claude/kit/PROTOCOL.md).
#
# Usage: work.sh init <ID> ["title"]   creates .work/<ID>/ (purpose, ledger with the base commit), marks it current
#        work.sh triage <ID>           records the task's key while the triage runs (before init), so the
#                                      dashboard shows the worktree as that task in triage
#        work.sh status                every stage: gate, status or verdict, open clarifications, what is next
#        work.sh review-package        writes .work/<ID>/review.diff (commits and diff since the base) for the reviewers
#        work.sh done [ID]             closes the task (current one by default): writes <ID>/closed, which
#                                      makes it "done" without a PR/MR, and unmarks it if current
#        work.sh findings <ID>         each review finding with its key and state, for the keyed
#                                      lines of findings-decision.md
#        work.sh cleanup <ID>|--all-done [--state] [--worktree] [--memory]
#                                      prints what the cleanup of done tasks deletes (the dashboard's
#                                      plan); in a terminal, deletes it after "yes"
#        work.sh ui start | stop       dashboard in the background (first free port from 8787), or stop it;
#                                      a dashboard already running serves this project too: start
#                                      registers it there and prints its link instead
#        work.sh ui [port]             dashboard in the foreground (default 8787)
#        work.sh ui turns [on|off]     lets the dashboard send this worktree's main session its next
#                                      message when it finishes a turn (held while the page is open)
#        work.sh ui answers [on|off]   lets the dashboard answer this worktree's AskUserQuestion
#                                      (held up to an hour while the page is open and in view); no argument
#                                      prints the state. Default off: alerts only.
# Exit codes: 0 ok, 1 a check failed, 2 configuration or usage error.
set -euo pipefail

root="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"
hook="$(dirname "$0")/../hooks/flow.py"
work=$(CLAUDE_PROJECT_DIR="$root" python3 "$hook" work-dir)
mkdir -p "$work"

case "${1:-status}" in
  init)
    id="${2:?usage: work.sh init <ID> [title]}"
    # The hook ignores any other ID, so a bad one would silently turn the gate off.
    [[ "$id" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "ID must match [A-Za-z0-9._-]+ (got: $id)" >&2; exit 1; }
    mkdir -p "$work/$id"
    if [ ! -f "$work/$id/00-purpose.md" ]; then
      cat > "$work/$id/00-purpose.md" <<EOF
# $id ${3:-}

Lane: Light
<!-- None | Light | Full, as the dev confirmed in the triage. -->

## Purpose
<!-- One sentence: what the user gets when this is done. -->

## Acceptance criteria
<!-- Copied from the task, not summarized, one per line with a stable id that every stage cites:
- AC-001: ...
- AC-002: ...
Full lane: write each one as Given / When / Then. -->

## Edge cases
<!-- Full lane: the boundaries and failure cases the criteria must hold for. -->

## Decisions from the interview
<!-- What the dev decided out loud, with the why. -->

## Clarifications
<!-- Filled by the clarification loop: ### Session YYYY-MM-DD, then "- Q: ... -> A: ..." -->

## Out of scope
EOF
    fi
    if [ ! -f "$work/$id/ledger.md" ]; then
      base=$(git -C "$root" rev-parse HEAD 2>/dev/null || echo none)
      printf '# Ledger: %s\n\nBase: %s\n\n<!-- One line per decision taken without asking the dev:\n- Ruling: <decision> | why: <reason> | cost if wrong: <impact> -->\n' "$id" "$base" > "$work/$id/ledger.md"
    fi
    # The branch the task starts on is the task's: a later branch switch in this worktree (the
    # next part of a split story) must not move the finished task back to ready-for-mr.
    if [ ! -f "$work/$id/branch" ]; then
      git -C "$root" rev-parse --abbrev-ref HEAD 2>/dev/null > "$work/$id/branch" || rm -f "$work/$id/branch"
    fi
    echo "$id" > "$work/current"
    echo "$work/$id/00-purpose.md"
    ;;
  triage)
    id="${2:?usage: work.sh triage <ID>}"
    [[ "$id" =~ ^[A-Z][A-Z0-9]*-[0-9]+$ ]] || { echo "the key must look like ABC-12 (got: $id)" >&2; exit 1; }
    mkdir -p "$work/attention" && echo "$id" > "$work/attention/.task-key"
    ;;
  status)
    id=$(cat "$work/current" 2>/dev/null || true)
    [ -n "$id" ] || { echo "no current work"; exit 0; }
    echo "current: $id"
    # The hook lets a stage through on its second stop to avoid a loop: this is the real gate.
    CLAUDE_PROJECT_DIR="$root" exec python3 "$hook" status "$id"
    ;;
  review-package)
    id=$(cat "$work/current" 2>/dev/null) || { echo "no current work" >&2; exit 2; }
    base=$(sed -n 's/^Base: //p' "$work/$id/ledger.md")
    [ -n "$base" ] && [ "$base" != none ] || { echo "no base commit in $work/$id/ledger.md" >&2; exit 2; }
    # One file every reviewer reads: the same diff for all four lenses, in one call each.
    { echo "# Review package: $id, base $base"; echo; git -C "$root" log --oneline "$base..HEAD"; echo
      git -C "$root" diff --stat "$base"; echo; git -C "$root" diff -U10 "$base"
      # New files the executor has not added to git yet: without this they never reach the review.
      git -C "$root" ls-files --others --exclude-standard -z |
        while IFS= read -r -d '' f; do git -C "$root" diff --no-index -U10 /dev/null "$f" || true; done
    } > "$work/$id/review.diff"
    echo "$work/$id/review.diff"
    ;;
  done)
    cur=$(cat "$work/current" 2>/dev/null || true)
    id="${2:-$cur}"
    [ -n "$id" ] || { echo "no current work: work.sh done <ID>" >&2; exit 1; }
    # Same id rule as the dashboard; an unknown task writes nothing.
    if ! [[ "$id" =~ ^[A-Za-z0-9._-]+$ ]] || [ ! -f "$work/$id/00-purpose.md" ]; then
      echo "no task $id in $work" >&2; exit 1
    fi
    [ -f "$work/$id/05-retro.md" ] || echo "note: no retrospective for $id (task skill, step 7)." >&2
    date -u +%Y-%m-%dT%H:%M:%SZ > "$work/$id/closed"
    [ "$cur" = "$id" ] && rm -f "$work/current"
    echo "$id closed; evidence stays in $work/$id (work.sh cleanup $id removes it)"
    ;;
  findings)
    exec python3 "$(dirname "$0")/dashboard.py" findings "$root" "${2:?usage: work.sh findings <ID>}"
    ;;
  cleanup)
    shift
    [ $# -ge 1 ] || { echo "usage: work.sh cleanup <ID>|--all-done [--state] [--worktree] [--memory]" >&2; exit 1; }
    exec python3 "$(dirname "$0")/dashboard.py" cleanup "$root" "$@"
    ;;
  ui)
    pidf="$work/ui.pid"
    case "${2:-}" in
      start)
        # Background server, so neither the terminal nor the agent's shell stays blocked.
        if [ -f "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null; then cat "$work/ui.url"; exit 0; fi
        # One dashboard serves every project: a running one (it answers /api/projects) gets this
        # project in its registry, and no second server starts.
        python3 "$(dirname "$0")/dashboard.py" register "$root" >/dev/null 2>&1 || true
        for p in $(seq 8787 8799); do
          if curl -fs -m 1 -o /dev/null "http://127.0.0.1:$p/api/projects" 2>/dev/null; then
            name=$(basename "$root")
            echo "http://127.0.0.1:$p/?project=$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "$name")"
            exit 0
          fi
        done
        # First free port from 8787. One dashboard already lists every worktree's tasks; a
        # second one started from another worktree is the same page on another port.
        port=$(python3 -c 'import socket,sys
for p in range(8787, 8800):
    s = socket.socket()
    # As the server binds (allow_reuse_address): a port left in TIME_WAIT by the last page is
    # free. On macOS that bind also succeeds next to a listener on 0.0.0.0, so a port that accepts
    # a connection is busy; checked before the bind, which would take the connection to itself.
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        c = socket.socket(); c.settimeout(0.3)
        busy = c.connect_ex(("127.0.0.1", p)) == 0; c.close()
        if busy:
            continue
        s.bind(("127.0.0.1", p)); print(p); sys.exit(0)
    except OSError:
        pass
    finally:
        s.close()
sys.exit(1)') || { echo "no free port in 8787-8799" >&2; exit 2; }
        nohup bash "$0" ui "$port" > "$work/ui.log" 2>&1 &
        echo $! > "$pidf"
        echo "http://127.0.0.1:$port" > "$work/ui.url"
        for _ in $(seq 1 25); do
          curl -fs -o /dev/null "http://127.0.0.1:$port/" 2>/dev/null && break
          sleep 0.2
        done
        cat "$work/ui.url"
        ;;
      stop)
        [ -f "$pidf" ] && kill "$(cat "$pidf")" 2>/dev/null || true
        rm -f "$pidf" "$work/ui.url"
        echo "dashboard stopped"
        ;;
      turns)
        # Per worktree, like answers: the Stop hold reads the flag from its own work dir.
        att="$work/attention"
        case "${3:-}" in
          on) mkdir -p "$att" && touch "$att/turns-on" && echo "messages from the dashboard on for $root" ;;
          off) rm -f "$att/turns-on" && echo "messages from the dashboard off for $root" ;;
          "") if [ -f "$att/turns-on" ]; then echo on; else echo off; fi ;;
          *) echo "usage: work.sh ui turns [on|off]" >&2; exit 2 ;;
        esac
        ;;
      answers)
        # Per worktree, because .work is: the hook reads the flag from its own work dir.
        att="$work/attention"
        case "${3:-}" in
          on) mkdir -p "$att" && touch "$att/answers-on" && echo "web answers on for $root" ;;
          off) rm -f "$att/answers-on" && echo "web answers off for $root" ;;
          "") if [ -f "$att/answers-on" ]; then echo on; else echo off; fi ;;
          *) echo "usage: work.sh ui answers [on|off]" >&2; exit 2 ;;
        esac
        ;;
      *)
        port="${2:-8787}"
        echo "http://127.0.0.1:$port"
        # Lists the tasks of every worktree of the repository, not only this one's.
        exec python3 "$(dirname "$0")/dashboard.py" "$root" "$port"
        ;;
    esac
    ;;
  *) sed -n 2,24p "$0"; exit 2 ;;
esac
