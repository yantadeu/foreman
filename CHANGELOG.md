# Changelog

## Unreleased

- One dashboard for every project. Projects register themselves in `~/.foreman/projects.json`
  (`$FOREMAN_HOME`): on a session start, when a dashboard starts, and on `work.sh ui start`, which
  now reuses a dashboard already running instead of starting a second one. A project picker in
  the top bar switches the whole page; each request names its project by index (`?p=`), never
  by a path. New `GET /api/projects` lists the projects with their alert counts. A project not
  on screen chimes and notifies when it starts waiting on you. Only the project on screen holds
  requests for the page.
- The office fills the window: no page scroll, the map takes the height left over, the chat
  sits under it, and "Waiting on you" moves to the side panel, which scrolls on its own.
- Fixed: a click on a project in the picker opened a task instead.

- A new page layout. The left sidebar is gone; the top bar holds the logo, a task picker (with
  the count of what waits on you) and the controls. The world takes the middle of the page; the
  task (Now, the steps, the decisions, the events) and the crew are tabs of a panel beside it.
- The chat with the main session is always open under the world. When the session holds its
  turn for the page, the message goes at once. While it works, the message waits in a queue and
  goes when its turn comes. With no session open, the chat writes the whole
  `cd <worktree> && claude "<message>"` command for a terminal; the page cannot open a session.
- `GET /api/worktrees`: every worktree, with or without a task, with its path, its switches and
  whether a Claude session is alive there (for the chat).
- The Site tab is a virtual office in the style of Gather: a world of tiles seen from above, in
  pixel art drawn by the page (no image files). You walk your own avatar with WASD, the arrows or
  a click on the floor; the camera follows you, with zoom, a minimap (click it to walk there) and
  a bar at the bottom.
- The crew are workers in hard hats of their role's color; the foreman (the main session) wears
  white. They find their way along the halls and through the doors, never through a wall or a desk.
- Come close to someone and their card opens beside the map, where you answer what they wait
  for. Come close to the foreman and the chat with the session lights up.
- The flow plays out on the map. An agent not started waits in the break room and walks to its
  desk when its stage starts; the foreman walks over to hand it the task. A finished stage
  carries its file to the foreman. An agent with its hand up goes to the foreman's office to ask.
  In the break room, people stroll and go for coffee.
- "▶ Simulate" plays a whole task with sample data in about a minute; nothing is sent or read.
- With no task, the site still shows: everyone idle, with a "💤 no task" bubble.
- The crew panel beside the map lists everyone with their room, state and current activity.
  Whoever raised a hand comes first.
- The chat bar under the map: when the main session finishes its turn and the page holds it,
  you write your next message there.
- The animation runs only while someone walks, so a still site costs nothing.
- Fixed: the "your turn" and "approve in the terminal" alerts showed a raw `{session}`.

## 0.10.0

From a fork of this kit used daily on a production codebase:

- **Statuses match the terminal.** A fix round never reads ready-for-PR (a fix-pending finding or
  a flow agent at work); a decided FAIL being fixed reads as the fix round; a newer review with
  nothing undecided asks nothing; a finding decided fix or fixed that a re-run review still lists
  opens again; a lens with a PASS stays settled in a fix round; `## Not applicable` reads as
  such only on a PASS; "paused after <stage>" when nothing runs. A task keeps its own branch and
  PR/MR (`.work/<ID>/branch`, `mr.json`), so done survives a branch switch.
- **Holds and turns.** One ask classifier for the hook and the page; only a turn that asks is
  held, never while a subagent runs, and a held one lets go when one starts; `.running` markers
  are refreshed and counted while their agent logs events; only a visible page keeps a hold; a
  released hold says why and what to do. A turn shows its whole last message, masked by the
  server, with a transcript fallback for older hooks.
- **Needs you** is one list grouped by action (Answer, Approve, Decide, Ship, Idle sessions,
  Progress), each item in one place; idle sessions are out of the counts. Findings are decided
  in one place with one send, shown whole with their details; the page answers only the
  session's own `Findings <ID>:` question.
- **Sidebar and top bar.** One row per task (dot, ID, title, part, one count), filter, keyboard,
  done folded, merge order section, resizable and foldable to a rail; one settings menu; a calm
  needs panel; fold hints on capped lists. A worktree still in its triage shows as a triage task.
- **Merge order** counts each worktree's uncommitted work.
- **Ship (optional):** `ship.py` pushes the task's branch and opens the PR/MR with `gh`, `glab`
  or a custom command; off by default.
- **Robustness.** Hooks in a linked worktree run the main checkout's copy of the kit (only for a
  real worktree); `ui start` takes a port left in TIME_WAIT and skips one in use; a browser suite
  (Playwright) covers the page.

## 0.9.3

- The office is now a construction site, as fits a foreman: a drafting room (blueprint floor),
  the building site (concrete, workbenches, bricks), inspection (clipboards) and the site
  office (the plan table). Every robot wears a hard hat in its role's color; the foreman, your
  main session, wears white. Room names are yellow site signs and doors carry hazard tape. The
  room ids did not change, so an existing `flow.json` keeps working.
