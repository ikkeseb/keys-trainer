"""Unit tests for shell checks (checks_shell.py) and curriculum/shell.toml."""

import json
import os
import shutil
import time
import tomllib
import unittest
from pathlib import Path

import checks
from shell_backend import ACTIONS, FIXTURES, ShellSession, shell_bindings
from world import ShellWorld, World


HERE = Path(__file__).parent
DATA = HERE / "test_data"
SHELL_ROWS = json.loads((DATA / "rigpage_shell.json").read_text(encoding="utf-8"))
HERDR_ROWS = json.loads((DATA / "rigpage_herdr.json").read_text(encoding="utf-8"))
CURRICULUM = tomllib.loads((HERE / "curriculum" / "shell.toml").read_text(encoding="utf-8"))
LESSONS = {lesson["id"]: lesson for lesson in CURRICULUM["lesson"]}
SCENARIOS = {scenario["id"]: scenario for scenario in CURRICULUM["scenario"]}


def _world(**kw) -> World:
    defaults = dict(cwd="Practice", last_cmd="", last_status=0, widgets=(), zoxide=(), opened=(), tools=(), seq=1)
    defaults.update(kw)
    return World(shell=ShellWorld(**defaults))


class ShellCheckUnitTests(unittest.TestCase):
    def test_shell_cwd_is_check_and_need(self):
        start = _world(cwd="Practice")
        docs = _world(cwd="Practice/Docs", seq=2)
        spec = [{"check": "shell_cwd_is", "folder": "Docs"}]
        checks.validate(spec, [{"check": "shell_cwd_is", "folder": "Practice"}])
        self.assertEqual(checks.unmet([{"check": "shell_cwd_is", "folder": "Practice"}], start), [])
        self.assertTrue(checks.unmet([{"check": "shell_cwd_is", "folder": "Docs"}], start))
        self.assertTrue(checks.evaluate(spec, start, [docs]).done)
        self.assertFalse(checks.evaluate(spec, start, [start]).done)

    def test_widget_used_requires_new_matching_widget_since_ref(self):
        start = _world()
        spec = [{"check": "widget_used", "widget": "history", "contains": "ship-42"}]
        cd_any = [{"check": "widget_used", "widget": "cd"}]
        cd_docs = [{"check": "widget_used", "widget": "cd", "contains": "Docs"}]
        checks.validate(spec + cd_any + cd_docs)
        good = _world(widgets=(("history", "echo ship-42"),), seq=2)
        self.assertTrue(checks.evaluate(spec, start, [good]).done)
        # Wrong widget or missing substring must fail.
        wrong_widget = _world(widgets=(("file", "echo ship-42"),), seq=2)
        self.assertFalse(checks.evaluate(spec, start, [wrong_widget]).done)
        wrong_text = _world(widgets=(("history", "git status"),), seq=2)
        self.assertFalse(checks.evaluate(spec, start, [wrong_text]).done)
        # Cancelled widget (empty/whitespace text) must fail even without `contains`.
        self.assertFalse(checks.evaluate(cd_any, start, [_world(widgets=(("cd", ""),), seq=2)]).done)
        self.assertFalse(checks.evaluate(cd_any, start, [_world(widgets=(("cd", "   \n"),), seq=2)]).done)
        self.assertTrue(checks.evaluate(cd_docs, start, [_world(widgets=(("cd", "cd -- 'Docs'"),), seq=2)]).done)
        self.assertFalse(checks.evaluate(cd_docs, start, [_world(widgets=(("cd", "cd -- 'Archive'"),), seq=2)]).done)
        # Already present at ref without a new trigger must fail.
        self.assertFalse(checks.evaluate(spec, good, [good]).done)

    def test_command_ran_requires_new_seq_substring_and_zero_status(self):
        start = _world(seq=1)
        spec = [{"check": "command_ran", "contains": "ship-42"}]
        checks.validate(spec)
        ok = _world(last_cmd="echo ship-42", last_status=0, seq=2)
        self.assertTrue(checks.evaluate(spec, start, [ok]).done)
        # Substring inside a longer token (ship-420) must fail word-boundary matching.
        superstring = _world(last_cmd="echo ship-420", last_status=0, seq=2)
        self.assertFalse(checks.evaluate(spec, start, [superstring]).done)
        failed = _world(last_cmd="echo ship-42", last_status=1, seq=2)
        self.assertFalse(checks.evaluate(spec, start, [failed]).done)
        stale = _world(last_cmd="echo ship-42", last_status=0, seq=1)
        self.assertFalse(checks.evaluate(spec, start, [stale]).done)

    def test_zoxide_jumped_requires_zoxide_entry_and_target_cwd(self):
        start = _world(cwd="Practice/Docs")
        spec = [{"check": "zoxide_jumped", "folder": "Projects"}]
        checks.validate(spec)
        ok = _world(cwd="Practice/Projects", zoxide=(("z", "Practice/Projects"),), seq=2)
        self.assertTrue(checks.evaluate(spec, start, [ok]).done)
        # Plain cd without zoxide must fail.
        plain_cd = _world(cwd="Practice/Projects", zoxide=(), seq=2)
        self.assertFalse(checks.evaluate(spec, start, [plain_cd]).done)
        # Zoxide jump to a different folder must fail.
        wrong_dir = _world(cwd="Practice/Archive", zoxide=(("z", "Practice/Archive"),), seq=2)
        self.assertFalse(checks.evaluate(spec, start, [wrong_dir]).done)

    def test_tool_matched_requires_new_tool_event_containing_file(self):
        start = _world()
        fd_spec = [{"check": "tool_matched", "program": "fd", "file": "Practice/Projects/deploy.md", "query": "deploy"}]
        rg_spec = [{"check": "tool_matched", "program": "rg", "file": "Practice/Docs/guide.txt", "query": "lantern"}]
        checks.validate(fd_spec + rg_spec)
        fd_ok = _world(tools=(("fd", ("deploy",), ("Practice/Projects/deploy.md",)),), seq=2)
        self.assertTrue(checks.evaluate(fd_spec, start, [fd_ok]).done)
        self.assertFalse(checks.evaluate(rg_spec, start, [fd_ok]).done)
        rg_ok = _world(tools=(("rg", ("-i", "lantern"), ("Practice/Docs/guide.txt",)),), seq=2)
        self.assertTrue(checks.evaluate(rg_spec, start, [rg_ok]).done)
        # Matching the same file via a different query (e.g. `rg onboarding`) must fail.
        wrong_query = _world(tools=(("rg", ("onboarding",), ("Practice/Docs/guide.txt",)),), seq=2)
        self.assertFalse(checks.evaluate(rg_spec, start, [wrong_query]).done)
        # Flag-only args must not satisfy query.
        flag_only = _world(tools=(("rg", ("--lantern",), ("Practice/Docs/guide.txt",)),), seq=2)
        self.assertFalse(checks.evaluate(rg_spec, start, [flag_only]).done)
        # Empty matches or already recorded at ref must fail.
        self.assertFalse(checks.evaluate(fd_spec, start, [_world(tools=(("fd", ("deploy",), ()),), seq=2)]).done)
        self.assertFalse(checks.evaluate(fd_spec, fd_ok, [fd_ok]).done)

    def test_shell_opened_requires_new_stub_entry_matching_opener_and_file(self):
        start = _world()
        default_spec = [{"check": "shell_opened", "opener": "default", "file": "Practice/notes.txt"}]
        editor_spec = [{"check": "shell_opened", "opener": "editor", "file": "Practice/notes.txt"}]
        checks.validate(default_spec + editor_spec, [{"check": "shell_clean"}])
        self.assertEqual(checks.unmet([{"check": "shell_clean"}], start), [])
        opened_default = _world(opened=(("default", "Practice/notes.txt"),), seq=2)
        self.assertTrue(checks.evaluate(default_spec, start, [opened_default]).done)
        self.assertFalse(checks.evaluate(editor_spec, start, [opened_default]).done)
        self.assertTrue(checks.unmet([{"check": "shell_clean"}], opened_default))
        opened_editor = _world(opened=(("editor", "Practice/notes.txt"),), seq=2)
        self.assertTrue(checks.evaluate(editor_spec, start, [opened_editor]).done)
        self.assertFalse(checks.evaluate(default_spec, start, [opened_editor]).done)
        self.assertFalse(checks.evaluate(editor_spec, opened_editor, [opened_editor]).done)

    def test_every_shell_check_and_need_is_used_by_a_scenario(self):
        used = {item["check"] for scenario in SCENARIOS.values() for item in scenario["checks"]}
        self.assertEqual(used, {name for name, entry in checks.CHECKS.items() if entry.tool == "shell"})
        used_needs = {item["check"] for scenario in SCENARIOS.values() for item in scenario.get("needs", ())}
        self.assertEqual(used_needs, {name for name, entry in checks.NEEDS.items() if entry.tool == "shell"})

    def test_curriculum_schema_and_export_bindings(self):
        bindings = shell_bindings(SHELL_ROWS)
        for scenario_id, scenario in SCENARIOS.items():
            with self.subTest(scenario_id):
                plain = [{k: v for k, v in c.items() if k != "checklist"} for c in scenario["checks"]]
                checks.validate(plain, scenario.get("needs", ()))
                for card in scenario["cards"]:
                    action = card.removeprefix("shell:")
                    self.assertIn(action, ACTIONS)
                    self.assertTrue(bindings.get(action), card)


