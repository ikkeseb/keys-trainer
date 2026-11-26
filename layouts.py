"""Geometry and constructive hints for pane-layout puzzles."""

from dataclasses import dataclass
from math import isfinite
from typing import Iterable, Literal, NamedTuple


Direction = Literal["right", "down"]
EPSILON = 1e-7


class Rect(NamedTuple):
    x: float
    y: float
    width: float
    height: float


UNIT_RECT = Rect(0, 0, 1, 1)


@dataclass(frozen=True)
class Pane:
    id: str
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class Split:
    direction: Direction
    first: "Split | None" = None
    second: "Split | None" = None


@dataclass(frozen=True)
class Blueprint:
    id: str
    title: str
    tree: Split

    @property
    def rects(self) -> tuple[Rect, ...]:
        """Leaf rectangles in blueprint coordinates, with an origin of (0, 0)."""
        return _leaves(self.tree, UNIT_RECT)

    @property
    def directions(self) -> frozenset[Direction]:
        return _directions(self.tree)


BLUEPRINTS = (
    Blueprint("twin-rows", "Twin rows", Split("down")),
    Blueprint("twin-columns", "Twin columns", Split("right")),
    Blueprint("right-stack", "Editor with a right stack", Split("right", None, Split("down"))),
    Blueprint("split-bottom", "Wide top, split bottom", Split("down", None, Split("right"))),
    Blueprint("grid", "Four-pane grid", Split("down", Split("right"), Split("right"))),
    Blueprint("console", "Five-pane console", Split("right", Split("down", Split("right")), Split("down"))),
)


@dataclass(frozen=True)
class Assessment:
    solved: bool
    recoverable: bool
    matched_ids: frozenset[str] = frozenset()
    pane_id: str | None = None
    direction: Direction | None = None
    region: Rect | None = None
    needs_focus: bool = False


def _halves(rect: Rect, direction: Direction) -> tuple[Rect, Rect]:
    x, y, width, height = rect
    if direction == "right":
        half = width / 2
        return Rect(x, y, half, height), Rect(x + half, y, half, height)
    half = height / 2
    return Rect(x, y, width, half), Rect(x, y + half, width, half)


def _leaves(tree: Split | None, rect: Rect) -> tuple[Rect, ...]:
    if tree is None:
        return (rect,)
    first, second = _halves(rect, tree.direction)
    return _leaves(tree.first, first) + _leaves(tree.second, second)


def _directions(tree: Split | None) -> frozenset[Direction]:
    if tree is None:
        return frozenset()
    return frozenset({tree.direction}) | _directions(tree.first) | _directions(tree.second)


def _contains(parent: Rect, child: Rect) -> bool:
    return (parent.x <= child.x and parent.y <= child.y
            and parent.x + parent.width >= child.x + child.width
            and parent.y + parent.height >= child.y + child.height)


def _goal_cuts(region: Rect, goals: tuple[Rect, ...]):
    """Yield midpoint cuts that leave every target pane whole."""
    for direction in ("right", "down"):
        first, second = _halves(region, direction)
        first_goals = tuple(goal for goal in goals if _contains(first, goal))
        second_goals = tuple(goal for goal in goals if _contains(second, goal))
        if first_goals and second_goals and len(first_goals) + len(second_goals) == len(goals):
            yield direction, (first, first_goals), (second, second_goals)


def _envelope(rects: Iterable[Rect]) -> Rect:
    rects = tuple(rects)
    x = min(rect.x for rect in rects)
    y = min(rect.y for rect in rects)
    return Rect(x, y, max(rect.x + rect.width for rect in rects) - x,
                max(rect.y + rect.height for rect in rects) - y)


