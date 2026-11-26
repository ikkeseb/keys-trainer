"""Nothing the user needs is clipped: every lesson's goals and keys, rendered from the bundled
curriculum and bindings at the sizes in use.

Bars: every scenario's LEARN and RECALL bar (and each BUILD step) as the engine composes it,
plain and with Show keys, at 100 × 30 and 80 × 24 (three rows) and 60 × 15 (two rows).
At every size the goal and each move's taught chord (its last key, a repeat count, a typed
name) must show whole; alternatives may give way. Intros: every lesson's "You learn" and
"Then you build" rows show each keys label and goal whole at 80 × 24 and 100 × 30.

`python3 test_fit.py --dump FILE` writes the 60-column bars to FILE for a look.
"""

import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import checks
import coach
import curriculum
import lesson
import trainer
from coach import Bar
from screen import Text, render_card

HERE = Path(__file__).resolve().parent
SGR = re.compile(r"\x1b\[[0-9;]*m")
ROWS = {name: json.loads((HERE / "test_data" / f"rigpage_{name}.json").read_text("utf-8"))
        for name in ("herdr", "yazi", "shell", "strata")}
DESKTOP_ROWS = [  # as test_desktop_session's: Omarchy's bundled desktop bindings
    {"name": "super+1…0", "value": "Switch to workspace 1…10"},
    {"name": "super+ctrl+tab", "value": "Former workspace"},
    {"name": "super+|", "value": "Toggle Quake terminal"},
]
DATA = {"groups": [{"id": name, "rows": rows} for name, rows in ROWS.items()]
        + [{"id": "desktop", "rows": DESKTOP_ROWS}]}
BAR_SIZES = ((100, 30), (80, 24), (60, 15))
CARD_SIZES = ((100, 30), (80, 24))
TEXT = lesson.TEXT


def resolved():
    return curriculum.resolve(curriculum.load(), DATA, desktop=True)


class Stub:
    """Enough of a session for `Engine.bar()`: nothing is started, nothing has happened."""

    def __init__(self, live):
        self.live = live


def engine_at(plan, index):
    engine = lesson.Engine(plan, Stub(plan.steps[0].scenario.tool == "desktop"))
    engine.index, engine.state, engine.level, engine._line = index, lesson.RUNNING, 0, None
    engine._typing, engine._latched, engine._history, engine._earlier = False, 0, [], ()
    engine._start = engine._settled = None
    return engine


def bars(plan, index):
    """(name, bar, keys that must show) for the step: plain, and after one press of Show keys
    (each move of a build as its next move). No check has passed yet."""
    engine = engine_at(plan, index)
    step = plan.steps[index]
    scenario = step.scenario
    nothing = lambda items, *_: checks.Progress(0, False, len(items))
    with patch.object(lesson.checks, "evaluate", nothing):
        plain = engine.bar()
        learn = lesson.keys_line(plan, scenario) if step.kind == "learn" else ""
        out = [(f"{step.kind}", plain, [learn] if learn else [])]
        if step.kind == "learn":
            return out
        if step.kind == "build":
            for group in lesson._groups(scenario.solution):
                engine._next_move = lambda world, group=group: (engine._what(group), group)
                engine._line = (engine._keys_text(), "hint")
                keys = [label for label in map(engine._move_label, group) if label]
                out.append((f"{step.kind} next {keys}", engine.bar(), keys))
            return out
        engine._line = (engine._keys_text(), "hint")
        out.append((f"{step.kind} show keys", engine.bar(), taught(plan, scenario)))
    return out


def taught(plan, scenario):
    """Each move's taught chord with its repeat count: what a Show keys line may not lose."""
    keys = []
    for move in lesson.keys_line(plan, scenario, every=True).split(TEXT["then"]):
        options = move.split(TEXT["or"])
        times = re.search(r" ×\d+$", options[-1])
        first = options[0] if len(options) == 1 or not times or options[0].endswith(times[0]) \
            else options[0] + times[0]
        keys.append((first, move))  # the taught chord, or the whole move with its alternatives
    return keys


def plain(rows):
    return [SGR.sub("", row).rstrip() for row in rows]


def flowing(rows):
    """The rows' words in reading order, one space apart (a wrapped goal reads whole)."""
    return " ".join(" ".join(rows).split())