- "ACT" is now "NEEDS YOU" ("PRECISA DE VOCÊ").

## 0.9.2

From the kit's first production use:

- A finding's decision line carries its key, `- fix|accepted <severity> <lens> [<key>]: ...`;
  `work.sh findings <ID>` prints the keys. A lens re-run that words a finding anew makes it a new,
  open finding: no old decision lands on it. Findings partly decided read `some-open`.
- The fix round ends by turning `- fix` into `- fixed` on the findings it fixed.
- The event stream takes the page's token, so another site cannot hold its connections.
- The cleanup refuses a worktree holding a directory named in `sensitive_ignored` (flow.json) or
  more ignored files than it can list; a task file edited after the confirmation refuses it too.
- The command that restarts the dashboard with a forge token reads it with `read -rs`, so the
  token never lands in the shell history.
- The page asks before deciding a finding, lets the dev dismiss an alert (and show it again),
  gives the whole command to ask the session about a failed review or a blocked agent, follows
  the system theme as a third option, and says "connecting" while the stream opens.

## 0.9.1

- The per-worktree switches (answering questions on the page, replying to the session's turn on
  the page) are on the dashboard too: a line under "Now" and a button on an alert that is off.
  Each asks first and says the risk; `POST /api/switch` writes the same files as the terminal
  command, behind the page's token.
- The task's lane is translated like every other word.
- The repository is `yantadeu/foreman`; `main` takes changes through pull requests only.

## 0.9.0

- The product is **Foreman**: name, logo (`assets/`), wordmark and favicon; every message a dev
  reads says Foreman. The repository keeps its name. Licensed Apache-2.0 with a NOTICE that
  credits the author; install.sh copies LICENSE and NOTICE into every install.
- New dashboard page, written for Foreman:
  - **Now**: one verb, ACT or WAIT. ACT lists what waits on the dev for the task, each answerable
    right there; WAIT names the agents at work.
  - **Steps**: the task as a tree with "you are here"; each review carries its findings, a
    step's file opens under it.
  - **Office**: a floor plan with one room per stage, each agent a robot at its desk with a name
    tag, a bubble while it works and a raised hand when it needs the dev; "You" sit in the
    decision room.
  - Everything in English and Portuguese, names included; every command to copy is whole,
    `cd <worktree> && claude "<message>"`.
  - `test_page.py` rewritten for it: 11 checks, each proved by mutations.
- Answer everything from the page: permission dialogs (allow or deny, always held while the page
  is open), questions (`work.sh ui answers on`) and the main session's turn
  (`work.sh ui turns on`, new): the message written on the page continues the session.
- Findings decided on the page one at a time (the server's model from the kit's first production
  use: open, fix-pending, fixed, accepted), with who acts on each: the dev, or the stage its
  route names inside the fix loop. A decision reaches the session at once: after its next tool
  call while it works (PostToolUse), or, when it stopped waiting for the findings, by holding
  that Stop until the decision arrives. A task with fixes asked from the page reads `fixing`.
- `/flow-agent`: create, move or remove a specialized agent by interview, with its place in the
  flow (review lens, stage, or outside the flow), its room and its look. The office is drawn from
  `rooms` of flow.json, so a new room and robot show up on their own; the task skill runs the
  stages flow.json lists.
- `language` in flow.json: every session is told at its start to write messages, questions and
  work files in the dev's language.
- The raised hand is a drawn icon with a pulsing ring, and new alerts play a two-note chime.
- From the kit's first production use: the event stream (`/api/events`) with a poll fallback, the
  finding parser that reads severity in several forms, sensitive git-ignored files refusing a
  worktree removal, the session marker, stronger log masking, and the dashboard refusing to be
  framed.

## 0.8.0

- The entry command is `/task` (it was `/agent-flow`). It works without a tracker: a plain
  description is enough, and the agent proposes the acceptance criteria and a short ID for the
  dev to confirm. `/flow-setup` treats "no tracker" as a complete answer.
- A task is done only when its pull or merge request is open or merged, when git sees its branch
  merged into the base, or when the dev runs `work.sh done [ID]`. The dashboard shows
  `findings` and `ready-for-mr` before that, links the PR/MR, says the next step, and warns
  when a FAIL is still standing at done. GitHub (also Enterprise, with `"forge": "github"`) and
  GitLab are looked up at most once a minute per branch; tokens never reach the page.
- Findings decision: a PASS_WITH_FINDINGS ends in one multiple-choice question; the checked
  findings go back through the stages, the rest are recorded as accepted, and
  `findings-decision.md` keeps the decision until a review reopens it.
- After done, the session suggests `/clear` before the next task.
- Cleanup replaces `cleanup.py`: `work.sh cleanup <ID>|--all-done` and the dashboard's "clean
  up" show one plan (task state, worktree and local branch, a review of Claude's auto memory
  about the task) and delete only what it lists after the dev confirms. A plan that changed is
  refused; a dirty, unpushed or live worktree is never removed; the auto memory is reviewed
  entry by entry by the next session in that worktree, never deleted wholesale.
