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
    "yazi-copy-path": ["yazi:copy_filename"],                            # the name, not the path
    "yazi-copy-filename": ["yazi:copy_path"],
    "yazi-copy-dirpath": ["yazi:copy_path"],
    "yazi-create": ["yazi:create", {"type": "todo.txt"}],               # typed, never confirmed
    "yazi-rename": ["yazi:rename", {"type": "-old"}],
    "yazi-trash": ["yazi:rename", {"type": "-old"}, {"press": "enter"}],  # renamed, not trashed
    "yazi-select-all": ["yazi:next_file"],                              # nothing selected
    "yazi-history": ["yazi:enter_folder", "yazi:leave_folder", "yazi:enter_folder"],  # plain leave/enter
    "yazi-new-tab": ["yazi:enter_folder", "yazi:new_tab"],              # tab opened in Projects, not Practice
    "yazi-search-name": ["yazi:search_content", {"type": "deploy"}, {"press": "enter"}],   # rg instead of fd
    "yazi-search-content": ["yazi:search_name", {"type": "lantern"}, {"press": "enter"}],  # fd instead of rg
    "yazi-zoxide": ["yazi:next_file", "yazi:enter_folder"],             # manual enter Docs, not zoxide
    "yazi-fzf": ["yazi:jump_zoxide", {"type": "Projects"}, {"press": "enter"}],  # zoxide instead of fzf
    "yazi-open-default": ["yazi:open_with", {"press": "down"}, {"press": "enter"}],  # editor via open-with
    "yazi-open-with": ["yazi:open_with", {"press": "enter"}],           # picked default instead of editor
    "yazi-find-open": ["yazi:search_content", {"type": "lantern"}, {"press": "enter"}],  # searched, never opened
}


def lesson_of(scenario_id):
    return next(lesson for lesson in LESSONS.values()
                if any(step.split(":", 1)[1] == scenario_id for step in lesson["steps"]))


def fixture_of(scenario_id):
    return SCENARIOS[scenario_id].get("fixture") or lesson_of(scenario_id)["fixture"]


