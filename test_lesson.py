import json
import unittest
from dataclasses import replace
from unittest import mock
from pathlib import Path

import curriculum
import lesson
from herdr_backend import kitty_keys
from lesson import BEAT, DONE, PAUSED, RUNNING, SHOWING, Engine, plan_lesson, plan_warmup
from router import canonical, key_name
from strings import LESSON as TEXT
from world import HerdrWorld, PaneState, Rect, TabState, World, WorkspaceState

ROWS = json.loads(Path(__file__).with_name("test_data").joinpath("rigpage_herdr.json").read_text("utf-8"))
DATA = {"groups": [{"id": "herdr", "rows": ROWS}]}
CURRICULUM = curriculum.loads({"herdr.toml": curriculum.FOLDER.joinpath("herdr.toml").read_text("utf-8")})
AREA = (0, 0, 100, 30)
YAZI_ROWS = json.loads(Path(__file__).with_name("test_data").joinpath("rigpage_yazi.json").read_text("utf-8"))
YAZI_DATA = {"groups": [{"id": "herdr", "rows": ROWS}, {"id": "yazi", "rows": YAZI_ROWS}]}
BOTH = curriculum.loads({name: curriculum.FOLDER.joinpath(name).read_text("utf-8")
                         for name in ("herdr.toml", "yazi.toml")})


def only(lesson_id, scenario_id, kind=None):
    """A plan of one step of a lesson (its fixture and bindings kept)."""
    def plan(resolved):
        full = plan_lesson(resolved, lesson_id)
        step = next(s for s in full.steps if s.scenario.id == scenario_id and kind in (None, s.kind))
        return replace(full, steps=(step,), actions=lesson.session_actions(resolved, (step,)))
    return plan


