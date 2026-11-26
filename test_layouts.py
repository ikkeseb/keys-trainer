import unittest
from types import SimpleNamespace

from layouts import BLUEPRINTS, Pane, Rect, assess


ROWS, COLUMNS, RIGHT_STACK, SPLIT_BOTTOM, GRID, CONSOLE = BLUEPRINTS


class BlueprintTests(unittest.TestCase):
    def test_goal_leaves_cover_the_surface_in_the_expected_positions(self):
        self.assertEqual(ROWS.rects, (Rect(0, 0, 1, .5), Rect(0, .5, 1, .5)))
        self.assertEqual(RIGHT_STACK.rects, (
            Rect(0, 0, .5, 1), Rect(.5, 0, .5, .5), Rect(.5, .5, .5, .5),
        ))
        self.assertEqual(SPLIT_BOTTOM.rects, (
            Rect(0, 0, 1, .5), Rect(0, .5, .5, .5), Rect(.5, .5, .5, .5),
        ))
        self.assertEqual(ROWS.directions, {"down"})
        for blueprint in (RIGHT_STACK, SPLIT_BOTTOM, GRID, CONSOLE):
            self.assertEqual(blueprint.directions, {"right", "down"})
            self.assertEqual(sum(rect.width * rect.height for rect in blueprint.rects), 1)
        self.assertEqual([len(b.rects) for b in BLUEPRINTS], [2, 2, 3, 3, 4, 5])

    def test_matching_depends_on_shape_not_pane_identity_origins_or_focus(self):
        for blueprint in BLUEPRINTS:
            with self.subTest(blueprint=blueprint.id):
                panes = [Pane(f"another-{i}", 3 + 108 * x, 7 + 60 * y, 108 * w, 60 * h)
                         for i, (x, y, w, h) in enumerate(reversed(blueprint.rects))]
                solved = assess(blueprint, panes, "not focused")
                self.assertTrue(solved.solved)
                self.assertTrue(solved.recoverable)
                self.assertEqual(solved.matched_ids, {p.id for p in panes})
                self.assertIsNone(solved.direction)

    def test_starting_hint_communicates_the_first_correct_orientation(self):
        for blueprint, axis in ((ROWS, "down"), (RIGHT_STACK, "right"), (SPLIT_BOTTOM, "down")):
            with self.subTest(blueprint=blueprint.id):
                hint = assess(blueprint, [Pane("base", 0, 0, 96, 32)], "base")
                self.assertFalse(hint.solved)
                self.assertTrue(hint.recoverable)
                self.assertEqual((hint.pane_id, hint.direction, hint.region), ("base", axis, Rect(0, 0, 1, 1)))
                self.assertFalse(hint.needs_focus)
                self.assertFalse(hint.matched_ids)

    def test_normalized_backend_coordinates_use_cell_dimensions_for_dividers(self):
        panes = [
            SimpleNamespace(id="editor", x=0, y=0, width=45 / 89, height=1),
            SimpleNamespace(id="stack", x=46 / 89, y=0, width=43 / 89, height=1),
        ]
        hint = assess(RIGHT_STACK, panes, "editor", size=(89, 29))
        self.assertEqual(hint.matched_ids, {"editor"})
        self.assertEqual((hint.pane_id, hint.direction, hint.region), ("stack", "down", Rect(.5, 0, .5, 1)))
        self.assertTrue(hint.needs_focus)

        panes[1:] = [
            SimpleNamespace(id="top", x=46 / 89, y=0, width=43 / 89, height=14 / 29),
            SimpleNamespace(id="bottom", x=46 / 89, y=15 / 29, width=43 / 89, height=14 / 29),
        ]
        self.assertTrue(assess(RIGHT_STACK, panes, "bottom", size=(89, 29)).solved)

    def test_grid_accepts_either_first_cut_and_prefers_work_in_the_focused_pane(self):
        rows = [Pane("top", 0, 0, 100, 20), Pane("bottom", 0, 21, 100, 19)]
        columns = [Pane("left", 0, 0, 50, 40), Pane("right", 51, 0, 49, 40)]
        for panes, axis in ((rows, "right"), (columns, "down")):
            with self.subTest(axis=axis):
                hint = assess(GRID, panes, panes[1].id)
                self.assertTrue(hint.recoverable)
                self.assertEqual(hint.direction, axis)
                self.assertEqual(hint.pane_id, panes[1].id)
                self.assertFalse(hint.needs_focus)

    def test_grid_accepts_local_rounding_in_both_slicing_orders(self):
        rows_first = [
            Pane("tl", 0, 0, 55, 20), Pane("tr", 56, 0, 54, 20),
            Pane("bl", 0, 21, 54, 20), Pane("br", 55, 21, 55, 20),
        ]
        columns_first = [
            Pane("tl", 0, 0, 55, 20), Pane("tr", 56, 0, 54, 21),
            Pane("bl", 0, 21, 55, 20), Pane("br", 56, 22, 54, 19),
        ]
        for panes in (rows_first, columns_first):
            with self.subTest(panes=panes):
                self.assertTrue(assess(GRID, panes, "tr").solved)
        columns_first[1] = Pane("tr", 56, 0, 54, 22)
        columns_first[3] = Pane("br", 56, 23, 54, 18)
        self.assertFalse(assess(GRID, columns_first, "tr").recoverable)

    def test_reversed_cut_extra_panes_and_wrong_orientation_require_restart(self):
        reversed_stack = [Pane("top", 0, 0, 50, 15), Pane("bottom", 0, 16, 50, 15),
                          Pane("editor", 51, 0, 50, 31)]
        wrong_orientation = [Pane("left", 0, 0, 50, 31), Pane("right", 51, 0, 50, 31)]
        cases = ((RIGHT_STACK, reversed_stack), (ROWS, wrong_orientation), (ROWS, reversed_stack))
        for blueprint, panes in cases:
            with self.subTest(blueprint=blueprint.id, panes=panes):
                status = assess(blueprint, panes, panes[0].id)
                self.assertFalse(status.recoverable)
                self.assertFalse(status.solved)
                self.assertIsNone(status.pane_id)

    def test_invalid_gaps_and_overlap_do_not_match(self):
        for bottom_y in (18, 22):
            with self.subTest(bottom_y=bottom_y):
                panes = [Pane("top", 0, 0, 80, 20), Pane("bottom", 0, bottom_y, 80, 20)]
                self.assertFalse(assess(ROWS, panes, "top").recoverable)

    def test_restart_can_highlight_a_goal_pane_that_is_already_correct(self):
        panes = [Pane("wide", 0, 0, 100, 20), Pane("middle", 0, 21, 100, 10),
                 Pane("bottom", 0, 32, 100, 9)]
        status = assess(SPLIT_BOTTOM, panes, "wide")
        self.assertFalse(status.recoverable)
        self.assertEqual(status.matched_ids, {"wide"})