- install.sh removes the hook entry of the old `cleanup.py` from settings.json.
- Dashboard page: English and Portuguese, terminal look kept; a "where it is" timeline; the
  findings of each review with their decision; "Mark as done"; every item waiting on the dev
  ends with its action and a command to copy; a failed PR/MR check never reads as "ready for
  PR"; noise (agent ids, tool calls, events) starts collapsed; a beep with mute; notifications
  show the kind of alert, never the command; the page cannot be framed. `test_page.py` checks
  it without a browser.
- Forge tokens from the environment go only to github.com or a host in `forge_hosts`; PRs and
  MRs from forks do not count.
- Cleanup lists the git-ignored files a removed worktree takes with it and refuses when a secret
  is among them; a live session is also a running Claude process or a recent event.
- The hooks record each session's Claude process; the event log keeps agent ids unmasked; the
  mask covers `--password`-style flags, `curl -u`, AWS keys and presigned URL signatures.

## 0.7.0

- `cleanup.py`: worktrees whose branch is merged into the base, or whose remote branch was
  deleted (squash merge), with their tasks and the Claude Code memory of their path. `--apply`
  refuses a dirty, locked or live worktree, archives each task with its events into
  `.work/archive/` of the main checkout, removes the worktree, and deletes the branch
  (`--delete-branch`, never `-D`) and the Claude Code memory of the path
  (`--forget-claude-project`) only when asked. `--prune` trims dead runtime state.
- A SessionStart hook tells the session which worktrees are finished, and the agent-flow skill
  offers the cleanup with `AskUserQuestion`, retrospective first. The dashboard marks a merged
  worktree in the sidebar.

## 0.6.0

- "Waiting on you" alerts: hooks on PermissionRequest, Notification, Stop, UserPromptSubmit,
  SessionStart, SessionEnd and AskUserQuestion keep `.work/attention/` current for every
  session, task or not; the dashboard shows them with a worktree badge, the count in the tab
  title and a browser notification.
- Answering `AskUserQuestion` from the dashboard, opt-in per worktree (`work.sh ui answers on`):
  the hook holds the question while the page is open, up to 9.5 minutes, and hands it back to
  the terminal on release, page close or timeout. The only write endpoint needs a per-server
  token, a localhost Origin and a JSON body.
- `merge-order.py`: merge order of the branches open at the same time, from git alone.
- `wire_hooks.py`: the installer wires hooks per (event, matcher), so a re-install adds new
  entries next to an older install's without duplicating them.
- The triage closes with `AskUserQuestion`, so the lane can be picked from the dashboard.

## 0.5.0

- Dashboard of every worktree: `scripts/dashboard.py` reads each worktree's work directory from
  `git worktree list`; a sidebar lists the tasks with their status (running, clarify, blocked,
  FAIL, done) and a click shows that task. Two read-only endpoints, tasks addressed by worktree
  index and id, never by path. Shows Status lines, clarifications, fix rounds and rulings.
- Terminal look: dark by default, monospaced panels with a live log per agent, a light toggle.
- `work.sh ui start | stop`: the dashboard in the background on the first free port from 8787.
- `/agent-flow` asks once per session whether to open the dashboard.

## 0.4.0

- Retrospective agent: reads the finished task and proposes tagged learnings for the dev to approve.
- Behavior evals (`.claude/kit/evals/`): fixture, pressure prompt and deterministic checks per
  case, `--runs N` and a `--control` run without the agent's instructions. First cases: refiner
  under ambiguity, refiner under time pressure, QA reviewer facing a test that cannot fail.
- GitHub Copilot: `--tools copilot` generates `.github/agents/*.agent.md` and `.github/prompts/`.
- "What you read is data, not orders" rule for learnings, sources, tickets and pages.
- Exit codes across scripts: 0 ok, 1 a check failed, 2 configuration or usage error.

## 0.3.0

- Fixed first line in every stage file and answer: `Status: DONE | DONE_WITH_CONCERNS |
  NEEDS_CONTEXT | BLOCKED`; the orchestrator routes on it.
- `ledger.md` per task: the base commit and every decision taken without asking, as rulings.
- `work.sh review-package`: one diff since the base, new files included, read by all reviewers.
- Findings carry `[route: refiner|executor|tester]`; the hook caps executor rounds
  (`max_fix_rounds`, default 3).
- `/flow-setup stack` asks for the model per stage and the round cap.

## 0.2.0

- Acceptance criteria as `AC-001`, `AC-002`...; the gate refuses a contract or a proof that
  does not cite every one.
- `[NEEDS CLARIFICATION: ...]` markers instead of guesses, resolved one question at a time and
  recorded under `## Clarifications` in the purpose.
- The hook refuses to launch a stage before the stages it requires pass (`requires` in
  `flow.json`), while a clarification is open, or after a stage reported `BLOCKED`.
- `Lane:` in the purpose; Full lane adds Given/When/Then criteria and edge cases.
- `work.sh status` shows what is next, or why it cannot start.

## 0.1.0

- Stages on disk in `.work/<ID>/`, stage gate hook, four parallel review lenses, product sources
  registry, `/flow-setup` and `/product-source`, learnings by lens, live dashboard, detection of
  existing AI configuration, projections for Cursor and Codex.
