"""The start check (doctor.start_check): one plain line per missing or mismatched piece."""

import json
import unittest
from pathlib import Path

import curriculum
import doctor
from strings import START

HERE = Path(__file__).resolve().parent
ROWS = {name: json.loads((HERE / "test_data" / f"rigpage_{name}.json").read_text("utf-8")) for name in ("herdr", "yazi", "strata")}
DESKTOP_ROWS = [
    {"name": "super+1…0", "value": "Switch to workspace 1…10"},
    {"name": "super+ctrl+tab", "value": "Former workspace"},
    {"name": "super+|", "value": "Toggle Quake terminal"},
]
LESSONS = curriculum.load()


def keys(drop=(), **extra):
    groups = [{"id": name, "rows": [row for row in rows if row["value"] not in drop]}
              for name, rows in (*ROWS.items(), ("desktop", DESKTOP_ROWS))]
    return {"groups": groups, **extra}


def everything(tool):
    return f"/usr/bin/{tool}"


def without(*missing):
    return lambda tool: None if tool in missing else everything(tool)


def check(data=None, *, desktop=True, env=None, which=everything):
    data = keys() if data is None else data
    resolved = curriculum.resolve(LESSONS, data, desktop=desktop)
    return doctor.start_check(data, resolved, {} if env is None else env, which=which)


class StartCheckTests(unittest.TestCase):
    def test_all_well_says_nothing(self):
        self.assertEqual(check(), [])
        self.assertEqual(check(env={"HERDR_ENV": "1"}), [])  # foot is there

    def test_missing_herdr_is_fatal_and_says_how_to_install(self):
        lines = check(which=without("herdr"))
        self.assertEqual(lines, [START["herdr"]])
        self.assertIn("sudo pacman -S herdr", lines[0])
        self.assertTrue(doctor.fatal(without("herdr")))
        self.assertFalse(doctor.fatal(everything))

    def test_missing_yazi_turns_off_the_yazi_lessons_by_title(self):
        self.assertEqual(check(which=without("yazi")), [
            "Yazi is missing, so lessons 6 “Yazi: get around”, 7 “Yazi: select and move”, 8 “Yazi: copy paths”, "
            "9 “Yazi: file tricks” and 10 “Yazi: back, forward, tabs” are off. Install it with: sudo pacman -S yazi"])

    def test_hyprland_not_running_turns_off_the_live_lesson(self):
        self.assertEqual(check(desktop=False), [
            "Hyprland is not running, so lesson 13 “Desktop (live)” is off. "
            "Start the trainer from a terminal on your Omarchy desktop to practise it."])

    def test_missing_foot_matters_only_inside_herdr(self):
        self.assertEqual(check(which=without("foot")), [])
        lines = check(env={"HERDR_ENV": "1"}, which=without("foot"))
        self.assertEqual(lines, [START["foot"]])
        self.assertIn("without herdr", lines[0])
        self.assertIn("sudo pacman -S foot", lines[0])

    def test_key_notes_come_with_their_source(self):
        note = "herdr config not found; using herdr's defaults"
        self.assertEqual(check(keys(source="scan", notes=[note])),
                         ["Your keys: read from your config files", f"Keys: {note}"])
        self.assertEqual(check(keys(source="scan")), [])  # a source alone is no issue
        self.assertEqual(doctor.key_source(keys(source="rigpage")), ["Your keys: from rigpage keys"])
        self.assertEqual(doctor.key_source(keys()), [])

    def test_a_lesson_with_missing_keys_says_its_steps_show_how_to_add_them(self):
        lines = check(keys(drop=("new tab", "rename tab", "switch tab", "zoom")))
        tail = " Those steps show how to add the key, and s skips them."
        self.assertEqual(lines, [
            "Lesson 2 “Move between panes”: you have no herdr key for zoom." + tail,
            "Lesson 3 “Tabs”: you have no herdr key for new tab, switch tab or rename tab." + tail,
            "Lesson 4 “Workspaces”: you have no herdr key for new tab." + tail,
        ])

    def test_lessons_already_off_for_a_missing_tool_are_not_said_twice(self):
        data = keys(drop=("zoom",))
        data["groups"] = [group for group in data["groups"] if group["id"] != "desktop"]
        self.assertEqual(check(data, desktop=False), [  # no desktop keys either, but Hyprland is the reason given
            "Hyprland is not running, so lesson 13 “Desktop (live)” is off. "
            "Start the trainer from a terminal on your Omarchy desktop to practise it.",
            "Lesson 2 “Move between panes”: you have no herdr key for zoom. "
            "Those steps show how to add the key, and s skips them."])
        self.assertEqual(check(data, which=without("herdr")), [START["herdr"]])

    def test_a_key_the_terminal_cannot_send_is_not_called_missing(self):
        data = keys()
        resolved = curriculum.resolve(LESSONS, data, lambda chord: True, desktop=True)  # every chord ambiguous
        lines = doctor.start_check(data, resolved, {}, which=everything)
        self.assertFalse([line for line in lines if "herdr key" in line or "Yazi key" in line])


if __name__ == "__main__":
    unittest.main()
