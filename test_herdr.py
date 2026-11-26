import json
import os
import re
import shutil
import time
import tomllib
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from herdr_backend import (
    FIXTURES,
    HerdrBackend,
    HerdrLayout,
    HerdrUnavailable,
    LAYOUT_ACTIONS,
    LayoutPane,
    NATIVE_FRAME_CLOSE,
    NATIVE_FRAME_OPEN,
    UNCONFIGURED_GROUP,
    _key_config,
    _tab_layout,
    demo_keys,
    herdr_actions,
    kitty_keys,
    selected_bindings,
)
from pathlib import Path


ROWS = [
    {"name": "ctrl+space", "value": "prefix"},
    {"name": "prefix+h", "value": "split horizontal"},
    {"name": "alt+enter", "value": "split horizontal"},
    {"name": "prefix+v", "value": "split vertical"},
    {"name": "alt+shift+enter", "value": "split vertical"},
    {"name": "prefix+c", "value": "new tab"},
    {"name": "prefix+n", "value": "next tab"},
    {"name": "alt+right", "value": "next tab"},
    {"name": "prefix+p", "value": "previous tab"},
    {"name": "ctrl+alt+left", "value": "focus pane left"},
    {"name": "ctrl+alt+right", "value": "focus pane right"},
]


RIGPAGE_ROWS = json.loads(Path(__file__).with_name("test_data").joinpath("rigpage_herdr.json").read_text("utf-8"))
# A keys export lists herdr's own defaults under a group of their own: they are not in the
# user's config, so a private [keys] must never bind them (M6).
UNCONFIGURED = [{"name": chord, "value": action, "group": f"{UNCONFIGURED_GROUP} (herdr defaults)"}
                for chord, action in (("prefix+s", "settings"), ("prefix+tab", "cycle pane next"),
                                      ("prefix+shift+tab", "cycle pane previous"), ("prefix+w", "workspace picker"),
                                      ("prefix+w, up", "navigate workspace up"), ("prefix+g", "goto"),
                                      ("prefix+b", "toggle sidebar"))]
FULL_EXPORT = RIGPAGE_ROWS + UNCONFIGURED
# Lessons 1 to 5 bind these; the rest of the export must stay out of [keys].
LESSON_ACTIONS = frozenset({
    "split_horizontal", "split_vertical", "close_pane", "zoom", "last_pane",
    "focus_pane_left", "focus_pane_right", "focus_pane_up", "focus_pane_down",
    "resize_pane_left", "resize_pane_right", "resize_pane_up", "resize_pane_down",
    "new_tab", "rename_tab", "switch_tab", "previous_tab", "next_tab",
    "new_workspace", "rename_workspace", "close_workspace", "previous_workspace", "next_workspace",
})


def rectangle(x, y, width, height):
    return {"x": x, "y": y, "width": width, "height": height}


def pane_created(pane="w1:p2", tab="w1:t1"):
    return {
        "event": "pane_created",
        "data": {"pane": {"workspace_id": "w1", "tab_id": tab, "pane_id": pane}},
    }


def painted_rows(frames):
    """Compose the absolute cursor paints used by the private client's frames."""
    cells = {}
    row = col = 1
    tokens = re.finditer(
        r"\x1b\[([0-9;]*)([HfJK])|\x1b\[[0-?]*[ -/]*[@-~]"
        r"|\x1b\].*?(?:\x07|\x1b\\)|[^\x1b]+",
        frames.decode(errors="replace"),
    )
    for token in tokens:
        text = token[0]
        if token[2] in ("H", "f"):
            args = [int(arg or 1) for arg in token[1].split(";")]
            row, col = (args + [1])[:2]
        elif token[2] == "J" and token[1] in ("2", "3"):
            cells.clear()
        elif token[2] == "K":
            cells[row] = {x: char for x, char in cells.get(row, {}).items() if x < col}
        elif not text.startswith("\x1b"):
            line = cells.setdefault(row, {})
            for char in text:
                if char.isprintable():
                    line[col] = char
                    col += 1
    return "\n".join(
        "".join(line.get(x, " ") for x in range(1, max(line, default=0) + 1))
        for _, line in sorted(cells.items())
    )


