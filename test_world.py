import unittest

from test_checks import load_recorded
from world import HerdrWorld, World, YaziWorld, describe


def rect(x, y, width, height):
    return {"x": x, "y": y, "width": width, "height": height}


class HerdrWorldTests(unittest.TestCase):
    def test_snapshot_order_gives_positions_not_numbers(self):
        # After a tab move herdr keeps each tab's number; the list order is the tab bar.
        world = HerdrWorld.from_snapshot({
            "focused_workspace_id": "w1", "focused_tab_id": "w1:t3", "focused_pane_id": "w1:p3",
            "workspaces": [{"workspace_id": "w1", "number": 1, "label": "Practice", "active_tab_id": "w1:t3"}],
            "tabs": [
                {"tab_id": "w1:t1", "workspace_id": "w1", "number": 1, "label": "Start"},
                {"tab_id": "w1:t3", "workspace_id": "w1", "number": 3, "label": "web"},
                {"tab_id": "w1:t2", "workspace_id": "w1", "number": 2, "label": "api"},
            ],
            "layouts": [{
                "tab_id": "w1:t3", "zoomed": True, "area": rect(0, 0, 74, 27), "focused_pane_id": "w1:p3",
                "panes": [{"pane_id": "w1:p3", "focused": True, "rect": rect(0, 0, 74, 27)}],
                "splits": [],
            }],
        })
        self.assertEqual(world.tab_position, 2)
        self.assertEqual(world.workspace.label, "Practice")
        self.assertEqual(world.tab.label, "web")
        self.assertTrue(world.tab.zoomed)
        self.assertEqual(world.pane.rect.right, 74)
        # A tab without a layout entry is still listed, with no panes.
        self.assertEqual(world.find_tab("w1:t2").panes, ())
        self.assertEqual(world.workspace_of("w1:t2").id, "w1")

    def test_recorded_worlds_are_comparable_and_hashable(self):
        recorded = load_recorded()
        _, (start,), history = recorded["resize_right_down"]["solution"]
        self.assertNotEqual(start, history[0])
        self.assertEqual(len({start, start, *history}), 1 + len(set(history)))
        split = history[0].herdr.tab.splits
        self.assertEqual([(item.direction, round(item.ratio, 2)) for item in split], [("right", 0.55), ("down", 0.5)])

    def test_yazi_world_from_hook_event(self):
        world = YaziWorld.from_event({
            "cwd": "/tmp/Practice", "hovered": "notes.txt", "entries": ["Projects", "notes.txt"],
            "selected": ["notes.txt"], "show_hidden": True, "filter": "no", "layer": "input",
            "tab_count": 2, "tab_index": 1, "yanked": 1, "cut": True, "visual": False,
        })
        self.assertEqual(world.entries, ("Projects", "notes.txt"))
        self.assertEqual(world.selected, frozenset({"notes.txt"}))
        self.assertEqual((world.layer, world.tab_count, world.yanked, world.cut), ("input", 2, 1, True))
        self.assertEqual(YaziWorld.from_event({"hovered": ""}), YaziWorld())


class DescribeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.recorded = load_recorded()

    def first_change(self, case, run="solution", step=0):
        _, (start,), history = self.recorded[case][run]
        before = start if step == 0 else history[step - 1]
        return describe(before, history[step])

    def test_each_kind_of_change_has_one_sentence(self):
        for case, run, step, expected in (
            ("split_down", "solution", 0, "The pane split into two rows."),
            ("split_right", "solution", 0, "The pane split into two columns."),
            ("close_pane", "solution", 0, "A pane closed."),
            ("focus_left", "solution", 0, "Focus moved left."),
            ("focus_left", "decoy", 0, "Focus moved up."),
            ("walk_right_down", "solution", 0, "Focus moved right."),
            ("walk_right_down", "solution", 1, "Focus moved down."),
            ("zoom", "solution", 0, "The pane is zoomed."),
            ("zoom", "solution", 1, "Zoom is off."),
            ("resize_right_down", "solution", 0, "The pane got narrower."),
            ("resize_right_down", "solution", 2, "The pane got shorter."),
            ("resize_left_up", "solution", 0, "The pane got wider."),
            ("resize_left_up", "solution", 2, "The pane got taller."),
            ("new_tab", "solution", 0, "A new tab opened."),
            ("new_tab", "decoy", 0, "You switched to tab 3."),
            ("rename_tab", "solution", 0, "The tab is now called “logs”."),
            ("rename_tab", "decoy", 0, "The tab is now called “apilogs”."),
            ("new_workspace", "solution", 0, "A new workspace opened."),
            ("previous_next_workspace", "solution", 0, "You switched workspace."),
            ("rename_workspace", "solution", 0, "The workspace is now called “web”."),
            ("build_workspace", "solution", 3, "A workspace closed."),
        ):
            with self.subTest(case=case, run=run, step=step):
                self.assertEqual(self.first_change(case, run, step), expected)

    def test_nothing_relevant_changed(self):
        _, (start,), _ = self.recorded["zoom"]["solution"]
        self.assertIsNone(describe(start, start))
        self.assertIsNone(describe(World(), World()))
        self.assertIsNone(describe(World(), start))

    def test_yazi_changes(self):
        base = YaziWorld(cwd="/tmp/Practice", hovered="draft.txt")
        for after, expected in (
            (YaziWorld(cwd="/tmp/Practice/Projects"), "You are now in the Projects folder."),
            (YaziWorld(cwd="/tmp/Practice", hovered="notes.txt"), "The hovered file is now notes.txt."),
            (YaziWorld(cwd="/tmp/Practice", hovered="draft.txt", show_hidden=True), "Hidden files are shown."),
            (YaziWorld(cwd="/tmp/Practice", hovered="draft.txt", selected=frozenset({"draft.txt"})),
             "The selection changed."),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(describe(World(yazi=base), World(yazi=after)), expected)


if __name__ == "__main__":
    unittest.main()
