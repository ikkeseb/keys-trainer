"""The lesson engine: steps, hints, misses, beats and outcomes over a polled session.

No terminal calls and no wall clock: the shell drives it with `tick(now)` about 20 times
a second, passes forwarded key tokens to `forwarded(raw)`, the hint key to `hint()` and
the pause key to `pause()`, then draws `bar()` under the native view and acts on
`drain()` events. Design §§ 1, 3 and 4.

States: running (the user's turn), beat (success shown, keys dropped), showing («Show me»
plays), paused (menu: resume, skip, redo, quit), lesson_done.
Events: ("step", index) as each step begins; ("typing", bool) for the router; ("reset",)
after a rebuild or playback (call `router.reset()`); ("result", StepResult) per finished
step; ("done", LessonResult).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, replace

import checks
import keyscan
import layouts
from coach import Bar
from curriculum import Binding, Move, Resolved, Scenario, chord_text
from router import canonical, key_name
from screen import BOLD, GOLD, INK, MUTED, Card, Text
from session import perform
from strings import LESSON as TEXT, MOVES, NOTICE, PLACES
from world import World, adjacent_side, describe

RUNNING, BEAT, SHOWING, PAUSED, DONE = "running", "beat", "showing", "paused", "lesson_done"
SETTLE = 0.15        # a change counts once the world has been still this long (today's WRONG_SETTLE)
BEAT_SECONDS = 0.9
BUILD_BEAT = 1.4
WARMUP_DONE = 1.2
OFFER_AFTER = 10.0   # «? Hint» turns gold after this long without progress
SHOW_LEAD = 1.2      # «Show me»: a key is on screen this long before it is pressed
SHOW_GAP = 0.9       # after a press, to see what it did before the next key shows
SHOW_PAUSE = 1.6     # after the last press
SHOWN_BEAT = 3.0     # then the result, with all the keys still up
TRAIL = 64
WARMUP_CAP = 6
DOTS = {"met": "done", "clean": "clean", "nudged": "nudged", "shown": "shown", "skipped": "shown"}
# Three-line target maps for BUILD steps, by blueprint; ● marks where focus must end.
MAPS = {
    "twin-rows": ("┌──────┐", "├──────┤", "└──────┘"),
    "twin-columns": ("┌───┬───┐", "│   │   │", "└───┴───┘"),
    "right-stack": ("┌────┬───┐", "│    ├───┤", "└────┴───┘"),
    "split-bottom": ("┌───────┐", "├───┬───┤", "└───┴───┘"),
    "grid": ("┌───┬───┐", "├───┼───┤", "└───┴───┘"),
    "console": ("┌─────┬───┐", "├──┬──┼───┤", "└──┴──┴───┘"),
}
FOCUS_MAPS = {("right-stack", "left"): ("┌────┬───┐", "│ ●  ├───┤", "└────┴───┘")}
BLUEPRINTS = {blueprint.id: blueprint for blueprint in layouts.BLUEPRINTS}
CLOSE_PANE = "herdr:close_pane"


@dataclass(frozen=True)
class PlanStep:
    kind: str  # learn | recall | build
    scenario: Scenario
    fixture: str
    requeued: bool = False
    missing: tuple = ()  # a notice step: `keyscan.bind_advice` for each card with no key


@dataclass(frozen=True)
class Plan:
    title: str
    lesson: str | None  # None for a warm-up
    steps: tuple[PlanStep, ...]
    bindings: Mapping[str, Binding]
    actions: tuple[str, ...]  # card ids the private session binds
    fixture: str
    warmup: bool = False


@dataclass(frozen=True)
class StepResult:
    kind: str
    scenario: str
    cards: tuple  # practice.Card for each scenario card (history keys)
    outcome: str  # met (LEARN done) | skipped (LEARN skipped) | clean | nudged | shown
    seconds: float | None  # kept for clean outcomes only
    hint_level: int  # highest ladder level reached: 0..2, 3 for «Show me»
    misses: int
    skipped: bool = False
    requeued: bool = False
    notice: bool = False  # a step with no key in the user's setup, skipped from its notice


def scored(steps):
    """The steps that count for stars: a notice step is the setup's gap, not the user's miss."""
    return tuple(step for step in steps if not getattr(step, "notice", False))


@dataclass(frozen=True)
class LessonResult:
    lesson: str | None
    title: str
    steps: tuple[StepResult, ...]
    complete: bool
    seconds: float
    warmup: bool = False


def plan_lesson(resolved: Resolved, lesson_id: str, *, test_out: bool = False) -> Plan:
    """The active and notice steps of a lesson, in order; `test_out` («I know this») keeps
    only RECALL and BUILD. A notice step (a card with no key) carries how to add the key."""
    state = resolved.lesson(lesson_id)
    lesson = state.lesson
    scenarios = resolved.curriculum.scenarios
    steps = []
    for step in state.plan or state.steps:
        if test_out and step.kind == "learn":
            continue
        scenario = scenarios[step.scenario]
        item = resolved.scenarios.get(step.scenario)
        missing = tuple(keyscan.bind_advice(resolved.data, card) for card in item.missing) if item and item.notice else ()
        steps.append(PlanStep(step.kind, scenario, scenario.fixture or lesson.fixture, missing=missing))
    steps = tuple(steps)
    active = tuple(step for step in steps if not step.missing)
    return Plan(lesson.title, lesson.id, steps, resolved.bindings, session_actions(resolved, active), lesson.fixture)


def session_actions(resolved: Resolved, steps) -> tuple[str, ...]:
    """The cards the private session binds for these steps. A build that must add a tab also
    binds close pane: closing an extra new tab's last pane closes it, and the next-move hint
    names that way out."""
    actions = resolved.actions(step.scenario.id for step in steps)
    if (CLOSE_PANE in resolved.bindings and CLOSE_PANE not in actions
            and any(step.kind == "build" and any(item["check"] == "tab_added" for item in step.scenario.checks)
                    for step in steps)):
        actions += (CLOSE_PANE,)
    return actions


def plan_warmup(resolved: Resolved, card_ids, *, tool: str = "herdr", cap: int = WARMUP_CAP,
                build: str | None = None, title: str | None = None) -> Plan | None:
    """RECALL steps for due cards, grouped by fixture so the session rebuilds as rarely as possible.

    Each card is practised through the first recall scenario that names it (or, for a card
    taught only in a LEARN step, that step's scenario); at most `cap` steps. «Today's round»
    adds a `build` scenario last and its own `title`.
    """
    found: dict[str, PlanStep] = {}
    for card in card_ids:
        pick = None
        for kind in ("recall", "learn"):
            for state in resolved.visible:
                if state.lesson.tool != tool:
                    continue
                for step in state.steps:
                    scenario = resolved.curriculum.scenarios[step.scenario]
                    if step.kind == kind and card in scenario.cards and pick is None:
                        pick = PlanStep("recall", scenario, scenario.fixture or state.lesson.fixture)
            if pick:
                break
        if pick and pick.scenario.id not in found:
            found[pick.scenario.id] = pick
    order = list(dict.fromkeys(step.fixture for step in found.values()))
    steps = tuple(sorted(found.values(), key=lambda step: order.index(step.fixture)))[:cap]
    if build is not None:
        state = next(state for state in resolved.visible for step in state.steps if step.scenario == build)
        scenario = resolved.curriculum.scenarios[build]
        steps += (PlanStep("build", scenario, scenario.fixture or state.lesson.fixture),)
    if not steps:
        return None
    return Plan(title or TEXT["warmup_title"], None, steps, resolved.bindings, session_actions(resolved, steps),
                steps[0].fixture, warmup=True)


def keys_line(plan: Plan, scenario: Scenario, every: bool = False) -> str:
    """The user's keys for the whole solution: «Ctrl + Alt + →, ↓», «Ctrl + Space → z ×2».

    `every` lists each card's alternatives («Alt + → or Ctrl + Space → n, then …»).
    The bar, the intro card and the lesson-complete card all spell keys this way.
    """
    parts: list[list] = []  # [identity, count, label, chord]
    for move in scenario.solution:
        if move.card and move.card in plan.bindings:
            binding = plan.bindings[move.card]
            ident = ("card", move.card, move.number)
            label = TEXT["or"].join(binding.labels(move.number)) if every else binding.label(move.number)
            chord = binding.taught
        elif move.press == "backspace":
            ident, label, chord = ("clear",), TEXT["clear"], None
        elif move.press:
            ident, label, chord = ("press", move.press, id(move)), chord_text(move.press), None
        elif move.text:
            ident, label, chord = ("type", id(move)), TEXT["type"].format(text=move.text), None
        else:
            continue
        if parts and parts[-1][0] == ident:
            parts[-1][1] += 1
        else:
            parts.append([ident, 1, label, chord])
    shown = []
    for index, (ident, count, label, chord) in enumerate(parts):
        previous = parts[index - 1][3] if index else None
        if not every and chord and previous and _same_modifiers(chord, previous):
            label = chord_text(chord.rsplit("+", 1)[-1], number=ident[2])
        if ident[0] == "card" and count > 1:
            label += TEXT["times"].format(count=count)
        shown.append(label)
    joiner = TEXT["then"] if every else TEXT["list"]
    return joiner.join(shown)


def move_label(plan: Plan, move: Move) -> str:
    """One move as the bar spells it: the taught chord, a key, or the text to type."""
    if move.card:
        binding = plan.bindings.get(move.card)
        return binding.label(move.number) if binding else move.card
    if move.press == "backspace":
        return TEXT["clear"]
    if move.press:
        return chord_text(move.press)
    if move.text:
        return TEXT["type"].format(text=move.text)
    return ""


def seconds_text(seconds: float) -> str:
    return TEXT["seconds"].format(seconds=f"{seconds:.1f}")


class Engine:
    """Run a plan against a session. See the module docstring for how a shell drives it."""

    def __init__(self, plan: Plan, session, *, hint_key: str = "?", pause_key: str = "q",
                 skip_key: str = "s",
                 history=None, sleep=time.sleep):
        self.plan = plan
        self.session = session
        self.hint_key, self.pause_key, self.skip_key = hint_key, pause_key, skip_key
        self.history = history  # optional: best_seconds(card) for the beat's remark
        self.sleep = sleep
        self.queue = list(plan.steps)
        self.results: list[StepResult | None] = [None] * len(self.queue)
        self.index = 0
        self.state = RUNNING
        self._seen = None
        self.offer = False  # «? Hint» should turn gold (10 s without progress)
        self.rebuilds = 0
        self.now = 0.0
        self._events = []
        self._typing = False
        self._final = False
        self._result = None
        self._prefix = canonical(next((b.prefix for b in plan.bindings.values() if b.prefix), ""))
        self._after_prefix = False
        # The user's live desktop: the trainer only watches, so there is no «Show me».
        self.live = bool(getattr(session, "live", False))

    # Driving.

    def begin(self, now: float) -> None:
        """Start the private session on the plan's fixture and set up the first step."""
        self.now = self._began = now
        world = self.session.start(self.plan.actions, self.plan.fixture)
        self._trail = [world]
        self._seen = self._settled = world
        self._seen_at = now
        self._enter_step(now)

    def tick(self, now: float) -> None:
        self.now = now
        if self.state in (DONE, PAUSED):
            return
        if self.notice and self.state == RUNNING:
            return  # nothing to watch: the step waits for the skip key
        world = self.session.world()
        typing = bool(self.session.typing())
        if typing != self._typing:
            self._typing = typing
            self._emit("typing", typing)
        if world != self._seen:
            self._seen, self._seen_at = world, now
            self._remember(world)
            if self.state == RUNNING:
                # Every look counts for the checks, so two quick moves still latch in order;
                # a change is judged once the world has settled.
                self._history.append(world)
                self._unjudged = True
        if self.state == BEAT:
            if now >= self._beat_until:
                self._finish(now) if self._final else self._advance(now)
            return
        if self.state == SHOWING:
            self._play(now)
            return
        if now - self._seen_at >= SETTLE:
            if self._seen != self._settled:
                previous, self._settled = self._settled, self._seen
                self._changed(previous, self._settled, now)
            elif self._unjudged:
                self._returned(now)
        if self.state == RUNNING:
            self.offer = self.level < 3 and now - self._progress_at >= OFFER_AFTER

    def forwarded(self, raw: bytes) -> bool:
        """A key token the router forwarded. Fed to the session only on the user's turn (never on
        a notice step); returns whether."""
        if self.state != RUNNING or self.notice:
            return False
        self.session.feed(raw)
        self._touched = True
        name = key_name(raw)
        if self._after_prefix:
            self._after_prefix = False
            self._keys.append(f"prefix+{name}")
        elif name and name == self._prefix:
            self._after_prefix = True
        elif name:
            self._keys.append(name)
        return True

    def hint(self) -> bool:
        """The hint key (decision 7): once, every chord the user has for it; twice, «Show me»
        plays the solution and the step completes as shown. In LEARN the keys are on screen
        already, so the first press is «Show me». A notice step has no keys to show."""
        if self.state != RUNNING or self.notice:
            return False
        self.offer = False
        if self.live:  # the ladder stops at the keys: the trainer never presses the user's
            self.level = self.max_level = 2
            self._line = (self._keys_text(), "hint")
            return True
        if self.step.kind == "learn" or self.level >= 2:
            return self._show(self.now)
        self.level = self.max_level = 2
        self._line = (self._keys_text(), "hint")
        return True

    def pause(self) -> bool:
        if self.state != RUNNING:
            return False
        self.state, self._paused_at, self.offer = PAUSED, self.now, False
        return True

    def resume(self) -> bool:
        if self.state != PAUSED:
            return False
        gap = self.now - self._paused_at
        self._attempt_at += gap
        self._progress_at += gap
        self._seen_at += gap
        self.state = RUNNING
        return True

    def skip(self) -> bool:
        """Leave the step: a RECALL counts as shown and comes back once; a LEARN is not marked met.
        A notice step is recorded as skipped, comes back never and costs no star."""
        if self.state not in (RUNNING, PAUSED):
            return False
        step = self.step
        outcome = "skipped" if step.kind == "learn" or step.missing else "shown"
        self._record(outcome, None, skipped=True)
        self.state = RUNNING
        self._advance(self.now)
        return True

    def redo(self) -> bool:
        """Set the step up again from its fixture; help already given still counts."""
        if self.state != PAUSED:
            return False
        self.state = RUNNING
        if self.notice:
            return True  # nothing to set up
        self._start_attempt(self._rebuild(self.step), self.now)
        self._line = None
        return True

    def quit(self) -> bool:
        if self.state == DONE:
            return False
        self._finish(self.now, complete=False)
        return True

    def drain(self) -> list[tuple]:
        events, self._events = self._events, []
        return events

    @property
    def step(self) -> PlanStep:
        return self.queue[min(self.index, len(self.queue) - 1)]

    @property
    def result(self) -> LessonResult | None:
        return self._result

    @property
    def notice(self) -> tuple:
        """`keyscan.bind_advice` for each card of this step with no key; () for a normal step."""
        return self.step.missing if self.state != DONE else ()

    @property
    def skippable(self) -> bool:
        """The skip key is the trainer's on this step: a notice, or a key awkward on the layout."""
        return bool(self.notice or self.awkward)

    @property
    def awkward(self) -> str:
        """The layout's name when this step needs a key that is hard to type on it, else ""."""
        scenario = self.step.scenario
        used = (self.plan.bindings.get(move.card) for move in (*scenario.solution, *scenario.setup) if move.card)
        return next((b.awkward_on for b in used if b and b.awkward_on), "")

    # What to draw.

    def bar(self, typing: bool = False) -> Bar:
        """The coach bar for this moment. `typing`: the router's prompt state (herdr prompts)."""
        step, scenario = self.step, self.step.scenario
        common = {"hint_key": self.hint_key, "pause_key": self.pause_key, "title": self.plan.title,
                  "steps": self._dots(), "target": self._target(step)}
        if self.state == DONE:
            return Bar(TEXT["tag_done"], TEXT["lesson_done"], hint=None, **common)
        if self.state == BEAT:
            if self._final:
                clean = sum(1 for r in self.results if r and r.outcome == "clean")
                goal = TEXT["warmup_done"].format(title=self.plan.title, clean=clean, total=len(self.results))
                return Bar(TEXT["tag_done"], goal, kind="success", hint=None, **common)
            return Bar(TEXT["tag_done"], scenario.done, line=self._beat_line, kind="success", hint=None, **common)
        if self.state == PAUSED:
            return Bar(TEXT["tag_pause"], scenario.goal, line=TEXT["pause_line"], hint=None,
                       controls=self.pause_controls(), **common)
        if self.notice:
            actions = TEXT["list"].join(item["action"] for item in self.notice)
            return Bar(NOTICE["tag"], scenario.goal, line=NOTICE["line"].format(actions=actions, key=self.skip_key),
                       kind="warn", hint="", skip_key=self.skip_key, **common)
        tag = TEXT["tag_live"] if self.live else TEXT["tag_" + step.kind]
        chord = self._keys_line(scenario) if step.kind == "learn" else ""
        if self.state == SHOWING:
            played = TEXT["list"].join(self._played) or "…"
            line = TEXT["watch"].format(keys=played)
            return Bar(tag, scenario.goal, chord=chord, line=line, kind="hint", hint=None, **common)
        layout = self.awkward
        warning = (TEXT["awkward"].format(layout=layout, key=self.skip_key), "warn") if layout else None
        if self.live:
            line, kind = self._line or warning or (self._live_line(), "reason")
        else:
            line, kind = self._line or warning or self._checklist() or (scenario.why, "reason")
        # Yazi's menu of next keys («which») waits for a key, not for text.
        waiting = self._seen is not None and self._seen.yazi is not None and self._seen.yazi.layer == "which"
        controls = TEXT["typing"] if (typing or self._typing) and not waiting else ""
        return Bar(tag, scenario.goal, chord=chord, line=line, kind=kind, hint=self._hint_label(),
                   controls=controls, skip_key=self.skip_key if layout else "", **common)

    def pause_controls(self) -> str:
        """The pause menu, as the shell maps its keys: Enter, s, r and the pause key."""
        return "   ".join((f"Enter {TEXT['pause_resume']}", f"s {TEXT['pause_skip']}",
                           f"r {TEXT['pause_redo']}", f"{self.pause_key} {TEXT['pause_quit']}"))

    # Steps.

    def notice_card(self) -> Card | None:
        """The notice for a step whose key is missing from the user's setup: what the action
        does, the file to add the key to and the line to add. None for a normal step."""
        if not self.notice:
            return None
        scenario = self.step.scenario
        lines = [((scenario.goal, BOLD),), Text(scenario.why, MUTED)]
        for item in self.notice:
            tool = NOTICE["tools"].get(item["tool"], item["tool"])
            lines += ["", Text(NOTICE["missing"].format(action=item["action"], tool=tool), INK)]
            if item["tool"] == "desktop" or not item["line"]:
                lines.append(Text(NOTICE["desktop"].format(action=item["action"]), INK))
                continue
            lines.append(Text(NOTICE["where_" + item["tool"]].format(path=item["path"]), INK))
            lines.append((("  " + item["line"], GOLD),))
            if item["taken"]:
                lines.append(Text(NOTICE["taken"].format(tool=tool, chord=item["default"], taken=item["taken"]), MUTED))
            elif not item["default"]:
                lines.append(Text(NOTICE["placeholder"], MUTED))
        lines += ["", Text(NOTICE["after"], MUTED)]
        footer = (NOTICE["skip"].format(key=self.skip_key), NOTICE["pause"].format(key=self.pause_key))
        return Card(NOTICE["header"], self.plan.title, tuple(lines), footer, header_style=GOLD)

    def _enter_step(self, now):
        self._emit("step", self.index)
        if self.step.missing:  # a notice: the session is not touched until the step is skipped
            self.level = self.max_level = self.misses = 0
            self.showed = self.nudged = False
            self._line = None
            self._step_at = now
            self._start_attempt(self._seen, now)
            return
        if hasattr(self.session, "arm"):
            self.session.arm()  # what happened before the step is the present, not a move
        if hasattr(self.session, "vary"):
            self.queue[self.index] = replace(self.step, scenario=self.session.vary(self.step.scenario))
        step = self.step
        self.level = self.max_level = self.misses = 0
        self.showed = self.nudged = False
        self._line = None
        self._step_at = now
        world = self.session.world()
        self._seen = self._settled = world
        self._remember(world)
        if checks.unmet(step.scenario.needs, world, self._trail):
            world = self._rebuild(step)
        self._start_attempt(world, now)

    def _start_attempt(self, world, now):
        self._start = world
        self._history: list[World] = []
        self._earlier = tuple(self._trail)
        self._latched = 0
        self._unjudged = False  # worlds seen since the last judgement
        self._moves_done = 0
        self._attempt_at = self._progress_at = now
        self._keys: list[str] = []
        self._after_prefix = False
        self._touched = False  # a key reached the session, seen in the world or not
        self._typed_from = None  # the settled world when a prompt opened, judged once it closes
        self.offer = False

    def _rebuild(self, step):
        world = self.session.apply_fixture(step.fixture)
        self._trail = [world]
        for move in step.scenario.setup:
            perform(self.session, move, self._taught(move), self.sleep)
            world = self._settle_now(world)
        self._seen = self._settled = world
        self._seen_at = self.now
        self.rebuilds += 1
        self._emit("reset")
        return world

    def _settle_now(self, previous, polls=75, quiet=3, patience=12):
        """Wait (really) for a setup move to land: changed and still for a few polls."""
        last, still = previous, 0
        for _ in range(polls):
            world = self.session.world()
            if world != last:
                last, still = world, 0
                self._remember(world)
            else:
                still += 1
            if still >= (quiet if last != previous else patience):
                break
            self.sleep(0.02)
        return last

    def _changed(self, previous, world, now):
        step, scenario = self.step, self.step.scenario
        self._unjudged = False
        progress = checks.evaluate(scenario.checks, self._start, self._history, self._earlier)
        if progress.done:
            self._succeed(now)
            return
        if self._typing:
            # A prompt shows each letter as it is typed (Yazi's filter); judge once it closes.
            if self._typed_from is None:
                self._typed_from = previous
            if progress.latched > self._latched:
                self._latched = progress.latched
                self._progressed(now)
            return
        if self._typed_from is not None:
            previous, self._typed_from = self._typed_from, None
            if world == previous:  # the prompt was cancelled: nothing to judge
                self._keys = []
                return
        keys, self._keys = self._keys, []
        if progress.latched > self._latched:
            self._latched = progress.latched
            self._progressed(now)
        elif self.live:
            self._live_changed(world, now)
        elif step.kind == "build" and scenario.blueprints and world.herdr:
            if self._dead_end(world):
                self._miss(previous, world, now, dead_end=True)
            else:
                self._progressed(now)
        elif keys and self._on_track(keys[-1]):
            self._progressed(now)  # a first resize press, a move towards a build
        elif keys and not self._moving_about(previous, world):
            self._miss(previous, world, now)

    def _moving_about(self, previous, world):
        """In a Yazi step of several moves, moving the cursor or changing folder is never a miss:
        there is more than one way there, and the checks judge where it ends."""
        before, after = previous.yazi, world.yazi
        if before is None or after is None or sum(1 for m in self.step.scenario.solution if m.card) < 2:
            return False
        return before == replace(after, cwd=before.cwd, hovered=before.hovered, entries=before.entries,
                                 layer=before.layer)

    def _returned(self, now):
        """The world moved and settled back where it was (a round trip faster than the settle):
        the checks still see every world on the way, so it can succeed; it is never a miss."""
        self._unjudged = False
        progress = checks.evaluate(self.step.scenario.checks, self._start, self._history, self._earlier)
        if progress.done:
            self._keys = []
            self._succeed(now)
        elif progress.latched > self._latched:
            self._latched = progress.latched
            self._progressed(now)

    def _live_changed(self, world, now):
        """A change that latched nothing on the live desktop. While the user is away the bar
        cannot be read, so a miss is told once the user is back where the step started, about the trip."""
        home = self._start.desktop.place
        if world.desktop.place != home:
            return
        away = next((w for w in reversed(self._history) if w.desktop.place != home), None)
        if away is not None:
            self._miss(self._start, world, now, what=describe(self._start, away))

    def _progressed(self, now):
        self._progress_at = now
        self.offer = False
        self._line = None
        if self.step.kind == "build":
            self._moves_done += 1
            self.level = 0

    def _miss(self, previous, world, now, dead_end=False, what=None):
        step, scenario = self.step, self.step.scenario
        self.misses += 1
        what = what or describe(previous, world) or TEXT["miss_unknown"]
        if dead_end:
            self._start_attempt(self._rebuild(step), now)
            text = TEXT["miss_build"].format(what=what)
        elif step.kind == "build":
            text = TEXT["miss_here"].format(what=what)  # a build keeps what is there
        elif not checks.unmet(scenario.needs, world, self._trail):
            self._start_attempt(world, now)
            text = TEXT["miss_here"].format(what=what)
        else:
            self._start_attempt(self._rebuild(step), now)
            text = TEXT["miss_rebuild"].format(what=what)
        self._line = (text, "miss")
        if step.kind != "learn" and self.misses >= 2 and self.level == 0:
            # The quiet offer: the direction, once the user has missed twice (outcome nudged).
            self.nudged = True
            self.max_level = max(self.max_level, 1)
            self._line = (self._nudge_text(), "hint")

    def _succeed(self, now):
        step = self.step
        seconds = max(0.0, self._seen_at - self._attempt_at)
        outcome = "met" if step.kind == "learn" else self._outcome()
        self._beat_line = self._remark(step, outcome, seconds)
        if self.showed:
            self._beat_line = TEXT["shown_keys"].format(keys=self._keys_line(step.scenario), remark=self._beat_line)
        self._record(outcome, seconds if outcome == "clean" else None)
        self.state = BEAT
        self.offer = False
        beat = SHOWN_BEAT if self.showed else BUILD_BEAT if step.kind == "build" else BEAT_SECONDS
        self._beat_until = now + beat

    def _outcome(self):
        if self.showed or self.max_level >= 2:
            return "shown"
        return "nudged" if self.nudged or self.misses else "clean"

    def _record(self, outcome, seconds, skipped=False):
        step = self.step
        cards = () if step.missing else tuple(self.plan.bindings[c].practice for c in step.scenario.cards
                                               if c in self.plan.bindings)
        result = StepResult(step.kind, step.scenario.id, cards, outcome, seconds, self.max_level, self.misses,
                            skipped, step.requeued, bool(step.missing))
        self.results[self.index] = result
        self._emit("result", result)
        if step.kind == "recall" and outcome == "shown" and not step.requeued:
            # Back once after two other steps, and before the build.
            build = next((i for i in range(self.index + 1, len(self.queue)) if self.queue[i].kind == "build"),
                         len(self.queue))
            at = min(self.index + 3, build)
            self.queue.insert(at, replace(step, requeued=True))
            self.results.insert(at, None)

    def _advance(self, now):
        self.index += 1
        if self.index < len(self.queue):
            self.state = RUNNING
            self._enter_step(now)
        elif self.plan.warmup:
            self.index = len(self.queue) - 1
            self._final, self.state, self._beat_until = True, BEAT, now + WARMUP_DONE
        else:
            self.index = len(self.queue) - 1
            self._finish(now)

    def _finish(self, now, complete=True):
        self.state = DONE
        self.offer = False
        done = tuple(r for r in self.results if r is not None)
        self._result = LessonResult(self.plan.lesson, self.plan.title, done, complete, now - self._began,
                                    self.plan.warmup)
        self._emit("done", self._result)

    # «Show me».

    def _show(self, now):
        """Play the solution with the user's keys shown, always whole from the step's start: a build,
        a step the user has begun (a check latched or the world moved) or a state that no longer
        fits its needs is set up again first."""
        step = self.step
        moves = step.scenario.solution
        if not moves or self.live:
            return False
        if (step.kind == "build" or self._latched or self._touched or self._seen != self._start
                or checks.unmet(step.scenario.needs, self._seen, self._trail)):
            self._start_attempt(self._rebuild(step), now)
        self.showed = True
        self.level = self.max_level = 3
        self.state = SHOWING
        self._pending = list(moves)
        self._played: list[str] = []
        self._play_at, self._show_end, self._announced = now, None, None
        return True

    def _play(self, now):
        """One move at a time: its key shows, then it is pressed, then its effect stays a moment."""
        if now < self._play_at:
            return
        if self._announced is not None:
            move, self._announced = self._announced, None
            perform(self.session, move, self._taught(move), self.sleep)
            self._pressed(now, SHOW_GAP + move.wait)
        elif self._pending:
            move = self._pending.pop(0)
            label = self._move_label(move)
            if label:
                self._played.append(label)
                self._announced, self._play_at = move, now + SHOW_LEAD
            else:  # a pause: nothing to show
                perform(self.session, move, self._taught(move), self.sleep)
                self._pressed(now, move.wait)
        elif self._show_end is not None and now >= self._show_end:
            self._after_show(now)

    def _pressed(self, now, gap):
        self._play_at = now + gap
        if not self._pending:
            self._show_end = now + SHOW_PAUSE
            self._play_at = now

    def _after_show(self, now):
        """The solution has played: the step completes as shown and the lesson moves on;
        the re-queue brings a RECALL back once, for the user to do."""
        world = self.session.world()
        self._seen = self._settled = world
        self._seen_at = now
        self._remember(world)
        self._emit("reset")
        self._succeed(now)

    # Copy.

    def _hint_label(self):
        if self.live:
            return TEXT["label_keys"]
        if self.step.kind == "learn" or self.level >= 2:
            return TEXT["label_show"]
        return TEXT["label_keys"]

    def _keys_text(self):
        """Every chord the user has for the step; in a build, for its next move."""
        step, scenario = self.step, self.step.scenario
        if step.kind == "build":
            move = self._next_move(self._settled)
            if move is not None:
                keys = TEXT["list"].join(filter(None, map(self._move_label, move[1])))
                return TEXT["keys"].format(keys=TEXT["next"].format(what=keys))
        return TEXT["keys"].format(keys=self._keys_line(scenario, every=True))

    def _nudge_text(self):
        """The direction, never the chord: the scenario's nudge, or a build's next move."""
        if self.step.kind == "build" and (move := self._next_move(self._settled)) is not None:
            return TEXT["next"].format(what=move[0])
        return self.step.scenario.nudge

    def _remark(self, step, outcome, seconds):
        scenario = step.scenario
        if outcome == "met":
            others = [label for card in dict.fromkeys(m.card for m in scenario.solution if m.card)
                      if card in self.plan.bindings
                      for label in self.plan.bindings[card].labels(self._number(scenario, card))[1:]]
            return TEXT["also"].format(keys=TEXT["or"].join(others)) if others else seconds_text(seconds)
        if outcome == "shown":
            return TEXT["with_help"] if step.kind == "recall" and not step.requeued else TEXT["shown"]
        if outcome == "nudged":
            return TEXT["nudged"].format(time=seconds_text(seconds))
        card = next((self.plan.bindings[c].practice for c in scenario.cards if c in self.plan.bindings), None)
        best = self.history.best_seconds(card) if self.history is not None and card is not None else None
        if step.kind == "recall" and best is None:
            return TEXT["first_clean"].format(time=seconds_text(seconds))
        if best is not None and seconds < best:
            return TEXT["record"].format(time=seconds_text(seconds), best=seconds_text(best))
        return seconds_text(seconds)

    def _keys_line(self, scenario: Scenario, every: bool = False) -> str:
        return keys_line(self.plan, scenario, every)

    def _move_label(self, move: Move) -> str:
        return move_label(self.plan, move)

    def _live_line(self):
        """LIVE: say where this happens and name the way back before the user leaves (§ 5.9)."""
        last = self.step.scenario.solution[-1] if self.step.scenario.solution else None
        back = self._move_label(last) if last else ""
        return TEXT["live_line"].format(keys=back) if back else TEXT["live_only"]

    def _checklist(self):
        scenario = self.step.scenario
        if len(scenario.checks) < 2:
            return None
        marks = []
        for index, (item, text) in enumerate(zip(scenario.checks, scenario.texts)):
            if item.get("mode") == "hold":
                passed = checks.evaluate([item], self._start, self._history or [self._settled], self._earlier).done
            else:
                latches = [c for c in scenario.checks if c.get("mode", "latch") == "latch"]
                passed = latches.index(item) < self._latched if item in latches else False
            mark = TEXT["check_done" if passed else "check_todo"]
            marks.append(f"{mark} {text or index + 1}")
        return "    ".join(marks), "checklist"

    def _target(self, step):
        if step.kind != "build" or not step.scenario.blueprints:
            return ()
        blueprint = step.scenario.blueprints[0]
        focus = next((c["position"] for c in step.scenario.checks if c["check"] == "focus_at"), None)
        return FOCUS_MAPS.get((blueprint, focus), MAPS.get(blueprint, ()))

    def _dots(self):
        dots = []
        for index, _ in enumerate(self.queue):
            result = self.results[index]
            if result is not None:
                dots.append(DOTS[result.outcome] if not (result.outcome == "met" and result.hint_level) else "shown")
            elif index == self.index and self.state != DONE:
                dots.append("current")
            else:
                dots.append("todo")
        return tuple(dots)

    # Helpers.

    def _emit(self, *event):
        self._events.append(event)

    def _remember(self, world):
        if not self._trail or self._trail[-1] != world:
            self._trail.append(world)
            del self._trail[:-TRAIL]

    def _taught(self, move):
        binding = self.plan.bindings.get(move.card) if move.card else None
        return binding.taught if binding else None

    @staticmethod
    def _number(scenario, card):
        return next((m.number for m in scenario.solution if m.card == card and m.number), None)

    def _on_track(self, pressed):
        """The last key is one of the user's chords for a card the solution plays (in a build, also
        one of its keys: the Enter that ends a rename). A wrong name typed in RECALL is a miss."""
        solution = self.step.scenario.solution
        if self.step.kind == "build" and any(canonical(m.press) == pressed for m in solution if m.press):
            return True
        for card in dict.fromkeys(m.card for m in solution if m.card):
            binding = self.plan.bindings.get(card)
            for chord in (*binding.chords, *binding.ambiguous) if binding else ():
                # A sequence («g, g») arrives one key at a time: each of its keys is on track.
                if pressed in map(canonical, chord.split(", ")):
                    return True
                name = canonical(chord)
                if name == pressed or ("1..9" in name and any(name.replace("1..9", d) == pressed
                                                               for d in "123456789")):
                    return True
        return False

    # BUILD next move.

    def _blueprint_tab(self, world, item):
        """The tab a blueprint check judges: one with its tab_label (a new one, with new = true),
        else the focused tab, which is where the user is building."""
        herdr = world.herdr
        if herdr is None:
            return None
        if item.get("tab_label") is not None:
            found = checks.labelled_tabs(self._start, world, item["tab_label"], item.get("new", False))
            if found:
                return next((tab for tab in found if tab.id == herdr.focused_tab_id), found[0])
        return herdr.tab

    def _dead_end(self, world):
        """A blueprint the panes can no longer grow into (an extra or wrong split)."""
        return any(not _assess(self._blueprint_tab(world, item), item["blueprint"]).recoverable
                   for item in self.step.scenario.checks if item["check"] == "blueprint")

    def _next_move(self, world):
        """(what, moves) for the next move of a build, or None when the layout is a dead end.

        Checks are taken in order: a tab the build must add comes before any split in it."""
        scenario = self.step.scenario
        groups = _groups(scenario.solution)
        for item in scenario.checks:
            if item["check"] == "tab_added" and not self._holds(item, world):
                return self._tab_move(world, groups)
        tab = world.herdr.tab if world.herdr else None
        for item in (item for item in scenario.checks if item["check"] == "blueprint"):
            target = self._blueprint_tab(world, item)
            assessment = _assess(target, item["blueprint"])
            if assessment.solved:
                continue
            if not assessment.recoverable:
                return None
            if target is not None and (tab is None or target.id != tab.id):
                return self._switch_move(world, target.id)
            if assessment.needs_focus:
                return self._focus_move(tab, assessment.pane_id)
            action = "split_horizontal" if assessment.direction == "down" else "split_vertical"
            card = f"herdr:{action}"
            if card in self.plan.bindings:
                return MOVES[action].format(where=PLACES["focused"]), (Move(card=card),)
            break
        if scenario.blueprints and tab is not None:
            for item in scenario.checks:
                if item["check"] == "focus_at" and not checks.evaluate(
                        [item], self._start, [world], self._earlier).done:
                    target = next((pane for pane in tab.panes if checks._touches(tab, pane.id, item["position"])),
                                  None)
                    if target is not None and target.id != tab.focused_pane_id:
                        return self._focus_move(tab, target.id)
        for item in scenario.checks:
            if item["check"] == "tab_at" and world.herdr and not self._holds(item, world):
                workspace = world.herdr.workspace
                if workspace is not None and item["position"] <= len(workspace.tabs):
                    return self._switch_move(world, workspace.tab_ids[item["position"] - 1])
        if not groups:
            return None
        moves = groups[min(self._moves_done, len(groups) - 1)]
        return self._what(moves), moves

    def _holds(self, item, world):
        return checks.evaluate([{**item, "mode": "hold"}], self._start, [world], self._earlier).done

    def _tab_move(self, world, groups):
        """Toward a tab_added check: open the new tab, go to it, then give it its name."""
        added = checks.added_tabs(self._start, world)
        if added and len(added) > 1:
            # One new tab too many: keep the first that has the label (else the first new one)
            # and close the others, each by closing its panes.
            label = next((item.get("label") for item in self.step.scenario.checks
                          if item["check"] == "tab_added"), None)
            keep = next((tab for tab in added if label is not None and tab.label == label), added[0])
            extra = [tab for tab in added if tab.id != keep.id]
            if CLOSE_PANE not in self.plan.actions:
                return None
            if world.herdr.focused_tab_id not in {tab.id for tab in extra}:
                return self._switch_move(world, extra[0].id)
            moves = (Move(card=CLOSE_PANE),)
            return self._what(moves), moves
        if not added:
            card = "herdr:new_tab"
        elif world.herdr.focused_tab_id != added[0].id:
            return self._switch_move(world, added[0].id)
        else:
            card = "herdr:rename_tab"
        moves = next((group for group in groups if group[0].card == card), None)
        if moves is None and card in self.plan.bindings:
            moves = (Move(card=card),)
        return (self._what(moves), moves) if moves else None

    def _switch_move(self, world, tab_id):
        """Jump to the tab by its number."""
        workspace = world.herdr.workspace
        if workspace is None or tab_id not in workspace.tab_ids or "herdr:switch_tab" not in self.plan.bindings:
            return None
        position = workspace.tab_ids.index(tab_id) + 1
        if position > 9:
            return None
        moves = (Move(card="herdr:switch_tab", number=position),)
        return self._what(moves), moves

    def _focus_move(self, tab, pane_id):
        origin, target = tab.pane(tab.focused_pane_id), tab.pane(pane_id)
        if origin is None or target is None:
            return None
        side = adjacent_side(origin.rect, target.rect)
        if side is None:
            dx = (target.rect.x + target.rect.width / 2) - (origin.rect.x + origin.rect.width / 2)
            dy = (target.rect.y + target.rect.height / 2) - (origin.rect.y + origin.rect.height / 2)
            side = ("right" if dx > 0 else "left") if abs(dx) >= abs(dy) else ("down" if dy > 0 else "up")
        card = f"herdr:focus_pane_{side}"
        if card not in self.plan.bindings:
            return None
        return MOVES["focus"].format(where=_place(tab, pane_id)), (Move(card=card),)

    def _what(self, moves):
        card = next((m for m in moves if m.card), None)
        text = next((m.text for m in moves if m.text), "")
        action = card.card.partition(":")[2] if card else ""
        if action.startswith("resize_pane"):
            action = "resize"
        template = MOVES.get(action)
        if template is None:
            return MOVES["other"].format(keys=TEXT["list"].join(filter(None, map(self._move_label, moves))))
        return template.format(where=PLACES["focused"], text=text, number=card.number if card else "")


def _same_modifiers(chord, previous):
    if chord.startswith("prefix+") or previous.startswith("prefix+"):
        return False
    mods, _, _ = chord.rpartition("+")
    before, _, _ = previous.rpartition("+")
    return bool(mods) and mods == before


def _groups(solution):
    """Split a solution into moves: each card with the presses and text that follow it."""
    groups = []
    for move in solution:
        if move.card or not groups:
            groups.append([move])
        else:
            groups[-1].append(move)
    return [tuple(group) for group in groups]


def _assess(tab, blueprint):
    if tab is None or not tab.panes:
        return layouts.Assessment(False, False)
    panes = [layouts.Pane(p.id, p.rect.x, p.rect.y, p.rect.width, p.rect.height) for p in tab.panes]
    return layouts.assess(BLUEPRINTS[blueprint], panes, tab.focused_pane_id)


def _place(tab, pane_id):
    pane, area = tab.pane(pane_id), tab.area
    if pane is None or area is None:
        return PLACES["other"]
    left, right = pane.rect.x <= area.x, pane.rect.right >= area.right
    top, bottom = pane.rect.y <= area.y, pane.rect.bottom >= area.bottom
    across = "left" if left and not right else "right" if right and not left else ""
    down = "top" if top and not bottom else "bottom" if bottom and not top else ""
    return PLACES.get("-".join(filter(None, (down, across))) or "other", PLACES["other"])