class HerdrBackendTests(unittest.TestCase):
    def test_private_config_uses_allowlisted_actions_and_bindings(self):
        default = (
            '[keys]\n# prefix = "ctrl+b"\n# focus_pane_left = "prefix+h"\n'
            '# type = "shell" runs detached in the background.\n'
            '# close_pane = ["prefix+d", "ctrl+w"]\nnew_workspace = "alt+w"\n[ui]'
        )
        rows = [*ROWS, {"name": "prefix+r", "value": "rm -rf ignored"}]
        keys = tomllib.loads(_key_config(default, rows, {"split_horizontal", "split_vertical", "new_tab"}))["keys"]
        self.assertEqual(keys["prefix"], "ctrl+space")
        self.assertEqual(keys["split_horizontal"], ["prefix+h", "alt+enter"])
        self.assertEqual(keys["focus_pane_left"], "")
        self.assertEqual(keys["close_pane"], "")
        self.assertEqual(keys["new_workspace"], "")
        self.assertNotIn("type", keys)
        self.assertNotIn("prefix+r", keys.values())

    def test_layout_config_enables_only_split_and_directional_focus(self):
        defaults = '[keys]\n# new_tab = "prefix+c"\n# close_pane = "prefix+x"\n[ui]'
        keys = tomllib.loads(_key_config(defaults, ROWS, LAYOUT_ACTIONS))["keys"]
        self.assertEqual(keys["split_vertical"], ["prefix+v", "alt+shift+enter"])
        self.assertEqual(keys["focus_pane_left"], ["ctrl+alt+left"])
        self.assertEqual(keys["new_tab"], "")
        self.assertEqual(keys["close_pane"], "")

    def test_layout_normalizes_panes_in_focused_tab(self):
        visible = {
            "tab_id": "t2",
            "focused_pane_id": "p3",
            "area": rectangle(20, 3, 40, 10),
            "panes": [
                {"pane_id": "p2", "rect": rectangle(20, 3, 19, 10)},
                {"pane_id": "p3", "rect": rectangle(40, 3, 20, 10)},
            ],
        }
        layout = _tab_layout({
            "focused_tab_id": "t2",
            "layouts": [{"tab_id": "t1"}, visible],
        })
        self.assertEqual((layout.width, layout.height), (40, 10))
        self.assertEqual(layout.focused_id, "p3")
        self.assertEqual(layout.panes, (
            LayoutPane("p2", 0, 0, 19 / 40, 1, False),
            LayoutPane("p3", 0.5, 0, 0.5, 1, True),
        ))

    def test_split_undo_requires_ordered_creation_evidence_for_visible_panes(self):
        backend = HerdrBackend(ROWS)
        backend._layout_scope = ("w1", "w1:t1", "w1:p1")
        backend._layout_events = Mock()
        backend._layout_events.receive.side_effect = [
            pane_created(pane="w1:p1"),
            pane_created(pane="w1:p6", tab="w1:t9"),
            pane_created(pane="w1:p8"),
            None,
            pane_created(pane="w1:p2"),
            None,
        ]
        original = HerdrLayout("w1:t1", "w1:p1", (LayoutPane("w1:p1", 0, 0, 1, 1, True),), 80, 24)
        second = LayoutPane("w1:p8", 0.5, 0, 0.5, 1, False)
        third = LayoutPane("w1:p2", 0.5, 0.5, 0.5, 0.5, False)
        two = replace(original, panes=(*original.panes, second))
        three = replace(two, panes=(*two.panes, third))
        backend._poll_split_events()
        self.assertEqual(backend._eligible_split(two, "w1"), "w1:p8")
        self.assertIsNone(backend._eligible_split(three, "w1"))
        self.assertIsNone(backend._eligible_split(two, "w9"))

        backend._poll_split_events()
        self.assertEqual(backend._eligible_split(three, "w1"), "w1:p2")
        self.assertIsNone(backend._eligible_split(two, "w1"))

    def test_key_encoding(self):
        self.assertEqual(demo_keys("prefix+h", "ctrl+space"), b"\x00h")
        self.assertEqual(demo_keys("alt+enter", ""), b"\x1b\r")
        self.assertEqual(demo_keys("alt+right", ""), b"\x1b[1;3C")
        self.assertEqual(demo_keys("prefix+h", "f12"), b"\x1b[24~h")
        self.assertIsNone(demo_keys("super+h", ""))

    def test_kitty_encoding_is_unambiguous_for_exported_chords(self):
        for chord, expected in (
            ("alt+esc", b"\x1b[27;3u"),  # legacy ESC ESC is ignored by the private client (M5)
            ("alt+shift+enter", b"\x1b[13;4u"),
            ("alt+enter", b"\x1b[13;3u"),
            ("prefix+shift+c", b"\x1b[32;5u\x1b[99;2u"),
            ("prefix+;", b"\x1b[32;5u;"),
            ("prefix+3", b"\x1b[32;5u3"),
            ("alt+3", b"\x1b[51;3u"),
            ("ctrl+alt+left", b"\x1b[1;7D"),
            ("ctrl+alt+shift+down", b"\x1b[1;8B"),
            ("alt+up", b"\x1b[1;3A"),
            ("esc", b"\x1b[27u"),
            ("enter", b"\r"),
            ("backspace", b"\x7f"),
            ("prefix+minus", b"\x1b[32;5u-"),
            ("f5", b"\x1b[15~"),
        ):
            with self.subTest(chord=chord):
                self.assertEqual(kitty_keys(chord, "ctrl+space"), expected)
        self.assertIsNone(kitty_keys("hyper+h", ""))
        self.assertIsNone(kitty_keys("prefix+h", ""))
        self.assertIsNone(kitty_keys("ctrl+ø", ""))

    def test_private_keymap_never_binds_unconfigured_rows(self):
        # M6: a [keys] with herdr's own default rows left every binding dead.
        names = sorted(LESSON_ACTIONS | {row["value"].replace(" ", "_") for row in UNCONFIGURED})
        default = "[keys]\n" + "".join(f'# {name} = "prefix+f1"\n' for name in names) + "[ui]"
        keys = tomllib.loads(_key_config(default, FULL_EXPORT, set(names)))["keys"]
        bound = {chord for value in keys.values() for chord in ([value] if isinstance(value, str) else value)}
        for row in UNCONFIGURED:
            with self.subTest(row=row["name"]):
                self.assertNotIn(row["name"], bound)
                self.assertFalse(keys.get(row["value"].replace(" ", "_")))
        self.assertEqual(keys["close_pane"], ["prefix+x", "alt+esc"])
        self.assertEqual(keys["switch_tab"], ["prefix+1..9", "alt+1..9"])
        self.assertEqual(keys["prefix"], "ctrl+space")

    def test_session_bindings_name_only_the_requested_actions(self):
        bindings = selected_bindings(RIGPAGE_ROWS, {"zoom", "last_pane", "cycle_pane_next"})
        self.assertEqual(bindings, {"prefix": ["ctrl+space"], "zoom": ["prefix+z"], "last_pane": ["prefix+l", "prefix+;"]})
        self.assertEqual(herdr_actions('[keys]\n# prefix = "ctrl+b"\n# zoom = "prefix+z"\n[ui]'), {"zoom"})

    def test_session_api_needs_a_session(self):
        backend = HerdrBackend(RIGPAGE_ROWS)
        with self.assertRaises(HerdrUnavailable):
            backend.world()
        with self.assertRaises(HerdrUnavailable):
            backend.apply_fixture("one_pane")
        with self.assertRaises(ValueError):
            backend.apply_fixture("pyramid")
        with self.assertRaises(ValueError):
            backend.start_session(LESSON_ACTIONS, "pyramid")
        with self.assertRaises(ValueError):
            backend.play("zoom")

    def test_native_output_keeps_partial_frames_and_drops_terminal_queries(self):
        backend = HerdrBackend(ROWS)
        backend._native_active = True
        first = NATIVE_FRAME_OPEN + b"private frame" + NATIVE_FRAME_CLOSE
        with patch.object(backend, "_drain_client"):
            backend._native_buffer.extend(b"\x1b[0c" + NATIVE_FRAME_OPEN[:3])
            self.assertEqual(backend.native_output(), b"")
            backend._native_buffer.extend(first[3:] + b"\x1b[?1049h")
            self.assertEqual(backend.native_output(), first)
            self.assertEqual(backend.native_output(), b"")
            backend.end_native()
            self.assertEqual(backend.native_output(), b"")


