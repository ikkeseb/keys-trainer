import json
import os
import shutil
import time
import unittest
from dataclasses import dataclass, replace
from pathlib import Path

import checks
from herdr_backend import HerdrBackend
from world import HerdrWorld, World


DATA = Path(__file__).with_name("test_data")
RECORDED = DATA / "herdr_worlds.json"
RIGPAGE_ROWS = json.loads((DATA / "rigpage_herdr.json").read_text(encoding="utf-8"))
CLEAR = ("press:backspace",) * 8  # rename prompts are prefilled (M3)
SAMPLE_PARAMS = {"direction": "down", "side": "left", "on": True, "blueprint": "grid", "label": "web", "step": 1}


@dataclass(frozen=True)
class Case:
    """A scenario as data: start state, the moves that solve it and a wrong attempt."""

    fixture: str
    checks: tuple[dict, ...]
    solution: tuple[str, ...]
    decoy: tuple[str, ...]
    setup: tuple[str, ...] = ()
    needs: tuple[dict, ...] = ()


def hold(item):
    return {**item, "mode": "hold"}


# Lessons 1 to 5 (design § 2.3). A move is an action name ("split_vertical"),
# an action's n-th chord ("split_vertical#1"), an indexed chord ("switch_tab:3",
# "switch_tab#1:3"), a key ("press:enter") or text ("type:logs").
CASES = {
    "split_down": Case("one_pane", ({"check": "split", "direction": "down"},),
                       ("split_horizontal",), ("split_vertical",), needs=({"check": "max_panes", "count": 3},)),
    "split_down_alt": Case("one_pane", ({"check": "split", "direction": "down"},),
                           ("split_horizontal#1",), ("split_vertical#1",)),
    "split_right": Case("one_pane", ({"check": "split", "direction": "right"},),
                        ("split_vertical",), ("split_horizontal",)),
    "split_right_alt": Case("one_pane", ({"check": "split", "direction": "right"},),
                            ("split_vertical#1",), ("split_horizontal#1",)),
    "close_pane": Case("one_pane", ({"check": "pane_closed"},), ("close_pane",), ("focus_pane_left",),
                       setup=("split_vertical",), needs=({"check": "min_panes", "count": 2},)),
    "close_pane_alt": Case("one_pane", ({"check": "pane_closed"},), ("close_pane#1",), ("split_horizontal",),
                           setup=("split_vertical",)),
    "build_split_bottom": Case("one_pane", (hold({"check": "blueprint", "blueprint": "split-bottom"}),),
                               ("split_horizontal", "split_vertical"), ("split_vertical", "split_horizontal"),
                               needs=({"check": "max_panes", "count": 1},)),
    "focus_left": Case("right_stack", ({"check": "focus_moved", "side": "left"},),
                       ("focus_pane_left",), ("focus_pane_up",), needs=({"check": "has_neighbour", "side": "left"},)),
    "walk_right_down": Case("right_stack", ({"check": "focus_moved", "side": "right"},
                                            {"check": "focus_moved", "side": "down"}),
                            ("focus_pane_right", "focus_pane_down"), ("focus_pane_down", "focus_pane_right"),
                            setup=("focus_pane_left",), needs=({"check": "focus_at", "position": "left"},)),
    "walk_up_left": Case("right_stack", ({"check": "focus_moved", "side": "up"},
                                         {"check": "focus_moved", "side": "left"}),
                         ("focus_pane_up", "focus_pane_left"), ("focus_pane_left", "focus_pane_up")),
    "last_pane": Case("right_stack", ({"check": "last_pane"},), ("last_pane",), ("focus_pane_left",),
                      setup=("focus_pane_up",), needs=({"check": "has_focus_history"},)),
    "zoom": Case("right_stack", ({"check": "zoomed", "on": True}, {"check": "zoomed", "on": False}),
                 ("zoom", "zoom"), ("zoom", "focus_pane_left"),
                 needs=({"check": "min_panes", "count": 2}, {"check": "not_zoomed"})),
    "build_right_stack": Case("one_pane", (hold({"check": "blueprint", "blueprint": "right-stack"}),
                                           hold({"check": "focus_at", "position": "left"})),
                              ("split_vertical#1", "split_horizontal#1", "focus_pane_left"),
                              ("split_vertical", "split_horizontal")),
    "new_tab": Case("four_tabs", ({"check": "new_tab"},), ("new_tab",), ("next_tab",),
                    needs=({"check": "max_tabs", "count": 6},)),
    "next_previous_tab": Case("four_tabs", ({"check": "tab_step", "step": 1}, {"check": "tab_step", "step": -1}),
                              ("next_tab", "previous_tab"), ("next_tab", "next_tab"),
                              needs=({"check": "min_tabs", "count": 3},)),
    "next_previous_tab_alt": Case("four_tabs", ({"check": "tab_step", "step": 1},
                                                {"check": "tab_step", "step": -1}),
                                  ("next_tab#1", "previous_tab#1"), ("previous_tab#1", "next_tab#1")),
    "tab_three": Case("four_tabs", ({"check": "tab_at", "position": 3},), ("switch_tab:3",), ("switch_tab:4",),
                      needs=({"check": "tab_unfocused", "position": 3},)),
    # State alone cannot tell alt+3 from next_tab when tab 3 is next door: pick N apart.
    "tab_three_alt": Case("four_tabs", ({"check": "tab_at", "position": 3},), ("switch_tab#1:3",),
                          ("switch_tab#1:4",)),
    "rename_tab": Case("four_tabs", ({"check": "tab_label", "label": "logs"},),
                       ("rename_tab", *CLEAR, "type:logs", "press:enter"),
                       ("rename_tab", "type:logs", "press:enter")),
    "build_tab": Case("four_tabs", (hold({"check": "tab_added", "label": "build"}),
                                    hold({"check": "blueprint", "blueprint": "twin-columns", "tab_label": "build",
                                          "new": True}),
                                    hold({"check": "tab_at", "position": 1})),
                      ("new_tab", "rename_tab", *CLEAR, "type:build", "press:enter", "split_vertical#1",
                       "switch_tab#1:1"),
                      ("new_tab", "rename_tab", *CLEAR, "type:build", "press:enter", "split_vertical#1"),
                      needs=({"check": "max_tabs", "count": 5},)),
    "new_workspace": Case("two_tabs", ({"check": "new_workspace"},), ("new_workspace",), ("new_tab",),
                          needs=({"check": "max_workspaces", "count": 3},)),
    "previous_next_workspace": Case(
        "two_workspaces", ({"check": "workspace_step", "step": -1}, {"check": "workspace_step", "step": 1}),
        ("previous_workspace", "next_workspace"), ("previous_workspace", "previous_workspace"),
        # With only two workspaces, previous and next land on the same one.
        setup=("new_workspace",),
        needs=({"check": "min_workspaces", "count": 2}, {"check": "on_last_workspace"}),
    ),
    "previous_next_workspace_alt": Case(
        "two_workspaces", ({"check": "workspace_step", "step": -1}, {"check": "workspace_step", "step": 1}),
        ("previous_workspace#1", "next_workspace#1"), ("next_workspace#1", "previous_workspace#1"),
        setup=("new_workspace",),
    ),
    "rename_workspace": Case("two_workspaces", ({"check": "workspace_label", "label": "web"},),
                             ("rename_workspace", *CLEAR, "type:web", "press:enter"),
                             ("rename_workspace", "type:web", "press:enter"),
                             needs=({"check": "not_first_workspace"},)),
    "build_workspace": Case(
        "two_tabs",
        ({"check": "workspace_exists", "label": "web", "min_tabs": 2},
         hold({"check": "workspace_gone", "label": "web", "focused_label": "Practice"})),
        ("new_workspace", "rename_workspace", *CLEAR, "type:web", "press:enter", "new_tab",
         "close_workspace", "press:enter"),
        # The confirm box takes Enter; y does nothing (M4).
        ("new_workspace", "rename_workspace", *CLEAR, "type:web", "press:enter", "new_tab",
         "close_workspace", "type:y"),
        needs=({"check": "max_workspaces", "count": 1},),
    ),
    "resize_right_down": Case("right_stack", ({"check": "resized", "side": "right"},
                                              {"check": "resized", "side": "down"}),
                              ("resize_pane_right", "resize_pane_right", "resize_pane_down", "resize_pane_down"),
                              ("resize_pane_right", "resize_pane_down", "resize_pane_down"),
                              needs=({"check": "has_neighbour", "side": "horizontal"},
                                     {"check": "has_neighbour", "side": "vertical"})),
    "resize_left_up": Case("right_stack", ({"check": "resized", "side": "left"},
                                           {"check": "resized", "side": "up"}),
                           ("resize_pane_left", "resize_pane_left", "resize_pane_up", "resize_pane_up"),
                           ("resize_pane_right", "resize_pane_right", "resize_pane_up", "resize_pane_up")),
    "build_grid": Case("one_pane", (hold({"check": "blueprint", "blueprint": "grid"}),),
                       ("split_horizontal", "split_vertical", "focus_pane_up", "split_vertical"),
                       # Columns first, then each split in two, is also a grid: stop one short.
                       ("split_vertical", "split_horizontal", "focus_pane_left")),
    "build_console": Case("one_pane", (hold({"check": "blueprint", "blueprint": "console"}),),
                          ("split_vertical", "split_horizontal", "focus_pane_left", "split_horizontal",
                           "focus_pane_up", "split_vertical"),
                          ("split_horizontal", "split_vertical", "focus_pane_up", "split_vertical")),
}
CASE_ACTIONS = sorted({
    move.split(":")[0].split("#")[0]
    for case in CASES.values() for move in (*case.setup, *case.solution, *case.decoy)
    if not move.startswith(("press:", "type:"))
})


