import json
import unittest
from pathlib import Path

import curriculum
from curriculum import CurriculumError, chord_text, loads, report, resolve

ROWS = json.loads(Path(__file__).with_name("test_data").joinpath("rigpage_herdr.json").read_text("utf-8"))
DATA = {"groups": [{"id": "herdr", "rows": ROWS}]}
HERDR = curriculum.FOLDER.joinpath("herdr.toml").read_text("utf-8")

LESSON = '''
[[lesson]]
id = "demo"
order = 1
title = "Demo"
tool = "herdr"
fixture = "one_pane"
intro = "A small lesson."
pattern = "Alt is the direct layer."
steps = ["learn:rows", "recall:rows", "learn:cols"]
'''
SCENARIO = '''
[[scenario]]
id = "{id}"
goal = "Split the pane into two rows"
why = "Because."
nudge = "The Alt layer."
done = "Split"
cards = ["herdr:{action}"]
checks = [{{ check = "split", direction = "down" }}]
solution = ["herdr:{action}"]
'''


def demo(lesson=LESSON, **scenarios):
    scenarios = scenarios or {"rows": "split_horizontal", "cols": "split_vertical"}
    return lesson + "".join(SCENARIO.format(id=sid, action=action) for sid, action in scenarios.items())


class LoaderTest(unittest.TestCase):
    def problems(self, text, name="demo.toml"):
        with self.assertRaises(CurriculumError) as caught:
            loads({name: text})
        return caught.exception.problems

    def test_shipped_lessons_load_in_order_with_the_design_steps(self):
        loaded = curriculum.load()
        herdr = [lesson for lesson in loaded.lessons if lesson.tool == "herdr"]
        self.assertEqual([lesson.title for lesson in herdr],
                         ["Split the screen", "Move between panes", "Tabs", "Workspaces", "Resize and build",
                          "herdr: jump around"])
        self.assertEqual([len(lesson.steps) for lesson in herdr], [7, 9, 9, 7, 4, 4])
        self.assertEqual([f"{s.kind[0].upper()}:{s.scenario}" for s in herdr[0].steps],
                         ["L:split-rows", "L:split-cols", "R:split-rows", "L:close-pane", "R:split-cols",
                          "R:close-pane", "B:build-split-bottom"])
        for scenario in loaded.scenarios.values():
            if scenario.tool == "herdr":
                self.assertTrue(scenario.nudge and scenario.decoy, scenario.id)

    def test_good_file_loads(self):
        loaded = loads({"demo.toml": demo()})
        self.assertEqual(loaded.lesson("demo").steps[1], curriculum.Step("recall", "rows"))

    def test_unknown_check_and_need_names_and_parameters(self):
        text = demo().replace('check = "split", direction = "down"', 'check = "teleport"', 1)
        self.assertIn("demo.toml: scenario rows: Unknown check 'teleport'.", self.problems(text))
        text = demo().replace('cards = ["herdr:split_horizontal"]',
                              'cards = ["herdr:split_horizontal"]\nneeds = [{ check = "magic" }]', 1)
        self.assertIn("demo.toml: scenario rows: Unknown need 'magic'.", self.problems(text))
        text = demo().replace('direction = "down"', 'direction = "diagonal"', 1)
        self.assertTrue(any("direction must be" in p for p in self.problems(text)))

    def test_missing_and_over_long_copy(self):
        text = demo().replace('why = "Because."\n', "", 1)
        self.assertIn("demo.toml: scenario rows: missing why", self.problems(text))
        text = demo().replace('goal = "Split the pane into two rows"', f'goal = "{"x" * 67}"', 1)
        self.assertIn("demo.toml: scenario rows: goal is 67 characters, at most 66", self.problems(text))
        text = demo().replace('nudge = "The Alt layer."', f'nudge = "{"x" * 81}"', 1)
        self.assertIn("demo.toml: scenario rows: nudge is 81 characters, at most 80", self.problems(text))
        text = demo().replace('title = "Demo"', 'title = ""', 1)
        self.assertIn("demo.toml: lesson demo: title must be non-empty text", self.problems(text))
        text = demo().replace('direction = "down" }', 'direction = "down", checklist = "' + "y" * 40 + '" }', 1)
        self.assertIn("demo.toml: scenario rows: checklist is 40 characters, at most 32", self.problems(text))

    def test_steps_must_name_real_scenarios(self):
        text = demo().replace('"learn:cols"', '"learn:nowhere"')
        self.assertIn("demo.toml: lesson demo: step learn:nowhere names no scenario", self.problems(text))
        text = demo().replace('"learn:cols"', '"teach:cols"')
        self.assertTrue(any("must be learn:, recall: or build:" in p for p in self.problems(text)))

    def test_card_ids_and_solution_items(self):
        text = demo().replace('cards = ["herdr:split_horizontal"]', 'cards = ["split_horizontal"]', 1)
        self.assertTrue(any("'split_horizontal' is not a card id" in p for p in self.problems(text)))
        text = demo().replace('solution = ["herdr:split_horizontal"]', 'solution = ["yazi:next_file"]', 1)
        self.assertIn("demo.toml: scenario rows: yazi:next_file is not a herdr card", self.problems(text))
        text = demo().replace('solution = ["herdr:split_horizontal"]', 'solution = [{ press = "enter", type = "x" }]', 1)
        self.assertTrue(any("exactly one of card, press, type, wait" in p for p in self.problems(text)))
        text = demo().replace('solution = ["herdr:split_horizontal"]',
                              'solution = [{ card = "herdr:switch_tab", number = 12 }]', 1)
        self.assertTrue(any("number must be 1..9" in p for p in self.problems(text)))
        text = demo().replace('solution = ["herdr:split_horizontal"]', 'solution = []', 1)
        self.assertIn("demo.toml: scenario rows: solution must be a non-empty list", self.problems(text))

    def test_unknown_keys_fixtures_duplicates_and_tools(self):
        text = demo().replace('why = "Because."', 'why = "Because."\ncolour = "red"', 1)
        self.assertIn("demo.toml: scenario rows: unknown key colour", self.problems(text))
        text = demo().replace('fixture = "one_pane"', 'fixture = "pyramid"')
        self.assertIn("demo.toml: lesson demo: unknown herdr fixture 'pyramid'", self.problems(text))
        text = demo().replace('tool = "herdr"', 'tool = "emacs"')
        self.assertTrue(any("tool must be one of" in p for p in self.problems(text)))
        with self.assertRaises(CurriculumError) as caught:
            loads({"a.toml": demo(), "b.toml": demo(LESSON.replace('order = 1', 'order = 2'))})
        self.assertIn("b.toml: scenario rows: id already used in a.toml", caught.exception.problems)
        self.assertIn("b.toml: lesson demo: id already used in a.toml", caught.exception.problems)
        self.assertTrue(any(p.startswith("demo.toml: not valid TOML") for p in self.problems("[[lesson]\n")))


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.herdr = loads({"herdr.toml": HERDR})

    def test_every_herdr_scenario_is_active_with_the_export(self):
        resolved = resolve(self.herdr, DATA)
        self.assertTrue(all(state.active for state in resolved.scenarios.values()))
        self.assertEqual(len(resolved.visible), 6)
        self.assertEqual(resolved.bindings["herdr:split_horizontal"].taught, "alt+enter")
        self.assertEqual(resolved.bindings["herdr:split_horizontal"].label(), "Alt + Enter")

    def test_removed_binding_makes_its_scenario_a_notice_and_report_says_why(self):
        rows = [row for row in ROWS if row["value"] != "zoom"]
        resolved = resolve(self.herdr, {"groups": [{"id": "herdr", "rows": rows}]})
        self.assertFalse(resolved.active("zoom"))
        self.assertTrue(resolved.scenarios["zoom"].notice)
        self.assertEqual(resolved.scenarios["zoom"].missing, ("herdr:zoom",))
        state = resolved.lesson("panes-move")
        self.assertTrue(state.visible)
        self.assertNotIn("zoom", [step.scenario for step in state.steps])  # warm-ups read these
        self.assertIn("zoom", [step.scenario for step in state.plan])  # the lesson run plays it as a notice
        self.assertEqual(state.notices, 1)
        lines = report(resolved)
        self.assertIn("  no key    zoom: you have no key for herdr:zoom; the lesson shows how to add it", lines)
        self.assertIn("2. Move between panes (herdr): shown, 5 of 6 scenarios active", lines)
        self.assertIn("  active    focus-left", lines)

    def test_a_lesson_with_unbound_cards_stays_visible_with_notices_in_place(self):
        rows = [row for row in ROWS if row["value"] not in ("new workspace", "previous workspace")]
        resolved = resolve(self.herdr, {"groups": [{"id": "herdr", "rows": rows}]})
        state = resolved.lesson("workspaces")
        self.assertTrue(state.visible)
        self.assertEqual((state.active, state.total, state.notices), (1, 4, 3))
        lesson = self.herdr.lesson("workspaces")
        self.assertEqual(state.plan, lesson.steps)  # every step, in its place
        self.assertTrue(all(resolved.active(step.scenario) for step in state.steps))

    def test_a_lesson_whose_tool_is_missing_stays_hidden(self):
        everything = curriculum.load()
        resolved = resolve(everything, DATA, desktop=True)  # herdr keys only: no Yazi, no desktop rows
        for state in resolved.lessons:
            if state.lesson.tool != "herdr":
                self.assertFalse(state.visible, state.lesson.id)
                self.assertEqual((state.plan, state.notices), ((), 0))
        self.assertIn("no Yazi keys were found", "\n".join(report(resolved)))
        self.assertFalse(resolve(everything, DATA).lesson("desktop").visible)  # Hyprland not running

    def test_a_lesson_with_fewer_than_two_runnable_scenarios_is_hidden(self):
        resolved = resolve(self.herdr, DATA, lambda chord: True)  # no chord the terminal can send
        self.assertFalse(any(state.visible or state.notices for state in resolved.lessons))
        self.assertIn("hidden: fewer than two scenarios it can run", "\n".join(report(resolved)))

    def test_legacy_mode_teaches_the_prefix_twin_and_drops_a_chord_without_one(self):
        from router import ambiguous_in_legacy
        resolved = resolve(self.herdr, DATA, ambiguous_in_legacy)
        self.assertEqual(resolved.bindings["herdr:split_vertical"].chords, ("prefix+v",))
        self.assertEqual(resolved.bindings["herdr:close_pane"].taught, "prefix+x")
        rows = [row for row in ROWS if row["name"] != "prefix+x"]
        resolved = resolve(self.herdr, {"groups": [{"id": "herdr", "rows": rows}]}, ambiguous_in_legacy)
        self.assertFalse(resolved.active("close-pane"))
        self.assertIn("alt+esc", resolved.scenarios["close-pane"].reason)

    def test_legacy_mode_keeps_the_strata_chord_that_enter_stands_in_for(self):
        from router import ambiguous_in_legacy
        data = {"groups": [{"id": "strata", "rows": [{"name": "ctrl+shift+c", "value": "copy_path"}]}]}
        resolved = resolve(curriculum.load(), data, ambiguous_in_legacy)
        self.assertEqual(resolved.bindings["strata:copy_path"].taught, "ctrl+shift+c")

    def test_legacy_mode_keeps_the_desktop_lesson(self):
        # Hyprland reads Super chords itself, not through the terminal: a legacy terminal
        # must not hide lesson 8.
        from router import ambiguous_in_legacy
        rows = [{"name": "super+1…0", "value": "Switch to workspace 1…10"},
                {"name": "super+ctrl+tab", "value": "Former workspace"},
                {"name": "super+|", "value": "Toggle Quake terminal"}]
        data = {"groups": [{"id": "desktop", "rows": rows}]}
        resolved = resolve(curriculum.load(), data, ambiguous_in_legacy, desktop=True)
        self.assertTrue(resolved.lesson("desktop").visible)
        self.assertEqual(resolved.bindings["desktop:switch_workspace"].chords, ("super+1..9",))
        self.assertEqual(resolved.bindings["desktop:toggle_quake"].ambiguous, ())

    def test_the_chord_that_is_easy_on_the_layout_is_taught_whatever_the_order(self):
        # `;` is Shift+comma on a Norwegian keyboard.
        for rows in (ROWS, list(reversed(ROWS))):
            binding = resolve(self.herdr, {"groups": [{"id": "herdr", "rows": rows}]}).bindings["herdr:last_pane"]
            self.assertEqual(binding.labels(), ("Ctrl + Space → l", "Ctrl + Space → ;"))
            self.assertEqual(binding.awkward_on, "")

    def test_a_binding_with_only_a_hard_key_is_marked_on_that_layout_only(self):
        data = {"groups": [{"id": "herdr", "rows": [row for row in ROWS if row["name"] != "prefix+l"]}]}
        self.assertEqual(resolve(self.herdr, data).bindings["herdr:last_pane"].awkward_on, "Norwegian")
        self.assertEqual(resolve(self.herdr, data, layout="us").bindings["herdr:last_pane"].awkward_on, "")

    def test_letters_shift_chords_and_the_norwegian_plain_keys_are_not_hard(self):
        for chord in ("G", "g, g", "prefix+shift+c", ".", "super+|", "prefix+minus", "alt+1..9", "ctrl++"):
            self.assertFalse(curriculum.awkward_on(chord, "no"), chord)
        for chord in ("prefix+;", "prefix+[", "prefix+?", "prefix+backtick", "g, /"):
            self.assertTrue(curriculum.awkward_on(chord, "no"), chord)

    def test_unbindable_rows_do_not_count(self):
        # «Mer å lære» rows are herdr's defaults, not the user's config (M6).
        rows = [dict(row, group="Mer å lære (herdrs egne)") if row["value"] == "last pane" else row for row in ROWS]
        resolved = resolve(self.herdr, {"groups": [{"id": "herdr", "rows": rows}]})
        self.assertFalse(resolved.active("last-pane"))

    def test_chord_text(self):
        self.assertEqual(chord_text("ctrl+alt+shift+left"), "Ctrl + Alt + Shift + ←")
        self.assertEqual(chord_text("prefix+shift+c", "ctrl+space"), "Ctrl + Space → Shift + c")
        self.assertEqual(chord_text("alt+1..9", number=4), "Alt + 4")
        self.assertEqual(chord_text("prefix+;", "ctrl+space"), "Ctrl + Space → ;")
        self.assertEqual(chord_text("alt+1..9"), "Alt + 1…9")


class NeedsTest(unittest.TestCase):
    def test_tab_away_wraps_and_labels_differ(self):
        import checks
        from world import HerdrWorld, TabState, World, WorkspaceState
        tabs = tuple(TabState(f"t{i}", label, ()) for i, label in enumerate(("Start", "api", "web", "db", "5", "6"), 1))

        def world(position):
            return World(herdr=HerdrWorld((WorkspaceState("w", "Practice", tabs),), "w", f"t{position}"))

        need = [{"check": "tab_away", "position": 4, "distance": 2}]
        self.assertEqual([not checks.unmet(need, world(p)) for p in range(1, 7)],
                         [True, True, False, False, False, True])
        self.assertTrue(checks.unmet([{"check": "tab_label_not", "label": "db"}], world(4)))
        self.assertFalse(checks.unmet([{"check": "workspace_label_not", "label": "web"}], world(4)))


if __name__ == "__main__":
    unittest.main()
