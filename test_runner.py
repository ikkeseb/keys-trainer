"""The lesson loop (`runner`) with the engine's fake herdr, and the real CLI end to end.

The end-to-end tests run `keys-trainer` in an outer PTY with its own state directory and a
private herdr only (`KEYS_TRAINER_TEST_HERDR=1`): first-run Home, Enter, Enter, the user's chords
for every step of lesson 1, the lesson-complete card, `q`, session end, and the terminal
put back. A second run checks the hint key and a miss.
"""

import fcntl
import json
import os
import pty
import select
import shutil
import struct
import subprocess
import tempfile
import termios
import time
import unittest
from pathlib import Path

import curriculum
import lesson
import practice
import runner
from herdr_backend import kitty_keys
from lesson import Engine, plan_lesson
from test_lesson import CURRICULUM, DATA, FakeHerdr

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE / "keys-trainer"
PREFIX = next(row["name"] for row in DATA["groups"][0]["rows"] if row["value"] == "prefix")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Player:
    """A fake terminal: every read advances the clock and may answer the current step."""

    def __init__(self, clock, script):
        self.clock, self.script = clock, script
        self.reads = 0

    def read(self, delay, *, forward_ctrl_c=False):
        assert forward_ctrl_c
        self.clock.now += delay
        self.reads += 1
        if self.reads > 4000:
            raise AssertionError("the lesson never finished")
        return self.script() or b""


class View:
    def __init__(self):
        self.bars, self.closed = [], False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def show_bar(self, bar):
        self.bars.append(bar)


class Mode:
    pending = b""


def chord_bytes(resolved, move):
    binding = resolved.bindings[move.card]
    return kitty_keys(binding.taught.replace("1..9", str(move.number or 1)), PREFIX)


