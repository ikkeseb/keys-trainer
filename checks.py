"""World-state checks for scenarios: a registry of pure functions over observed worlds.

A scenario's checks and needs are plain data, as read from the curriculum TOML:

    checks = [{ check = "focus_moved", side = "right" },
              { check = "focus_moved", side = "down" }]
    needs = [{ check = "has_neighbour", side = "left" }]

Checks latch in order (mode "latch", the default): each one must pass on a world
observed after the one where the previous check latched, relative to that world.
A "hold" check must pass on the latest world once every latch has latched.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import layouts
from world import HerdrWorld, TabState, World, adjacent_side, bounding_split, split_side


RESIZE_STEP = 0.05  # herdr moves a divider by this ratio per resize press (measured, 0.9.5)
SIDES = ("left", "right", "up", "down")
EDGES = {"left", "right", "top", "bottom"}
BLUEPRINTS = {blueprint.id: blueprint for blueprint in layouts.BLUEPRINTS}


@dataclass(frozen=True)
class Context:
    """What a check sees. `ref` is the world where the previous check latched."""

    start: World
    ref: World
    now: World
    trail: tuple[World, ...]  # worlds before the scenario, then up to and including ref


@dataclass(frozen=True)
class Progress:
    latched: int  # checks satisfied so far: latches in order, then holds once all latched
    done: bool
    total: int


@dataclass(frozen=True)
class _Param:
    validate: Callable[[Any], bool]
    default: Any = ...
    hint: str = ""


def _choice(*values, default=...):
    return _Param(lambda value: value in values, default, " | ".join(map(str, values)))


def _int(low, high, default=...):
    return _Param(lambda value: type(value) is int and low <= value <= high, default, f"integer {low}..{high}")


def _text(default=...):
    return _Param(lambda value: isinstance(value, str) and 0 < len(value) <= 40, default, "text")


def _flag(default=...):
    return _Param(lambda value: type(value) is bool, default, "true | false")


def _position(default=...):
    return _Param(
        lambda value: isinstance(value, str) and bool(parts := set(value.split("-"))) and parts <= EDGES
        and not {"left", "right"} <= parts and not {"top", "bottom"} <= parts,
        default, "left | top-left | bottom-right | ...",
    )


@dataclass(frozen=True)
class _Entry:
    function: Callable
    params: dict[str, _Param]
    tool: str  # the World field the function reads; a world without it never passes


CHECKS: dict[str, _Entry] = {}
NEEDS: dict[str, _Entry] = {}
# Checks that read only the latest world. They also serve as needs.
STATE_CHECKS = frozenset({
    "focus_at", "blueprint", "tab_at", "tab_label", "workspace_label", "workspace_exists", "workspace_gone",
})


def _register(table, name, tool, params):
    def wrap(function):
        table[name] = _Entry(function, params, tool)
        return function
    return wrap


def check(name, *, tool="herdr", **params):
    return _register(CHECKS, name, tool, params)


def need(name, *, tool="herdr", **params):
    return _register(NEEDS, name, tool, params)


def _resolve(table, item):
    kind, key = ("check" if table is CHECKS else "need"), "check"
    if not isinstance(item, Mapping) or not isinstance(item.get(key), str):
        raise ValueError(f"Expected a table with a {key!r} name, got {item!r}.")
    name = item[key]
    if table is NEEDS and name in STATE_CHECKS:
        entry = CHECKS[name]
        function = entry.function
        entry = _Entry(lambda world, earlier, **params: function(Context(world, world, world, earlier), **params),
                       entry.params, entry.tool)
    elif name in table:
        entry = table[name]
    else:
        raise ValueError(f"Unknown {kind} {name!r}.")
    params = {}
    extra = set(item) - set(entry.params) - {key, "mode"}
    if extra:
        raise ValueError(f"{kind} {name!r} has no parameter {', '.join(sorted(extra))}.")
    for param, spec in entry.params.items():
        if param in item:
            if not spec.validate(item[param]):
                raise ValueError(f"{kind} {name!r}: {param} must be {spec.hint}, got {item[param]!r}.")
            params[param] = item[param]
        elif spec.default is ...:
            raise ValueError(f"{kind} {name!r} needs {param} ({spec.hint}).")
        else:
            params[param] = spec.default
    mode = item.get("mode", "latch")
    if table is CHECKS and mode not in ("latch", "hold"):
        raise ValueError(f"check {name!r}: mode must be latch | hold, got {mode!r}.")
    if table is NEEDS and "mode" in item:
        raise ValueError(f"need {name!r} has no mode.")
    tool = entry.tool
    if table is CHECKS:
        def run(context, **params):
            return getattr(context.now, tool) is not None and entry.function(context, **params)
    else:
        def run(world, earlier, **params):
            return getattr(world, tool) is not None and entry.function(world, earlier, **params)
    return run, params, mode


def validate(checks: Sequence[Mapping] = (), needs: Sequence[Mapping] = ()) -> None:
    """Raise ValueError for an unknown name, a bad parameter or a bad mode."""
    for item in checks:
        _resolve(CHECKS, item)
    for item in needs:
        _resolve(NEEDS, item)


def evaluate(checks: Sequence[Mapping], start: World, history: Sequence[World],
             earlier: Sequence[World] = ()) -> Progress:
    """Score a scenario.

    `history` holds the worlds observed after `start`, oldest first. `earlier`
    holds worlds observed before the scenario began (the trainer's focus history).
    """
    resolved = [_resolve(CHECKS, item) for item in checks]
    worlds = (start, *history)
    earlier = tuple(earlier)
    ref = latched = 0
    for function, params, mode in resolved:
        if mode == "hold":
            continue
        found = next((
            index for index in range(ref + 1, len(worlds))
            if function(Context(start, worlds[ref], worlds[index], earlier + worlds[:ref + 1]), **params)
        ), None)
        if found is None:
            return Progress(latched, False, len(resolved))
        latched, ref = latched + 1, found
    holds = [(function, params) for function, params, mode in resolved if mode == "hold"]
    if len(worlds) == 1:
        # Nothing happened yet: a start that already holds is not a solution.
        return Progress(latched, False, len(resolved))
    context = Context(start, worlds[ref], worlds[-1], earlier + worlds[:ref + 1])
    holding = sum(1 for function, params in holds if function(context, **params))
    return Progress(latched + holding, holding == len(holds), len(resolved))


def unmet(needs: Sequence[Mapping], world: World, earlier: Sequence[World] = ()) -> list[Mapping]:
    """Return the needs the world does not meet; an empty list means the scenario can start here."""
    failed = []
    for item in needs:
        function, params, _ = _resolve(NEEDS, item)
        if not function(world, tuple(earlier), **params):
            failed.append(item)
    return failed


# Helpers over herdr worlds.

def _herdr(world: World) -> HerdrWorld:
    return world.herdr if world.herdr is not None else HerdrWorld(())


def _same_tab(context: Context) -> tuple[TabState, TabState] | None:
    """The focused tab in `ref` and the same tab in `now`, when focus stayed in it."""
    ref, now = _herdr(context.ref), _herdr(context.now)
    if ref.tab is None or now.focused_tab_id != ref.focused_tab_id or now.tab is None:
        return None
    return ref.tab, now.tab


def _stable_tab(context: Context) -> tuple[TabState, TabState] | None:
    """As _same_tab, and no pane was added or removed."""
    tabs = _same_tab(context)
    return tabs if tabs and tabs[0].pane_ids == tabs[1].pane_ids else None


def _touches(tab: TabState, pane_id: str | None, position: str) -> bool:
    pane = tab.pane(pane_id)
    if pane is None or tab.area is None:
        return False
    rect, area = pane.rect, tab.area
    edges = {"left": rect.x <= area.x, "top": rect.y <= area.y,
             "right": rect.right >= area.right, "bottom": rect.bottom >= area.bottom}
    return all(edges[edge] for edge in position.split("-"))


def _neighbour(tab: TabState, pane_id: str | None, side: str) -> bool:
    pane = tab.pane(pane_id)
    return pane is not None and any(
        adjacent_side(pane.rect, other.rect) == side for other in tab.panes if other.id != pane.id
    )


def _previous_focus(trail: Sequence[World], tab: TabState) -> str | None:
    """The pane focused in this tab before its current one, if it still exists."""
    previous = next((
        world.herdr.focused_pane_id for world in reversed(trail)
        if world.herdr and world.herdr.focused_tab_id == tab.id
        and world.herdr.focused_pane_id not in (None, tab.focused_pane_id)
    ), None)
    return previous if previous in tab.pane_ids else None


def _step(items: tuple[str, ...], current: str | None, step: int) -> str | None:
    if current not in items:
        return None
    return items[(items.index(current) + step) % len(items)]


def _assess(tab: TabState | None, blueprint: str) -> bool:
    if tab is None or not tab.panes:
        return False
    panes = [layouts.Pane(pane.id, pane.rect.x, pane.rect.y, pane.rect.width, pane.rect.height)
             for pane in tab.panes]
    return layouts.assess(BLUEPRINTS[blueprint], panes, tab.focused_pane_id).solved


# Panes.

@check("split", direction=_choice("down", "right"))
def split(context, direction):
    """One new pane, the second half of the pane that was focused: below it or to its right."""
    tabs = _same_tab(context)
    if tabs is None:
        return False
    before, after = tabs
    added = after.pane_ids - before.pane_ids
    return (len(after.panes) == len(before.panes) + 1 and len(added) == 1
            and split_side(before, after, next(iter(added))) == direction)


@check("pane_closed")
def pane_closed(context):
    """The pane that was focused is gone; every other pane of its tab remains."""
    ref, now = _herdr(context.ref), _herdr(context.now)
    before = ref.tab
    after = now.find_tab(ref.focused_tab_id)
    if before is None or after is None:
        return False
    return after.pane_ids == before.pane_ids - {ref.focused_pane_id} and len(after.panes) == len(before.panes) - 1


@check("focus_moved", side=_choice(*SIDES))
def focus_moved(context, side):
    """Focus went to a pane next to the one that was focused, on that side. No pane added or removed."""
    tabs = _stable_tab(context)
    if tabs is None:
        return False
    before, after = tabs
    origin, target = before.pane(before.focused_pane_id), after.pane(after.focused_pane_id)
    return (origin is not None and target is not None and target.id != origin.id
            and adjacent_side(after.pane(origin.id).rect, target.rect) == side)


@check("last_pane")
def last_pane(context):
    """Focus went back to the pane focused before the latest move, by the trainer's history."""
    tabs = _stable_tab(context)
    if tabs is None:
        return False
    before, after = tabs
    previous = _previous_focus(context.trail, before)
    return previous is not None and after.focused_pane_id == previous


@check("zoomed", on=_flag())
def zoomed(context, on):
    """The tab's zoom is on (or off) with the same pane focused and the same panes."""
    tabs = _stable_tab(context)
    if tabs is None:
        return False
    before, after = tabs
    return after.zoomed is on and after.focused_pane_id == before.focused_pane_id


@check("resized", side=_choice(*SIDES), steps=_int(1, 9, 2))
def resized(context, side, steps):
    """The divider bounding the focused pane moved toward `side` by at least `steps` presses."""
    tabs = _stable_tab(context)
    if tabs is None:
        return False
    before, after = tabs
    if after.focused_pane_id != before.focused_pane_id:
        return False
    direction = "right" if side in ("left", "right") else "down"
    old = bounding_split(before, before.pane(before.focused_pane_id), direction)
    new = bounding_split(after, after.pane(after.focused_pane_id), direction)
    if old is None or new is None:
        return False
    moved = (new.ratio - old.ratio) * (1 if side in ("right", "down") else -1)
    return moved >= steps * RESIZE_STEP - 1e-3


@check("focus_at", position=_position())
def focus_at(context, position):
    """The focused pane touches these edges of its tab ("left", "top-right", ...)."""
    now = _herdr(context.now)
    return now.tab is not None and _touches(now.tab, now.focused_pane_id, position)


@check("blueprint", blueprint=_choice(*BLUEPRINTS), tab_label=_text(None), new=_flag(False))
def blueprint(context, blueprint, tab_label, new):
    """The focused tab (or a tab with this label in the focused workspace) solves a blueprint.

    With `new`, only a labelled tab that did not exist at the start counts.
    """
    if tab_label is None:
        return _assess(_herdr(context.now).tab, blueprint)
    return any(_assess(tab, blueprint) for tab in labelled_tabs(context.start, context.now, tab_label, new))


def added_tabs(start: World, now: World) -> tuple[TabState, ...] | None:
    """The tabs of the focused workspace that did not exist at the start, in tab-bar order;
    None unless focus is in the start's workspace and every tab of the start is still there."""
    before, after = _herdr(start), _herdr(now)
    if before.workspace is None or after.workspace is None or after.focused_workspace_id != before.focused_workspace_id:
        return None
    old = set(before.workspace.tab_ids)
    if not old <= set(after.workspace.tab_ids):
        return None
    return tuple(tab for tab in after.workspace.tabs if tab.id not in old)


def labelled_tabs(start: World, now: World, label: str, new: bool = False) -> tuple[TabState, ...]:
    """Tabs of the focused workspace with this label; with `new`, only those added since the start."""
    workspace = _herdr(now).workspace
    tabs = (added_tabs(start, now) or ()) if new else (workspace.tabs if workspace else ())
    return tuple(tab for tab in tabs if tab.label == label)


# Tabs.

@check("new_tab")
def new_tab(context):
    """One new tab in the same workspace, and it is focused."""
    ref, now = _herdr(context.ref), _herdr(context.now)
    if ref.workspace is None or now.focused_workspace_id != ref.focused_workspace_id or now.workspace is None:
        return False
    before, after = set(ref.workspace.tab_ids), set(now.workspace.tab_ids)
    return len(after) == len(before) + 1 and now.focused_tab_id in after - before


@check("tab_added", label=_text(None))
def tab_added(context, label):
    """Exactly one tab more than at the start, in the same workspace; with a label, the new tab has it.
    A build's «new tab called build» (renaming an old tab is not a new one)."""
    added = added_tabs(context.start, context.now)
    return added is not None and len(added) == 1 and (label is None or added[0].label == label)


@check("tab_step", step=_choice(1, -1))
def tab_step(context, step):
    """Focus went to the next (1) or previous (-1) tab, wrapping; no tab added or removed."""
    ref, now = _herdr(context.ref), _herdr(context.now)
    if ref.workspace is None or now.workspace is None or now.workspace.id != ref.workspace.id:
        return False
    tabs = ref.workspace.tab_ids
    return (len(tabs) > 1 and now.workspace.tab_ids == tabs
            and now.focused_tab_id == _step(tabs, ref.focused_tab_id, step))


@check("tab_at", position=_int(1, 9))
def tab_at(context, position):
    """The focused tab is at this 1-based position in its workspace."""
    return _herdr(context.now).tab_position == position


@check("tab_label", label=_text(), focused=_flag(True))
def tab_label(context, label, focused):
    """The focused tab has this label (or, with focused = false, any tab of the focused workspace)."""
    now = _herdr(context.now)
    if focused:
        return now.tab is not None and now.tab.label == label
    return now.workspace is not None and any(tab.label == label for tab in now.workspace.tabs)


# Workspaces.

@check("new_workspace")
def new_workspace(context):
    """One new workspace, and it is focused."""
    ref, now = _herdr(context.ref), _herdr(context.now)
    before = {item.id for item in ref.workspaces}
    after = {item.id for item in now.workspaces}
    return len(after) == len(before) + 1 and now.focused_workspace_id in after - before


@check("workspace_step", step=_choice(1, -1))
def workspace_step(context, step):
    """Focus went to the next (1) or previous (-1) workspace, wrapping; none added or removed."""
    ref, now = _herdr(context.ref), _herdr(context.now)
    order = tuple(item.id for item in ref.workspaces)
    return (len(order) > 1 and tuple(item.id for item in now.workspaces) == order
            and now.focused_workspace_id == _step(order, ref.focused_workspace_id, step))


@check("workspace_label", label=_text())
def workspace_label(context, label):
    """The focused workspace has this label."""
    workspace = _herdr(context.now).workspace
    return workspace is not None and workspace.label == label


@check("workspace_exists", label=_text(), min_tabs=_int(1, 9, 1))
def workspace_exists(context, label, min_tabs):
    """A workspace with this label and at least `min_tabs` tabs exists."""
    return any(item.label == label and len(item.tabs) >= min_tabs for item in _herdr(context.now).workspaces)


@check("workspace_gone", label=_text(), focused_label=_text(None))
def workspace_gone(context, label, focused_label):
    """No workspace has this label; optionally, focus is on the workspace labelled `focused_label`."""
    now = _herdr(context.now)
    if any(item.label == label for item in now.workspaces):
        return False
    return focused_label is None or (now.workspace is not None and now.workspace.label == focused_label)


# Needs: predicates over the world a scenario would start from.

def _count(world, what):
    herdr = _herdr(world)
    if what == "panes":
        return len(herdr.tab.panes) if herdr.tab else 0
    if what == "tabs":
        return len(herdr.workspace.tabs) if herdr.workspace else 0
    return len(herdr.workspaces)


for _what in ("panes", "tabs", "workspaces"):
    need(f"min_{_what}", count=_int(0, 99))(
        lambda world, earlier, count, _what=_what: _count(world, _what) >= count)
    need(f"max_{_what}", count=_int(0, 99))(
        lambda world, earlier, count, _what=_what: _count(world, _what) <= count)


@need("has_neighbour", side=_choice(*SIDES, "horizontal", "vertical"))
def has_neighbour(world, earlier, side):
    """The focused pane has a neighbour on that side (horizontal: left or right; vertical: up or down)."""
    herdr = _herdr(world)
    if herdr.tab is None:
        return False
    sides = {"horizontal": ("left", "right"), "vertical": ("up", "down")}.get(side, (side,))
    return any(_neighbour(herdr.tab, herdr.focused_pane_id, item) for item in sides)


@need("tab_unfocused", position=_int(1, 9))
def tab_unfocused(world, earlier, position):
    """A tab exists at this position and is not the focused one."""
    herdr = _herdr(world)
    return herdr.workspace is not None and len(herdr.workspace.tabs) >= position != herdr.tab_position


@need("not_first_workspace")
def not_first_workspace(world, earlier):
    herdr = _herdr(world)
    return bool(herdr.workspaces) and herdr.focused_workspace_id != herdr.workspaces[0].id


@need("on_last_workspace")
def on_last_workspace(world, earlier):
    herdr = _herdr(world)
    return bool(herdr.workspaces) and herdr.focused_workspace_id == herdr.workspaces[-1].id


@need("not_zoomed")
def not_zoomed(world, earlier):
    herdr = _herdr(world)
    return herdr.tab is not None and not herdr.tab.zoomed


@need("has_focus_history")
def has_focus_history(world, earlier):
    """An earlier world focused another pane that still exists in this tab: last_pane can return to it."""
    herdr = _herdr(world)
    return herdr.tab is not None and _previous_focus(earlier, herdr.tab) is not None
import checks_yazi  # noqa: E402,F401  (registers the Yazi checks)
