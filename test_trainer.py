"""The shell without a terminal: arguments, Home's model and cards, the flow, inside herdr, check.

The real CLI in an outer PTY is `test_runner.OuterTerminalTests`.
"""

import io
import json
import os
import re
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import curriculum
import lesson
import practice
import trainer
from practice import LessonRecord
from router import ambiguous_in_legacy
from screen import render_card
from strings import START

HERE = Path(__file__).resolve().parent
ROWS = {name: json.loads((HERE / "test_data" / f"rigpage_{name}.json").read_text("utf-8")) for name in ("herdr", "yazi")}
DATA = {"groups": [{"id": name, "rows": rows} for name, rows in ROWS.items()]}
LESSONS = curriculum.load()
SGR = re.compile(r"\x1b\[[0-9;]*m")
NOW = 1_800_000_000.0
OPEN = []


def memory():
    history = practice.History(":memory:")
    OPEN.append(history)
    return history


def tearDownModule():
    for history in OPEN:
        history.close()


def text(card, size=(100, 30)):
    rows, _ = render_card(card, *size)
    return "\n".join(SGR.sub("", rows[y]) for y in sorted(rows))


class UI:
    def __init__(self):
        self.cards, self.cache, self.size = [], {}, None

    def card(self, card):
        if not self.cards or self.cards[-1] is not card:
            self.cards.append(card)

    @property
    def headers(self):
        return [card.header for card in self.cards]


class Keys:
    """Scripted key names; running out ends the test's flow with KeyboardInterrupt."""

    def __init__(self, *names):
        self.names = list(names)
        self.inputs = None
        self.queue = []

    def next(self, delay=0.1):
        if not self.names:
            raise KeyboardInterrupt
        return self.names.pop(0)

    def clear(self):
        pass


def clean_run(plan):
    steps = tuple(lesson.StepResult(step.kind, step.scenario.id,
                                    tuple(plan.bindings[c].practice for c in step.scenario.cards),
                                    "met" if step.kind == "learn" else "clean", 1.0, 0, 0)
                  for step in plan.steps)
    return lesson.LessonResult(plan.lesson, plan.title, steps, True, 60.0, plan.warmup)


class ScriptedShell(trainer.Shell):
    """The flow with every lesson played cleanly at once; records what was played."""

    def __init__(self, *names, history=None, **kw):
        self.history_ = history or memory()
        super().__init__(UI(), Keys(*names), curriculum.resolve(LESSONS, DATA), DATA, self.history_,
                         clock=lambda: NOW, **kw)
        self.played = []

    def play(self, plan, tool, *, autoplay=None):
        self.played.append((tool, plan.lesson, plan.warmup, len(plan.steps)))
        result = clean_run(plan)
        for step in result.steps:
            self.history.record_step(step, NOW)
        if plan.lesson:
            self.history.finish_lesson(plan.lesson, result.steps, NOW, seconds=60)
        self.results.append(result)
        return result