LIVE = os.environ.get("KEYS_TRAINER_TEST_HERDR") == "1" and shutil.which("herdr") and shutil.which("bash")


def _run_item(session, item):
    if isinstance(item, str):
        session.play(item)
    elif "press" in item:
        session.press(item["press"])
    elif "type" in item:
        session.type_text(item["type"])
    else:
        time.sleep(item["wait"])


def _settle(session, trail, timeout=3.0):
    end = time.monotonic() + timeout
    changed = time.monotonic()
    while time.monotonic() < end and time.monotonic() - changed < 0.4:
        world = session.world()
        if world.shell != trail[-1].shell:
            trail.append(world)
            changed = time.monotonic()
        time.sleep(0.02)


@unittest.skipUnless(LIVE, "set KEYS_TRAINER_TEST_HERDR=1 to test shell lessons live")
class LiveShellLessonTests(unittest.TestCase):
    def test_each_shell_scenario_solution_passes_and_decoy_fails(self):
        for lesson in LESSONS.values():
            cards = sorted({c for step in lesson["steps"] for c in SCENARIOS[step.split(":", 1)[1]]["cards"]})
            with ShellSession(SHELL_ROWS, HERDR_ROWS) as session:
                session.start(cards, lesson["fixture"])
                for sid in dict.fromkeys(step.split(":", 1)[1] for step in lesson["steps"]):
                    scenario = SCENARIOS[sid]
                    fixture = scenario.get("fixture") or lesson["fixture"]
                    plain = [{k: v for k, v in c.items() if k != "checklist"} for c in scenario["checks"]]
                    decoy = scenario.get("decoy")
                    decoy_items = decoy if isinstance(decoy, list) else ([decoy] if decoy else [])
                    for kind, items in (("solution", scenario["solution"]), ("decoy", decoy_items)):
                        if not items:
                            continue
                        with self.subTest(f"{sid}:{kind}"):
                            start = session.apply_fixture(fixture)
                            self.assertEqual(checks.unmet(scenario.get("needs", ()), start), [])
                            trail = [start]
                            for item in items:
                                _run_item(session, item)
                                _settle(session, trail)
                            done = checks.evaluate(plain, start, trail[1:]).done
                            self.assertEqual(done, kind == "solution", trail[-1].shell)


if __name__ == "__main__":
    unittest.main()
