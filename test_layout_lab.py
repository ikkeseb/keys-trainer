import io
import os
import shutil
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from herdr_backend import HerdrBackend, HerdrLayout, LayoutPane
from layout_lab import _build, available, split_keys
from router import Click, Input
from layout_screen import gallery, puzzle
from layouts import BLUEPRINTS, assess
from practice import History
from screen import Terminal


ROWS = [{"name": "ctrl+space", "value": "prefix"},
        {"name": "prefix+h", "value": "split horizontal"},
        {"name": "prefix+v", "value": "split vertical"}]


class LayoutControlsTests(unittest.TestCase):
    def test_fragmented_mouse_report_and_following_shortcut(self):
        reader = Input()
        self.assertEqual(list(reader.events(b"\x1b[<0;4")), [])
        events = list(reader.events(b"1;9M\x1b[<0;41;9m\x00v"))
        self.assertEqual(events, [Click(41, 9), b"\x00", b"v"])
        self.assertEqual(list(reader.events(b"\x1b[1;7C")), [b"\x1b[1;7C"])

    def test_guided_split_uses_encodable_alternate(self):
        labels, keys, _ = split_keys([ROWS[0], {"name": "super+h", "value": "split horizontal"}, ROWS[1]])
        self.assertEqual(keys["down"], b"\x00h")
        self.assertIn("Space", labels["down"])

    def test_native_wrong_split_reports_undo(self):
        panes = (LayoutPane("l",0,0,.5,1,False), LayoutPane("r",.5,0,.5,1,True))
        source, ui = Mock(), Mock()
        source.layout.return_value = HerdrLayout("t", "r", panes, 80, 24)
        ui.read.side_effect = [b"t", b"q"]
        with patch("layout_lab.NativeView") as view, patch("layout_lab.puzzle", return_value=[]):
            _build(ui, source, BLUEPRINTS[0], Mock(), *split_keys(ROWS), Input())
        title, _ = view.return_value.__enter__.return_value.show.call_args.args
        self.assertIn("Wrong split", title)
        self.assertIn("u Undo", title)

    def test_control_follows_rebind_including_prefix_tail(self):
        rows = [*ROWS, {"name": "prefix+u", "value": "split horizontal"}]
        _, _, controls = split_keys(rows)
        self.assertNotIn("u", controls)
        self.assertEqual([bp.id for bp in available([ROWS[0], ROWS[2]])], ["twin-columns"])

    def test_assisted_build_preserves_personal_best_and_drill_schedule(self):
        history = History(":memory:")
        self.addCleanup(history.close)
        history.finish_layout("grid", 5, True)
        self.assertEqual(history.layout_records(), {"grid": None})
        history.finish_layout("grid", 4, False)
        history.finish_layout("grid", 1, True)
        history.finish_layout("grid", 10, False)
        self.assertEqual(history.layout_records()["grid"], 4)
        self.assertEqual(history.progress(), {})

    def test_small_gallery_and_puzzle_preserve_preview_and_click_targets(self):
        ui = Terminal()
        ui.size, ui.cache = None, {}
        blueprint = BLUEPRINTS[0]
        layout = HerdrLayout("t", "p", (LayoutPane("p", 0, 0, 1, 1, True),), 80, 16)
        assessment = assess(blueprint, layout.panes, "p", size=(80, 16))
        with patch("layout_screen.shutil.get_terminal_size", return_value=os.terminal_size((40,12))), redirect_stdout(io.StringIO()):
            gallery(ui, [blueprint], 0, {"unavailable": 1.5})
            rendered = "\n".join(ui.cache.values())
            for token in ("0 / 1 built", blueprint.title, "┌", "├", "q Back"):
                self.assertIn(token, rendered)
            hits = puzzle(ui, blueprint, layout, assessment, "Split the focused pane.", ("?","u","Tab","q","r","g","t"), 1)
            self.assertTrue(any(h.item == "p" and h.top < h.bottom for h in hits))
            self.assertIn("●1", "\n".join(ui.cache.values()))

    def test_gallery_and_puzzle_copy_comes_from_strings(self):
        import strings

        ui = Terminal()
        ui.size, ui.cache = None, {}
        blueprint = BLUEPRINTS[0]
        layout = HerdrLayout("t", "p", (LayoutPane("p", 0, 0, 1, 1, True),), 80, 16)
        assessment = assess(blueprint, layout.panes, "p", size=(80, 16))
        controls = ("?", "u", "Tab", "q", "r", "g", "t")
        marked = {key: f"<{key}>" + value for key, value in strings.LAYOUTS.items()}
        with patch.dict(strings.LAYOUTS, marked), redirect_stdout(io.StringIO()):
            for size, keys in (((100, 30), ("brand", "choose", "built", "panes", "footer")),
                               ((40, 12), ("brand_short", "built", "footer_short")),
                               ((20, 8), ("too_small", "back"))):
                with self.subTest(view="gallery", size=size), \
                        patch("layout_screen.shutil.get_terminal_size", return_value=os.terminal_size(size)):
                    gallery(ui, [blueprint], 0, {})
                    rendered = "\n".join(ui.cache.values())
                    for key in keys:
                        self.assertIn(f"<{key}>", rendered)
            for size, keys in (((100, 30), ("brand", "target", "yours", "matched", "seconds", "footer_wide", "play")),
                               ((60, 16), ("footer_mid",)), ((44, 16), ("footer_narrow",)),
                               ((20, 8), ("too_small", "back_layouts"))):
                with self.subTest(view="puzzle", size=size), \
                        patch("layout_screen.shutil.get_terminal_size", return_value=os.terminal_size(size)):
                    puzzle(ui, blueprint, layout, assessment, "", controls, 1, guide="1")
                    rendered = "\n".join(ui.cache.values())
                    for key in keys:
                        self.assertIn(f"<{key}>", rendered)
            with patch("layout_screen.shutil.get_terminal_size", return_value=os.terminal_size((100, 30))):
                puzzle(ui, blueprint, layout, assessment, "", controls, 1, solved=True)
                self.assertIn("<footer_solved>", "\n".join(ui.cache.values()))

    def test_solved_layout_is_frozen_across_view_changes(self):
        panes = (LayoutPane("top",0,0,1,.5,False), LayoutPane("bottom",0,.5,1,.5,True))
        source, ui, history = Mock(), Mock(), Mock()
        source.layout.side_effect = [HerdrLayout("t","bottom",panes,80,24), None]
        history.layout_records.return_value = {}
        ui.read.side_effect = [b"t", b"t", b"q"]
        labels, groups, controls = split_keys(ROWS)
        with patch("layout_lab.NativeView"), patch("layout_lab.puzzle", return_value=[]):
            self.assertEqual(_build(ui, source, BLUEPRINTS[0], history, labels, groups, controls, Input()), "gallery")
        history.finish_layout.assert_called_once()
        source.feed.assert_not_called()
        self.assertEqual(source.layout.call_count, 1)

    def test_two_tab_keys_in_same_read_cycle_through_both_panes(self):
        layout = HerdrLayout("t", "left", (LayoutPane("left",0,0,.5,1,True),
                                           LayoutPane("right",.5,0,.5,1,False)), 80, 16)
        source = Mock()
        source.layout.return_value = layout
        ui = Mock()
        ui.read.side_effect = [b"\t\tq"]
        labels, groups, controls = split_keys(ROWS)
        with patch("layout_lab.puzzle", return_value=[]):
            value = _build(ui, source, BLUEPRINTS[2], Mock(), labels, groups, controls, Input())
        self.assertEqual(value, "gallery")
        self.assertEqual([call.args[0] for call in source.focus_pane.call_args_list], ["right","left"])