@unittest.skipUnless(
    os.environ.get("KEYS_TRAINER_TEST_HERDR") == "1" and shutil.which("herdr"),
    "set KEYS_TRAINER_TEST_HERDR=1 to test with a private Herdr server",
)
class PrivateHerdrTests(unittest.TestCase):
    def test_native_tab_repaints_survive_session_reads(self):
        with HerdrBackend(ROWS) as backend:
            backend.start_session({"new_tab", "next_tab"})
            for action in ("new_tab", "next_tab"):
                with self.subTest(action=action):
                    origin = backend.layout()
                    backend.begin_native(100, 28)
                    initial = self._wait_for_native(backend, "Practice pane  " + origin.focused_id)
                    backend.play(action)
                    after = self._wait_for_layout(backend, lambda layout: layout.tab_id != origin.tab_id)
                    painted = self._wait_for_native(backend, "Practice pane  " + after.focused_id, initial)
                    self.assertIn("Start", painted_rows(painted))
                    self.assertNotIn("config.toml", painted_rows(painted))
                    backend.end_native()
                    self.assertEqual(backend.native_output(), b"")

    def test_native_split_repaint_survives_layout_sampling(self):
        with HerdrBackend(ROWS) as backend:
            backend.start_layout()
            initial = backend.layout()
            backend.begin_native(92, 25)
            first = self._wait_for_native(backend, "Practice pane  " + initial.focused_id)
            backend.feed(b"\x00v")
            two = self._wait_for_layout(backend, lambda layout: len(layout.panes) == 2)
            self.assertLess(two.width, initial.width)
            self._wait_for_native(backend, "Practice pane  " + two.focused_id, first)

            backend.begin_native(42, 13)
            self._wait_for_layout(backend, lambda layout: layout.height < two.height)
            backend.end_native()
            restored = self._wait_for_layout(
                backend, lambda layout: (layout.width, layout.height) == (initial.width, initial.height),
            )
            self.assertEqual(restored.focused_id, two.focused_id)
            self.assertEqual(len(restored.panes), 2)
            backend.begin_native(92, 25)
            self._wait_for_native(backend, "Practice pane  " + two.focused_id)

    def test_layout_follows_real_splits_focus_and_bounded_undo(self):
        with HerdrBackend(ROWS) as backend:
            backend.start_layout()
            initial = backend.layout()
            self.assertEqual(len(initial.panes), 1)
            first = initial.focused_id
            self.assertIsNone(backend.undo_id)
            self.assertFalse(backend.undo_split(first))
            self.assertFalse(backend.focus_pane("outside"))

            backend.feed(demo_keys("prefix+c", "ctrl+space"))
            backend.feed(demo_keys("prefix+v", "ctrl+space"))
            columns = self._wait_for_layout(backend, lambda view: len(view.panes) == 2)
            left, right = sorted(columns.panes, key=lambda pane: pane.x)
            self.assertEqual(columns.tab_id, initial.tab_id)
            self.assertEqual(left.id, first)
            self.assertGreater(right.x, left.x)
            self.assertEqual(right.y, left.y)
            self.assertTrue(right.focused)
            self.assertEqual(backend.undo_id, right.id)

            backend.feed(demo_keys("ctrl+alt+left", "ctrl+space"))
            self._wait_for_layout(backend, lambda view: view.focused_id == left.id)
            backend.feed(demo_keys("prefix+h", "ctrl+space"))
            three = self._wait_for_layout(backend, lambda view: len(view.panes) == 3)
            below = next(pane for pane in three.panes if pane.focused)
            self.assertEqual(below.x, left.x)
            self.assertGreater(below.y, left.y)
            self.assertEqual(backend.undo_id, below.id)
            self.assertTrue(backend.focus_pane(right.id))
            self.assertEqual(backend.layout().focused_id, right.id)

            self.assertFalse(backend.undo_split(first))
            self.assertFalse(backend.undo_split(right.id))
            self.assertFalse(backend.undo_split("outside"))
            self.assertTrue(backend.undo_split(below.id))
            restored = self._wait_for_layout(backend, lambda view: len(view.panes) == 2)
            self.assertEqual({pane.id for pane in restored.panes}, {left.id, right.id})
            self.assertEqual(backend.undo_id, right.id)

    def test_rapid_splits_can_be_undone_in_order_without_sampling_between_keys(self):
        with HerdrBackend(ROWS) as backend:
            backend.start_layout()
            original = backend.layout().focused_id
            # Both shortcuts enter the PTY before the first post-input layout sample.
            backend.feed(b"\x00v\x00h")
            three = self._wait_for_layout(backend, lambda view: len(view.panes) == 3 and backend.undo_id)
            newest = backend.undo_id
            self.assertEqual(newest, three.focused_id)
            self.assertFalse(backend.undo_split(original))
            self.assertFalse(backend.undo_split("w9:p7"))

            self.assertTrue(backend.undo_split(newest))
            self.assertNotIn(backend.undo_id, (None, newest, original))
            self.assertTrue(backend.undo_split(backend.undo_id))
            self.assertEqual(backend.layout().focused_id, original)
            self.assertEqual(len(backend.layout().panes), 1)
            self.assertIsNone(backend.undo_id)

    def test_session_fixtures_are_focused_and_take_keys(self):
        with HerdrBackend(RIGPAGE_ROWS) as backend:
            world = backend.start_session(LESSON_ACTIONS)
            self.assertEqual([(item.label, [tab.label for tab in item.tabs]) for item in world.workspaces],
                             [("Practice", ["Start"])])
            shapes = {}
            for name in FIXTURES:
                world = backend.apply_fixture(name)
                shapes[name] = (
                    [(item.label, [tab.label for tab in item.tabs]) for item in world.workspaces],
                    world.workspace.label, world.tab_position, len(world.tab.panes),
                )
                # The client takes the next key in the new state (M8): a split lands here.
                before = len(world.tab.panes)
                backend.play("split_vertical")
                self._wait_for_world(backend, lambda view: len(view.tab.panes) == before + 1)
            self.assertEqual(shapes, {
                "one_pane": ([("Practice", ["Start"])], "Practice", 1, 1),
                "right_stack": ([("Practice", ["Start"])], "Practice", 1, 3),
                "four_tabs": ([("Practice", ["Start", "api", "web", "db"])], "Practice", 2, 1),
                "two_tabs": ([("Practice", ["Start", "api"])], "Practice", 1, 1),
                "two_workspaces": ([("Practice", ["Start"]), ("api", ["Start"])], "api", 1, 1),
                "three_workspaces": ([("Practice", ["Start"]), ("api", ["Start"]), ("docs", ["Start"])],
                                     "docs", 1, 1),
            })
            stack = backend.apply_fixture("right_stack")
            focused = stack.pane.rect
            self.assertEqual((focused.right, focused.bottom), (stack.tab.area.right, stack.tab.area.bottom))

    def test_fixture_leaves_prompts_and_confirm_boxes(self):
        with HerdrBackend(RIGPAGE_ROWS) as backend:
            backend.start_session(LESSON_ACTIONS, "two_tabs")
            for opener in ("rename_tab", "rename_workspace", "close_workspace"):
                with self.subTest(opener=opener):
                    backend.play(opener)
                    time.sleep(0.15)
                    world = backend.apply_fixture("one_pane")
                    backend.play("split_horizontal")
                    self._wait_for_world(backend, lambda view: len(view.tab.panes) == 2)
                    self.assertEqual(backend.world().tab.label, "Start")
                    self.assertEqual(len(world.workspaces), 1)

    def test_full_export_keymap_stays_alive(self):
        # Every herdr action named in a full export, herdr's own defaults included:
        # their rows are dropped, so the self-test chord still works (M6).
        actions = {row["value"].replace(" ", "_") for row in FULL_EXPORT} - {"prefix"}
        with HerdrBackend(FULL_EXPORT) as backend:
            known = actions & set(LESSON_ACTIONS | {"help", "detach", "reload_config", "settings",
                                                    "edit_scrollback", "cycle_pane_next", "cycle_pane_previous",
                                                    "workspace_picker", "goto", "toggle_sidebar",
                                                    "open_notification_target", "new_worktree",
                                                    "remote_image_paste", "resize_mode", "rename_pane",
                                                    "move_tab_previous", "move_tab_next"})
            backend.start_session(known)
            self.assertEqual(backend.chords("cycle_pane_next"), ())
            self.assertEqual(backend.chords("close_pane"), ("prefix+x", "alt+esc"))

    def test_unknown_action_and_dead_bindings_raise_clearly(self):
        with HerdrBackend(RIGPAGE_ROWS) as backend:
            with self.assertRaisesRegex(ValueError, "no action named teleport"):
                backend.start_session({"split_vertical", "teleport"})
            with patch.object(HerdrBackend, "feed", lambda self, raw: None):
                with self.assertRaisesRegex(HerdrUnavailable, r"ignored prefix\+v \(split vertical\).*dead"):
                    backend.start_session({"split_vertical"})
            with self.assertRaises(HerdrUnavailable):
                backend.world()

    def _wait_for_world(self, backend, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(view := backend.world()):
                return view
            time.sleep(0.01)
        self.fail(f"Private herdr did not reach the expected state: {view!r}")

    def _wait_for_layout(self, backend, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            view = backend.layout()
            if view is not None and predicate(view):
                return view
            time.sleep(0.01)
        self.fail(f"Herdr layout did not reach the expected shape or focus: {view!r}")

    def _wait_for_native(self, backend, label, previous=b""):
        deadline = time.monotonic() + 2
        rendered = bytearray(previous)
        while time.monotonic() < deadline:
            frames = backend.native_output()
            if frames:
                self.assertTrue(frames.startswith(NATIVE_FRAME_OPEN))
                self.assertTrue(frames.endswith(NATIVE_FRAME_CLOSE))
                rendered.extend(frames)
                if label in painted_rows(rendered):
                    return bytes(rendered)
            time.sleep(0.01)
        self.fail(f"Native Herdr did not paint {label!r} in {len(rendered)} received bytes.")


if __name__ == "__main__":
    unittest.main()