def perform(backend, move):
    if move.startswith("press:"):
        backend.press(move.removeprefix("press:"))
    elif move.startswith("type:"):
        backend.type_text(move.removeprefix("type:"))
    else:
        action, _, number = move.partition(":")
        action, _, index = action.partition("#")
        backend.play(action, index=int(index or 0), number=int(number) if number else None)


def settle(backend, previous, quiet=0.06, patience=0.25, limit=1.5):
    """Wait until the world changed and stayed still, or did not change for `patience` seconds."""
    begun = changed = time.monotonic()
    last = previous
    while time.monotonic() - begun < limit:
        current = backend.world()
        now = time.monotonic()
        if current != last:
            last, changed = current, now
        elif now - changed >= (quiet if last != previous else patience):
            break
        time.sleep(0.01)
    return last


def run_moves(backend, moves, previous):
    """Play the moves; return the distinct worlds seen after each one."""
    worlds = []
    for move in moves:
        perform(backend, move)
        if move.startswith(("type:", "press:backspace")):
            continue  # typing into a prompt changes nothing until Enter
        current = settle(backend, previous)
        if current != previous:
            worlds.append(current)
        previous = current
    return worlds


def record(backend, case):
    """Run a case twice from its fixture: once solved, once with the decoy."""
    runs = {}
    for name, moves in (("solution", case.solution), ("decoy", case.decoy)):
        fixture = backend.apply_fixture(case.fixture)
        earlier = [fixture, *run_moves(backend, case.setup, fixture)]
        start = earlier.pop()
        runs[name] = (earlier, start, run_moves(backend, moves, start))
    return runs