class FakeHerdr:
    """A small herdr: panes split and move focus by geometry, tabs, workspaces, prompts.

    The user's chords arrive as kitty bytes through `feed` (as the router forwards them) or as
    `play`; the world changes at once, so the engine's settle is the test's clock.
    """

    def __init__(self, resolved):
        self.bindings = resolved.bindings
        self.fixtures, self.fed, self.played = [], [], []
        self.typing_now = False
        self._prompt = None
        self._after_prefix = False
        self._ids = 0

    # Session protocol.

    def start(self, actions, fixture):
        self.actions = tuple(actions)
        return self.apply_fixture(fixture)

    def apply_fixture(self, name):
        self.fixtures.append(name)
        self._prompt, self._after_prefix = None, False
        practice = self._workspace("Practice")
        tab = practice["tabs"][0]
        tab["label"] = "Start"
        if name == "right_stack":
            left = tab["focus"]
            tab["panes"][left] = [0, 0, 50, 30]
            top, bottom = self._id("p"), self._id("p")
            tab["panes"][top] = [51, 0, 49, 15]
            tab["panes"][bottom] = [51, 16, 49, 14]
            tab["focus"] = bottom
        self.workspaces, self.focus = [practice], 0
        if name in ("four_tabs", "two_tabs"):
            for label in ("api", "web", "db") if name == "four_tabs" else ("api",):
                practice["tabs"].append(self._tab(label))
            practice["active"] = 1 if name == "four_tabs" else 0
        if name in ("two_workspaces", "three_workspaces"):
            for label in ("api", "docs")[: 1 if name == "two_workspaces" else 2]:
                self.workspaces.append(self._workspace(label))
            self.focus = len(self.workspaces) - 1
        return self.world()

    def world(self):
        workspaces = []
        for ws in self.workspaces:
            tabs = tuple(TabState(t["id"], t["label"], tuple(
                PaneState(pid, Rect(*rect), pid == t["focus"]) for pid, rect in t["panes"].items()),
                t["focus"], t["zoomed"], (), Rect(*AREA)) for t in ws["tabs"])
            workspaces.append(WorkspaceState(ws["id"], ws["label"], tabs, ws["tabs"][ws["active"]]["id"]))
        ws = self.workspaces[self.focus]
        tab = ws["tabs"][ws["active"]]
        return World(herdr=HerdrWorld(tuple(workspaces), ws["id"], tab["id"], tab["focus"]))

    def chords(self, card):
        return self.bindings[card].chords

    def play(self, card, *, index=0, number=None):
        self.played.append((card, number))
        self._act(card.partition(":")[2], number)
        return self.chords(card)[index]

    def press(self, chord):
        self.played.append(("press", chord))
        self._key(chord)

    def type_text(self, text):
        self.played.append(("type", text))
        if self._prompt:
            self._prompt[1] += text

    def feed(self, raw):
        self.fed.append(raw)
        name = key_name(raw)
        if self._prompt:
            self._key(name)
            return
        if self._after_prefix:
            self._after_prefix = False
            name = f"prefix+{name}"
        elif name == "ctrl+space":
            self._after_prefix = True
            return
        for card, binding in self.bindings.items():
            for chord in binding.chords:
                pattern = canonical(chord)
                if pattern == name or ("1..9" in pattern and name and name[-1].isdigit()
                                       and pattern.replace("1..9", name[-1]) == name):
                    self._act(card.partition(":")[2], int(name[-1]) if "1..9" in pattern else None)
                    return

    def typing(self):
        return self.typing_now

    def close(self):
        pass

    # The small herdr.

    def _id(self, kind):
        self._ids += 1
        return f"{kind}{self._ids}"

    def _tab(self, label):
        pane = self._id("p")
        return {"id": self._id("t"), "label": label, "panes": {pane: list(AREA)}, "focus": pane, "zoomed": False}

    def _workspace(self, label):
        return {"id": self._id("w"), "label": label, "tabs": [self._tab("1")], "active": 0}

    @property
    def _ws(self):
        return self.workspaces[self.focus]

    @property
    def _current(self):
        return self._ws["tabs"][self._ws["active"]]

    def _key(self, name):
        if not self._prompt:
            return
        kind, text = self._prompt
        if name == "backspace":
            self._prompt[1] = text[:-1]
        elif name == "enter":
            self._prompt = None
            if kind == "tab":
                self._current["label"] = text
            elif kind == "workspace":
                self._ws["label"] = text
            elif kind == "close":
                del self.workspaces[self.focus]
                self.focus = max(0, self.focus - 1)
        elif name == "esc":
            self._prompt = None
        elif name and len(name) == 1 and kind != "close":
            self._prompt[1] += name

    def _act(self, action, number=None):
        tab, ws = self._current, self._ws
        panes = tab["panes"]
        if action in ("split_horizontal", "split_vertical"):
            x, y, w, h = panes[tab["focus"]]
            new = self._id("p")
            if action == "split_horizontal":
                panes[tab["focus"]] = [x, y, w, h // 2]
                panes[new] = [x, y + h // 2 + 1, w, h - h // 2 - 1]
            else:
                panes[tab["focus"]] = [x, y, w // 2, h]
                panes[new] = [x + w // 2 + 1, y, w - w // 2 - 1, h]
            tab["focus"], tab["zoomed"] = new, False
        elif action.startswith("focus_pane_"):
            from world import adjacent_side
            side = action.removeprefix("focus_pane_")
            origin = Rect(*panes[tab["focus"]])
            near = sorted((rect[1], rect[0], pid) for pid, rect in panes.items()
                          if pid != tab["focus"] and adjacent_side(origin, Rect(*rect)) == side)
            if near:
                tab["previous"], tab["focus"] = tab["focus"], near[0][2]
        elif action == "last_pane" and tab.get("previous") in panes:
            tab["previous"], tab["focus"] = tab["focus"], tab["previous"]
        elif action == "close_pane" and len(panes) > 1:
            del panes[tab["focus"]]
            tab["focus"] = next(iter(panes))
        elif action == "close_pane" and len(ws["tabs"]) > 1:
            # Its last pane closes the tab; focus goes to the tab before it (measured, private herdr).
            del ws["tabs"][ws["active"]]
            ws["active"] = max(0, ws["active"] - 1)
        elif action == "zoom" and len(panes) > 1:
            tab["zoomed"] = not tab["zoomed"]
        elif action == "new_tab":
            ws["tabs"].append(self._tab(str(len(ws["tabs"]) + 1)))
            ws["active"] = len(ws["tabs"]) - 1
        elif action in ("next_tab", "previous_tab"):
            ws["active"] = (ws["active"] + (1 if action == "next_tab" else -1)) % len(ws["tabs"])
        elif action == "switch_tab" and number and number <= len(ws["tabs"]):
            ws["active"] = number - 1
        elif action == "rename_tab":
            self._prompt = ["tab", tab["label"]]
        elif action == "new_workspace":
            self.workspaces.append(self._workspace("~"))
            self.focus = len(self.workspaces) - 1
        elif action in ("next_workspace", "previous_workspace"):
            self.focus = (self.focus + (1 if action == "next_workspace" else -1)) % len(self.workspaces)
        elif action == "rename_workspace":
            self._prompt = ["workspace", ws["label"]]
        elif action == "close_workspace":
            self._prompt = ["close", ""]


class Run:
    """An engine with a fake herdr and a hand-held clock."""

    def __init__(self, lesson_id="panes-move", ambiguous=None, history=None, plan=None, data=DATA, **kw):
        self.resolved = curriculum.resolve(CURRICULUM, data, ambiguous)
        self.session = FakeHerdr(self.resolved)
        self.plan = plan(self.resolved) if plan else plan_lesson(self.resolved, lesson_id, **kw)
        self.engine = Engine(self.plan, self.session, history=history, sleep=lambda seconds: None)
        self.now = 100.0
        self.engine.begin(self.now)

    def tick(self, seconds=0.0):
        self.now += seconds
        self.engine.tick(self.now)

    def wait(self, seconds, step=0.05):
        end = self.now + seconds
        while self.now < end - 1e-9:
            self.tick(min(step, end - self.now))

    def chord(self, chord):
        """The user's chord, typed: tokens as the router forwards them."""
        prefix = "ctrl+space"
        if chord.startswith("prefix+"):
            fed = [self.engine.forwarded(kitty_keys(prefix)),
                   self.engine.forwarded(kitty_keys(chord.removeprefix("prefix+")))]
        else:
            fed = [self.engine.forwarded(kitty_keys(chord))]
        self.wait(0.25)
        return all(fed)

    def card(self, card, number=None):
        chord = self.resolved.bindings[card].taught
        return self.chord(chord.replace("1..9", str(number)) if number else chord)

    def text(self, text):
        for char in text:
            self.engine.forwarded(char.encode())

    def solve(self):
        """Play the current step's solution with the user's keys, then let the beat pass."""
        for move in self.engine.step.scenario.solution:
            if move.card:
                self.card(move.card, move.number)
            elif move.press:
                for _ in range(move.repeat):
                    self.engine.forwarded(kitty_keys(move.press))
                self.wait(0.25)
            elif move.text:
                self.text(move.text)

    def bar(self):
        return self.engine.bar()


class RecordedYazi:
    """A Yazi that steps through recorded worlds, one per key typed. `typing()` follows
    the recorded input layer, as `YaziSession.typing` does."""

    def __init__(self, worlds):
        self.worlds, self.at = list(worlds), 0
        self.fixtures, self.fed = [], []

    def start(self, actions, fixture):
        return self.apply_fixture(fixture)

    def apply_fixture(self, name):
        self.fixtures.append(name)
        self.at = 0
        return self.world()

    def world(self):
        return self.worlds[self.at]

    def typing(self):
        return self.world().yazi.layer != "mgr"

    def feed(self, raw):
        self.fed.append(raw)
        self.at = min(self.at + 1, len(self.worlds) - 1)

    def close(self):
        pass


class TypingInAPromptTest(unittest.TestCase):
    """Review 1, item 1: the filter shows each letter while the prompt is open; no letter is a miss."""

    @classmethod
    def setUpClass(cls):
        from test_checks_yazi import load_recorded
        cls.recorded = load_recorded()

    def start(self, worlds):
        resolved = curriculum.resolve(BOTH, YAZI_DATA)
        self.session = RecordedYazi(worlds)
        self.engine = Engine(only("yazi-find", "yazi-filter", "recall")(resolved), self.session,
                             sleep=lambda seconds: None)
        self.now = 100.0
        self.engine.begin(self.now)
        return self.engine

    def key(self, raw):
        self.assertTrue(self.engine.forwarded(raw))
        for _ in range(6):  # well past the settle time
            self.now += 0.05
            self.engine.tick(self.now)

    def test_typing_the_filter_text_is_judged_once_the_prompt_closes(self):
        engine = self.start(self.recorded["yazi-filter:solution"])
        for raw in (b"f", b"n", b"o"):
            self.key(raw)
            self.assertEqual((engine.state, engine.misses, engine.rebuilds), (RUNNING, 0, 0), raw)
        self.assertEqual(self.session.world().yazi.filter, "no")
        self.key(kitty_keys("enter"))
        self.assertEqual(engine.state, BEAT)
        self.assertEqual((engine.results[0].outcome, engine.results[0].misses), ("clean", 0))
        self.assertEqual(len(self.session.fixtures), 1)

    def test_a_wrong_filter_confirmed_is_one_miss_and_a_cancelled_prompt_none(self):
        start, opened, n = self.recorded["yazi-filter:solution"][:3]
        nx = World(yazi=replace(n.yazi, filter="nx", entries=(), hovered=None))
        confirmed = World(yazi=replace(nx.yazi, layer="mgr"))
        engine = self.start([start, opened, n, nx, confirmed])
        for raw in (b"f", b"n", b"x"):
            self.key(raw)
        self.assertEqual((engine.misses, engine.rebuilds), (0, 0))
        self.key(kitty_keys("enter"))
        self.assertEqual((engine.state, engine.misses, engine.rebuilds), (RUNNING, 1, 1))
        self.assertEqual(engine.bar().kind, "miss")
        engine = self.start([start, opened, n, start])
        for raw in (b"f", b"n", kitty_keys("esc")):
            self.key(raw)
        self.assertEqual((engine.state, engine.misses, engine.rebuilds), (RUNNING, 0, 0))


class RoundTripTest(unittest.TestCase):
    """Review 2, item 1: a round trip whose last world equals its start completes, however fast."""

    def hooked_yazi(self, home):
        """The real YaziSession hook parser and queue, over a stub herdr and a byte stream."""
        from yazi_backend import YaziSession

        class Stream:
            data = b""

            def read(self):
                data, self.data = self.data, b""
                return data

        class Pane:
            def world(self, max_age=0):
                return None

            def feed(self, raw):
                pass

        session = YaziSession(YAZI_ROWS)
        session._home, session.herdr, session._events = Path(home), Pane(), Stream()
        session.start = lambda actions, fixture: session.world()
        session.apply_fixture = lambda name: session.world()
        return session

    @staticmethod
    def hook_line(home, world):
        body = {**json.loads(json.dumps(world)), "cwd": f"{home}/{world['cwd']}"}
        body.pop("tree")
        body["hovered"] = body["hovered"] or ""
        return b"keys-trainer-state,1,1," + json.dumps(body).encode() + b"\n"

    def test_the_recorded_yazi_folder_round_trip_in_one_batch_completes(self):
        import tempfile
        recorded = json.loads(Path(__file__).with_name("test_data").joinpath("yazi_worlds.json").read_text("utf-8"))
        worlds = recorded["yazi-in-out:solution"]
        with tempfile.TemporaryDirectory() as home:
            session = self.hooked_yazi(home)
            session._events.data = self.hook_line(home, worlds[0])
            resolved = curriculum.resolve(BOTH, YAZI_DATA)
            engine = Engine(only("yazi-find", "yazi-in-out", "recall")(resolved), session, sleep=lambda s: None)
            now = 100.0
            engine.begin(now)
            self.assertEqual(engine.state, RUNNING)
            # Projects → timeline.txt → Practice: three hook events in one read.
            session._events.data = b"".join(self.hook_line(home, world) for world in worlds[1:])
            for raw in (b"l", b"j", b"h"):
                engine.forwarded(raw)
            for _ in range(20):
                now += 0.05
                engine.tick(now)
            self.assertEqual(engine.state, BEAT)
            self.assertEqual((engine.results[0].outcome, engine.misses, engine.rebuilds), ("clean", 0, 0))

    def round_trip(self, lesson_id, scenario_id, first, second):
        run = Run(plan=only(lesson_id, scenario_id, "recall"))
        start = run.session.world()
        self.assertTrue(run.engine.forwarded(kitty_keys(first)))
        run.tick(0.05)
        self.assertTrue(run.engine.forwarded(kitty_keys(second)))
        run.tick(0.05)  # both moves seen within one settle window, back where it started
        self.assertEqual(run.session.world(), start)
        run.wait(0.5)
        self.assertEqual(run.engine.state, BEAT, scenario_id)
        self.assertEqual((run.engine.results[0].outcome, run.engine.misses), ("clean", 0))

    def test_a_fast_herdr_zoom_in_and_out_completes(self):
        run =Run(plan=only("panes-move", "zoom", "recall"))
        start = run.session.world()
        for _ in range(2):
            run.engine.forwarded(kitty_keys("ctrl+space"))
            run.engine.forwarded(kitty_keys("z"))
            run.tick(0.05)
        self.assertEqual(run.session.world(), start)
        run.wait(0.5)
        self.assertEqual(run.engine.state, BEAT)
        self.assertEqual((run.engine.results[0].outcome, run.engine.misses), ("clean", 0))

    def test_a_fast_herdr_tab_out_and_back_completes(self):
        self.round_trip("tabs", "tab-step", "alt+right", "alt+left")

    def test_a_fast_wrong_round_trip_is_neither_a_pass_nor_a_miss(self):
        run = Run(plan=only("tabs", "tab-step", "recall"))
        run.engine.forwarded(kitty_keys("alt+left"))
        run.tick(0.05)
        run.engine.forwarded(kitty_keys("alt+right"))
        run.wait(0.5)
        self.assertEqual((run.engine.state, run.engine.misses, run.engine.rebuilds), (RUNNING, 0, 0))


class LessonTwoScriptTest(unittest.TestCase):
    def test_scripted_run_shows_the_right_bar_at_each_step(self):
        run = Run("panes-move")
        expected = [
            ("LEARN", "Move focus to the left pane", "Ctrl + Alt + ←",
             "Type in the editor while the agent works on the right.", "Focus is on the left"),
            ("LEARN", "Go right, then down", "Ctrl + Alt + →, ↓", "○ right    ○ down", "Right, then down"),
            ("RECALL", "Move focus to the left pane", "", "Type in the editor while the agent works on the right.",
             "Focus is on the left"),
            ("LEARN", "Jump back to the pane you were just in", "Ctrl + Space → l",
             "Flip between two panes with one shortcut.", "Back where you were"),
            ("RECALL", "Go up, then left", "", "○ up    ○ left", "Up, then left"),
            ("LEARN", "Zoom the pane to full size, and back", "Ctrl + Space → z ×2",
             "○ zoom on    ○ zoom off", "Zoomed in and out"),
            ("RECALL", "Jump back to the pane you were just in", "",
             "Flip between two panes with one shortcut.", "Back where you were"),
            ("RECALL", "Zoom the pane to full size, and back", "", "○ zoom on    ○ zoom off", "Zoomed in and out"),
            ("BUILD", "Editor on the left, two stacked on the right, focus left", "",
             "○ two stacked on the right    ✓ focus on the left", "Coding layout ready"),
        ]
        for number, (tag, goal, chord, line, done) in enumerate(expected, 1):
            with self.subTest(step=number, goal=goal):
                bar = run.bar()
                self.assertEqual((bar.tag, bar.goal, bar.chord, bar.line), (tag, goal, chord, line))
                self.assertEqual(bar.hint, "Show me" if tag == "LEARN" else "Show keys")
                self.assertEqual(bar.title, "Move between panes")
                self.assertEqual(bar.steps[number - 1], "current")
                self.assertEqual(len(bar.steps), 9)
                self.assertEqual(bar.target, lesson.FOCUS_MAPS[("right-stack", "left")] if tag == "BUILD" else ())
                run.solve()
                self.assertEqual(run.engine.state, BEAT)
                beat = run.bar()
                self.assertEqual((beat.tag, beat.goal, beat.kind, beat.hint), ("✓", done, "success", None))
                run.wait(1.5)
        self.assertEqual(run.engine.state, DONE)
        result = run.engine.result
        self.assertTrue(result.complete)
        self.assertEqual([r.outcome for r in result.steps], ["met", "met", "clean", "met", "clean", "met",
                                                            "clean", "clean", "clean"])
        # One rebuild only: the build needs a single pane; everything else chains (design risk 3).
        self.assertEqual(run.session.fixtures, ["right_stack", "one_pane"])

    def test_multi_check_steps_tick_their_checklist_live(self):
        run = Run("panes-move")
        run.solve()
        run.wait(1.0)
        run.card("herdr:focus_pane_right")
        self.assertEqual(run.bar().line, "✓ right    ○ down")
        self.assertEqual(run.bar().kind, "checklist")

    def test_learn_beat_names_the_other_chord(self):
        run = Run("split")
        run.solve()
        self.assertEqual(run.bar().line, "Also bound to Ctrl + Space → h")


class LadderTest(unittest.TestCase):
    def recall(self):
        run = Run("panes-move")
        for _ in range(2):
            run.solve()
            run.wait(1.0)
        self.assertEqual(run.bar().tag, "RECALL")
        return run

    def test_keys_then_show_me_completes_as_shown_and_requeues(self):
        run = self.recall()
        self.assertEqual(run.bar().hint, "Show keys")
        self.assertTrue(run.engine.hint())
        bar = run.bar()
        self.assertEqual((bar.line, bar.kind, bar.hint), ("Keys · Ctrl + Alt + ←", "hint", "Show me"))
        self.assertTrue(run.engine.hint())
        self.assertEqual(run.engine.state, SHOWING)
        self.assertIsNone(run.bar().hint)
        self.assertFalse(run.engine.forwarded(kitty_keys("ctrl+alt+up")))  # the user's keys wait while it plays
        run.tick(0.0)
        self.assertEqual(run.bar().line, "Watch: Ctrl + Alt + ←")
        self.assertIn(("herdr:focus_pane_left", None), run.session.played)
        run.wait(lesson.SHOW_PAUSE + 0.1)
        self.assertEqual(run.engine.state, BEAT)
        self.assertEqual(run.bar().line, "Shown this time. It comes back soon for you to do yourself.")
        result = run.engine.results[2]
        self.assertEqual((result.outcome, result.hint_level), ("shown", 3))
        # Back once after two other steps, then before the build.
        self.assertEqual([s.scenario.id for s in run.engine.queue][3:7],
                         ["last-pane", "walk-back", "focus-left", "zoom"])
        self.assertTrue(run.engine.queue[5].requeued)
        self.assertEqual(len(run.bar().steps), 10)

    def test_show_me_rebuilds_first_when_the_state_no_longer_fits(self):
        run = Run("split")
        for _ in range(2):
            run.solve()
            run.wait(1.0)
        self.assertEqual(run.bar().tag, "RECALL")
        run.session.play("herdr:split_vertical")  # changed outside the user's keys: watched, not a miss
        run.wait(0.3)
        self.assertEqual((run.engine.state, run.engine.misses), (RUNNING, 0))
        run.engine.hint()
        run.engine.hint()  # four panes, split-rows needs at most three: set up again, then play
        self.assertEqual(run.session.fixtures, ["one_pane", "one_pane"])
        run.wait(2.0)
        self.assertEqual(run.engine.results[2].outcome, "shown")

    def test_show_me_after_partial_progress_plays_the_whole_solution_from_the_start(self):
        """Review 1, item 3: tab 2 → 3, then «Show me»: the user sees 2 → 3 → 2, not 3 → 4 → 3."""
        run = Run(plan=only("tabs", "tab-step"))
        session, positions = run.session, []
        act, fixture = session._act, session.apply_fixture
        session._act = lambda *args: (act(*args), positions.append(session.world().herdr.tab_position))
        session.apply_fixture = lambda name: (world := fixture(name),
                                              positions.append(world.herdr.tab_position))[0]
        self.assertEqual(session.world().herdr.tab_position, 2)
        run.card("herdr:next_tab")
        self.assertEqual(run.bar().line, "✓ next tab    ○ previous tab")
        self.assertTrue(run.engine.hint())  # LEARN: straight to «Show me»
        run.wait(2.0)
        self.assertEqual(positions, [3, 2, 3, 2])
        self.assertEqual(session.fixtures, ["four_tabs", "four_tabs"])
        self.assertEqual(session.played[-2:], [("herdr:next_tab", None), ("herdr:previous_tab", None)])
        self.assertEqual((run.engine.state, run.engine.results[0].hint_level), (BEAT, 3))

    def test_show_me_from_the_untouched_start_plays_from_there(self):
        run = Run(plan=only("tabs", "tab-step"))
        run.engine.hint()
        run.wait(2.0)
        self.assertEqual(run.session.fixtures, ["four_tabs"])
        self.assertEqual(run.engine.state, BEAT)

    def test_learn_hint_goes_straight_to_show_me(self):
        run = Run("panes-move")
        self.assertEqual(run.bar().hint, "Show me")
        run.engine.hint()
        self.assertEqual(run.engine.state, SHOWING)
        run.tick(0)
        self.assertEqual(run.bar().line, "Watch: Ctrl + Alt + ←")
        run.wait(1.2)
        self.assertEqual(run.engine.state, BEAT)
        self.assertEqual(run.engine.results[0].outcome, "met")
        self.assertEqual(run.engine.bar().steps[0], "shown")

    def test_build_hint_gives_the_next_move_and_show_me_plays_it_all(self):
        run = Run("split")
        for _ in range(6):
            run.solve()
            run.wait(1.6)
        self.assertEqual(run.bar().tag, "BUILD")
        run.engine.hint()
        self.assertEqual(run.bar().line, "Keys · Next: Alt + Enter")
        run.card("herdr:split_horizontal")
        self.assertEqual(run.bar().hint, "Show keys")  # a new move, a fresh offer
        run.engine.hint()
        self.assertEqual(run.bar().line, "Keys · Next: Alt + Shift + Enter")
        run.engine.hint()
        run.wait(3.0)
        self.assertEqual(run.engine.results[6].outcome, "shown")
        self.assertEqual(run.session.fixtures[-1], "one_pane")
        self.assertEqual(run.session.played[-2:], [("herdr:split_horizontal", None), ("herdr:split_vertical", None)])

    def test_quiet_offer_after_ten_seconds_and_nudge_after_second_miss(self):
        run = self.recall()
        run.wait(9.9)
        self.assertFalse(run.engine.offer)
        run.wait(0.2)
        self.assertTrue(run.engine.offer)
        run.card("herdr:focus_pane_up")
        self.assertEqual(run.bar().line, "Focus moved up. Try again from here.")
        self.assertEqual(run.bar().kind, "miss")
        run.card("herdr:focus_pane_down")
        bar = run.bar()
        self.assertEqual((bar.line, bar.kind, bar.hint),
                         ("A direct shortcut, no prefix. The arrow points where you want to go.", "hint", "Show keys"))
        run.card("herdr:focus_pane_left")
        self.assertEqual(run.engine.results[2].outcome, "nudged")

    def test_needs_failing_after_a_miss_rebuild(self):
        run = Run("split")
        run.card("herdr:split_vertical")  # a miss, three panes at most: continue here
        self.assertEqual(run.bar().line, "The pane split into two columns. Try again from here.")
        run.card("herdr:split_vertical")
        run.card("herdr:split_vertical")  # four panes: split-rows needs at most three
        self.assertEqual(run.bar().line, "The pane split into two columns. I'll set it up again, then you try.")
        self.assertEqual(run.session.fixtures, ["one_pane", "one_pane"])
        self.assertIn(("reset",), run.engine.drain())
        run.card("herdr:split_horizontal")
        self.assertEqual(run.engine.state, BEAT)

    def test_a_partial_move_toward_the_goal_is_not_a_miss(self):
        run = Run("shape")
        run.card("herdr:resize_pane_left")
        self.assertEqual(run.engine.misses, 0)

    def test_wrong_build_split_is_a_dead_end_and_rebuilds(self):
        run = Run("split")
        for _ in range(6):
            run.solve()
            run.wait(1.6)
        run.card("herdr:split_vertical")
        self.assertIn("That won't reach the target", run.bar().line)
        self.assertEqual(run.session.fixtures[-1], "one_pane")


class TabBuildTest(unittest.TestCase):
    """Review 1, item 4: lesson 3's build wants a new tab called build, not a renamed old one."""

    def run_build(self):
        run = Run(plan=only("tabs", "build-tab"))
        self.assertEqual(run.bar().tag, "BUILD")
        return run

    def next_keys(self, run, *cards):
        moves = [curriculum.Move(card=card) for card in cards]
        what = TEXT["list"].join(lesson.move_label(run.plan, move) for move in moves)
        return TEXT["keys"].format(keys=TEXT["next"].format(what=what))

    def rename(self, run, label):
        run.card("herdr:rename_tab")
        for _ in range(8):
            run.engine.forwarded(kitty_keys("backspace"))
        run.text(label)
        run.engine.forwarded(kitty_keys("enter"))
        run.wait(0.25)

    def test_the_next_move_hint_walks_the_build_in_order(self):
        run = self.run_build()
        run.engine.hint()
        self.assertEqual(run.bar().line, self.next_keys(run, "herdr:new_tab"))
        run.card("herdr:new_tab")
        run.engine.hint()
        self.assertIn(lesson.move_label(run.plan, curriculum.Move(card="herdr:rename_tab")), run.bar().line)
        self.rename(run, "build")
        run.engine.hint()
        self.assertEqual(run.bar().line, self.next_keys(run, "herdr:split_vertical"))
        run.card("herdr:split_vertical")
        run.engine.hint()
        self.assertEqual(run.bar().line, TEXT["keys"].format(keys=TEXT["next"].format(
            what=lesson.move_label(run.plan, curriculum.Move(card="herdr:switch_tab", number=1)))))
        run.card("herdr:switch_tab", 1)
        self.assertEqual(run.engine.state, BEAT)
        self.assertEqual(run.engine.results[0].outcome, "shown")  # the user asked for the keys

    def test_renaming_and_splitting_an_old_tab_does_not_pass(self):
        run = self.run_build()
        run.card("herdr:switch_tab", 1)
        self.rename(run, "build")
        run.card("herdr:split_vertical")
        self.assertEqual(run.engine.state, RUNNING)
        run.engine.hint()
        self.assertEqual(run.bar().line, self.next_keys(run, "herdr:new_tab"))
        # The real way still passes from there.
        run.card("herdr:new_tab")
        self.rename(run, "build")
        run.card("herdr:split_vertical")
        run.card("herdr:switch_tab", 1)
        self.assertEqual(run.engine.state, BEAT)

    def test_an_extra_new_tab_is_closed_before_the_build_goes_on(self):
        """Review 2, item 4: two new tabs, the first named build: the hint goes to the extra one
        and closes it, never «rename it build» again."""
        run = self.run_build()
        run.card("herdr:new_tab")
        run.card("herdr:new_tab")
        run.card("herdr:switch_tab", 5)
        self.rename(run, "build")
        self.assertEqual(run.engine.state, RUNNING)
        run.engine.hint()
        self.assertEqual(run.bar().line, TEXT["keys"].format(keys=TEXT["next"].format(
            what=lesson.move_label(run.plan, curriculum.Move(card="herdr:switch_tab", number=6)))))
        run.card("herdr:switch_tab", 6)
        run.engine.hint()
        self.assertEqual(run.bar().line, self.next_keys(run, "herdr:close_pane"))
        run.card("herdr:close_pane")
        self.assertEqual(len(run.session.world().herdr.workspace.tabs), 5)
        run.engine.hint()
        self.assertEqual(run.bar().line, self.next_keys(run, "herdr:split_vertical"))
        run.card("herdr:split_vertical")
        run.card("herdr:switch_tab", 1)
        self.assertEqual(run.engine.state, BEAT)

    def test_the_whole_build_passes_clean(self):
        run = self.run_build()
        run.solve()
        self.assertEqual((run.engine.state, run.engine.results[0].outcome), (BEAT, "clean"))


class OutcomeAndBeatTest(unittest.TestCase):
    def test_beat_drops_keys_and_lasts_its_time(self):
        run = Run("panes-move")
        run.engine.forwarded(kitty_keys("ctrl+alt+left"))
        while run.engine.state != BEAT:
            run.tick(0.01)
        began = run.now
        fed = len(run.session.fed)
        self.assertFalse(run.engine.forwarded(kitty_keys("ctrl+alt+up")))
        self.assertEqual(len(run.session.fed), fed)
        run.tick(began + lesson.BEAT_SECONDS - 0.02 - run.now)
        self.assertEqual(run.engine.state, BEAT)
        run.tick(0.03)
        self.assertEqual((run.engine.state, run.bar().goal), (RUNNING, "Go right, then down"))

    def test_build_beat_is_longer(self):
        run = Run("split")
        for _ in range(6):
            run.solve()
            run.wait(1.0)
        run.solve()
        run.wait(1.0)
        self.assertEqual(run.engine.state, BEAT)
        run.wait(0.5)
        self.assertEqual(run.engine.state, DONE)

    def test_outcome_table(self):
        cases = {"clean": [], "nudged": ["miss"], "shown": ["keys"]}
        for outcome, actions in cases.items():
            with self.subTest(outcome):
                run = LadderTest.recall(self)
                for action in actions:
                    if action == "miss":
                        run.card("herdr:focus_pane_up")
                    else:
                        run.engine.hint()
                run.card("herdr:focus_pane_left")
                self.assertEqual(run.engine.results[2].outcome, outcome)
                self.assertEqual(run.engine.results[2].seconds is not None, outcome == "clean")

    def test_clean_remarks_first_time_and_records(self):
        class Best:
            value = None

            def best_seconds(self, card):
                return self.value

        best = Best()
        run = Run("panes-move", history=best)
        run.solve()
        run.wait(1.0)
        run.solve()
        run.wait(1.0)
        run.solve()
        self.assertRegex(run.bar().line, r"^0\.\d s · first time without a hint$")
        best.value = 9.0
        run = Run("panes-move", history=best)
        for _ in range(2):
            run.solve()
            run.wait(1.0)
        run.solve()
        self.assertRegex(run.bar().line, r"^0\.\d s · new record · was 9\.0 s$")

    def test_typing_is_reported_for_the_router(self):
        run = Run("panes-move")
        run.session.typing_now = True
        run.tick(0.05)
        self.assertEqual(run.engine.drain()[-1], ("typing", True))
        self.assertEqual(run.engine.bar().controls, "Typing · Enter confirms · Esc cancels")


class PauseTest(unittest.TestCase):
    def test_pause_menu(self):
        run = Run("panes-move")
        self.assertTrue(run.engine.pause())
        self.assertEqual(run.engine.state, PAUSED)
        bar = run.bar()
        self.assertEqual(bar.tag, "PAUSE")
        self.assertIn("Resume", bar.controls)
        self.assertFalse(run.engine.forwarded(b"x"))
        run.wait(20)
        self.assertTrue(run.engine.resume())
        run.tick(0.05)
        self.assertFalse(run.engine.offer)  # paused time is not time without progress
        run.engine.pause()
        self.assertTrue(run.engine.redo())
        self.assertEqual(run.session.fixtures, ["right_stack", "right_stack"])
        run.engine.pause()
        self.assertTrue(run.engine.skip())
        self.assertEqual((run.engine.results[0].outcome, run.bar().goal), ("skipped", "Go right, then down"))
        self.assertTrue(run.engine.quit())
        self.assertEqual(run.engine.state, DONE)
        self.assertFalse(run.engine.result.complete)
        self.assertEqual(run.engine.drain()[-1][0], "done")

    def test_skipped_recall_comes_back_before_the_build(self):
        run = Run("split")
        for _ in range(5):
            run.solve()
            run.wait(1.0)
        self.assertEqual(run.bar().goal, "Close the focused pane")  # step 6 of 7, the build is next
        run.engine.skip()
        self.assertEqual([s.scenario.id for s in run.engine.queue][5:],
                         ["close-pane", "close-pane", "build-split-bottom"])
        self.assertEqual(run.engine.results[5].outcome, "shown")


class PlanTest(unittest.TestCase):
    def test_test_out_keeps_recall_and_build(self):
        resolved = curriculum.resolve(CURRICULUM, DATA)
        plan = plan_lesson(resolved, "split", test_out=True)
        self.assertEqual([s.kind for s in plan.steps], ["recall", "recall", "recall", "build"])

    def test_warmup_groups_by_fixture_caps_and_finishes(self):
        resolved = curriculum.resolve(CURRICULUM, DATA)
        due = ["herdr:new_tab", "herdr:split_horizontal", "herdr:focus_pane_left", "herdr:zoom",
               "herdr:split_vertical", "herdr:rename_tab", "herdr:close_pane", "herdr:last_pane"]
        plan = plan_warmup(resolved, due)
        self.assertTrue(plan.warmup)
        self.assertEqual(len(plan.steps), 6)
        self.assertTrue(all(step.kind == "recall" for step in plan.steps))
        fixtures = [step.fixture for step in plan.steps]
        self.assertEqual(fixtures, sorted(fixtures, key=fixtures.index))  # grouped
        self.assertEqual(fixtures[0], "four_tabs")
        run = Run(plan=lambda resolved: plan_warmup(resolved, ["herdr:split_horizontal", "herdr:split_vertical"]))
        self.assertEqual(run.bar().title, "Warm-up")
        run.solve()
        run.wait(1.0)
        run.solve()
        run.wait(1.0)
        self.assertEqual(run.engine.state, BEAT)
        self.assertEqual(run.bar().goal, "Warm-up done · 2 of 2 without a hint")
        run.wait(1.3)
        self.assertEqual(run.engine.state, DONE)
        self.assertTrue(run.engine.result.warmup)

    def test_legacy_mode_teaches_the_prefix_twin(self):
        from router import ambiguous_in_legacy
        run = Run("split", ambiguous=ambiguous_in_legacy)
        run.solve()
        run.wait(1.0)
        self.assertEqual(run.bar().chord, "Ctrl + Space → v")  # not Alt + Shift + Enter


NO_SPLIT_VERTICAL = {"groups": [{"id": "herdr", "rows": [row for row in ROWS if row["value"] != "split vertical"],
                                 "defaults": {"split_vertical": "alt+shift+enter", "zoom": "alt+enter"}}]}
HOME_ENV = {"HOME": "/home/friend", "PATH": "/usr/bin"}


def card_text(card):
    from screen import render_card
    painted, _ = render_card(card, 100, 40)
    import re
    return "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", painted[row]) for row in sorted(painted))


class NoticeTest(unittest.TestCase):
    """A step whose key the user does not have: a notice on how to add it, and `s` skips it."""

    def setUp(self):
        patcher = mock.patch.dict("os.environ", HOME_ENV, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_plan_keeps_the_unbound_steps_in_place_as_notices(self):
        resolved = curriculum.resolve(CURRICULUM, NO_SPLIT_VERTICAL)
        self.assertTrue(resolved.lesson("split").visible)
        plan = plan_lesson(resolved, "split")
        self.assertEqual([f"{s.kind}:{s.scenario.id}" for s in plan.steps],
                         [f"{s.kind}:{s.scenario}" for s in CURRICULUM.lesson("split").steps])
        notices = [s.scenario.id for s in plan.steps if s.missing]
        self.assertEqual(notices, ["split-cols", "split-cols", "build-split-bottom"])
        self.assertNotIn("herdr:split_vertical", plan.actions)

    def test_the_notice_names_the_action_the_file_and_the_line_with_the_default_chord(self):
        run = Run("split", data=NO_SPLIT_VERTICAL)
        run.solve()
        run.wait(1.0)
        self.assertEqual(run.engine.step.scenario.id, "split-cols")
        text = card_text(run.engine.notice_card())
        self.assertIn("split vertical", text)
        self.assertIn("Split the pane into two columns", text)
        self.assertIn("~/.config/herdr/config.toml", text)
        self.assertIn('split_vertical = "alt+shift+enter"', text)
        self.assertIn("[keys]", text)
        bar = run.bar()
        self.assertEqual((bar.tag, bar.skip_key, bar.hint), ("NO KEY", "s", ""))
        self.assertIn("s skips it", bar.line)

    def test_a_default_chord_already_in_use_is_said_and_left_out(self):
        rows = [row for row in ROWS if row["value"] != "zoom"]
        data = {"groups": [{"id": "herdr", "rows": rows, "defaults": {"zoom": "alt+enter"}}]}
        resolved = curriculum.resolve(CURRICULUM, data)
        step = next(s for s in plan_lesson(resolved, "panes-move").steps if s.missing)
        self.assertEqual(step.missing[0]["taken"], "split horizontal")
        self.assertEqual(step.missing[0]["line"], 'zoom = "<key>"')
        engine = Engine(replace(plan_lesson(resolved, "panes-move"), steps=(step,)), FakeHerdr(resolved))
        engine.begin(0.0)
        text = card_text(engine.notice_card())
        self.assertIn("alt+enter, is already used for split horizontal", " ".join(text.split()))

    def test_s_on_the_notice_skips_it_records_it_and_the_lesson_goes_on(self):
        run = Run("split", data=NO_SPLIT_VERTICAL)
        run.solve()
        run.wait(1.0)
        self.assertTrue(run.engine.skippable)
        self.assertFalse(run.engine.forwarded(b"x"))  # keys go nowhere on a notice
        self.assertFalse(run.engine.hint())
        fixtures = list(run.session.fixtures)
        run.wait(15)  # nothing to watch, no offer, no rebuild
        self.assertFalse(run.engine.offer)
        self.assertEqual(run.session.fixtures, fixtures)
        self.assertTrue(run.engine.skip())
        result = run.engine.results[1]
        self.assertEqual((result.scenario, result.outcome, result.skipped, result.notice, result.cards),
                         ("split-cols", "skipped", True, True, ()))
        self.assertEqual(len(run.engine.queue), 7)  # a notice never comes back
        self.assertEqual(run.bar().goal, "Split the pane into two rows")  # recall:split-rows
        self.assertEqual(lesson.scored([result]), ())

    def test_warm_up_never_schedules_a_notice_step(self):
        resolved = curriculum.resolve(CURRICULUM, NO_SPLIT_VERTICAL)
        plan = plan_warmup(resolved, ["herdr:split_vertical", "herdr:split_horizontal", "herdr:close_pane"])
        self.assertTrue(plan.steps)
        self.assertFalse([step for step in plan.steps if step.missing])
        self.assertNotIn("split-cols", [step.scenario.id for step in plan.steps])


if __name__ == "__main__":
    unittest.main()
