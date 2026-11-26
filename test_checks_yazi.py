"""Yazi checks for lessons 6 and 7, from the curriculum file itself.

Unit tests replay worlds recorded from a private Yazi inside a private herdr
(`python3 test_checks_yazi.py --record` regenerates test_data/yazi_worlds.json).
With KEYS_TRAINER_TEST_YAZI=1 and KEYS_TRAINER_TEST_HERDR=1 every scenario is
played live with the exported chords: the solution passes, the decoy and the near miss do not.
"""

import json
import os
import shutil
import sys
import time
import tomllib
import unittest
from dataclasses import asdict
from pathlib import Path

import checks
from world import World, YaziWorld
from yazi_backend import FIXTURES, KEYMAP, PRACTICE_TREE, YaziSession, YaziState, session_bindings


HERE = Path(__file__).parent
DATA = HERE / "test_data"
RECORDED = DATA / "yazi_worlds.json"
CURRICULUM = tomllib.loads((HERE / "curriculum" / "yazi.toml").read_text(encoding="utf-8"))
LESSONS = {lesson["id"]: lesson for lesson in CURRICULUM["lesson"]}
SCENARIOS = {scenario["id"]: scenario for scenario in CURRICULUM["scenario"]}
YAZI_ROWS = json.loads((DATA / "rigpage_yazi.json").read_text(encoding="utf-8"))
HERDR_ROWS = json.loads((DATA / "rigpage_herdr.json").read_text(encoding="utf-8"))
SETTLE = 0.4  # a world unchanged this long after a key counts as settled

# The mistakes each scenario must tell apart from its solution.
NEAR_MISSES = {
    "yazi-in-out": ["yazi:enter_folder", "yazi:next_file"],              # never comes back out
    "yazi-bottom-top": ["yazi:top", "yazi:bottom"],                      # the wrong order
    "yazi-hidden": ["yazi:filter", {"type": "."}, {"press": "enter"}],   # filters on a dot instead
    "yazi-filter": ["yazi:filter", {"type": "no"}],                      # typed, never confirmed
    "yazi-mark-one": ["yazi:next_file"],                                 # the cursor alone
    "yazi-range": ["yazi:visual", "yazi:next_file", "yazi:next_file"],   # still in visual mode
    "yazi-copy": ["yazi:cut", "yazi:top", "yazi:enter_folder", "yazi:paste"],   # moved, not copied
    "yazi-move-two": ["yazi:mark_one", "yazi:mark_one", "yazi:yank", "yazi:top",
                      "yazi:enter_folder", "yazi:paste"],                # copied, not moved
}


def lesson_of(scenario_id):
    return next(lesson for lesson in LESSONS.values()
                if any(step.split(":", 1)[1] == scenario_id for step in lesson["steps"]))


def fixture_of(scenario_id):
    return SCENARIOS[scenario_id].get("fixture") or lesson_of(scenario_id)["fixture"]


def lesson_cards(lesson):
    ids = {step.split(":", 1)[1] for step in lesson["steps"]}
    return sorted({card for scenario_id in ids for card in SCENARIOS[scenario_id]["cards"]}
                  | {item for scenario_id in ids for item in (*SCENARIOS[scenario_id]["solution"],
                                                               SCENARIOS[scenario_id].get("decoy", ""))
                     if isinstance(item, str) and item})


def run_item(session, item):
    """Play one solution item as the engine does: a card, { press }, { type } or { wait }."""
    if isinstance(item, str):
        session.play(item)
    elif "press" in item:
        session.press(item["press"])
    elif "type" in item:
        session.type_text(item["type"])
    else:
        time.sleep(item["wait"])


def settle(session, history, timeout=3.0):
    """Collect every new world until none has appeared for SETTLE seconds."""
    end = time.monotonic() + timeout
    changed = time.monotonic()
    while time.monotonic() < end and time.monotonic() - changed < SETTLE:
        world = session.world()
        if world.yazi != history[-1].yazi:
            history.append(world)
            changed = time.monotonic()
        time.sleep(0.02)


def play(session, items, start=None):
    """Play items from `start` (else the current world); return (start, history after it)."""
    trail = [start or session.world()]
    for item in items:
        run_item(session, item)
        settle(session, trail)
    return trail[0], trail[1:]


def to_json(world):
    state = asdict(world.yazi)
    return {key: sorted(value) if isinstance(value, (set, frozenset)) else value for key, value in state.items()}


def from_json(data):
    data = dict(data)
    for key in ("entries",):
        data[key] = tuple(data[key])
    for key in ("selected", "tree"):
        data[key] = frozenset(data[key])
    return World(yazi=YaziState(**data))