@unittest.skipUnless(os.getenv("KEYS_TRAINER_TEST_HERDR") == "1" and shutil.which("herdr"),
                     "opt in to private herdr integration")
class PlayableBlueprintTests(unittest.TestCase):
    def test_all_blueprints_can_be_built_in_the_real_private_client(self):
        _, keys, _ = split_keys(ROWS)
        with HerdrBackend(ROWS) as source:
            for bp in BLUEPRINTS:
                with self.subTest(bp=bp.id):
                    source.start_layout()
                    native_size = {"grid": (83, 19), "console": (119, 31)}.get(bp.id)
                    for _ in range(len(bp.rects)):
                        layout = source.layout()
                        if native_size and len(layout.panes) == 2:
                            previous = (layout.width, layout.height)
                            source.begin_native(*native_size)
                            deadline = time.monotonic() + 2
                            while (layout.width, layout.height) == previous and time.monotonic() < deadline:
                                time.sleep(.02)
                                layout = source.layout()
                            self.assertNotEqual((layout.width, layout.height), previous,
                                                f"{bp.id}: private client did not resize to {native_size}")
                            native_size = None
                        status = assess(bp, layout.panes, layout.focused_id, size=(layout.width, layout.height))
                        if status.solved:
                            break
                        self.assertTrue(status.recoverable, f"{bp.id}: {layout!r}")
                        if status.needs_focus:
                            self.assertTrue(source.focus_pane(status.pane_id))
                        count = len(layout.panes)
                        source.feed(keys[status.direction])
                        deadline = time.monotonic() + 2
                        updated = source.layout()
                        while len(updated.panes) == count and time.monotonic() < deadline:
                            time.sleep(.02)
                            updated = source.layout()
                        self.assertEqual(len(updated.panes), count + 1,
                                         f"{bp.id}: expected one new pane after {status.direction}: {updated!r}")
                    self.assertTrue(status.solved, f"{bp.id}: {layout!r}")
                    self.assertEqual(len(status.matched_ids), len(bp.rects))
