import io
import os
import re
import shutil
import signal
import termios
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from coach import Bar, bar_rows, cells, render
from native_view import NativeView

SGR = re.compile(r"\x1b\[[0-9;]*m")
SIZES = ((100, 30), (80, 20), (60, 15), (38, 10))
STEPS = ("clean", "nudged", "current") + ("todo",) * 6
BARS = {
    "recall": Bar("RECALL", "Move focus to the left pane", kind="hint", hint="Show keys",
                line="A direct shortcut, no prefix. The arrow points where you want to go.",
                title="Move between panes", steps=STEPS),
    "learn": Bar("LEARN", "Jump back to the pane you were just in", chord="Ctrl + Space → ;", hint="Show me",
                line="Flip between two panes with one shortcut.",
                title="Move between panes", steps=("current",) + ("todo",) * 8),
    "build": Bar("BUILD", "Editor on the left, two stacked on the right, focus left", kind="checklist",
                line="✓ three panes    ✓ two stacked on the right    ○ focus on the left",
                title="Move between panes", steps=("clean",) * 8 + ("current",),
                target=("┌─────┬────┐", "│  ●  ├────┤", "└─────┴────┘")),
    "rebound": Bar("RECALL", "Close the focused pane", hint="Hint", hint_key="!", pause_key=";",
                   line="A new tab opened. I'll set it up again, then you try.", kind="miss",
                   title="Split the screen", steps=("clean",) * 5 + ("current", "todo")),
    "long": Bar("RECALL", "Ø" * 200, chord="Ctrl + Space → " + "x" * 120, line="\x1b[31mlong\x07 " * 40,
                title="T" * 90, steps=("todo",) * 40),
}
SNAPSHOTS = {
    ("recall", 100, 30): [
        "RECALL Move focus to the left pane",
        "       A direct shortcut, no prefix. The arrow points where you want to go.",
        "? Show keys     q Pause                                          Move between panes  ●◐◉○○○○○○  3/9"],
    ("recall", 80, 20): [
        "RECALL Move focus to the left pane",
        "       A direct shortcut, no prefix. The arrow points where you want to go.",
        "? Show keys     q Pause                      Move between panes  ●◐◉○○○○○○  3/9"],
    ("recall", 60, 15): [
        "RECALL A direct shortcut, no prefix. The arrow points wher…",
        "? Show keys  q Pause                                    3/9"],
    ("recall", 38, 10): ["Enlarge the window to 40 × 12", "? Show keys  q Pause"],
    ("learn", 100, 30): [
        "LEARN  Jump back to the pane you were just in                               Press  Ctrl + Space → ;",
        "       Flip between two panes with one shortcut.",
        "? Show me     q Pause                                            Move between panes  ◉○○○○○○○○  1/9"],
    ("learn", 60, 15): [
        "LEARN  Jump back to the pane you were just in",
        "? Show me  q Pause             Press  Ctrl + Space → ;  1/9"],  # the goal keeps its row
    ("build", 100, 30): [
        "BUILD  Editor on the left, two stacked on the right, focus left                        ┌─────┬────┐",
        "       ✓ three panes    ✓ two stacked on the right    ○ focus on the left              │  ●  ├────┤",
        "? Hint     q Pause                                Move between panes  ●●●●●●●●◉  9/9   └─────┴────┘"],
    ("build", 80, 20): [
        "BUILD  Editor on the left, two stacked on the right, focus left    ┌─────┬────┐",
        "       ✓ three panes    ✓ two stacked on the right    ○ focus on…  │  ●  ├────┤",
        "? Hint     q Pause            Move between panes  ●●●●●●●●◉  9/9   └─────┴────┘"],
    ("build", 60, 15): [
        "BUILD  Editor on the left, two stacked on the right, focus…",  # the goal, never the checklist
        "? Hint  q Pause                                         9/9"],
}


def plain(rows):
    return [SGR.sub("", row).rstrip() for row in rows]