class RunLessonTests(unittest.TestCase):
    def setUp(self):
        self.resolved = curriculum.resolve(CURRICULUM, DATA)
        self.history = practice.History(":memory:")
        self.addCleanup(self.history.close)
        self.clock = Clock()
        self.view = View()

    def engine(self, lesson_id="split", **kw):
        plan = plan_lesson(self.resolved, lesson_id, **kw)
        self.router = runner.router_for(plan)
        self.session = FakeHerdr(self.resolved)
        return Engine(plan, self.session, hint_key=self.router.hint_key, pause_key=self.router.pause_key,
                      history=self.history, sleep=lambda _: None)

    def run_with(self, engine, script, **kw):
        self.sleeps = []
        kw.setdefault("sleep", self.sleep)
        return runner.run_lesson(Player(self.clock, script), self.session, engine, self.router, Mode(),
                                 view=self.view, clock=self.clock, **kw)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock.now += seconds

    def his_chords(self, engine):
        """Answer each step once with the user's taught chords, one move per read, 0.3 s apart."""
        sent, last = set(), [0.0]

        def script():
            if engine.state != lesson.RUNNING or self.clock.now - last[0] < 0.3:
                return b""
            for index, move in enumerate(engine.step.scenario.solution):
                key = (engine.index, index)
                if key not in sent:
                    sent.add(key)
                    last[0] = self.clock.now
                    return chord_bytes(self.resolved, move)
            return b""
        return script

    def test_the_chords_carry_lesson_one_to_the_end_and_into_history(self):
        engine = self.engine()
        result = self.run_with(engine, self.his_chords(engine))
        self.assertTrue(result.complete)
        self.assertEqual([step.outcome for step in result.steps],
                         ["met", "met", "clean", "met", "clean", "clean", "clean"])
        self.assertEqual(self.history.lessons()["split"].stars, 3)
        card = self.resolved.bindings["herdr:split_horizontal"].practice
        self.assertIsNotNone(self.history.best_seconds(card))
        self.assertTrue(self.view.closed)
        self.assertIn("LEARN", {bar.tag for bar in self.view.bars})
        self.assertIn("✓", {bar.tag for bar in self.view.bars})

    def test_a_step_with_a_hard_key_says_so_and_one_key_skips_it(self):
        data = {"groups": [{"id": "herdr", "rows": [row for row in DATA["groups"][0]["rows"]
                                                     if row["name"] != "prefix+l"]}]}
        self.resolved = curriculum.resolve(CURRICULUM, data)
        engine = self.engine("panes-move")
        engine.queue = [step for step in engine.queue if step.scenario.id == "last-pane" and step.kind == "learn"]
        engine.results = [None]

        result = self.run_with(engine, lambda: b"s" if engine.state == lesson.RUNNING else b"")
        self.assertEqual([step.outcome for step in result.steps], ["skipped"])
        bar = self.view.bars[0]
        self.assertEqual((bar.line, bar.skip_key), ("Awkward on a Norwegian keyboard · s skips it", "s"))

    def test_a_notice_step_draws_its_card_and_s_skips_it_without_a_star_lost(self):
        rows = [row for row in DATA["groups"][0]["rows"] if row["value"] != "zoom"]
        self.resolved = curriculum.resolve(CURRICULUM, {"groups": [{"id": "herdr", "rows": rows}]})
        engine = self.engine("panes-move")
        engine.queue = [step for step in engine.queue if step.missing]
        engine.results = [None] * len(engine.queue)
        notices = []
        self.view.show_notice = lambda card, bar: notices.append((card, bar))

        result = self.run_with(engine, lambda: b"s" if engine.state == lesson.RUNNING else b"")
        self.assertEqual([(step.outcome, step.notice) for step in result.steps], [("skipped", True)] * 2)
        card, bar = notices[0]
        self.assertEqual((card.header, bar.tag, bar.skip_key), ("NO KEY IN YOUR SETUP", "NO KEY", "s"))
        self.assertNotIn(b"s", self.session.fed)
        self.assertEqual(self.history.lessons()["panes-move"].stars, 3)  # the gap is the setup's, not a miss

    def test_the_demo_skips_a_notice_step_by_itself(self):
        rows = [row for row in DATA["groups"][0]["rows"] if row["value"] != "zoom"]
        self.resolved = curriculum.resolve(CURRICULUM, {"groups": [{"id": "herdr", "rows": rows}]})
        engine = self.engine("panes-move")
        engine.queue = [step for step in engine.queue if step.missing]
        engine.results = [None] * len(engine.queue)
        result = self.run_with(engine, lambda: b"", autoplay=0.5)
        self.assertEqual([step.outcome for step in result.steps], ["skipped", "skipped"])

    def test_the_skip_key_goes_to_the_session_on_an_ordinary_step(self):
        engine = self.engine()
        presses = iter([b"s"])

        def script():
            key = next(presses, None)
            if key is None:
                engine.quit()
            return key or b""

        self.run_with(engine, script)
        self.assertEqual(self.view.bars[0].skip_key, "")
        self.assertIn(b"s", self.session.fed)

    def test_hint_once_shows_the_keys_twice_plays_them_and_the_step_completes(self):
        engine = self.engine(test_out=True)
        presses = iter([b"?", b"", b"?"])
        done = []

        def script():
            if engine.state == lesson.RUNNING and engine.index == 0:
                return next(presses, b"")
            if engine.index > 0 and not done:
                done.append(True)
                engine.quit()
            return b""

        result = self.run_with(engine, script)
        lines = [bar.line for bar in self.view.bars]
        self.assertTrue(any(line.startswith("Keys · Alt + Enter") for line in lines), lines)
        self.assertTrue(any(line.startswith("Watch:") for line in lines))
        self.assertEqual(result.steps[0].outcome, "shown")
        self.assertIn(("herdr:split_horizontal", None), self.session.played)
        self.assertFalse(result.complete)
        self.assertEqual(self.history.lessons()["split"].stars, 0)

    def test_a_wrong_chord_gives_a_miss_line_and_the_offer_turns_the_label_gold(self):
        engine = self.engine(test_out=True)
        wrong = kitty_keys(self.resolved.bindings["herdr:split_vertical"].taught, PREFIX)
        sent = []

        def script():
            if not sent:
                sent.append(True)
                return wrong
            if self.clock.now > 1000 + lesson.OFFER_AFTER + 3:
                engine.quit()
            return b""

        self.run_with(engine, script)
        misses = [bar.line for bar in self.view.bars if bar.kind == "miss"]
        self.assertTrue(misses and misses[0].startswith("The pane split into two columns."), misses)
        self.assertTrue(any(bar.offer for bar in self.view.bars))

    def test_pause_menu_keys(self):
        engine = self.engine()
        keys = iter([b"q", b"\r", b"q", b"s", b"q", b"q"])
        self.run_with(engine, lambda: next(keys, b""))
        self.assertIn("PAUSE", {bar.tag for bar in self.view.bars})
        result = engine.result
        self.assertEqual(result.steps[0].outcome, "skipped")  # s skipped the first LEARN
        self.assertFalse(result.complete)  # the pause key in the menu quit

    def test_ctrl_c_leaves_at_once_in_both_encodings(self):
        for raw in (b"\x03", b"\x1b[99;5u"):
            engine = self.engine()
            with self.assertRaises(KeyboardInterrupt):
                self.run_with(engine, lambda: raw)
            self.assertTrue(self.view.closed)

    def test_reset_and_typing_events_reach_the_router(self):
        router = runner.router_for(plan_lesson(self.resolved, "split"))
        router.typing = True
        runner._apply([("reset",)], router, None)
        self.assertFalse(router.typing)
        runner._apply([("typing", True)], router, None)
        self.assertTrue(router.typing)

    def hint_after(self, engine, first, *, in_beat):
        """Answer step 0 with the user's chord, press the herdr prefix (in the beat, or in the same
        read as the answer), then `?` on step 1; return what step 1 made of the `?`."""
        answer = chord_bytes(self.resolved, engine.step.scenario.solution[0])
        stage = ["answer"]

        def script():
            if stage[0] == "answer" and engine.state == lesson.RUNNING:
                stage[0] = "beat" if in_beat else "next"
                return answer if in_beat else answer + first
            if stage[0] == "beat" and engine.state == lesson.BEAT:
                stage[0] = "next"
                return first
            if stage[0] == "next" and engine.index == 1 and engine.state == lesson.RUNNING:
                stage[0] = "hint"
                return b"?"
            if stage[0] == "hint":
                engine.quit()
            return b""

        self.run_with(engine, script)
        return engine

    def test_a_prefix_dropped_in_the_beat_leaves_the_hint_key_to_the_trainer(self):
        # The engine drops keys outside the user's turn; the router must not keep the prefix either.
        for prefix in (b"\x00", b"\x1b[32;5u"):
            engine = self.hint_after(self.engine(), prefix, in_beat=True)
            self.assertGreaterEqual(engine.max_level, 2, prefix)  # the ? was a hint
            self.assertNotIn(b"?", self.session.fed)

    def test_a_prefix_left_open_by_a_finished_step_is_forgotten_on_the_next_step(self):
        # Needs the engine's ("step", index) event at the top of Engine._enter_step.
        engine = self.hint_after(self.engine(), b"\x00", in_beat=False)
        self.assertIn(b"\x00", self.session.fed)  # the user's turn: the prefix reached herdr
        self.assertGreaterEqual(engine.max_level, 2)
        self.assertNotIn(b"?", self.session.fed)

    def test_step_event_forgets_only_the_prefix(self):
        router = runner.router_for(plan_lesson(self.resolved, "split"))
        self.assertEqual(router.route(b"\x00"), ("forward", b"\x00"))
        router.typing = True
        runner._apply([("step", 1)], router, None)
        self.assertTrue(router.typing)
        router.typing = False
        self.assertEqual(router.route(b"?"), ("trainer", runner.HINT))

    def test_a_prompt_opened_in_one_read_takes_the_rest_of_that_read(self):
        # Yazi's `f` opens the filter; a `q` in the same terminal read is filter text, not pause.
        engine = self.engine()
        opens = {b"f"}
        feed = self.session.feed

        def typed(raw):
            feed(raw)
            if raw in opens:
                self.session.typing_now = True
            elif raw in (b"\r", b"\x1b"):
                self.session.typing_now = False
        self.session.feed = typed
        reads = iter([b"fq", b"", b""])

        def script():
            if engine.state == lesson.PAUSED:
                engine.quit()
            raw = next(reads, None)
            if raw is None:
                engine.quit()
            return raw or b""

        self.run_with(engine, script)
        self.assertEqual(self.session.fed[-2:], [b"f", b"q"])
        self.assertNotIn("PAUSE", {bar.tag for bar in self.view.bars})

    def play_reads(self, engine, reads):
        """Hand the runner these terminal reads, one per frame; stop at Pause or the end."""
        reads, paused = iter(reads), []

        def script():
            if engine.state == lesson.PAUSED:
                paused.append(True)
                engine.quit()
            raw = next(reads, None)
            if raw is None:
                engine.quit()
            return raw or b""

        self.run_with(engine, script)
        return bool(paused)

    def prompt_after(self, delay):
        """The session reports its prompt open `delay` seconds after `f` reached it, as Yazi
        does through its hook."""
        opened, feed = [], self.session.feed

        def typed(raw):
            feed(raw)
            if raw == b"f":
                opened.append(self.clock.now)
        self.session.feed = typed
        self.session.typing = lambda: bool(opened) and self.clock.now >= opened[0] + delay

    def test_a_prompt_reported_late_still_takes_the_pause_key(self):
        # Yazi's filter reports itself after the key: up to 150 ms after `f`, `q` is filter text.
        for delay in (0.055, 0.12):
            for reads in ([b"fq"], [b"f", b"q"]):
                with self.subTest(delay=delay, reads=reads):
                    engine = self.engine()
                    self.prompt_after(delay)
                    self.assertFalse(self.play_reads(engine, reads))
                    self.assertEqual(self.session.fed[-2:], [b"f", b"q"])

    def test_the_pause_key_waits_150_ms_behind_a_key_and_not_otherwise(self):
        engine = self.engine()
        self.assertTrue(self.play_reads(engine, [b"q"]))
        self.assertEqual(self.sleeps, [])  # nothing reached the session: no wait
        engine = self.engine()
        self.play_reads(engine, [b"f", b"x"])
        self.assertEqual(self.sleeps, [])  # no trainer key behind it: no wait
        engine = self.engine()
        self.assertTrue(self.play_reads(engine, [b"fq"]))  # no prompt opened: q pauses
        self.assertAlmostEqual(sum(self.sleeps), 0.15)
        self.assertNotIn(b"q", self.session.fed)
        engine = self.engine()
        self.assertTrue(self.play_reads(engine, [b"f", b"", b"", b"", b"q"]))
        self.assertEqual(self.sleeps, [])  # `f` was 200 ms ago: q pauses at once

    def test_a_prompt_the_router_opened_is_not_polled(self):
        # herdr's rename prompt: the router owns `typing`, so q is tab-name text without a wait.
        engine = self.engine("tabs")
        rename = chord_bytes(self.resolved, lesson.Move(card="herdr:rename_tab"))
        self.assertFalse(self.play_reads(engine, [rename + b"q"]))
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.session.fed[-1], b"q")

    def test_a_prompt_closed_in_one_read_gives_the_pause_key_back(self):
        engine = self.engine()
        feed = self.session.feed
        self.session.typing_now = True

        def typed(raw):
            feed(raw)
            if raw == b"\r":
                self.session.typing_now = False
        self.session.feed = typed
        reads = iter([b"", b"\rq", b""])
        paused = []

        def script():
            if engine.state == lesson.PAUSED:
                paused.append(True)
                engine.quit()
            raw = next(reads, None)
            if raw is None:
                engine.quit()
            return raw or b""

        self.run_with(engine, script)
        self.assertEqual(paused, [True])
        self.assertNotIn(b"q", self.session.fed)

    def test_autoplay_presses_the_hint_key_and_ignores_the_users_keys(self):
        engine = self.engine()
        engine.history = None
        result = self.run_with(engine, lambda: chord_bytes(self.resolved, lesson.Move(card="herdr:split_vertical")),
                               history=None, autoplay=1.2)
        self.assertTrue(result.complete)
        self.assertTrue(all(step.outcome in ("met", "shown") for step in result.steps))
        self.assertEqual(self.session.fed, [])
        self.assertEqual(self.history.lessons(), {})

    def test_router_keeps_question_mark_and_q_unless_the_lesson_binds_them(self):
        plan = plan_lesson(self.resolved, "split")
        self.assertEqual((runner.router_for(plan).hint_key, runner.router_for(plan).pause_key), ("?", "q"))
        taken = runner.router_for(plan, extra=["q", "g, ?"])
        self.assertNotIn(taken.hint_key, "?q")
        self.assertNotIn(taken.pause_key, "?q")
        tabs = runner.router_for(plan_lesson(self.resolved, "tabs"))
        self.assertTrue(tabs.prompt_chords)  # rename tab opens a prompt the router types through