def herdr(world):
    return World(herdr=world)


def load_recorded():
    data = json.loads(RECORDED.read_text(encoding="utf-8"))
    worlds = {}
    for name, item in data["cases"].items():
        table = [HerdrWorld.from_snapshot(snapshot) for snapshot in item["worlds"]]
        worlds[name] = {
            run: tuple(tuple(herdr(table[index]) for index in part) for part in item[run])
            for run in ("solution", "decoy")
        }
    return worlds


class RecordedCheckTests(unittest.TestCase):
    """Snapshots recorded from a private herdr 0.9.5 session playing the exported chords (`python3 test_checks.py --record`)."""

    @classmethod
    def setUpClass(cls):
        cls.recorded = load_recorded()

    def test_every_case_was_recorded(self):
        self.assertEqual(set(self.recorded), set(CASES))

    def test_solution_passes_and_decoy_does_not(self):
        for name, case in CASES.items():
            with self.subTest(case=name):
                checks.validate(case.checks, case.needs)
                earlier, (start,), history = self.recorded[name]["solution"]
                self.assertEqual(checks.unmet(case.needs, start, earlier), [])
                progress = checks.evaluate(case.checks, start, history, earlier)
                self.assertEqual(progress, checks.Progress(len(case.checks), True, len(case.checks)))
                earlier, (start,), history = self.recorded[name]["decoy"]
                self.assertFalse(checks.evaluate(case.checks, start, history, earlier).done)

    def test_every_check_is_covered_by_a_case(self):
        used = {item["check"] for case in CASES.values() for item in case.checks}
        self.assertEqual(used, {name for name, entry in checks.CHECKS.items() if entry.tool == "herdr"})
        used = {item["check"] for case in CASES.values() for item in case.needs}
        self.assertLessEqual(used, set(checks.NEEDS) | checks.STATE_CHECKS)

    def test_progress_counts_ordered_latches(self):
        earlier, (start,), history = self.recorded["walk_right_down"]["solution"]
        walk = CASES["walk_right_down"].checks
        self.assertEqual(checks.evaluate(walk, start, ()).latched, 0)
        self.assertEqual(checks.evaluate(walk, start, history[:1]), checks.Progress(1, False, 2))
        # The same moves in the other order latch nothing after the first.
        self.assertEqual(checks.evaluate(walk[::-1], start, history).latched, 0)

    def test_hold_needs_a_change_and_the_latest_world(self):
        _, (start,), history = self.recorded["build_right_stack"]["solution"]
        build = CASES["build_right_stack"].checks
        self.assertFalse(checks.evaluate(build, history[-1], ()).done)
        self.assertTrue(checks.evaluate(build, start, history).done)
        # Solved layout, but focus not yet on the left pane: one of two holds.
        self.assertEqual(checks.evaluate(build, start, history[:-1]), checks.Progress(1, False, 2))

    def test_latch_survives_later_changes(self):
        _, (start,), history = self.recorded["zoom"]["decoy"]
        progress = checks.evaluate(CASES["zoom"].checks, start, history)
        self.assertEqual(progress, checks.Progress(1, False, 2))

    def test_needs_reject_worlds_that_cannot_start_the_scenario(self):
        one = self.recorded["split_down"]["solution"][1][0]
        stack = self.recorded["focus_left"]["solution"][1][0]
        tabs = self.recorded["tab_three"]["solution"][1][0]
        two = self.recorded["rename_workspace"]["solution"][1][0]
        practice = self.recorded["new_workspace"]["solution"][1][0]
        for need, world, expected in (
            ({"check": "has_neighbour", "side": "left"}, one, False),
            ({"check": "has_neighbour", "side": "left"}, stack, True),
            ({"check": "has_neighbour", "side": "right"}, stack, False),
            ({"check": "has_neighbour", "side": "vertical"}, stack, True),
            ({"check": "min_panes", "count": 2}, one, False),
            ({"check": "max_panes", "count": 1}, stack, False),
            ({"check": "min_tabs", "count": 4}, tabs, True),
            ({"check": "max_tabs", "count": 3}, tabs, False),
            ({"check": "tab_unfocused", "position": 2}, tabs, False),
            ({"check": "tab_unfocused", "position": 5}, tabs, False),
            ({"check": "min_workspaces", "count": 2}, practice, False),
            ({"check": "max_workspaces", "count": 1}, two, False),
            ({"check": "not_first_workspace"}, practice, False),
            ({"check": "on_last_workspace"}, two, True),
            ({"check": "not_zoomed"}, stack, True),
            ({"check": "has_focus_history"}, stack, False),
            ({"check": "focus_at", "position": "bottom-right"}, stack, True),
            ({"check": "focus_at", "position": "left"}, stack, False),
        ):
            with self.subTest(need=need):
                self.assertEqual(checks.unmet([need], world) == [], expected)

    def test_a_renamed_old_tab_is_not_the_new_build_tab(self):
        """Lesson 3's build wants a new tab: renaming and splitting tab 1 must not pass (review 1, item 4)."""
        _, (start,), history = self.recorded["build_tab"]["solution"]
        solved = history[-1].herdr
        built = solved.workspace.tabs[-1]
        old = solved.workspace.tabs[0]
        # Tab 1 renamed build and split in two columns, no new tab: the reviewer's way round.
        renamed = replace(built, id=old.id)
        workspace = replace(solved.workspace, tabs=(renamed, *solved.workspace.tabs[1:-1]), active_tab_id=old.id)
        cheat = herdr(replace(solved, workspaces=(workspace,), focused_tab_id=old.id,
                              focused_pane_id=renamed.focused_pane_id))
        self.assertEqual(cheat.herdr.tab_position, 1)
        loose = (hold({"check": "tab_label", "label": "build", "focused": False}),
                 hold({"check": "blueprint", "blueprint": "twin-columns", "tab_label": "build"}),
                 hold({"check": "tab_at", "position": 1}))
        self.assertTrue(checks.evaluate(loose, start, [cheat]).done)  # the hole the old checks left
        self.assertEqual(checks.evaluate(CASES["build_tab"].checks, start, [cheat]).latched, 1)  # only tab_at
        self.assertTrue(checks.evaluate(CASES["build_tab"].checks, start, history).done)
        added = {"check": "tab_added", "label": "build", "mode": "hold"}
        unlabelled = {"check": "tab_added", "mode": "hold"}
        self.assertTrue(checks.evaluate([unlabelled], start, history[:1]).done)  # the new tab, still «5»
        self.assertFalse(checks.evaluate([added], start, history[:1]).done)
        self.assertTrue(checks.evaluate([added], start, history[1:2]).done)
        # Two new tabs is not one new tab.
        extra = replace(built, id="w19:t6", label="6")
        two = herdr(replace(solved, workspaces=(replace(solved.workspace, tabs=(*solved.workspace.tabs, extra)),)))
        self.assertFalse(checks.evaluate([added], start, [two]).done)
        # Only a new tab counts for a blueprint with new = true.
        fresh = {"check": "blueprint", "blueprint": "twin-columns", "tab_label": "build", "new": True, "mode": "hold"}
        self.assertFalse(checks.evaluate([fresh], start, [cheat]).done)
        self.assertTrue(checks.evaluate([fresh], start, history).done)

    def test_zoomed_tab_is_not_ready_for_zoom(self):
        _, (start,), history = self.recorded["zoom"]["solution"]
        self.assertEqual(checks.unmet([{"check": "not_zoomed"}], history[0]), [{"check": "not_zoomed"}])

    def test_focus_history_comes_from_earlier_worlds(self):
        earlier, (start,), _ = self.recorded["last_pane"]["solution"]
        self.assertEqual(checks.unmet([{"check": "has_focus_history"}], start, earlier), [])
        self.assertNotEqual(checks.unmet([{"check": "has_focus_history"}], start, ()), [])