class ArgumentTests(unittest.TestCase):
    def test_modes_and_options(self):
        self.assertIsNone(trainer.parse_args([]).mode)
        self.assertIsNone(trainer.parse_args(["sandbox"]).mode)  # the silent alias
        for mode in ("layouts", "check", "demo"):
            self.assertEqual(trainer.parse_args([mode]).mode, mode)
        self.assertTrue(trainer.parse_args(["demo", "--all"]).all)
        ns = trainer.parse_args(["--lesson", "tabs", "--keys", "k.json"])
        self.assertEqual((ns.lesson, ns.keys), ("tabs", "k.json"))

    def test_cut_and_contradictory_arguments_fail(self):
        for argv in (["play"], ["--track", "herdr"], ["--rounds", "3"], ["--all"], ["check", "--lesson", "tabs"]):
            with self.subTest(argv=argv), redirect_stdout(io.StringIO()), \
                    patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                trainer.parse_args(argv)

    def test_help_does_not_name_the_alias(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            trainer.parse_args(["--help"])
        self.assertNotIn("sandbox", out.getvalue())

    def test_help_and_argument_errors_come_from_strings(self):
        import strings

        cli = {key: value for key, value in strings.SHELL.items() if key.startswith("cli_")}
        self.assertTrue(cli)
        marked = {key: f"<{key}>" + value for key, value in cli.items()}
        with patch.dict(strings.SHELL, marked):
            out = io.StringIO()
            with redirect_stdout(out), self.assertRaises(SystemExit):
                trainer.parse_args(["--help"])
            for key in ("cli_description", "cli_mode", "cli_keys", "cli_lesson", "cli_all"):
                self.assertIn(f"<{key}>", " ".join(out.getvalue().split()))
            for argv, key in ((["play"], "cli_unknown_mode"), (["--all"], "cli_all_demo"),
                              (["check", "--lesson", "tabs"], "cli_lesson_mode")):
                err = io.StringIO()
                with self.subTest(argv=argv), patch("sys.stderr", err), self.assertRaises(SystemExit):
                    trainer.parse_args(argv)
                self.assertIn(f"<{key}>", err.getvalue())


class HomeModelTests(unittest.TestCase):
    def setUp(self):
        self.resolved = curriculum.resolve(LESSONS, DATA)
        self.ids = [state.lesson.id for state in self.resolved.visible]

    def test_empty_history_is_the_first_run_from_lesson_one(self):
        home = trainer.home_model(self.resolved, {}, practised=False)
        self.assertTrue(home.first_run)
        self.assertEqual(home.recommended, 0)
        self.assertEqual([row.lesson for row in home.rows], self.ids)
        self.assertTrue(all(row.stars is None for row in home.rows))
        shown = text(trainer.home_card(home, home.recommended, self.resolved, now=NOW))
        for line in ("Learn your shortcuts by using them", "› 1  Split the screen", "start here",
                     "Enter  Start lesson 1", "l  Layout workshop", "Press ? to see the keys"):
            self.assertIn(line, shown)
        self.assertNotIn("Today", shown)

    def test_partial_history_returns_to_the_next_lesson_with_stars_and_a_warm_up(self):
        records = {"split": LessonRecord(3, 50.0, NOW - 86400), "panes-move": LessonRecord(2, 80.0, NOW - 86400),
                   "tabs": LessonRecord(0, None, NOW - 86400)}  # left before the end: not finished
        home = trainer.home_model(self.resolved, records, practised=True, warmup=4)
        self.assertFalse(home.first_run)
        self.assertEqual(home.rows[home.recommended].lesson, "tabs")
        self.assertEqual([row.stars for row in home.rows[:3]], [3, 2, None])
        shown = text(trainer.home_card(home, home.recommended, self.resolved, now=NOW))
        for line in ("Welcome back", "last practised yesterday", "1  Warm-up        4 shortcuts due for review",
                     "2  Lesson 3       Tabs", "✓ 1  Split the screen", "★★★", "★★☆   one to go for three stars",
                     "› 3  Tabs", "○○○   next", "↑ ↓  Choose lesson"):
            self.assertIn(line, shown)
        moved = text(trainer.home_card(home, 4, self.resolved, now=NOW))
        self.assertIn("2  Lesson 5       Resize and build", moved)  # the plan follows the cursor

    def test_complete_history_recommends_the_first_lesson_below_three_stars(self):
        records = {lesson_id: LessonRecord(3, 60.0, NOW) for lesson_id in self.ids}
        records["workspaces"], records["shape"] = LessonRecord(2, 90.0, NOW), LessonRecord(1, 90.0, NOW)
        home = trainer.home_model(self.resolved, records, practised=True)
        self.assertEqual(home.rows[home.recommended].lesson, "workspaces")
        shown = text(trainer.home_card(home, home.recommended, self.resolved, now=NOW))
        self.assertIn("› 4  Workspaces              ★★☆   next", shown)
        self.assertIn("✓ 5  Resize and build        ★☆☆   two to go for three stars", shown)
        records["shape"] = LessonRecord(3, 90.0, NOW)
        records["workspaces"] = LessonRecord(3, 90.0, NOW)
        self.assertEqual(trainer.home_model(self.resolved, records, practised=True).recommended, 0)

    def test_no_visible_lesson_says_so(self):
        shell = trainer.Shell(UI(), Keys(), curriculum.resolve(LESSONS, {"groups": []}), {"groups": []}, memory())
        with self.assertRaisesRegex(ValueError, "keys-trainer check"):
            shell.run()

    def test_hidden_lessons_and_missing_tools(self):
        herdr_only = {"groups": [{"id": "herdr", "rows": ROWS["herdr"]}]}
        resolved = curriculum.resolve(LESSONS, herdr_only)
        home = trainer.home_model(resolved, {}, practised=False)
        self.assertEqual([row.tool for row in home.rows], ["herdr"] * 5)  # Yazi lessons are hidden
        home = trainer.home_model(self.resolved, {}, ready=lambda tool: tool == "herdr")
        self.assertFalse(home.rows[-1].ready)
        self.assertIn("needs Yazi", text(trainer.home_card(home, 0, self.resolved, now=NOW)))


class FlowTests(unittest.TestCase):
    def test_first_run_enter_enter_plays_lesson_one_then_q_ends_the_day(self):
        shell = ScriptedShell("enter", "enter", "q", "enter")
        shell.run()
        self.assertEqual(shell.ui.headers, ["KEYS TRAINER", "LESSON 1 OF 7", "LESSON 1 COMPLETE", "DONE FOR TODAY"])
        self.assertEqual(shell.played, [("herdr", "split", False, 7)])
        complete, end = text(shell.ui.cards[2]), text(shell.ui.cards[3])
        self.assertIn("Enter  Next: Move between panes (≈ 4 min)", complete)
        self.assertIn("Three stars", complete)
        self.assertIn("3 shortcuts · 3 without a hint · 1 lesson complete", end)
        self.assertIn("Alt + Enter  ·  Alt + Shift + Enter  ·  Alt + Esc", end)  # new today
        self.assertIn("1 of 7 lessons", end)
        self.assertIn("Warm-up (3), then lesson 2 · Move between panes", end)  # all three due tomorrow

    def test_complete_enter_opens_the_next_intro_and_t_tests_out(self):
        shell = ScriptedShell("enter", "enter", "enter", "t", "q", "enter")
        shell.run()
        self.assertEqual(shell.ui.headers[3], "LESSON 2 OF 7")
        learn = sum(step.kind == "learn" for step in lesson.plan_lesson(shell.resolved, "panes-move").steps)
        self.assertEqual(shell.played[1], ("herdr", "panes-move", False, 9 - learn))

    def test_returning_with_due_cards_warms_up_per_tool_then_opens_the_intro(self):
        history = memory()
        resolved = curriculum.resolve(LESSONS, DATA)
        for card in ("herdr:split_horizontal", "herdr:focus_pane_left", "yazi:bottom", "yazi:toggle_hidden"):
            history.record_outcome(resolved.bindings[card].practice, "shown", NOW - 3600)
        history.finish_lesson("split", (), NOW - 86400, seconds=50)
        shell = ScriptedShell("enter", "esc", "q", "enter", history=history)
        shell.run()
        home = text(shell.ui.cards[0])
        self.assertIn("1  Warm-up        4 shortcuts due for review", home)
        self.assertEqual([(tool, warm) for tool, _, warm, _ in shell.played], [("herdr", True), ("yazi", True)])
        self.assertEqual(shell.ui.headers[1], "LESSON 2 OF 7")
        self.assertNotIn("Warm-up", text(shell.ui.cards[2]))  # once a session
        self.assertEqual(shell.ui.headers[-1], "DONE FOR TODAY")

    def test_warm_up_takes_the_most_overdue_six(self):
        history = memory()
        resolved = curriculum.resolve(LESSONS, DATA)
        cards = [card for card in resolved.bindings if card.startswith(("herdr:", "yazi:"))][:10]
        for age, card in enumerate(cards):
            history.record_outcome(resolved.bindings[card].practice, "shown", NOW - 3600 * (age + 1))
        plans, count = trainer.warmup_plans(resolved, history, now=NOW)
        self.assertLessEqual(sum(len(plan.steps) for _, plan in plans), 6)
        self.assertLessEqual(count, 6)
        self.assertEqual([tool for tool, _ in plans], sorted({tool for tool, _ in plans}, key=("herdr", "yazi").index))

    def test_lesson_flag_starts_at_its_intro_and_esc_goes_home(self):
        shell = ScriptedShell("esc", "q")
        shell.run("tabs")
        self.assertEqual(shell.ui.headers, ["LESSON 3 OF 7", "KEYS TRAINER"])
        self.assertEqual(shell.played, [])

    def test_a_session_that_cannot_start_shows_why_and_goes_home(self):
        shell = ScriptedShell("enter", "enter", "enter", "q")
        shell.play = lambda plan, tool, autoplay=None: RuntimeError("Private Herdr server did not start.")
        shell.run()
        self.assertEqual(shell.ui.headers, ["KEYS TRAINER", "LESSON 1 OF 7", "THE SESSION DID NOT START", "KEYS TRAINER"])
        self.assertIn("Private Herdr server did not start.", text(shell.ui.cards[2]))

    def test_error_card_puts_a_plain_headline_over_the_muted_detail(self):
        from screen import GOLD, MUTED, Text
        from strings import SHELL

        shell = ScriptedShell("enter", "enter", "enter", "q")
        shell.play = lambda plan, tool, autoplay=None: RuntimeError("Private Herdr server did not start. exit 2")
        shell.run()
        card = shell.ui.cards[2]
        self.assertEqual(card.lines[0], Text(SHELL["error_session"], GOLD))
        self.assertIn(Text("Private Herdr server did not start. exit 2", MUTED), card.lines)
        self.assertNotIn("exit 2", SHELL["error_session"])
        small = text(card, (40, 12))
        self.assertIn(SHELL["error_session"].split()[0], small)
        timeout = trainer.error_card(trainer.subprocess.TimeoutExpired(["herdr"], 5))
        self.assertEqual(timeout.lines[0], Text(SHELL["error_timeout"], GOLD))
        # A reason already written for the user (a missing tool) is the headline itself.
        own = trainer.error_card(SHELL["note_tool"].format(tool="herdr"))
        self.assertEqual(own.lines, (Text(SHELL["note_tool"].format(tool="herdr"), GOLD),))

    def test_layout_workshop_error_names_the_workshop(self):
        from screen import GOLD, MUTED, Text
        from strings import SHELL

        shell = ScriptedShell("l", "enter", "q")
        with patch("trainer.layout_lab.run", side_effect=ValueError("needs a split shortcut")):
            shell.run()
        card = next(card for card in shell.ui.cards if card.lines and card.lines[0] == Text(SHELL["error_layouts"], GOLD))
        self.assertIn(Text("needs a split shortcut", MUTED), card.lines)

    def test_legacy_terminal_resolves_with_prefix_twins(self):
        class Mode:
            pending = b""
            ambiguous = staticmethod(ambiguous_in_legacy)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                pass

        seen = {}
        terminal = Mock()
        terminal.return_value.__enter__ = Mock(return_value=UI())
        terminal.return_value.__exit__ = Mock(return_value=False)
        with patch("trainer.Terminal", terminal), patch("trainer.KeyboardMode", Mode), \
                patch("trainer._interactive", return_value=True), patch("trainer.Keys"), \
                patch("trainer.History", memory), patch("trainer.doctor.start_check", return_value=[]), \
                patch.object(trainer.Shell, "run", lambda self, lesson_id=None: seen.update(r=self.resolved)):
            trainer.guided(trainer.parse_args([]), DATA, [], Mock(), env={})
        self.assertEqual(seen["r"].bindings["herdr:split_vertical"].taught, "prefix+v")
        self.assertEqual(seen["r"].bindings["herdr:split_horizontal"].taught, "alt+enter")


class InsideHerdrTests(unittest.TestCase):
    ENV = {"HERDR_ENV": "1", "HERDR_SOCKET_PATH": "/run/user.sock", "WAYLAND_DISPLAY": "wayland-1", "PATH": "/usr/bin"}

    def test_home_offers_a_window_and_the_spawner_gets_the_launcher_and_the_args(self):
        shell, spawn = ScriptedShell("enter"), Mock()
        with patch("trainer.shutil.which", return_value="/usr/bin/foot"):
            shell.inside_herdr(["--lesson", "tabs", "--keys", "k.json"], self.ENV, spawn)
        shown = text(shell.ui.cards[0])
        self.assertIn("You are inside herdr", shown)
        self.assertIn("Enter  Open in its own window", shown)
        argv, env = spawn.call_args.args
        self.assertEqual(argv, ["foot", "--app-id", "keys-trainer", "--", str(HERE / "keys-trainer"),
                                "--lesson", "tabs", "--keys", "k.json"])
        self.assertEqual(env, {"WAYLAND_DISPLAY": "wayland-1", "PATH": "/usr/bin"})
        self.assertEqual(shell.played, [])

    def test_without_foot_or_wayland_it_explains_and_starts_nothing(self):
        for which, env in ((None, self.ENV), ("/usr/bin/foot", {"HERDR_ENV": "1"})):
            shell, spawn = ScriptedShell("enter", "l", "q"), Mock()
            with patch("trainer.shutil.which", return_value=which):
                shell.inside_herdr([], env, spawn)
            shown = text(shell.ui.cards[0])
            self.assertIn("Open a terminal without herdr (Super + Enter)", shown)
            self.assertNotIn("Open in its own window", shown)
            spawn.assert_not_called()
            self.assertEqual(shell.played, [])

    def test_real_spawner_detaches_with_quiet_stdio(self):
        with patch("trainer.subprocess.Popen") as popen:
            trainer.spawn_window(["foot"], {"A": "1"})
        kw = popen.call_args.kwargs
        self.assertTrue(kw["start_new_session"])
        self.assertEqual({kw[name] for name in ("stdin", "stdout", "stderr")}, {trainer.subprocess.DEVNULL})


class CheckTests(unittest.TestCase):
    def test_check_reports_terminal_tools_chords_and_lessons(self):
        lines = trainer.check_lines(LESSONS, DATA, (1, True, b""), env={"TERM": "foot"})
        joined = "\n".join(lines)
        self.assertIn("Keyboard protocol: kitty (flags 1)", joined)
        self.assertIn("herdr:split_vertical: Alt + Shift + Enter  ·  Ctrl + Space → v", joined)
        self.assertIn("1. Split the screen (herdr): shown", joined)
        legacy = "\n".join(trainer.check_lines(LESSONS, DATA, (None, True, b""), env={}))
        self.assertIn("herdr:split_vertical: Ctrl + Space → v · needs the kitty protocol: Alt + Shift + Enter", legacy)

    def test_check_from_the_command_line_without_a_terminal(self):
        keys = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()), "keys.json")
        keys.write_text(json.dumps(DATA))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(trainer.main(["check", "--keys", str(keys)]), 0)
        self.assertIn("no answer from the terminal", out.getvalue())
        self.assertIn("Lessons", out.getvalue())

    def test_check_names_found_tools_and_says_what_a_missing_one_means(self):
        def which(tool):
            return None if tool in ("yazi", "foot") else f"/usr/bin/{tool}"

        data = {**DATA, "source": "scan"}
        with patch("trainer.shutil.which", which):
            lines = trainer.check_lines(LESSONS, data, (1, True, b""), env={})
        tools = lines[lines.index("Tools") + 1:lines.index("Shortcuts (the first one is taught)") - 1]
        self.assertEqual(tools[0], "  herdr: /usr/bin/herdr")
        self.assertIn("  " + START["foot_optional"], tools)
        self.assertIn("  Your keys: read from your config files", tools)
        self.assertTrue(any(line.startswith("  Yazi is missing, so lessons 6") for line in tools))
        self.assertTrue(any(line.startswith("  Hyprland is not running") for line in tools))
        with patch("trainer.shutil.which", which):
            inside = trainer.check_lines(LESSONS, data, (1, True, b""), env={"HERDR_ENV": "1"})
        self.assertIn("  " + START["foot"], inside)
        self.assertNotIn("  " + START["foot_optional"], inside)

    def test_guided_path_needs_a_terminal(self):
        err = io.StringIO()
        with patch("trainer.read_keys", return_value=DATA), patch("sys.stderr", err), \
                patch("trainer._interactive", return_value=False):
            self.assertEqual(trainer.main([]), 1)
        self.assertIn("interactive terminal", err.getvalue())