def _pane_cuts(panes: tuple[Pane, ...], direction: Direction, cell: float):
    axis, span, cross, depth = (0, 2, 1, 3) if direction == "right" else (1, 3, 0, 2)
    ordered = tuple(sorted(panes, key=lambda pane: pane.x if axis == 0 else pane.y))
    for index in range(1, len(ordered)):
        first, second = ordered[:index], ordered[index:]
        a = _envelope(Rect(p.x, p.y, p.width, p.height) for p in first)
        b = _envelope(Rect(p.x, p.y, p.width, p.height) for p in second)
        gap = b[axis] - a[axis] - a[span]
        # A split consumes up to one divider cell. Its center may round by a cell.
        if (-EPSILON <= gap <= cell + EPSILON
                and abs(a[span] - b[span]) <= 2 * cell + EPSILON
                and abs(a[cross] - b[cross]) <= EPSILON
                and abs(a[depth] - b[depth]) <= EPSILON):
            yield first, second


def _fit(region: Rect, goals: tuple[Rect, ...], panes: tuple[Pane, ...], cell: float):
    """Find a subdivision that represents the observed panes and can reach the goal."""
    if len(panes) > len(goals):
        return None
    if len(panes) == 1:
        pane = panes[0]
        if len(goals) == 1:
            return (pane.id,), ()
        hints = tuple((pane.id, direction, region) for direction, _, _ in _goal_cuts(region, goals))
        return ((), hints) if hints else None

    for direction, first, second in _goal_cuts(region, goals):
        for first_panes, second_panes in _pane_cuts(panes, direction, cell):
            fitted_first = _fit(*first, first_panes, cell)
            if fitted_first is None:
                continue
            fitted_second = _fit(*second, second_panes, cell)
            if fitted_second is not None:
                return fitted_first[0] + fitted_second[0], fitted_first[1] + fitted_second[1]
    return None


def _matching_leaves(panes: tuple[Pane, ...], goals: tuple[Rect, ...], envelope: Rect, cell: float):
    projected = tuple(
        (envelope.x + goal.x * envelope.width, envelope.y + goal.y * envelope.height,
         envelope.x + (goal.x + goal.width) * envelope.width,
         envelope.y + (goal.y + goal.height) * envelope.height)
        for goal in goals
    )
    matched = set()
    for pane in panes:
        edges = (pane.x, pane.y, pane.x + pane.width, pane.y + pane.height)
        if any(all(abs(actual - goal) <= cell + EPSILON for actual, goal in zip(edges, reference))
               for reference in projected):
            matched.add(pane.id)
    return frozenset(matched)


def assess(blueprint: Blueprint, panes: Iterable[Pane], focused_id: str | None,
           *, size: tuple[int, int] | None = None) -> Assessment:
    """Compare pane geometry and identify a split that can complete the layout.

    Supply the terminal cell dimensions as `size` for normalized `panes` with
    dividers. Raw terminal-cell coordinates and exact normalized rectangles can
    be compared without `size`.
    """
    panes = tuple(panes)
    if not panes or any(pane.width <= 0 or pane.height <= 0 or not all(isfinite(value) for value in (
            pane.x, pane.y, pane.width, pane.height)) for pane in panes):
        return Assessment(False, False)
    if len({pane.id for pane in panes}) != len(panes):
        return Assessment(False, False)

    if size is not None:
        width, height = size
        if width <= 0 or height <= 0:
            return Assessment(False, False)
        panes = tuple(Pane(p.id, p.x * width, p.y * height, p.width * width, p.height * height) for p in panes)
    envelope = _envelope(Rect(p.x, p.y, p.width, p.height) for p in panes)
    normalized = size is None and envelope.width <= 1 + EPSILON and envelope.height <= 1 + EPSILON
    cell = 0.0 if normalized else 1.0
    goals = blueprint.rects
    fitted = _fit(UNIT_RECT, goals, panes, cell)
    if fitted is None:
        return Assessment(False, False, _matching_leaves(panes, goals, envelope, cell))
    matches, hints = fitted
    if not hints:
        return Assessment(True, True, frozenset(matches))
    pane_id, direction, region = next((hint for hint in hints if hint[0] == focused_id), hints[0])
    return Assessment(False, True, frozenset(matches), pane_id, direction, region, pane_id != focused_id)