class CoachBarTests(unittest.TestCase):
    def test_snapshots(self):
        for (name, width, height), expected in SNAPSHOTS.items():
            with self.subTest(bar=name, size=(width, height)):
                self.assertEqual(plain(render(BARS[name], width, height)), expected)

    def test_rows_fit_reset_and_keep_the_hint_bottom_left(self):
        for name, bar in BARS.items():
            for width, height in SIZES:
                with self.subTest(bar=name, size=(width, height)):
                    rows = render(bar, width, height)
                    self.assertEqual(len(rows), bar_rows(width, height) or 2)
                    for row in rows:
                        text = SGR.sub("", row)
                        self.assertLess(cells(text), width)
                        self.assertTrue(row.endswith("\x1b[0m"))
                        self.assertNotIn("\x07", text)
                        self.assertFalse(re.search(r"\x1b(?!\[[0-9;]*m)", row), "only SGR in a row")
                    self.assertTrue(SGR.sub("", rows[-1]).startswith(f"{bar.hint_key} {bar.hint}"))

    def test_a_long_show_me_line_keeps_the_newest_keys(self):
        keys = [f"Ctrl + Alt + {letter}" for letter in "ABCDEFGH"]
        bar = Bar("BUILD", "Build the console", line="Watch: " + ", ".join(keys), kind="hint", hint=None)
        for width, height in ((50, 15), (100, 30)):
            with self.subTest(size=(width, height)):
                shown = "\n".join(plain(render(bar, width, height)))
                self.assertIn("Watch: … ", shown)
                self.assertIn(keys[-1], shown)
                self.assertNotIn(keys[0], shown)

    def test_show_me_in_a_small_window_shows_the_key_being_pressed_in_full(self):
        for key, chord in (("Enter", "Ctrl + Space → Shift + w"), ("Ctrl + Space → Shift + t", "")):
            bar = Bar("LEARN", "Rename the workspace", chord=chord, line=f"Watch: Ctrl + Space → Shift + w, {key}",
                      kind="hint", hint=None, title="Workspaces", steps=("current", "todo"))
            for width in (40, 50):
                with self.subTest(key=key, width=width):
                    self.assertIn(key, plain(render(bar, width, 15))[0])

    def test_states_without_keys(self):
        beat = Bar("✓", "Focus is on the left", line="1.2 s · first time without a hint", kind="success",
                   hint=None, title="Move between panes", steps=("clean",) * 3 + ("current",) + ("todo",) * 5)
        self.assertEqual(plain(render(beat, 100, 30))[2].strip(), "Move between panes  ●●●◉○○○○○  4/9")
        typing = Bar("LEARN", "Name the tab logs", controls="Typing · Enter confirms · Esc cancels",
                     title="Tabs", steps=("clean",) * 5 + ("current",) + ("todo",) * 3)
        self.assertTrue(plain(render(typing, 60, 15))[1].startswith("Typing · Enter confirms"))
        self.assertTrue(plain(render(typing, 60, 15))[1].endswith("6/9"))

    def test_a_notice_bar_shows_skip_and_pause_but_no_hint_key(self):
        bar = Bar("NO KEY", "Split the pane into two columns", line="No key for split vertical · s skips it",
                  kind="warn", hint="", skip_key="s", steps=("done", "current", "todo"))
        plain = [re.sub(r"\x1b\[[0-9;]*m", "", row) for row in render(bar, 100, 30)]
        self.assertTrue(plain[0].startswith("NO KEY Split the pane"))
        self.assertIn("s Skip", plain[2])
        self.assertIn("Pause", plain[2])
        self.assertNotIn("?", plain[2])

    def test_offer_paints_only_the_hint_label_gold(self):
        from dataclasses import replace
        from screen import GOLD, MUTED

        quiet = BARS["recall"]
        offered = replace(quiet, offer=True)
        for width, height in ((100, 30), (60, 15)):
            calm, gold = render(quiet, width, height)[-1], render(offered, width, height)[-1]
            self.assertEqual(plain([calm]), plain([gold]))
            self.assertIn(f"\x1b[{GOLD}m Show keys", gold)
            self.assertIn(f"\x1b[{MUTED}m Show keys", calm)
            self.assertIn(f"\x1b[{MUTED}m Pause", gold)

    def test_narrow_keys_hint_keeps_every_moves_taught_chord(self):
        """The tab round trip's first hint (out, then back) at 60 and 80 columns: when every
        alternative does not fit, the line drops to the taught chords instead of clipping
        the way back."""
        import json
        from pathlib import Path

        import curriculum
        import lesson

        rows = {name: json.loads((Path(__file__).resolve().parent / "test_data" / f"rigpage_{name}.json")
                                 .read_text("utf-8")) for name in ("herdr", "yazi")}
        data = {"groups": [{"id": name, "rows": items} for name, items in rows.items()]}
        plan = lesson.plan_lesson(curriculum.resolve(curriculum.load(), data), "tabs")
        scenario = next(step.scenario for step in plan.steps if step.scenario.id == "tab-step")
        line = lesson.TEXT["keys"].format(keys=lesson.keys_line(plan, scenario, every=True))
        bar = Bar("RECALL", scenario.goal, line=line, kind="hint", title=plan.title, steps=STEPS)
        out, back = (plan.bindings[card] for card in scenario.cards)
        for width, height in ((60, 15), (80, 20)):
            with self.subTest(size=(width, height)):
                shown = plain(render(bar, width, height))[0 if width < 80 else 1]
                self.assertNotIn("…", shown)
                for binding in (out, back):
                    self.assertIn(binding.label(), shown)
                    self.assertEqual(all(label in shown for label in binding.labels()),
                                     all(label in shown for label in out.labels()), "one style per move")
        self.assertIn(line, plain(render(bar, 120, 30))[1])  # wide: every alternative