class StartCheckTests(unittest.TestCase):
    def guided(self, which, read=None):
        terminal, out = MagicMock(), io.StringIO()
        with patch("trainer._interactive", return_value=True), patch("trainer.shutil.which", which), \
                patch("trainer.HyprObserver.running", return_value=False), patch("trainer.Terminal", terminal), \
                patch("trainer.History", memory), patch("trainer.input", read, create=True), redirect_stdout(out):
            code = trainer.guided(trainer.parse_args([]), DATA, [], Mock(), env={})
        return code, out.getvalue(), terminal

    def test_without_herdr_it_says_why_and_exits_1_before_any_screen(self):
        read = Mock()
        code, out, terminal = self.guided(lambda tool: None, read)
        self.assertEqual(code, 1)
        self.assertIn("herdr is missing", out)
        self.assertIn("sudo pacman -S herdr", out)
        read.assert_not_called()
        terminal.assert_not_called()

    def test_an_optional_issue_waits_for_enter_and_q_quits(self):
        code, out, terminal = self.guided(lambda tool: f"/usr/bin/{tool}", Mock(return_value="q"))
        self.assertEqual(code, 0)
        self.assertIn("Hyprland is not running", out)
        terminal.assert_not_called()

    def test_enter_goes_on_and_nothing_to_say_goes_straight_in(self):
        for issues, read in ((["Yazi is missing."], Mock(return_value="")), ([], Mock())):
            with patch("trainer.doctor.start_check", return_value=issues), \
                    patch.object(trainer.Shell, "run", lambda self, lesson_id=None: None), \
                    patch("trainer.KeyboardMode"), patch("trainer.Keys"):
                code, out, terminal = self.guided(lambda tool: f"/usr/bin/{tool}", read)
            self.assertEqual(code, 0)
            terminal.assert_called_once()
            self.assertEqual(read.call_count, len(issues))
            self.assertEqual(bool(out), bool(issues))

    def test_lines_are_wrapped_under_a_header_and_end_of_input_quits(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertFalse(trainer.start_check(["word " * 40], read=Mock(side_effect=EOFError), width=60))
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], START["header"])
        self.assertTrue(lines[1].startswith("- word") and lines[2].startswith("  word"))
        self.assertTrue(all(len(line) <= 60 for line in lines))

    def test_launcher_refuses_an_old_python_in_one_line(self):
        import subprocess
        import sys
        code = ("import sys; sys.version_info = (3, 10, 4); sys.version = '3.10.4 (main)'; "
                "exec(compile(open('keys-trainer').read(), 'keys-trainer', 'exec'))")
        run = subprocess.run([sys.executable, "-c", code], cwd=HERE, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 1)
        self.assertEqual(run.stderr, START["old_python"].format(version="3.10.4") + "\n")