# The real CLI in an outer PTY.

LIVE = os.getenv("KEYS_TRAINER_TEST_HERDR") == "1" and shutil.which("herdr")
KITTY_REPLY = b"\x1b[?0u\x1b[?62c"  # flags 0 (supported), then the DA1 sentinel


class Outer:
    """keys-trainer in a 100 × 30 PTY with its own home and state directory."""

    def __init__(self, test, *args):
        self.root = Path(tempfile.mkdtemp(prefix="keys-shell-", dir="/tmp"))
        test.addCleanup(shutil.rmtree, self.root, True)
        rows = {name: json.loads((HERE / "test_data" / f"rigpage_{name}.json").read_text("utf-8"))
                for name in ("herdr", "yazi")}
        keys = self.root / "keys.json"
        keys.write_text(json.dumps({"groups": [{"id": name, "rows": value} for name, value in rows.items()]}))
        self.data = json.loads(keys.read_text())
        self.master, self.slave = pty.openpty()
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        self.attrs = termios.tcgetattr(self.slave)
        env = {"PATH": os.environ["PATH"], "HOME": str(self.root), "XDG_STATE_HOME": str(self.root / "state"),
               "LANG": "C.UTF-8", "TERM": "xterm-256color"}
        self.proc = subprocess.Popen([str(LAUNCHER), *args, "--keys", str(keys)], stdin=self.slave,
                                     stdout=self.slave, stderr=self.slave, start_new_session=True, env=env)
        test.addCleanup(self.stop)
        self.out, self.mark, self.answered = bytearray(), 0, 0

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0 or not select.select([self.master], [], [], left)[0]:
                return
            try:
                self.out += os.read(self.master, 65536)
            except OSError:
                return
            # Answer each keyboard query as a kitty terminal does.
            while self.out.count(b"\x1b[?u", self.answered) > 0:
                self.answered = self.out.index(b"\x1b[?u", self.answered) + 4
                os.write(self.master, KITTY_REPLY)

    def wait(self, text, timeout=15.0):
        needle = text.encode()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            found = self.out.find(needle, self.mark)
            if found >= 0:
                self.mark = found + len(needle)
                return
            self.pump(0.05)
        tail = bytes(self.out[self.mark:])[-1500:]
        raise AssertionError(f"never saw {text!r}; the screen ended with {tail!r}")

    def send(self, raw):
        os.write(self.master, raw)

    def finish(self, test):
        end = time.monotonic() + 10
        while self.proc.poll() is None and time.monotonic() < end:
            self.pump(0.05)
        test.assertEqual(self.proc.wait(timeout=1), 0)
        test.assertIn(b"\x1b[<u", self.out)       # the keyboard mode was popped
        test.assertTrue(self.out.rstrip().endswith(b"\x1b[?1049l"))  # and the main screen is back
        test.assertEqual(termios.tcgetattr(self.slave), self.attrs)