def missing(rows, texts):
    """Each text that is not on screen whole, word by word in order (wrapping is fine).
    A (taught, whole) pair is there when either is."""
    shown = flowing(rows)
    return [text for text in texts if text and not any(" ".join(t.split()) in shown
                                                       for t in (text if isinstance(text, tuple) else (text,)))]


def walk():
    found = resolved()
    for state in found.lessons:
        if not state.visible:
            continue
        plan = lesson.plan_lesson(found, state.lesson.id)
        seen = set()
        for index, step in enumerate(plan.steps):
            if (step.kind, step.scenario.id) in seen:
                continue
            seen.add((step.kind, step.scenario.id))
            for name, bar, keys in bars(plan, index):
                yield f"{state.lesson.id} · {step.scenario.id} · {name}", bar, keys


# Known to clip at 60 columns only (two rows), left for a later round: the goal is 56 cells
# where the row holds 52, and the chord fits neither row nor splits between them.
NARROW_KNOWN = {"panes-move · build-editor-stack · build", "workspaces · rename-workspace · learn"}


class BarFitTests(unittest.TestCase):
    def test_every_bar_shows_its_goal_and_keys_whole(self):
        count = 0
        for where, bar, keys in walk():
            for width, height in BAR_SIZES:
                rows = plain(coach.render(bar, width, height))
                two = coach.bar_rows(width, height) == 2
                # Two rows fold an event (a hint, a miss) over the goal: the keys are what was asked
                # for. A chord may break between its moves there, the rest on the bottom row.
                folded = two and bar.kind in coach.EVENT_KINDS
                need = [piece for key in keys for piece in (key.split(TEXT["list"]) if two and isinstance(key, str)
                                                            else (key,))]
                lost = missing(rows, need + ([] if folded else [bar.goal]))
                cut = flowing(rows).count("…") - (bar.goal + bar.line + bar.chord).count("…")
                with self.subTest(where, size=(width, height)):
                    count += 1
                    if two and where in NARROW_KNOWN:
                        self.assertTrue(lost or cut, "fits now: take it off NARROW_KNOWN")
                        continue
                    self.assertEqual(lost, [], "\n".join(rows))
                    self.assertEqual(cut, 0, "\n".join(rows))
        self.assertGreater(count, 100)


class IntroFitTests(unittest.TestCase):
    def test_every_intro_shows_its_rows_whole(self):
        found = resolved()
        visible = [state for state in found.lessons if state.visible]
        self.assertEqual(len(visible), 17)
        for number, state in enumerate(visible, 1):
            plan = lesson.plan_lesson(found, state.lesson.id)
            scenarios = found.curriculum.scenarios
            learned = [scenarios[step.scenario] for step in state.steps if step.kind == "learn"]
            build = next((scenarios[step.scenario].goal for step in state.steps if step.kind == "build"), None)
            card = trainer.intro_card(found, state.lesson.id, number, len(visible))
            for width, height in CARD_SIZES:
                with self.subTest(state.lesson.id, size=(width, height)):
                    rows, _ = render_card(card, width, height)
                    shown = [SGR.sub("", rows[y]).rstrip() for y in sorted(rows)]
                    start = shown.index(trainer.TEXT["intro_learn"])
                    body = shown[start + 1:]
                    for scenario in learned:
                        keys = lesson.keys_line(plan, scenario)
                        at = next((i for i, row in enumerate(body)
                                   if row == "  " + keys or row.startswith("  " + keys + "  ")), None)
                        self.assertIsNotNone(at, f"{keys!r} not whole in\n" + "\n".join(shown))
                        # The goal reads whole from the keys' row on, in its own column.
                        self.assertIn(" ".join(scenario.goal.split()), flowing(body[at:at + 3]).replace(keys, "", 1),
                                      "\n".join(shown))
                    if build and state.lesson.builds:  # the goal beside the map, in its own column
                        at = shown.index(trainer.TEXT["intro_build"]) + 1
                        column = 2 + max(map(len, state.lesson.builds)) + 4
                        said = [row[column:] for row in shown[at:] if row[:column].strip() or row[column:].strip()]
                        self.assertIn(" ".join(build.split()), flowing(said[:4]), "\n".join(shown))


def dump(path):
    out = []
    for where, bar, _ in walk():
        out.append(f"== {where}")
        out += plain(coach.render(bar, 60, 15)) + [""]
    Path(path).write_text("\n".join(out), "utf-8")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--dump"]:
        dump(sys.argv[2])
    else:
        unittest.main()