class CardTests(unittest.TestCase):
    def setUp(self):
        self.resolved = curriculum.resolve(LESSONS, DATA)

    def test_intro_names_the_pattern_the_keys_and_the_build(self):
        shown = text(trainer.intro_card(self.resolved, "panes-move", 2, 7))
        for line in ("LESSON 2 OF 7", "≈ 4 min", "Pattern       Ctrl + Alt + arrow key", "You learn",
                     "Ctrl + Alt + ←", "Then you build", "│   ●   ├──────┤",
                     "Enter  Start      t  I know this: test me      Esc  Back"):
            self.assertIn(line, shown)

    def test_complete_card_says_what_the_next_star_takes(self):
        plan = lesson.plan_lesson(self.resolved, "split")
        run = clean_run(plan)
        steps = list(run.steps)
        steps[2] = lesson.StepResult("recall", "split-rows", steps[2].cards, "nudged", None, 1, 2)
        shown = text(trainer.complete_card(self.resolved, replace(run, steps=tuple(steps)), 1))
        self.assertIn("★★☆", shown)
        self.assertIn("One to go for three stars: “Split the pane into two rows” without a hint.", " ".join(shown.split()))
        self.assertIn("with a nudge", shown)
        self.assertIn("Enter  Done for today", shown)  # no next lesson given

    def test_complete_card_lists_a_notice_step_and_keeps_its_stars(self):
        rows = [row for row in ROWS["herdr"] if row["value"] != "split vertical"]
        resolved = curriculum.resolve(LESSONS, {"groups": [{"id": "herdr", "rows": rows}]})
        plan = lesson.plan_lesson(resolved, "split")
        steps = tuple(lesson.StepResult(step.kind, step.scenario.id, (), "skipped", None, 0, 0, True, False, True)
                      if step.missing else
                      lesson.StepResult(step.kind, step.scenario.id, (), "met" if step.kind == "learn" else "clean",
                                        1.0, 0, 0) for step in plan.steps)
        run = lesson.LessonResult("split", plan.title, steps, True, 30.0)
        shown = " ".join(text(trainer.complete_card(resolved, run, 1)).split())
        self.assertIn("No key: split vertical", shown)
        self.assertIn("No key: split vertical skipped", shown)
        self.assertIn("★★★", shown)

    def test_cards_fit_small_terminals(self):
        home = trainer.home_model(self.resolved, {}, practised=False)
        for size in ((80, 20), (60, 15), (40, 12)):
            rows, margin = render_card(trainer.home_card(home, 0, self.resolved, now=NOW), *size)
            self.assertTrue(rows)
            self.assertLessEqual(max(rows), size[1])
            self.assertIn("q  Quit", SGR.sub("", "".join(rows.values())))

    def test_home_scrolls_the_path_to_keep_the_selection_on_screen(self):
        home = trainer.home_model(self.resolved, {}, practised=False)
        last = len(home.rows) - 1
        for size in ((60, 15), (40, 12)):
            for cursor in range(len(home.rows)):
                with self.subTest(size=size, cursor=cursor):
                    row = home.rows[cursor]
                    shown = text(trainer.home_card(home, cursor, self.resolved, now=NOW), size)
                    self.assertIn(f"› {row.number}  {row.title}"[:size[0] - 6], shown)
                    self.assertIn("q  Quit", shown)
        # The text above the path stays; only the path scrolls.
        shown = text(trainer.home_card(home, last, self.resolved, now=NOW), (60, 15))
        self.assertIn("Learn your shortcuts by using them", shown)
        self.assertNotIn("1  Split the screen", shown)
        # With room for the whole path nothing scrolls, as before.
        shown = text(trainer.home_card(home, last, self.resolved, now=NOW), (80, 20))
        for line in ("The path", "  1  Split the screen        ○○○   start here", "› 7  Yazi: select and move   ○○○",
                     "Stuck? Press ? to see the keys.", "Enter  Start lesson 7", "q  Quit"):
            self.assertIn(line, shown)