@unittest.skipUnless(LIVE, "set KEYS_TRAINER_TEST_HERDR=1 to play the shell against a private herdr")
class OuterTerminalTests(unittest.TestCase):
    def test_first_run_through_lesson_one_with_the_chords(self):
        outer = Outer(self)
        resolved = curriculum.resolve(curriculum.load(), outer.data)
        plan = plan_lesson(resolved, "split")
        outer.wait("Learn your shortcuts by using them")
        outer.wait("q  Quit")
        outer.send(b"\r")
        outer.wait("LESSON 1 OF")
        outer.wait("Esc  Back")
        outer.send(b"\r")
        for step in plan.steps:
            scenario = step.scenario
            outer.wait(scenario.goal)
            for index, move in enumerate(scenario.solution):
                if index:
                    outer.pump(0.4)
                outer.send(chord_bytes(resolved, move))
            outer.wait(scenario.done)
        outer.wait("LESSON 1 COMPLETE", timeout=10)
        outer.wait("Done for today")
        outer.send(b"q")
        outer.wait("DONE FOR TODAY")
        outer.wait("Enter  Quit")
        outer.send(b"\r")
        outer.finish(self)

    def test_hint_key_and_a_miss_then_ctrl_c(self):
        outer = Outer(self, "--lesson", "split")
        resolved = curriculum.resolve(curriculum.load(), outer.data)
        outer.wait("LESSON 1 OF")
        outer.wait("Esc  Back")
        outer.send(b"t")  # test me: RECALL and BUILD only
        outer.wait("RECALL")
        outer.wait("Split the pane into two rows")
        outer.send(kitty_keys(resolved.bindings["herdr:split_vertical"].taught, PREFIX))
        outer.wait("The pane split into two columns. Try again from here.")
        outer.send(b"?")
        outer.wait("Keys · Alt + Enter")
        outer.send(b"?")
        outer.wait("Watch: Alt + Enter")
        outer.wait("Split into two rows")
        outer.wait("Split the pane into two columns")
        outer.send(b"\x1b[99;5u")  # Ctrl+C, as kitty sends it
        outer.finish(self)


if __name__ == "__main__":
    unittest.main()