class CheckDataTests(unittest.TestCase):
    def test_unknown_names_and_bad_parameters_raise(self):
        for item, message in (
            ({"check": "teleport"}, "Unknown check 'teleport'"),
            ({"check": "split"}, "needs direction"),
            ({"check": "split", "direction": "up"}, "direction must be"),
            ({"check": "split", "direction": "down", "side": "left"}, "no parameter side"),
            ({"check": "split", "direction": "down", "mode": "sometimes"}, "mode must be"),
            ({"check": "tab_at", "position": True}, "position must be"),
            ({"check": "focus_at", "position": "left-right"}, "position must be"),
            ({"check": "blueprint", "blueprint": "pyramid"}, "blueprint must be"),
            ({"split": "down"}, "Expected a table"),
        ):
            with self.subTest(item=item):
                with self.assertRaisesRegex(ValueError, message):
                    checks.validate([item])
        with self.assertRaisesRegex(ValueError, "Unknown need 'split'"):
            checks.validate(needs=[{"check": "split", "direction": "down"}])
        with self.assertRaisesRegex(ValueError, "no mode"):
            checks.validate(needs=[{"check": "not_zoomed", "mode": "hold"}])
        with self.assertRaisesRegex(ValueError, "Unknown check"):
            checks.evaluate([{"check": "teleport"}], World(), ())

    def test_checks_read_as_toml(self):
        import tomllib
        data = tomllib.loads(
            'checks = [{ check = "focus_moved", side = "right" },\n'
            '          { check = "focus_at", position = "left", mode = "hold" }]\n'
            'needs = [{ check = "has_neighbour", side = "left" }]\n'
        )
        checks.validate(data["checks"], data["needs"])
        self.assertEqual(checks.evaluate(data["checks"], World(), ()), checks.Progress(0, False, 2))

    def test_empty_worlds_never_pass(self):
        for name, entry in checks.CHECKS.items():
            if entry.tool != "herdr":
                continue  # test_checks_yazi covers the Yazi registry
            params = {key: value for key, value in SAMPLE_PARAMS.items() if key in entry.params}
            if "position" in entry.params:
                params["position"] = 1 if name == "tab_at" else "left"
            with self.subTest(check=name):
                self.assertFalse(checks.evaluate([{"check": name, **params}], World(), (World(),)).done)