class Source:
    def __init__(self, frames=()):
        self.frames = list(frames)
        self.sizes = []
        self.ended = 0

    def begin_native(self, columns, rows):
        self.sizes.append((columns, rows))

    def native_output(self):
        return self.frames.pop(0) if self.frames else b""

    def end_native(self):
        self.ended += 1


class NativeBarTests(unittest.TestCase):
    def show(self, source, bar, *sizes):
        out = io.TextIOWrapper(io.BytesIO())
        view = NativeView(Mock(), source)
        with redirect_stdout(out):
            for size in sizes:
                with patch("native_view.shutil.get_terminal_size", return_value=os.terminal_size(size)):
                    view.show_bar(bar)
        out.flush()
        return out.buffer.getvalue()

    def test_frame_gets_the_rows_above_the_bar_and_the_bar_is_drawn_inside_the_frame(self):
        frame = b"\x1b[?2026h\x1b[2J\x1b[1;1Hherdr\x1b[?2026l"
        source = Source([frame])
        out = self.show(source, BARS["recall"], (100, 30), (60, 15), (38, 10))
        self.assertEqual(source.sizes, [(100, 27), (60, 13)])
        self.assertEqual(source.ended, 1)
        self.assertIn(b"\x1b[1;27r", out)
        self.assertIn(b"\x1b[1;13r", out)
        start = out.index(b"herdr")
        bar = out.index("\x1b[30;1H".encode(), start)
        self.assertLess(out.index(b"\x1b[28;1H", start), bar)
        self.assertLess(bar, out.index(b"\x1b[?2026l", start), "bar inside the synchronized frame")
        self.assertIn("? Show keys", SGR.sub("", out[out.index(b"\x1b[15;1H"):].decode()))
        self.assertIn("Enlarge the window".encode(), out[out.rindex(b"\x1b[2J"):])

    def test_unchanged_bar_without_a_frame_writes_nothing(self):
        source = Source()
        out = io.TextIOWrapper(io.BytesIO())
        view = NativeView(Mock(), source)
        with redirect_stdout(out), patch("native_view.shutil.get_terminal_size",
                                         return_value=os.terminal_size((80, 20))):
            view.show_bar(BARS["learn"])
            out.flush()
            first = len(out.buffer.getvalue())
            view.show_bar(BARS["learn"])
            out.flush()
            self.assertEqual(len(out.buffer.getvalue()), first)
            view.show_bar(BARS["recall"])
            out.flush()
            self.assertGreater(len(out.buffer.getvalue()), first)
            view.close()
        self.assertIn(b"\x1b[r", out.buffer.getvalue())

    def test_status_lines_still_work(self):
        source = Source()
        out = io.TextIOWrapper(io.BytesIO())
        with redirect_stdout(out), patch("native_view.shutil.get_terminal_size",
                                         return_value=os.terminal_size((80, 24))):
            NativeView(Mock(), source).show("Layout matched", "t  Target map")
        out.flush()
        self.assertEqual(source.sizes, [(80, 22)])
        self.assertIn(b"\x1b[24;1H", out.buffer.getvalue())


@unittest.skipUnless(os.getenv("KEYS_TRAINER_TEST_HERDR") == "1" and shutil.which("herdr"),
                     "opt in to private Herdr integration")
class PrivateHerdrFrameTests(unittest.TestCase):
    def test_frames_stay_in_the_rows_above_the_bar(self):
        from test_router import PrivateHerdr

        with PrivateHerdr() as herdr:
            termios.tcsetwinsize(herdr.master, (27, 100))
            herdr.client.send_signal(signal.SIGWINCH)
            end = time.monotonic() + 1
            while time.monotonic() < end:
                herdr.drain(.05)
            out = bytearray()
            for keys in (b"\x00v", b"\x1b\r", b"\x00c"):
                herdr.feed(keys)
                end = time.monotonic() + .4
                while time.monotonic() < end:
                    out += herdr.drain(.05)
            self.assertTrue(out)
            rows = [int(match[1]) for match in re.finditer(rb"\x1b\[(\d+)(?:;\d+)?H", out)]
            self.assertLessEqual(max(rows), 27)
            self.assertNotIn(b"\n", out)


if __name__ == "__main__":
    unittest.main()