def record():
    cases = {}
    for lesson in LESSONS.values():
        with YaziSession(YAZI_ROWS, HERDR_ROWS) as session:
            session.start(lesson_cards(lesson), lesson["fixture"])
            for scenario_id in dict.fromkeys(step.split(":", 1)[1] for step in lesson["steps"]):
                scenario = SCENARIOS[scenario_id]
                runs = {"solution": scenario["solution"], "near": NEAR_MISSES[scenario_id]}
                if scenario.get("decoy"):
                    runs["decoy"] = [scenario["decoy"]]
                for kind, items in runs.items():
                    start, history = play(session, items, session.apply_fixture(fixture_of(scenario_id)))
                    cases[f"{scenario_id}:{kind}"] = [to_json(world) for world in (start, *history)]
    RECORDED.write_text(json.dumps(cases, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Recorded {len(cases)} runs to {RECORDED}")


def load_recorded():
    data = json.loads(RECORDED.read_text(encoding="utf-8"))
    return {name: [from_json(world) for world in worlds] for name, worlds in data.items()}


class CurriculumTests(unittest.TestCase):
    def test_lessons_scenarios_and_copy_follow_the_schema(self):
        self.assertEqual([lesson["order"] for lesson in LESSONS.values()], [6, 7])
        for lesson in LESSONS.values():
            self.assertEqual(lesson["tool"], "yazi")
            self.assertIn(lesson["fixture"], FIXTURES)
            for step in lesson["steps"]:
                kind, scenario_id = step.split(":", 1)
                self.assertIn(kind, ("learn", "recall", "build"))
                self.assertIn(scenario_id, SCENARIOS)
        for scenario_id, scenario in SCENARIOS.items():
            with self.subTest(scenario_id):
                checks.validate(scenario["checks"], scenario["needs"])
                self.assertLessEqual(len(scenario["goal"]), 66)
                self.assertLessEqual(len(scenario["why"]), 70)
                self.assertLessEqual(len(scenario["nudge"]), 80)
                self.assertTrue(scenario["done"])
                self.assertIn(fixture_of(scenario_id), FIXTURES)
                cards = [*scenario["cards"], *(item for item in scenario["solution"] if isinstance(item, str)),
                         scenario.get("decoy", "yazi:top")]
                for card in cards:
                    self.assertIn(card.removeprefix("yazi:"), KEYMAP)
        self.assertEqual([scenario_id for scenario_id, s in SCENARIOS.items() if s.get("typing")], ["yazi-filter"])

    def test_the_export_binds_every_card_the_lessons_use(self):
        bindings = session_bindings(YAZI_ROWS, KEYMAP)
        for lesson in LESSONS.values():
            for card in lesson_cards(lesson):
                self.assertTrue(bindings.get(card.removeprefix("yazi:")), card)

    def test_every_yazi_check_is_used_by_a_scenario(self):
        used = {item["check"] for scenario in SCENARIOS.values() for item in scenario["checks"]}
        self.assertEqual(used, {name for name, entry in checks.CHECKS.items() if entry.tool == "yazi"})
        used = {item["check"] for scenario in SCENARIOS.values() for item in scenario["needs"]}
        self.assertEqual(used, {name for name, entry in checks.NEEDS.items() if entry.tool == "yazi"})


class RecordedYaziTests(unittest.TestCase):
    """Worlds recorded from a private Yazi 26.9.1 in private herdr 0.9.5, played with the exported chords."""

    @classmethod
    def setUpClass(cls):
        cls.recorded = load_recorded()

    def test_every_run_was_recorded(self):
        expected = {f"{scenario_id}:{kind}" for scenario_id, scenario in SCENARIOS.items()
                    for kind in ("solution", "near", *(("decoy",) if scenario.get("decoy") else ()))}
        self.assertEqual(set(self.recorded), expected)

    def test_solutions_pass_from_a_start_their_needs_accept(self):
        for scenario_id, scenario in SCENARIOS.items():
            with self.subTest(scenario_id):
                start, *history = self.recorded[f"{scenario_id}:solution"]
                self.assertEqual(checks.unmet(scenario["needs"], start), [])
                progress = checks.evaluate(scenario["checks"], start, history)
                self.assertTrue(progress.done, progress)
                # Never done before the last move, and never on the start alone.
                self.assertFalse(checks.evaluate(scenario["checks"], start, []).done)

    def test_decoys_and_near_misses_fail(self):
        for name, (start, *history) in self.recorded.items():
            scenario_id, kind = name.split(":")
            if kind == "solution":
                continue
            with self.subTest(name):
                self.assertEqual(checks.unmet(SCENARIOS[scenario_id]["needs"], start), [])
                self.assertFalse(checks.evaluate(SCENARIOS[scenario_id]["checks"], start, history).done)

    def test_every_need_is_met_somewhere_and_unmet_somewhere(self):
        worlds = [world for run in self.recorded.values() for world in run]
        for scenario in SCENARIOS.values():
            for item in scenario["needs"]:
                with self.subTest(item):
                    results = {not checks.unmet([item], world) for world in worlds}
                    self.assertEqual(results, {True, False})

    def test_checks_need_the_yazi_world_and_the_tree_on_disk(self):
        start, *history = self.recorded["yazi-copy:solution"]
        copy = SCENARIOS["yazi-copy"]["checks"]
        self.assertFalse(checks.evaluate(copy, World(), [World()]).done)
        # Without the disk tree a hook listing alone never proves a file landed.
        bare = [World(yazi=YaziWorld(**{name: getattr(world.yazi, name) for name in YaziWorld.__dataclass_fields__}))
                for world in (start, *history)]
        self.assertFalse(checks.evaluate(copy, bare[0], bare[1:]).done)
        self.assertTrue(checks.evaluate(copy, start, history).done)
        self.assertEqual(start.yazi.tree, frozenset(PRACTICE_TREE))

    def test_cut_then_paste_moves_and_empties_the_clipboard(self):
        *_, end = self.recorded["yazi-move-two:solution"]
        self.assertEqual(end.yazi.cwd, "Practice/Projects")
        self.assertEqual(end.yazi.tree, frozenset({
            "Practice/.env", "Practice/report.md", "Practice/Projects/draft.txt", "Practice/Projects/notes.txt",
            "Practice/Projects/planning.txt", "Practice/Projects/timeline.txt",
        }))
        self.assertEqual(end.yazi.yanked, 0)

    def test_the_filter_prompt_is_the_input_layer(self):
        start, *history = self.recorded["yazi-filter:solution"]
        self.assertEqual([world.yazi.layer for world in history][0], "input")
        self.assertEqual(history[-1].yazi.layer, "mgr")
        self.assertEqual(history[-1].yazi.entries, ("notes.txt",))


LIVE = (os.environ.get("KEYS_TRAINER_TEST_YAZI") == "1" and os.environ.get("KEYS_TRAINER_TEST_HERDR") == "1"
        and shutil.which("yazi") and shutil.which("herdr"))


@unittest.skipUnless(LIVE, "set KEYS_TRAINER_TEST_YAZI=1 and KEYS_TRAINER_TEST_HERDR=1 to play lessons 6 and 7 live")
class LiveYaziLessonTests(unittest.TestCase):
    def test_each_scenario_solution_passes_and_its_decoy_and_near_miss_do_not(self):
        for lesson in LESSONS.values():
            with YaziSession(YAZI_ROWS, HERDR_ROWS) as session:
                session.start(lesson_cards(lesson), lesson["fixture"])
                for scenario_id in dict.fromkeys(step.split(":", 1)[1] for step in lesson["steps"]):
                    scenario = SCENARIOS[scenario_id]
                    runs = {"solution": scenario["solution"], "near": NEAR_MISSES[scenario_id],
                            "decoy": [scenario["decoy"]]}
                    for kind, items in runs.items():
                        with self.subTest(f"{scenario_id}:{kind}"):
                            start = session.apply_fixture(fixture_of(scenario_id))
                            self.assertEqual(checks.unmet(scenario["needs"], start), [])
                            start, history = play(session, items, start)
                            done = checks.evaluate(scenario["checks"], start, history).done
                            self.assertEqual(done, kind == "solution")

    def test_typing_in_the_filter_prompt_is_reported(self):
        with YaziSession(YAZI_ROWS, HERDR_ROWS) as session:
            session.start(["yazi:filter"], "practice_tree")
            self.assertFalse(session.typing())
            session.play("yazi:filter")
            settle(session, [session.world()])
            self.assertTrue(session.typing())
            session.type_text("no")
            settle(session, [session.world()])
            self.assertTrue(session.typing())
            self.assertEqual(session.world().yazi.filter, "no")
            session.press("enter")
            settle(session, [session.world()])
            self.assertFalse(session.typing())
            session.press("esc")
            settle(session, [session.world()])
            self.assertEqual(session.world().yazi.filter, "")

    def test_each_lesson_chains_its_steps_rebuilding_only_when_needs_fail(self):
        for lesson in LESSONS.values():
            with self.subTest(lesson["id"]), YaziSession(YAZI_ROWS, HERDR_ROWS) as session:
                world = session.start(lesson_cards(lesson), lesson["fixture"])
                for number, step in enumerate(lesson["steps"]):
                    scenario_id = step.split(":", 1)[1]
                    scenario = SCENARIOS[scenario_id]
                    if checks.unmet(scenario["needs"], world):
                        self.assertTrue(number, "the lesson fixture must fit its first step")
                        world = session.apply_fixture(fixture_of(scenario_id))
                    self.assertEqual(checks.unmet(scenario["needs"], world), [], step)
                    start, history = play(session, scenario["solution"], world)
                    self.assertTrue(checks.evaluate(scenario["checks"], start, history).done, step)
                    world = history[-1]


if __name__ == "__main__":
    if sys.argv[1:] == ["--record"]:
        record()
    else:
        unittest.main()