@unittest.skipUnless(
    os.environ.get("KEYS_TRAINER_TEST_HERDR") == "1" and shutil.which("herdr"),
    "set KEYS_TRAINER_TEST_HERDR=1 to test with a private Herdr server",
)
class PrivateCheckTests(unittest.TestCase):
    """Exported chords, typed into a private herdr session, pass each check; the decoy does not."""

    @classmethod
    def setUpClass(cls):
        cls.backend = HerdrBackend(RIGPAGE_ROWS)
        cls.backend.start_session(CASE_ACTIONS)

    @classmethod
    def tearDownClass(cls):
        cls.backend.close()

    def test_exported_chords_pass_and_decoys_do_not(self):
        for name, case in CASES.items():
            with self.subTest(case=name):
                runs = record(self.backend, case)
                earlier, start, history = runs["solution"]
                self.assertEqual(checks.unmet(case.needs, herdr(start), map(herdr, earlier)), [])
                progress = checks.evaluate(case.checks, herdr(start), [herdr(w) for w in history],
                                           [herdr(w) for w in earlier])
                self.assertTrue(progress.done, f"{name}: {progress}")
                earlier, start, history = runs["decoy"]
                progress = checks.evaluate(case.checks, herdr(start), [herdr(w) for w in history],
                                           [herdr(w) for w in earlier])
                self.assertFalse(progress.done, f"{name} decoy: {progress}")


