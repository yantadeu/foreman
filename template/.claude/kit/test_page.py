#!/usr/bin/env python3
"""Checks of the Foreman dashboard page that need no browser.

Usage: python3 .claude/kit/test_page.py

The value sets (statuses, next steps, warnings, PR/MR errors, refusals, cleanup items, alert kinds
and actions) are read from dashboard.py, so a value added there without a label fails here. Each
check also runs against mutated copies of the page that must make it fail (MUTATIONS): a check
that passes on a broken page does not bite. Rendering is checked by hand in a browser.
"""
import html.parser
import importlib.util
import json
import os
import re
import sys

KIT = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(KIT, "dashboard", "index.html")
FLOW = os.path.join(KIT, "..", "flow.json")
LANGS = ("en", "pt")
BRAND = ("Foreman", "FOREMAN")


def server():
    spec = importlib.util.spec_from_file_location("dashboard", os.path.join(KIT, "scripts", "dashboard.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def dictionary(page):
    m = re.search(r"/\* i18n:begin \*/(.*?)/\* i18n:end \*/", page, re.S)
    assert m, "the dictionary markers are missing"
    return json.loads(m.group(1))


def script(page):
    """The page's script without the dictionary and without // comments."""
    code = page[page.index("<script>"):page.rindex("</script>")]
    code = re.sub(r"/\* i18n:begin \*/.*?/\* i18n:end \*/", "{}", code, flags=re.S)
    return re.sub(r"(?m)(^|[\s;])//.*$", r"\1", code)


def both_languages(page):
    """Every key in both languages, non-empty, same placeholders; every value the server can
    return, every name the page shows and every key the code asks for has a label."""
    d, srv = dictionary(page), server()
    assert set(d) == set(LANGS), f"languages {sorted(d)}"
    en, pt = d["en"], d["pt"]
    assert set(en) == set(pt), f"only in en: {sorted(set(en) - set(pt))}; only in pt: {sorted(set(pt) - set(en))}"
    ph = lambda s: sorted(set(re.findall(r"\{(\w+)\}", s)))
    for k in en:
        assert en[k].strip() and pt[k].strip(), f"empty text for {k}"
        assert ph(en[k]) == ph(pt[k]), f"{k}: placeholders differ {ph(en[k])} vs {ph(pt[k])}"
    flow = json.load(open(FLOW))
    hooks = open(os.path.join(KIT, "hooks", "flow.py")).read()
    need = [f"status.{s}" for s in srv.STATUSES] + [f"status.{s}.m" for s in srv.STATUSES]
    need += ["status.stage", "status.stage.m", "status.mr-unchecked", "status.ready-to-close"]
    need += [f"next.{s}" for s in srv.NEXT_STEPS] + [f"warning.{w}" for w in srv.WARNINGS]
    need += [f"mr.{e}" for e in srv.MR_ERRORS] + [f"refuse.{r}" for r in srv.REFUSALS]
    need += [f"clean.item.{i}" for i in srv.CLEANUP_ITEMS] + [f"action.{a}" for a in srv.ALERT_ACTIONS]
    need += [f"kind.{k}" for k in srv.ALERT_KINDS] + [f"kind.{k}.m" for k in srv.ALERT_KINDS]
    need += [f"role.{a}" for a in flow["stages"]] + ["role.main", "role.foreman"]
    need += [f"verdict.{v}" for v in ("PASS", "PASS_WITH_FINDINGS", "FAIL")]
    need += [f"ev.{e}" for e in sorted(set(re.findall(r'"ev": "([a-z]+)"', hooks)))]
    need += [f"lens.{l}" for l in flow["lenses"]]
    need += [f"room.{r}" for r in ("refine", "workshop", "review", "decision")]
    need += [f"fstate.{s}" for s in ("needs-you", "agent", "fixing", "fixed", "accepted")]
    need += ["prompt.memory", "prompt.decide-findings", "prompt.apply-findings"]
    for lang, table in d.items():
        missing = [k for k in need if k not in table]
        assert not missing, f"{lang} lacks {missing}"
    m = re.search(r"const STATUSES = (\[[^\]]*\]);", page)
    assert m and set(json.loads(m.group(1))) == set(srv.STATUSES), "the page's STATUSES differ from dashboard.py"
    code = script(page)
    used = set(re.findall(r'\bt\("([\w.-]+)"\s*[,)]', code)) | set(re.findall(r'data-i18n="([\w.-]+)"', page))
    unknown = sorted(k for k in used if k not in en)
    assert not unknown, f"keys used but not in the dictionary: {unknown}"
    assert "(I18N[lang] || {})[key] ?? I18N.en[key] ?? key" in page, "t() must fall back to English, then to the key"


class Text(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.found = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in ("meta", "link", "input", "br", "rect", "path", "circle", "line"):
            self.stack.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        while self.stack and self.stack.pop()[0] != tag:
            pass

    def handle_data(self, data):
        if data.strip() and self.stack and self.stack[-1][0] not in ("script", "style", "title"):
            self.found.append((self.stack[-1], data.strip()))


def every_word_translated(page):
    """Not only the titles: static text is tagged, and agent roles, verdicts, events, lenses and
    the messages to type reach the page through the dictionary."""
    body = page[page.index("<body>"):page.index("<script>")]
    p = Text()
    p.feed(body)
    loose = [txt for (tag, attrs), txt in p.found if "data-i18n" not in attrs and txt not in BRAND]
    assert not loose, f"static text without data-i18n: {loose}"
    code = script(page)
    for raw in ("esc(s.agent)", "esc(l.type)", "esc(e.ev)", "esc(x.lens)", "esc(e.type || e.agent)"):
        assert raw not in code, f"a name printed without the dictionary: {raw}"
    for part in ("lens ? lensName(lens) : roleName(r)", "esc(roleName(e.type || e.agent))", "esc(evName(e.ev))", "esc(lensName(x.lens))", "verdictName(v)"):
        assert part in code, f"a name must go through the dictionary: {part}"
    assert "stage: roleName(s)" in code, "the paused-after status names the stage in the dev's language"
    assert 'lane: word("lane.", d.lane)' in code and all(f"lane.{l}" in dictionary(page)["pt"] for l in ("Light", "Full", "None")), \
        "the lane is a word of the dev's language too"


def whole_commands(page):
    """A command to copy carries the whole message to type, in the dev's language: never a bare
    `cd <worktree> && claude`."""
    code = script(page)
    assert "function cmdText(command)" in code and "<code>${cmdText(command)}</code>" in code, "every command block goes through cmdText"
    assert "replace(/claude '([^']*)'$/" in code, "the quoted message after claude is translated"
    d = dictionary(page)
    for key in ("prompt.memory", "prompt.decide-findings", "prompt.apply-findings"):
        en = d["en"][key].replace("{id}", "")
        assert f'["{en}", "{key}"]' in code, f"{key}: the page must know the server's English message"
    assert 'sessionCmd(d.path || ".", "prompt.apply-findings", d.id)' in code, "the findings decision gives the whole command"
    assert "&& claude\"" not in code and "&& claude`" not in code, "no bare `&& claude`"


def answers_from_the_page(page):
    """A question, a permission (allow or deny) and a session's turn (a message) are answered on
    the page while held, through /api/answer with the token; not held, the page says where."""
    code = script(page)
    assert 'const post = (path, body) => fetch(scoped(path), { method: "POST", headers: { "Content-Type": "application/json", "X-Kit-Token": TOKEN }' in code
    assert 'post("api/answer", { wt: a.wt, session: a.session, agent: a.agent, qid: a.qid, ...extra })' in code
    for part in ('{ behavior: "allow" }', '{ behavior: "deny" }', "{ message: text }", "{ release: true }", "{ answers }"):
        assert part in code, f"the page must send {part}"
    body = code[code.index("function alertBody"):code.index("const goneAlerts")]
    assert '(a.held ? `<div class="actions"><button class="primary" data-allow=' in body, "allow and deny only on a held permission"
    assert '? `<textarea rows="3" data-msg=' in body, "the message box only on a held turn"
    assert "ui turns on" in body and "ui answers on" in body, "a switched-off answer shows the whole command to turn it on"
    assert "if (!text.trim()) return;" in code, "an empty message is not sent"


def office(page):
    """The office: a room per stage, a robot per agent at its desk, the task in the room of its
    step, a raised hand on the agent that waits on the dev, and its request answerable there."""
    code = script(page)
    d = dictionary(page)
    rooms = re.search(r"const DEFAULT_ROOMS = (\[.*?\]);", code, re.S).group(1)
    for role in json.load(open(FLOW))["stages"]:
        assert f'"{role}"' in rooms, f"{role} has no desk in the default office"
    assert '"main"' in rooms, "the main session sits in the decision room"
    assert "flowCfg.rooms" in code and 'getJSON("flow.json")' in code, "the office is the rooms of flow.json"
    assert "String(pick(agentCfg(a).label) || a)" in code, "an agent the dev added shows its own label"
    assert "const alert = g && g.alerts.find(a => a.agent === lane.agent" in code, "a hand belongs to the agent that raised the alert"
    assert "hand ? '<span class=\"bubble hand\"" in code, "an agent waiting on the dev raises its hand"
    assert 'alertBody(hand.alert, 0, "p")' in code and 'wireAlerts(pop, [hand.alert], "p")' in code, "the request is answered from the robot"
    # The same question shows in more than one box: radios group by name across the page, so each
    # box names its own, or checking one unchecks the other.
    assert 'name="${p}a${n}q${i}" value="${j}"' in code and 'input[name="${p}a${n}q${i}"]:checked' in code, "radio names per box"
    # The robot's card is redrawn only when its content changes: its clocks are filled after, or a
    # redraw every tick wipes what the dev was marking.
    pop = code[code.index("function showPop"):code.index("function purposeText")]
    drawn = pop[pop.index("let html ="):pop.index("if (pop.dataset.html !== html)")]
    assert "<span data-when></span>" in drawn and "ago(" not in drawn and "when ?" not in drawn, "no clock in the redraw check"
    assert "const here = stepRoom(list[now]);" in code, "the task stands in the room of its current step"
    assert all(k in d["pt"] for k in ("office.hand", "pop.blocked", "pop.idle"))
    assert 'const away = r !== "main" && !lane && !(st && st.written);' in code, "an agent not started waits in the break room"
    assert 'post("api/answer", { wt: turn.wt, session: turn.session, agent: turn.agent, qid: turn.qid, message: text })' in code and "mh.alert.held" in code, "the session's held turn is answered from the chat under the map"
    assert "if (turn && chatQueue && chatQueue.wt === wtN)" in code, "a message written while the session works goes when its turn comes"
    assert "cmdBlock(`cd ${q(c.path)} && claude ${q(text)}`)" in code, "with no session, the chat writes the whole command"
    assert all(k in d["pt"] for k in ("room.lounge", "chat.title", "chat.working", "chat.off", "site.full"))
    assert "function findPath(wr, from, to)" in code and "a.path = findPath(world, [a.x, a.y], goal);" in code, "people walk around walls and furniture"
    assert "let best = null, bd = 2.3;" in code and "openBot = near; nearOpened = true;" in code, "coming close to someone opens their card"
    assert all(k in d["pt"] for k in ("office.foreman", "world.near", "world.alone", "world.goto"))


def findings_panel(page):
    """Each finding shows lens, severity, its whole text, state and who acts; only an open finding
    that waits on the dev offers Fix or Accept, which stage a choice; one "Send N decisions" posts
    each with its key. A dismissed alert hides and comes back."""
    code = script(page)
    f = code[code.index("function findingRow"):code.index("function decisionsBar")]
    for part in ("esc(lensName(x.lens))", 't("sev." + x.severity)', "esc(x.full_text || x.summary)", 't("fstate." + x.view)', "who(x)"):
        assert part in f, f"a finding lacks {part}"
    assert 'x.view === "needs-you" && x.state === "open"' in f, "only an open finding that waits on the dev offers a choice"
    w = code[code.index("function wireFindings"):code.index("async function tellDecided")]
    assert 'post("api/findings-decision"' not in w[:w.index("async function sendDecisions")], "Fix and Accept only stage a choice"
    assert 'post("api/findings-decision", { wt, id: d.id, key: x.key, decision })' in w, "Send posts each choice with its key"
    assert "staged.delete(stageKey(d, x));" in w and w.index("if (r.ok)") < w.index("staged.delete(stageKey(d, x));"), \
        "only a choice that landed leaves the staged set"
    tell = code[code.index("async function tellDecided"):code.index("let tab = ")]
    assert ".filter(a => findingsAsk(a, d.id))" in tell, "only the findings ask itself is answered"
    assert 'kind: "findings-decided", id: d.id' in tell, "a turn gets the server's message"
    assert "dismissed.delete(b.dataset.restore)" in code and "if (dismissed.has(akey(a))) return;" in code, "a dismissed alert hides and comes back"


def now_panel(page):
    """One line for the dev: NEEDS YOU when an item of this worktree or a step waits on them, WAIT
    otherwise, naming who works on what. Alerts and findings are not repeated there: Now counts
    this worktree's items and leads to the steps view for the findings."""
    code = script(page)
    n = code[code.index("function showNow"):code.index("const LENS_ROLE")]
    assert 't(act ? "now.act" : "now.wait")' in n and "const act = items.length > 0;" in n
    assert "alertBody(" not in n and "findingRow" not in n, "Now repeats no alert and no finding"
    assert 't("needs.mine", { n: mine.length })' in n and "decideIn(+b.dataset.decide, b.dataset.id)" in n
    assert "wireFindings($(\"now\"), d)" in n, "open findings are decided in Now"
    assert 'l.state === "running" && l.type !== "main"' in n, "WAIT says which agents work"


def needs_list(page):
    """One list of what needs the dev, grouped Answer, Approve, Decide, Ship, then Idle sessions and
    a closed Progress group: each item in exactly one row, the count, the title and the sidebar
    badges counting the same items of the four action groups; amber only for Answer and Approve."""
    code = script(page)
    assert 'const NEED_GROUPS = ["answer", "approve", "decide", "ship"];' in code
    b = code[code.index("function buildNeeds"):code.index("function needRowHTML")]
    assert 'a.awaits === false ? "progress" : a.idle ? "idle" : a.kind === "permission" ? "approve" : "answer"' in b
    sh = code[code.index("function showAttention"):code.index("function decideIn")]
    assert 'needCount = needItems.filter(i => NEED_GROUPS.includes(i.group)).length;' in sh
    assert 'document.title = (needCount ? `(${needCount}) ` : "") + "Foreman";' in sh
    assert "const pending = (wt) => needItems.filter(i => i.wt === wt && NEED_GROUPS.includes(i.group)).length;" in code
    assert '<details class="ngroup" data-group="progress" hidden>' in page, "Progress starts closed"
    assert 'id="needs-count" aria-live="polite"' in page
    assert '.ngroup[data-group="answer"] .nrow, .ngroup[data-group="approve"] .nrow { border-left-color:var(--warn) }' in page
    assert "function patchRows" in code and 'el.dataset.html !== r.html' in code, "a row whose markup is the same is kept"
    assert "d.hidden = s.has(r.dataset.row)" in code, "a row not looked at yet has a dot"


def findings_fold(page):
    """Only the findings decision itself folds into its task's Decide row: the main session's held
    question whose every question text starts with "Findings <ID>:", or its held turn whose whole
    message is the announcement. Anything else keeps its own row."""
    code = script(page)
    f = code[code.index("function findingsAsk"):code.index("const findingsWts")]
    assert 'if (!a || !a.held || a.agent !== "main" || !id) return false;' in f
    assert 'a.questions.every(qq => qq && typeof qq.question === "string" && qq.question.startsWith("Findings " + id + ":"))' in f
    assert 'String(a.message || "").trim() === ANNOUNCE(id)' in f, "the announcement is the turn's whole message"


def tree(page):
    """The steps as a tree: the step the task is at says "you are here", the reviews carry their
    findings, and a step's file opens under it."""
    code = script(page)
    tr = code[code.index("function showTree"):code.index("function showOffice")]
    assert 'const here = i === now ? `<span class="you">${t("tree.here")}</span>` : "";' in tr
    assert "fs.map(findingRow)" in tr, "findings sit under their review"
    assert "openNodes.has(s.file)" in tr and "d.files[s.file]" in tr, "a step's file opens under it"

def not_applicable(page):
    """A PASS with a "## Not applicable" section (dashboard.py "na") reads as such on its lens."""
    code = script(page)
    assert '${s.na ? t("verdict.na") : v ? esc(verdictName(v))' in code, "a not applicable lens reads as a plain approval"
    d = dictionary(page)
    assert all("verdict.na" in d[l] for l in LANGS)


def released_holds(page):
    """A question or turn whose hold let go says why and when, and the way out: a question keeps its
    options pickable and lists what to choose in the terminal; a turn gets a text box and Copy."""
    code = script(page)
    body = code[code.index("function alertBody"):code.index("const goneAlerts")]
    assert body.count("goneHTML(a, n)") == 2, "both a question and a turn show why their hold let go"
    assert '${a.held || a.released ? "" : " disabled"}' in body, "a released question's options stay pickable"
    gone = code[code.index("function goneHTML"):code.index("function goneAnswer")]
    assert 't("gone." + (has("gone." + a.released) ? a.released : "stopped")' in gone and "data-copy" in gone \
        and "data-gone-picks" in gone, "why it let go, Copy for a turn, the picks for a question"
    srv = server()
    d = dictionary(page)
    assert all(f"gone.{r}" in d[l] for r in srv.RELEASE_REASONS for l in LANGS), "every release reason is said"


def visible_page(page):
    """The page says whether it is in view on every poll: only a page in view keeps a hold."""
    code = script(page)
    assert 'getJSON("api/attention?visible=" + (document.visibilityState === "hidden" ? "0" : "1"))' in code
    assert 'addEventListener("visibilitychange"' in code, "a page back in view polls at once"


def whole_turn(page):
    """A turn shows its whole message, open and scrolled to its end, where the ask is."""
    code = script(page)
    assert '<div class="turn-msg" data-turn-msg>${esc(a.message || a.detail)}</div>' in code
    assert "el.scrollTop = el.scrollHeight" in code, "scrolled to its end"


def quiet_turns(page):
    """Only what waits on the dev counts in the title and beeps: an informative or idle turn stays quiet."""
    code = script(page)
    assert "const loud = (a) => a.awaits !== false && !a.idle;" in code
    assert "if (seenAlerts.has(key(a)) || !loud(a)) return;" in code, "no beep for a quiet turn"


def whole_finding(page):
    """Each finding is shown whole where the dev decides: its full text and its details."""
    code = script(page)
    assert "${esc(x.full_text || x.summary)}" in code and '<div class="fdetail">${esc(x.details)}</div>' in code


def sidebar(page):
    """The sidebar navigates: one row per task with a status dot and at most one count, grouped by
    worktree only when it has more than one task, needs-you first, done ones closed; a filter on
    "/", arrow keys, a resizable edge kept apart from the clamp, a rail of dots, and the count of
    an item with no task row on its worktree."""
    code = script(page)
    sd = code[code.index("function sideCounts"):code.index("function renderSidePop")]
    assert "grouped = g.tasks.length > 1" in sd, "grouped only for a worktree with several tasks"
    assert "else if (g && g.tasks.length === 1)" in sd and "loose.set(i.wt" in sd, "an item of no row counts on the only row, or the header"
    assert 'const sideRank = (k, n) => n ? 0 : RUNNING.includes(k.status) ? 1 : k.status === "done" ? 3 : 2;' in code
    assert '<details class="done" id="side-done">' in sd and "if (dd.open !== sideDoneOpen) dd.open = sideDoneOpen;" in sd, \
        "done tasks sit in a closed Done section"
    assert 'if (e.key === "/" && !typing)' in code and 'e.key !== "ArrowDown" && e.key !== "ArrowUp"' in code
    assert "Math.max(200, Math.min(520, Math.floor(window.innerWidth * 0.6)))" in code and "chooseSide(SIDE_DEFAULT)" in code
    assert "try { sideChosen = +localStorage.getItem(SIDE_KEY) || SIDE_DEFAULT;" in code, "the width is read inside try"
    assert "const w = Math.max(200, Math.min(sideMax(), sideChosen));" in code, "the chosen width is kept apart from the clamped one"


def top_bar(page):
    """The top bar is the brand, the live dot and one menu holding language, theme, sound and
    notifications: a disclosure (aria-expanded, no aria-haspopup) that Escape and focus leaving close."""
    m = page[page.index('<div class="controls">'):page.index("</header>")]
    for el in ("lang-btn", "theme-btn", "mute-btn", "notify-btn"):
        assert m.index('id="menu"') < m.index(f'id="{el}"'), f"{el} sits in the menu"
    assert 'id="menu-btn" type="button" class="icon" aria-expanded="false" aria-controls="menu"' in m and "aria-haspopup" not in page
    code = script(page)
    d = code[code.index("function disclosure"):code.index('disclosure($("side-gear")')]
    assert 'if (e.key === "Escape") { e.stopPropagation(); close(true) }' in d and '"focusout"' in d and "btn.focus()" in d


def calm_panel(page):
    """A calm needs panel: no red band, no solid verb block; a capped box fades at its fold."""
    assert "background:var(--bad); border-radius:9px" not in page, "sidebar counts are not red"
    assert ".now.act .verb { background:var(--warn)" not in page
    code = script(page)
    assert "function foldHints" in code and 'el.classList.toggle("fold", over);' in code and "foldHints();" in code


def switches_on_page(page):
    """The worktree's switches are on the page; turning one on or off asks first (with the risk
    when turning on) and posts with the token only from the confirmation."""
    code = script(page)
    sw = code[code.index("function switchesLine"):code.index("function openClose")]
    assert 'post("api/switch", { wt, name, on })' in sw, "the switch is posted"
    go = sw[sw.index('$("switch-go").onclick'):]
    assert 'post("api/switch"' in go and sw.index('post("api/switch"') > sw.index('$("switch-go").onclick'), "only the confirmation posts"
    assert '${on ? `<div class="warn" style="margin-top:8px">${t("switch.risk")}</div>` : ""}' in sw, "turning on says the risk"
    assert "${switchesLine(g.wt)}" in code and "disclosure($(\"side-gear\"), $(\"side-pop\"));" in code, \
        "the switches of each worktree sit in the sidebar's gear popover"
    assert 'data-switch="answers"' in code and 'data-switch="turns"' in code, "an alert that is off offers to turn it on"


def noise_collapsed(page):
    """Events, agent ids and tool details sit in closed <details>."""
    assert '<details id="events"><summary id="events-sum"></summary><table id="feed"></table></details>' in page, "events must be closed"
    code = script(page)
    for tag in re.findall(r"<details[^>`]*>", code):
        assert not re.search(r"\bopen\b", tag), f"a <details> rendered open: {tag}"
    pop = code[code.index("function showPop"):code.index("function purposeText")]
    inner = pop[pop.index("<details><summary>"):]
    inner = inner[:inner.index("</details>")]
    assert "data-laneid" in inner and "lane.last" in inner, "the agent id and last tool call stay in the robot's <details>"


def cleanup_wiring(page):
    """The plan comes from the server and goes back with its hash and only offered items."""
    code = script(page)
    assert 'getJSON("api/cleanup-plan?" + query)' in code
    assert 'post("api/cleanup", { targets: picked, plan: cleanPlan.plan })' in code, "the plan hash goes back"
    assert "ITEMS.includes(k) && g.items && g.items[k] && !g.items[k].refused" in code
    assert 'if (d.status === "done") buttons.push(`<button id="clean-one"' in code, "clean up only on a done task"
    assert 'post("api/close", { wt, id: d.id })' in code


def preferences(page):
    """Language, theme and sound are remembered inside try/catch; the browser language decides
    the first one; the page is out of date when served without its token; live with a fallback."""
    assert re.search(r'navigator\.language[^;\n]*\.toLowerCase\(\)\.startsWith\("pt"\) \? "pt" : "en"', page)
    for key in ("LANG_KEY", "THEME_KEY", "MUTE_KEY"):
        assert re.search(r"try \{[^}\n]*localStorage\.getItem\(" + key, page) and re.search(r"try \{ localStorage\.setItem\(" + key, page), key
    assert 'document.documentElement.lang = lang === "pt" ? "pt-BR" : "en"' in page
    assert "attn === null || TOKEN === PLACEHOLDER" in page and 'const PLACEHOLDER = "__KIT_" + "TOKEN__";' in page
    assert 'new EventSource(scoped("api/events?t=" + encodeURIComponent(TOKEN)))' in page and "startPoll()" in page, "live stream with a poll fallback"
    assert "const getJSON = async (p) => { const r = await fetch(scoped(p));" in page, "every read names the project on screen"
    assert 'path.startsWith("api/projects") ? path' in page and "x.p !== proj && x.pending > (projSeen[x.root] ?? x.pending)" in page, \
        "the project list is not scoped, and another project that starts waiting on the dev chimes"


CHECKS = (calm_panel, top_bar, sidebar, needs_list, findings_fold, whole_finding, quiet_turns, whole_turn, visible_page, released_holds, not_applicable, both_languages, every_word_translated, whole_commands, answers_from_the_page, office, findings_panel, now_panel, tree, switches_on_page,
          noise_collapsed, cleanup_wiring, preferences)


def drop_key(page, lang, key):
    d = dictionary(page)
    del d[lang][key]
    return re.sub(r"/\* i18n:begin \*/.*?/\* i18n:end \*/", lambda m: "/* i18n:begin */" + json.dumps(d) + "/* i18n:end */",
                  page, flags=re.S)


MUTATIONS = (
    (both_languages, "a PT status removed", lambda p: drop_key(p, "pt", "status.fixing")),
    (both_languages, "a PT room removed", lambda p: drop_key(p, "pt", "room.workshop")),
    (both_languages, "an action label removed", lambda p: drop_key(drop_key(p, "en", "action.approve-on-page"), "pt", "action.approve-on-page")),
    (both_languages, "a status missing from the page", lambda p: p.replace('"fixing", "ready-for-mr"', '"ready-for-mr"')),
    (every_word_translated, "static text untagged", lambda p: p.replace('<button type="button" data-tab="tree" data-i18n="tab.tree">Steps</button>', '<button type="button" data-tab="tree">Steps</button>')),
    (every_word_translated, "a robot's name raw", lambda p: p.replace("lens ? lensName(lens) : roleName(r)", "r")),
    (every_word_translated, "an event kind raw", lambda p: p.replace("${esc(evName(e.ev))}", "${esc(e.ev)}")),
    (whole_commands, "commands not translated", lambda p: p.replace("<code>${cmdText(command)}</code>", "<code>${esc(command)}</code>")),
    (whole_commands, "bare claude", lambda p: p.replace('sessionCmd(d.path || ".", "prompt.apply-findings", d.id)', '"cd " + d.path + " && claude"')),
    (answers_from_the_page, "no deny", lambda p: p.replace('{ behavior: "deny" }', '{ behavior: "allow" }')),
    (answers_from_the_page, "allow on a permission not held", lambda p: p.replace('(a.held ? `<div class="actions"><button class="primary" data-allow=', '(true ? `<div class="actions"><button class="primary" data-allow=')),
    (answers_from_the_page, "no token", lambda p: p.replace('"X-Kit-Token": TOKEN }, body: JSON.stringify(body) })', '}, body: JSON.stringify(body) })')),
    (office, "one radio name for every box", lambda p: p.replace('name="${p}a${n}q${i}" value="${j}"', 'name="a${n}q${i}" value="${j}"')),
    (office, "clock redraws the card", lambda p: p.replace('<span data-when></span>', '${when ? " · " + when : ""}')),
    (office, "no hand", lambda p: p.replace("hand ? '<span class=\"bubble hand\"", "false ? '<span class=\"bubble hand\"")),
    (now_panel, "always WAIT", lambda p: p.replace('t(act ? "now.act" : "now.wait")', 't("now.wait")')),
    (now_panel, "Now repeats alerts", lambda p: p.replace('if (mine.length) items.push(', 'mine.forEach(a => items.push(alertBody(a, 0)));\n  if (mine.length) items.push(')),
    (needs_list, "idle counted", lambda p: p.replace('a.awaits === false ? "progress" : a.idle ? "idle" :', 'a.awaits === false ? "progress" :')),
    (needs_list, "title counts every row", lambda p: p.replace('document.title = (needCount ? `(${needCount}) `', 'document.title = (needItems.length ? `(${needItems.length}) `')),
    (needs_list, "amber everywhere", lambda p: p.replace('.ngroup[data-group="answer"] .nrow, .ngroup[data-group="approve"] .nrow {', '.nrow {')),
    (findings_fold, "fold on the header", lambda p: p.replace('qq.question.startsWith("Findings " + id + ":")', 'String(qq.header).includes(id)')),
    (findings_fold, "fold any turn", lambda p: p.replace('String(a.message || "").trim() === ANNOUNCE(id)', 'String(a.message || "").includes(id)')),
    (findings_fold, "subagent folds", lambda p: p.replace('if (!a || !a.held || a.agent !== "main" || !id) return false;', 'if (!a || !a.held || !id) return false;')),
    (switches_on_page, "no risk said", lambda p: p.replace('${t("switch.risk")}', "")),
    (switches_on_page, "posts on the first click", lambda p: p.replace('const dlg = $("dlg");\n  dlg.innerHTML = `<h3>${t("switch.confirm.title."', 'post("api/switch", { wt, name, on });\n  const dlg = $("dlg");\n  dlg.innerHTML = `<h3>${t("switch.confirm.title."')),
    (tree, "no you-are-here", lambda p: p.replace('const here = i === now ? `<span class="you">', 'const here = false ? `<span class="you">')),
    (office, "QA has no desk", lambda p: p.replace('"security-reviewer", "qa-reviewer", "product-reviewer"] }', '"security-reviewer", "product-reviewer"] }')),
    (office, "nobody in the break room", lambda p: p.replace('const away = r !== "main" && !lane', 'const away = false && !lane')),
    (office, "no chat under the map", lambda p: p.replace("message: text })", "message: \"\" })")),
    (office, "queue never sent", lambda p: p.replace("if (turn && chatQueue && chatQueue.wt === wtN)", "if (false)")),
    (office, "nobody comes close", lambda p: p.replace("let best = null, bd = 2.3;", "let best = null, bd = 0;")),
    (office, "walking through walls", lambda p: p.replace("a.path = findPath(world, [a.x, a.y], goal);", "a.path = [goal];")),
    (office, "custom agents unnamed", lambda p: p.replace("String(pick(agentCfg(a).label) || a)", "String(a)")),
    (findings_panel, "choice on every finding", lambda p: p.replace('const choose = x.view === "needs-you" && x.state === "open"', "const choose = true")),
    (findings_panel, "posted on the click", lambda p: p.replace("staged.get(k) === b.dataset.stage ? staged.delete(k) : staged.set(k, b.dataset.stage);", 'post("api/findings-decision", { wt: sel.wt, id: d.id, key: x.key, decision: b.dataset.stage });')),
    (findings_panel, "failed choice dropped", lambda p: p.replace("      if (r.ok) {\n        ok++;\n        staged.delete(stageKey(d, x));", "      staged.delete(stageKey(d, x));\n      if (r.ok) {\n        ok++;")),
    (findings_panel, "tells every held ask", lambda p: p.replace(".filter(a => findingsAsk(a, d.id))", ".filter(a => a.held)")),
    (findings_panel, "who acts dropped", lambda p: p.replace('<div class="factor">${who(x)}</div>${choose}', "${choose}")),
    (noise_collapsed, "events open", lambda p: p.replace('<details id="events">', '<details id="events" open>')),
    (cleanup_wiring, "plan hash not sent", lambda p: p.replace("plan: cleanPlan.plan })", "plan: null })")),
    (preferences, "theme outside try", lambda p: p.replace('try { theme = ["light", "dark", "system"].includes(localStorage.getItem(THEME_KEY)) ? localStorage.getItem(THEME_KEY) : "dark" } catch (e) {}', 'theme = localStorage.getItem(THEME_KEY);')),
    (findings_panel, "dismissed alerts not restorable", lambda p: p.replace("dismissed.delete(b.dataset.restore); saveDismissed()", "dismissed.add(b.dataset.restore); saveDismissed()")),
    (preferences, "reads not scoped", lambda p: p.replace("const r = await fetch(scoped(p));", "const r = await fetch(p);")),
    (preferences, "other projects silent", lambda p: p.replace("x.p !== proj && x.pending > (projSeen[x.root] ?? x.pending)", "false")),
    (released_holds, "released question disabled", lambda p: p.replace('${a.held || a.released ? "" : " disabled"}', '${a.held ? "" : " disabled"}')),
    (released_holds, "released turn silent", lambda p: p.replace("    : a.released ? goneHTML(a, n)\n    : a.turns", "    : a.turns")),
    (released_holds, "no copy for a turn", lambda p: p.replace("<button type=\"button\" data-copy data-gone-for=", "<button type=\"button\" data-gone-for=")),
    (visible_page, "always visible", lambda p: p.replace('(document.visibilityState === "hidden" ? "0" : "1")', '"1"')),
    (whole_turn, "cut message", lambda p: p.replace("${esc(a.message || a.detail)}</div>", "${esc(a.detail).slice(0, 600)}</div>")),
    (whole_turn, "not scrolled", lambda p: p.replace("el.scrollTop = el.scrollHeight", "el.scrollTop = 0")),
    (quiet_turns, "idle beeps", lambda p: p.replace("|| !loud(a)) return;", ") return;")),
    (whole_finding, "summary only", lambda p: p.replace("${esc(x.full_text || x.summary)}", "${esc(x.summary)}")),
    (sidebar, "always grouped", lambda p: p.replace("grouped = g.tasks.length > 1", "grouped = true")),
    (sidebar, "clamp saved", lambda p: p.replace("const w = Math.max(200, Math.min(sideMax(), sideChosen));", "const w = sideChosen = Math.max(200, Math.min(sideMax(), sideChosen));")),
    (sidebar, "done open", lambda p: p.replace("if (dd.open !== sideDoneOpen) dd.open = sideDoneOpen;", "dd.open = true;")),
    (top_bar, "Escape ignored", lambda p: p.replace('if (e.key === "Escape") { e.stopPropagation(); close(true) }', "")),
    (top_bar, "theme outside", lambda p: p.replace('    <button id="theme-btn" type="button"></button>\n', "").replace('<div id="menu" class="popover menu" hidden>', '<button id="theme-btn" type="button"></button><div id="menu" class="popover menu" hidden>')),
    (calm_panel, "red badges", lambda p: p.replace("color:#111; background:var(--warn); border-radius:9px", "color:#fff; background:var(--bad); border-radius:9px")),
    (calm_panel, "no fold hint", lambda p: p.replace('el.classList.toggle("fold", over);', "")),
    (not_applicable, "na ignored", lambda p: p.replace("${s.na ? t(\"verdict.na\") : v ?", "${v ?")),
    (preferences, "no live stream", lambda p: p.replace('new EventSource(scoped("api/events?t=" + encodeURIComponent(TOKEN)))', 'new EventSource("api/events")')),
)


def main():
    page = open(PAGE, encoding="utf-8").read()
    for check in CHECKS:
        check(page)
        print(f"ok    {check.__name__}")
    caught = 0
    for check, label, mutate in MUTATIONS:
        broken = mutate(page)
        if broken == page:
            print(f"FAIL  mutation {label!r} did not apply: the page changed under the test")
            sys.exit(1)
        try:
            check(broken)
        except (AssertionError, ValueError, AttributeError, KeyError) as e:
            caught += 1
            print(f"bites {check.__name__}: {label}: {str(e)[:100]}")
            continue
        print(f"FAIL  {check.__name__} passes on a page with {label}")
        sys.exit(1)
    print(f"test_page: {len(CHECKS)} checks, {caught} mutations caught")


if __name__ == "__main__":
    main()
