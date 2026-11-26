"""Private-session acceptance tests and fast result/coverage checks."""

from collections import Counter
from contextlib import closing
from dataclasses import replace
import os
import unittest
from unittest.mock import patch

import curriculum
import practice
import selftest
from test_lesson import CURRICULUM, DATA, FakeHerdr


class BookkeepingTests(unittest.TestCase):
    def setUp(self):
        # The focus lesson exercises fixture setup, inter-step history and a necessary rebuild.
        loaded = replace(CURRICULUM, lessons=(CURRICULUM.lesson("panes-move"),))
        self.resolved = curriculum.resolve(loaded, DATA)
        self.sessions = []

        def session(tool, rows):
            fake = FakeHerdr(self.resolved)
            self.sessions.append(fake)
            return closing(fake)

        for name, value in (("_session", session), ("MOVE_GAP", 0),
                            ("SOLUTION_TIMEOUT", 0), ("DECOY_TIMEOUT", 0)):
            self.enterContext(patch.object(selftest, name, value))

    def test_fresh_attempts_and_one_chained_run(self):
        results = selftest.solve_all(self.resolved, DATA)
        self.assertEqual(Counter(r.kind for r in results), {"solution": 6, "decoy": 6, "chained": 1})
        self.assertTrue(all(r.passed for r in results), selftest.lines(results))
        self.assertEqual(len(self.sessions), len(results))
        self.assertTrue(all(len(fake.fixtures) == 1 for fake in self.sessions[:-1]))
        self.assertEqual(self.sessions[-1].fixtures, ["right_stack", "one_pane"])
        self.assertEqual(results[-1].rebuilds, 1)
        self.assertTrue(all(r.lesson == "panes-move" and r.seconds >= 0 for r in results))
        lines = selftest.lines(results)
        self.assertEqual(len(lines), len(results) + 1)
        self.assertTrue(all(len(line.splitlines()) == 1 for line in lines))
        self.assertIn("13/13", lines[-1])

    def test_decoy_that_solves_and_unbound_decoy_are_both_failures(self):
        for decoy in ((curriculum.Move(card="herdr:focus_pane_left"),),
                      (curriculum.Move(card="herdr:no_such_binding"),)):
            with self.subTest(decoy=decoy):
                state = self.resolved.scenarios["focus-left"]
                self.resolved.scenarios["focus-left"] = replace(state, scenario=replace(state.scenario, decoy=decoy))
                results = selftest.solve_all(self.resolved, DATA)
                self.assertEqual([(r.scenario, r.kind) for r in results if not r.passed], [("focus-left", "decoy")])
                self.assertIn("12/13", selftest.lines(results)[-1])

    def test_tab_build_decoy_catches_a_renamed_old_tab(self):
        """Review 1, item 4: the build-tab decoy renames and splits tab 1; only a new tab passes."""
        from session import perform
        self.enterContext(patch.object(selftest, "perform",
                                       lambda session, move, chord=None: perform(session, move, chord, lambda _: None)))
        loaded = replace(CURRICULUM, lessons=(CURRICULUM.lesson("tabs"),))
        self.resolved = curriculum.resolve(loaded, DATA)
        results = selftest.solve_all(self.resolved, DATA)
        self.assertTrue(all(r.passed for r in results), selftest.lines(results))
        self.assertIn(("build-tab", "decoy"), [(r.scenario, r.kind) for r in results])
        # Under the old checks (any tab called build) the same decoy passes: the hole it guards.
        state = self.resolved.scenarios["build-tab"]
        loose = ({"check": "tab_label", "label": "build", "focused": False, "mode": "hold"},
                 {"check": "blueprint", "blueprint": "twin-columns", "tab_label": "build", "mode": "hold"},
                 {"check": "tab_at", "position": 1, "mode": "hold"})
        self.resolved.scenarios["build-tab"] = replace(state, scenario=replace(state.scenario, checks=loose))
        results = selftest.solve_all(self.resolved, DATA)
        self.assertEqual([(r.scenario, r.kind) for r in results if not r.passed], [("build-tab", "decoy")])

    def test_decoy_that_passes_late_in_the_observation_window(self):
        scenario = self.resolved.scenarios["focus-left"].scenario
        fake = FakeHerdr(self.resolved)
        start = fake.start(scenario.played_cards, "right_stack")
        fake.play("herdr:focus_pane_left")
        solved = fake.world()
        now = 0.0

        def advance(seconds):
            nonlocal now
            self.assertLessEqual(seconds, 0.02)
            now += seconds

        with patch.object(fake, "world", side_effect=lambda: solved if now >= 1.4 else start), \
                patch.multiple(selftest, monotonic=lambda: now, sleep=advance, DECOY_TIMEOUT=1.5):
            with self.assertRaisesRegex(ValueError, "Decoy accepted"):
                selftest._attempt(fake, self.resolved, scenario, [start], decoy=True)
        self.assertLess(now, 1.5)


@unittest.skipUnless(os.environ.get("KEYS_TRAINER_TEST_HERDR") == "1", "private herdr tests disabled")
class LiveSelfTest(unittest.TestCase):
    def check_tool(self, tool):
        path = curriculum.FOLDER / f"{tool}.toml"
        loaded = curriculum.loads({path.name: path.read_text(encoding="utf-8")})
        rows = practice.read_keys()
        resolved = curriculum.resolve(loaded, rows)
        results = selftest.solve_all(resolved, rows)
        active = {sid for sid, state in resolved.scenarios.items() if state.active}
        self.assertTrue(active, f"No active {tool} scenario in the live export")
        expected = Counter(("solution", sid) for sid in active)
        expected.update(("decoy", sid) for sid in active if loaded.scenarios[sid].decoy)
        expected.update(("chained", state.lesson.id) for state in resolved.lessons if state.steps)
        actual = Counter((r.kind, r.lesson if r.kind == "chained" else r.scenario) for r in results)
        self.assertEqual(actual, expected)
        failures = [r for r in results if not r.passed]
        counts = ", ".join(f"{r.lesson}={r.rebuilds}" for r in results if r.kind == "chained")
        if failures:
            self.fail(f"Chained rebuilds: {counts}\n" + "\n".join(selftest.lines(failures)[:-1]))
        print(f"{tool}: {len(active)} solutions passed, {sum(r.kind == 'decoy' for r in results)} decoys rejected. "
              f"Chained rebuilds: {counts}", flush=True)

    def test_herdr(self):
        self.check_tool("herdr")

    @unittest.skipUnless(os.environ.get("KEYS_TRAINER_TEST_YAZI") == "1", "private Yazi tests disabled")
    def test_yazi(self):
        self.check_tool("yazi")


if __name__ == "__main__":
    unittest.main()