def rerecord(path=RECORDED):
    """Record every case from a private herdr session into test_data (run: python3 test_checks.py --record)."""
    from unittest.mock import patch

    snapshots = {}
    parse = HerdrWorld.from_snapshot.__func__

    def capture(cls, snapshot):
        built = parse(cls, snapshot)
        snapshots.setdefault(built, {
            **{key: snapshot[key] for key in ("focused_workspace_id", "focused_tab_id", "focused_pane_id")},
            "workspaces": [{key: item[key] for key in ("workspace_id", "label", "active_tab_id")}
                           for item in snapshot["workspaces"]],
            "tabs": [{key: item[key] for key in ("tab_id", "workspace_id", "number", "label")}
                     for item in snapshot["tabs"]],
            "layouts": [{
                **{key: item[key] for key in ("tab_id", "zoomed", "area", "focused_pane_id", "splits")},
                "panes": [{key: pane[key] for key in ("pane_id", "focused", "rect")} for pane in item["panes"]],
            } for item in snapshot["layouts"]],
        })
        return built

    cases = {}
    with patch.object(HerdrWorld, "from_snapshot", classmethod(capture)), HerdrBackend(RIGPAGE_ROWS) as backend:
        backend.start_session(CASE_ACTIONS)
        for name, case in CASES.items():
            table, index = [], {}

            def ref(world):
                if world not in index:
                    index[world] = len(table)
                    table.append(snapshots[world])
                return index[world]

            item = {"worlds": table}
            for run, (earlier, start, history) in record(backend, case).items():
                item[run] = [[ref(w) for w in earlier], [ref(start)], [ref(w) for w in history]]
            cases[name] = item
    lines = [f"{json.dumps(name)}: {json.dumps(item, separators=(',', ':'))}" for name, item in cases.items()]
    path.write_text('{"cases": {\n' + ",\n".join(lines) + "\n}}\n", encoding="utf-8")


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["--record"]:
        rerecord()
    else:
        unittest.main()