def lesson_cards(lesson):
    ids = {step.split(":", 1)[1] for step in lesson["steps"]}
    decoys = []
    for scenario_id in ids:
        decoy = SCENARIOS[scenario_id].get("decoy")
        if isinstance(decoy, str):
            decoys.append(decoy)
        elif isinstance(decoy, list):
            decoys.extend(item for item in decoy if isinstance(item, str))
    return sorted({card for scenario_id in ids for card in SCENARIOS[scenario_id]["cards"]}
                  | {item for scenario_id in ids for item in (*SCENARIOS[scenario_id]["solution"], *decoys)
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
    for key in ("selected", "tree", "trashed"):
        if key in data:
            data[key] = frozenset(data[key])
    if "opened" in data:
        data["opened"] = tuple((str(item[0]), bool(item[1]), str(item[2])) for item in data["opened"])
    return World(yazi=YaziState(**data))


def _decoy_items(scenario):
    decoy = scenario.get("decoy")
    if isinstance(decoy, list):
        return decoy
    return [decoy] if decoy else []


def record():
    cases = {}
    for lesson in LESSONS.values():
        with YaziSession(YAZI_ROWS, HERDR_ROWS) as session:
            session.start(lesson_cards(lesson), lesson["fixture"])
            for scenario_id in dict.fromkeys(step.split(":", 1)[1] for step in lesson["steps"]):
                scenario = SCENARIOS[scenario_id]
                runs = {"solution": scenario["solution"], "near": NEAR_MISSES[scenario_id]}
                if decoy := _decoy_items(scenario):
                    runs["decoy"] = decoy
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
        self.assertEqual([lesson["order"] for lesson in LESSONS.values()], [6, 7, 8, 9, 10, 13, 14])
        for lesson in LESSONS.values():
            self.assertEqual(lesson["tool"], "yazi")
            self.assertIn(lesson["fixture"], FIXTURES)
            for step in lesson["steps"]:
                kind, scenario_id = step.split(":", 1)
                self.assertIn(kind, ("learn", "recall", "build"))
                self.assertIn(scenario_id, SCENARIOS)
        for scenario_id, scenario in SCENARIOS.items():
            with self.subTest(scenario_id):
                plain = [{k: v for k, v in c.items() if k != "checklist"} for c in scenario["checks"]]
                checks.validate(plain, scenario["needs"])
                self.assertLessEqual(len(scenario["goal"]), 66)
                self.assertLessEqual(len(scenario["why"]), 70)
                self.assertLessEqual(len(scenario["nudge"]), 80)
                self.assertTrue(scenario["done"])
                self.assertIn(fixture_of(scenario_id), FIXTURES)
                decoy_cards = [item for item in _decoy_items(scenario) if isinstance(item, str)]
                cards = [*scenario["cards"], *(item for item in scenario["solution"] if isinstance(item, str)),
                         *decoy_cards]
                for card in cards:
                    self.assertIn(card.removeprefix("yazi:"), KEYMAP)
        self.assertEqual(
            [scenario_id for scenario_id, s in SCENARIOS.items() if s.get("typing")],
            ["yazi-filter", "yazi-create", "yazi-rename",
             "yazi-search-name", "yazi-search-content", "yazi-zoxide", "yazi-fzf", "yazi-find-open"],
        )

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


def _plain_checks(scenario):
    return [{k: v for k, v in c.items() if k != "checklist"} for c in scenario["checks"]]


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
                progress = checks.evaluate(_plain_checks(scenario), start, history)
                self.assertTrue(progress.done, progress)
                # Never done before the last move, and never on the start alone.
                self.assertFalse(checks.evaluate(_plain_checks(scenario), start, []).done)

    def test_decoys_and_near_misses_fail(self):
        for name, (start, *history) in self.recorded.items():
            scenario_id, kind = name.split(":")
            if kind == "solution":
                continue
            with self.subTest(name):
                self.assertEqual(checks.unmet(SCENARIOS[scenario_id]["needs"], start), [])
                self.assertFalse(checks.evaluate(_plain_checks(SCENARIOS[scenario_id]), start, history).done)

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

    def test_yazi_history_rejects_plain_leave_or_enter_and_accepts_back_then_forward(self):
        from dataclasses import replace

        start, *history = self.recorded["yazi-history:solution"]
        self.assertTrue(checks.evaluate(SCENARIOS["yazi-history"]["checks"], start, history).done)
        near_start, *near_history = self.recorded["yazi-history:near"]
        self.assertFalse(checks.evaluate(SCENARIOS["yazi-history"]["checks"], near_start, near_history).done)
        # Using back, then plain enter instead of forward, must also fail.
        back_then_enter = [
            World(yazi=replace(start.yazi, cwd="Practice/Projects", history="")),
            World(yazi=replace(start.yazi, cwd="Practice", history="back:Projects ")),
            World(yazi=replace(start.yazi, cwd="Practice/Projects", history="back:Projects ")),
        ]
        self.assertFalse(checks.evaluate(SCENARIOS["yazi-history"]["checks"], start, back_then_enter).done)
        # Typed so fast that Practice is never drawn: the end alone still counts.
        unseen = [back_then_enter[0], World(yazi=replace(
            start.yazi, cwd="Practice/Projects", history="back:Projects forward:Practice "))]
        self.assertTrue(checks.evaluate(SCENARIOS["yazi-history"]["checks"], start, unseen).done)

    def test_yazi_new_tab_requires_the_second_tab_in_practice(self):
        start, *history = self.recorded["yazi-new-tab:solution"]
        self.assertTrue(checks.evaluate(SCENARIOS["yazi-new-tab"]["checks"], start, history).done)
        near_start, *near_history = self.recorded["yazi-new-tab:near"]
        self.assertFalse(checks.evaluate(SCENARIOS["yazi-new-tab"]["checks"], near_start, near_history).done)

    def test_yazi_trash_rejects_rename_and_non_trash_changes(self):
        from dataclasses import replace

        trash_checks = SCENARIOS["yazi-trash"]["checks"]
        start, *history = self.recorded["yazi-trash:solution"]
        self.assertTrue(checks.evaluate(trash_checks, start, history).done)
        near_start, *near_history = self.recorded["yazi-trash:near"]
        self.assertFalse(checks.evaluate(trash_checks, near_start, near_history).done)
        # Disappearing without landing in the private trash fails.
        gone_only = World(yazi=replace(
            start.yazi,
            entries=("Projects", "draft.txt", "report.md"),
            tree=start.yazi.tree - {"Practice/notes.txt"},
            trashed=frozenset(),
        ))
        self.assertFalse(checks.evaluate(trash_checks, start, [gone_only]).done)
        # Trashing notes.txt while adding or removing another file also fails.
        extra_file = World(yazi=replace(
            gone_only.yazi,
            tree=gone_only.yazi.tree | {"Practice/extra.txt"},
            trashed=frozenset({"notes.txt"}),
        ))
        self.assertFalse(checks.evaluate(trash_checks, start, [extra_file]).done)


class SearchJumpOpenCheckTests(unittest.TestCase):
    def _state(self, **kw):
        defaults = dict(
            cwd="Practice", hovered="Archive", entries=("Archive", "Docs", "Projects", "notes.txt"),
            selected=frozenset(), show_hidden=False, filter="", layer="mgr", tab_count=1, tab_index=0,
            yanked=0, cut=False, visual=False, copied="", history="",
            search_via="", search_query="", jump_via="", open_interactive=False,
            tree=frozenset(), trashed=frozenset(), opened=(),
        )
        defaults.update(kw)
        return World(yazi=YaziState(**defaults))

    def test_search_is_requires_matching_via_query_entry_and_mgr_layer(self):
        start = self._state()
        spec = [{"check": "search_is", "via": "fd", "query": "deploy", "entry": "deploy.md"}]
        checks.validate(spec, [{"check": "no_search"}])
        self.assertEqual(checks.unmet([{"check": "no_search"}], start), [])
        good = self._state(search_via="fd", search_query="deploy", entries=("deploy.md",), hovered="deploy.md")
        self.assertTrue(checks.evaluate(spec, start, [good]).done)
        self.assertTrue(checks.unmet([{"check": "no_search"}], good))
        # Wrong search tool (rg instead of fd) must fail.
        wrong_via = self._state(search_via="rg", search_query="deploy", entries=("deploy.md",), hovered="deploy.md")
        self.assertFalse(checks.evaluate(spec, start, [wrong_via]).done)
        # Still typing in input layer must fail.
        typing = self._state(search_via="fd", search_query="deploy", entries=("deploy.md",), layer="input")
        self.assertFalse(checks.evaluate(spec, start, [typing]).done)
        # Missing target entry must fail.
        empty = self._state(search_via="fd", search_query="deploy", entries=(), hovered=None)
        self.assertFalse(checks.evaluate(spec, start, [empty]).done)

    def test_jumped_to_requires_jump_via_folder_and_optional_entry(self):
        start = self._state()
        zo_spec = [{"check": "jumped_to", "via": "zoxide", "folder": "Docs"}]
        fzf_spec = [{"check": "jumped_to", "via": "fzf", "folder": "Projects", "entry": "deploy.md"}]
        checks.validate(zo_spec + fzf_spec)
        zo_ok = self._state(cwd="Practice/Docs", jump_via="zoxide", entries=("guide.txt",), hovered="guide.txt")
        self.assertTrue(checks.evaluate(zo_spec, start, [zo_ok]).done)
        # Manual enter (empty jump_via) or wrong jump tool (fzf) must fail.
        self.assertFalse(checks.evaluate(zo_spec, start, [self._state(cwd="Practice/Docs", jump_via="")]).done)
        self.assertFalse(checks.evaluate(zo_spec, start, [self._state(cwd="Practice/Docs", jump_via="fzf")]).done)
        # fzf jump requires both folder and hovered entry.
        fzf_ok = self._state(cwd="Practice/Projects", jump_via="fzf", entries=("deploy.md",), hovered="deploy.md")
        self.assertTrue(checks.evaluate(fzf_spec, start, [fzf_ok]).done)
        fzf_wrong_hover = self._state(cwd="Practice/Projects", jump_via="fzf", entries=("deploy.md",), hovered="other.md")
        self.assertFalse(checks.evaluate(fzf_spec, start, [fzf_wrong_hover]).done)

    def test_opened_requires_new_stub_entry_and_interactive_mode(self):
        start = self._state(hovered="notes.txt")
        default_spec = [{"check": "opened", "opener": "default", "file": "Practice/notes.txt"}]
        inter_spec = [{"check": "opened", "opener": "editor", "file": "Practice/notes.txt", "interactive": True}]
        checks.validate(default_spec + inter_spec, [{"check": "nothing_opened"}])
        self.assertEqual(checks.unmet([{"check": "nothing_opened"}], start), [])
        opened_default = self._state(hovered="notes.txt", opened=(("default", False, "Practice/notes.txt"),), open_interactive=False)
        self.assertTrue(checks.evaluate(default_spec, start, [opened_default]).done)
        self.assertTrue(checks.unmet([{"check": "nothing_opened"}], opened_default))
        # Default open must not satisfy interactive editor open, and vice versa.
        self.assertFalse(checks.evaluate(inter_spec, start, [opened_default]).done)
        opened_editor = self._state(hovered="notes.txt", opened=(("editor", True, "Practice/notes.txt"),), open_interactive=True)
        self.assertTrue(checks.evaluate(inter_spec, start, [opened_editor]).done)
        self.assertFalse(checks.evaluate(default_spec, start, [opened_editor]).done)
        # An editor open without interactive flag must not satisfy interactive=True.
        non_inter = self._state(hovered="notes.txt", opened=(("editor", False, "Practice/notes.txt"),), open_interactive=False)
        self.assertFalse(checks.evaluate(inter_spec, start, [non_inter]).done)
        # Already opened at start must not pass without a new open event.
        self.assertFalse(checks.evaluate(default_spec, opened_default, [opened_default]).done)
        # An earlier (default, False, Practice/notes.txt) record is unaffected by a later open_interactive=True flag.
        later_flag = self._state(hovered="notes.txt", opened=(("default", False, "Practice/notes.txt"),), open_interactive=True)
        self.assertTrue(checks.evaluate(default_spec, start, [later_flag]).done)
        self.assertFalse(checks.evaluate(inter_spec, start, [later_flag]).done)

    def test_yazi_find_open_combined_snapshot_passes_and_without_search_fails(self):
        start = self._state()
        find_open_checks = _plain_checks(SCENARIOS["yazi-find-open"])
        combined = self._state(
            search_via="rg", search_query="lantern", entries=("guide.txt",), hovered="guide.txt",
            opened=(("default", False, "Practice/Docs/guide.txt"),),
        )
        self.assertTrue(checks.evaluate(find_open_checks, start, [combined]).done)
        opened_without_search = self._state(
            cwd="Practice/Docs", entries=("guide.txt",), hovered="guide.txt",
            opened=(("default", False, "Practice/Docs/guide.txt"),),
        )
        self.assertFalse(checks.evaluate(find_open_checks, start, [opened_without_search]).done)


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
                            "decoy": _decoy_items(scenario)}
                    for kind, items in runs.items():
                        with self.subTest(f"{scenario_id}:{kind}"):
                            start = session.apply_fixture(fixture_of(scenario_id))
                            self.assertEqual(checks.unmet(scenario["needs"], start), [])
                            start, history = play(session, items, start)
                            done = checks.evaluate(_plain_checks(scenario), start, history).done
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
                    self.assertTrue(checks.evaluate(_plain_checks(scenario), start, history).done, step)
                    world = history[-1]


if __name__ == "__main__":
    if sys.argv[1:] == ["--record"]:
        record()
    else:
        unittest.main()