class TerminalTests(unittest.TestCase):
    def test_outer_pty_preserves_private_chord_bytes_and_restores_terminal(self):
        import pty
        import termios
        from screen import Terminal

        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        previous = termios.tcgetattr(slave)
        stream = Mock()
        stream.fileno.return_value = slave
        with patch("screen.sys.stdin", stream), redirect_stdout(io.StringIO()):
            with Terminal() as ui:
                for chord in (b"\r", b"\x1b\r", b"\x13", b"\x11", b"\x1c", b"\x1a", b"\x03"):
                    os.write(master, chord)
                    self.assertEqual(ui.read(.2, forward_ctrl_c=True), chord)
                os.write(master, b"\x03")
                with self.assertRaises(KeyboardInterrupt):
                    ui.read(.2)
        self.assertEqual(termios.tcgetattr(slave), previous)

    def test_card_keys_read_the_same_in_kitty_and_legacy(self):
        ui = Mock()
        ui.read.side_effect = [b"\x1b[27u", b"\x1b[A", b"q", b"\r", b"\x1b[99;5u"]
        keys = trainer.Keys(ui)
        self.assertEqual([keys.next() for _ in range(4)], ["esc", "up", "q", "enter"])
        with self.assertRaises(KeyboardInterrupt):
            keys.next()

    def test_native_view_restores_display_after_failed_backend_resize(self):
        from native_view import NativeView

        ui, source = Mock(), Mock()
        source.end_native.side_effect = OSError("client closed")
        view = NativeView(ui, source)
        view.active = True
        with redirect_stdout(io.StringIO()) as out, self.assertRaises(OSError):
            view.close()
        self.assertIn("\x1b[r", out.getvalue())
        self.assertIsNone(ui.size)
        self.assertFalse(view.active)


if __name__ == "__main__":
    unittest.main()
