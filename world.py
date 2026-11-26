"""The observed state of the private practice tools, and what changed between two looks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from strings import WORLD as WORLD_TEXT


# Shown copy lives in strings.py (decision 2).
TEXT = WORLD_TEXT


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @classmethod
    def from_json(cls, value: Mapping) -> Rect:
        return cls(int(value["x"]), int(value["y"]), int(value["width"]), int(value["height"]))


@dataclass(frozen=True)
class PaneState:
    id: str
    rect: Rect
    focused: bool


@dataclass(frozen=True)
class SplitState:
    # herdr's split id is a tree path ("split_1_1") and changes when the tree does.
    id: str
    direction: str  # "right" (side by side) or "down" (stacked)
    ratio: float
    rect: Rect


@dataclass(frozen=True)
class TabState:
    id: str
    label: str
    panes: tuple[PaneState, ...] = ()
    focused_pane_id: str | None = None
    zoomed: bool = False
    splits: tuple[SplitState, ...] = ()
    area: Rect | None = None

    def pane(self, pane_id: str | None) -> PaneState | None:
        return next((pane for pane in self.panes if pane.id == pane_id), None)

    @property
    def pane_ids(self) -> frozenset[str]:
        return frozenset(pane.id for pane in self.panes)


@dataclass(frozen=True)
class WorkspaceState:
    id: str
    label: str
    tabs: tuple[TabState, ...] = ()  # in tab-bar order; position N is tabs[N - 1]
    active_tab_id: str | None = None

    def tab(self, tab_id: str | None) -> TabState | None:
        return next((tab for tab in self.tabs if tab.id == tab_id), None)

    @property
    def tab_ids(self) -> tuple[str, ...]:
        return tuple(tab.id for tab in self.tabs)


@dataclass(frozen=True)
class HerdrWorld:
    workspaces: tuple[WorkspaceState, ...]  # in sidebar order
    focused_workspace_id: str | None = None
    focused_tab_id: str | None = None
    focused_pane_id: str | None = None

    @classmethod
    def from_snapshot(cls, snapshot: Mapping) -> HerdrWorld:
        """Build from herdr's `session.snapshot` result (protocol 22).

        List order is the visible order. The `number` fields are not: a tab keeps
        its number after a move, so positions come from the lists.
        """
        layouts = {layout["tab_id"]: layout for layout in snapshot.get("layouts", ())}
        tabs: dict[str, list[TabState]] = {}
        for tab in snapshot.get("tabs", ()):
            layout = layouts.get(tab["tab_id"], {})
            tabs.setdefault(tab["workspace_id"], []).append(TabState(
                id=tab["tab_id"],
                label=str(tab.get("label", "")),
                panes=tuple(
                    PaneState(pane["pane_id"], Rect.from_json(pane["rect"]), bool(pane.get("focused")))
                    for pane in layout.get("panes", ())
                ),
                focused_pane_id=layout.get("focused_pane_id"),
                zoomed=bool(layout.get("zoomed", False)),
                splits=tuple(
                    SplitState(str(split.get("id", "")), split["direction"], float(split["ratio"]),
                               Rect.from_json(split["rect"]))
                    for split in layout.get("splits", ())
                ),
                area=Rect.from_json(layout["area"]) if "area" in layout else None,
            ))
        return cls(
            tuple(
                WorkspaceState(
                    workspace["workspace_id"], str(workspace.get("label", "")),
                    tuple(tabs.get(workspace["workspace_id"], ())), workspace.get("active_tab_id"),
                )
                for workspace in snapshot.get("workspaces", ())
            ),
            snapshot.get("focused_workspace_id"),
            snapshot.get("focused_tab_id"),
            snapshot.get("focused_pane_id"),
        )

    @property
    def workspace(self) -> WorkspaceState | None:
        return self.find_workspace(self.focused_workspace_id)

    @property
    def tab(self) -> TabState | None:
        return self.find_tab(self.focused_tab_id)

    @property
    def pane(self) -> PaneState | None:
        tab = self.tab
        return tab.pane(self.focused_pane_id) if tab else None

    def find_workspace(self, workspace_id: str | None) -> WorkspaceState | None:
        return next((item for item in self.workspaces if item.id == workspace_id), None)

    def find_tab(self, tab_id: str | None) -> TabState | None:
        return next((tab for item in self.workspaces for tab in item.tabs if tab.id == tab_id), None)

    def workspace_of(self, tab_id: str | None) -> WorkspaceState | None:
        return next((item for item in self.workspaces if item.tab(tab_id)), None)

    @property
    def tab_position(self) -> int | None:
        """1-based position of the focused tab in its workspace."""
        workspace = self.workspace
        if workspace is None or self.focused_tab_id not in workspace.tab_ids:
            return None
        return workspace.tab_ids.index(self.focused_tab_id) + 1


@dataclass(frozen=True)
class YaziWorld:
    """The private Yazi's state as its hook reports it."""

    cwd: str = ""
    hovered: str | None = None
    entries: tuple[str, ...] = ()
    selected: frozenset[str] = frozenset()
    show_hidden: bool = False
    filter: str = ""
    layer: str = "mgr"  # "mgr", or "input" while a Yazi prompt takes text
    tab_count: int = 1
    tab_index: int = 0
    yanked: int = 0
    cut: bool = False
    visual: bool = False
    copied: str = ""  # «what:text» of the latest copy-to-clipboard key, paths from the practice home
    history: str = ""  # the history keys used, oldest first: «back:Projects forward:Practice »

    @classmethod
    def from_event(cls, event: Mapping) -> YaziWorld:
        return cls(
            cwd=str(event.get("cwd", "")),
            hovered=event.get("hovered") or None,
            entries=tuple(event.get("entries", ())),
            selected=frozenset(event.get("selected", ())),
            show_hidden=bool(event.get("show_hidden", False)),
            filter=str(event.get("filter") or ""),
            layer=str(event.get("layer") or "mgr"),
            tab_count=int(event.get("tab_count", 1)),
            tab_index=int(event.get("tab_index", 0)),
            yanked=int(event.get("yanked", 0)),
            cut=bool(event.get("cut", False)),
            visual=bool(event.get("visual", False)),
            copied=str(event.get("copied") or ""),
            history=str(event.get("history") or ""),
        )


@dataclass(frozen=True)
class DesktopWorld:
    """The user's live Hyprland as its event socket reports it (`hypr.fold` builds it).

    `workspace` is the focused workspace; `special` the special workspaces shown, as
    sorted (monitor, name) pairs («quake» for `special:quake`). `seq` counts the changes
    the session has seen, so a trip away and back is two new worlds, never «no change».
    """

    workspace: int | None = None
    special: tuple[tuple[str, str], ...] = ()
    seq: int = 0

    @property
    def shown(self) -> frozenset[str]:
        return frozenset(name for _, name in self.special)

    @property
    def place(self) -> tuple:
        """Where the user is, without the change count: the workspace and the specials shown."""
        return self.workspace, self.special


@dataclass(frozen=True)
class World:
    herdr: HerdrWorld | None = None
    yazi: YaziWorld | None = None
    desktop: DesktopWorld | None = None
    strata: tuple[str, ...] | None = None  # a key-press practice: actions done, or keys that did none


EMPTY = World()


def adjacent_side(origin: Rect, other: Rect) -> str | None:
    """Return the side of `origin` that `other` touches, allowing a one-cell divider."""
    overlap_x = min(origin.right, other.right) - max(origin.x, other.x) > 0
    overlap_y = min(origin.bottom, other.bottom) - max(origin.y, other.y) > 0
    if overlap_y and 0 <= origin.x - other.right <= 1:
        return "left"
    if overlap_y and 0 <= other.x - origin.right <= 1:
        return "right"
    if overlap_x and 0 <= origin.y - other.bottom <= 1:
        return "up"
    if overlap_x and 0 <= other.y - origin.bottom <= 1:
        return "down"
    return None


def bounding_split(tab: TabState, pane: PaneState, direction: str) -> SplitState | None:
    """The innermost split of `direction` that contains the pane: the one its resize keys move."""
    containing = [
        split for split in tab.splits
        if split.direction == direction
        and split.rect.x <= pane.rect.x and split.rect.y <= pane.rect.y
        and split.rect.right >= pane.rect.right and split.rect.bottom >= pane.rect.bottom
    ]
    return min(containing, key=lambda split: split.rect.width * split.rect.height, default=None)


def split_side(before: TabState, after: TabState, new_pane_id: str) -> str | None:
    """Return "down" or "right" when the new pane is the second half of the pane that was focused."""
    origin = before.pane(before.focused_pane_id)
    current = after.pane(origin.id) if origin else None
    new = after.pane(new_pane_id)
    if origin is None or current is None or new is None or before.pane(new_pane_id):
        return None
    if (current.rect.x, current.rect.y) != (origin.rect.x, origin.rect.y):
        return None
    side = adjacent_side(current.rect, new.rect)
    if side == "down" and abs(new.rect.width - current.rect.width) <= 1 and new.rect.x == current.rect.x:
        return side
    if side == "right" and abs(new.rect.height - current.rect.height) <= 1 and new.rect.y == current.rect.y:
        return side
    return None


def describe(before: World, after: World) -> str | None:
    """One short sentence for what changed, or None when nothing relevant did."""
    if before.herdr and after.herdr:
        if text := _describe_herdr(before.herdr, after.herdr):
            return text
    if before.yazi and after.yazi:
        return _describe_yazi(before.yazi, after.yazi)
    if before.strata is not None and after.strata and len(after.strata) > len(before.strata):
        return TEXT["pressed"].format(key=after.strata[-1].replace("_", " "))
    if before.desktop and after.desktop:
        return _describe_desktop(before.desktop, after.desktop)
    return None


def _describe_herdr(before: HerdrWorld, after: HerdrWorld) -> str | None:
    old_workspaces = {item.id for item in before.workspaces}
    new_workspaces = {item.id for item in after.workspaces}
    if new_workspaces - old_workspaces:
        return TEXT["workspace_new"]
    if old_workspaces - new_workspaces:
        return TEXT["workspace_closed"]
    if after.focused_workspace_id != before.focused_workspace_id:
        return TEXT["workspace_focus"]
    workspace, previous = after.workspace, before.workspace
    if workspace is None or previous is None:
        return None
    if workspace.label != previous.label:
        return TEXT["workspace_label"].format(label=workspace.label)

    old_tabs, new_tabs = set(previous.tab_ids), set(workspace.tab_ids)
    if new_tabs - old_tabs:
        return TEXT["tab_new"]
    if old_tabs - new_tabs:
        return TEXT["tab_closed"]
    if after.focused_tab_id != before.focused_tab_id:
        return TEXT["tab_focus"].format(position=after.tab_position)
    tab, earlier = after.tab, before.tab
    if tab is None or earlier is None:
        return None
    if tab.label != earlier.label:
        return TEXT["tab_label"].format(label=tab.label)
    if workspace.tab_ids != previous.tab_ids:
        return TEXT["tab_moved"]

    added, removed = tab.pane_ids - earlier.pane_ids, earlier.pane_ids - tab.pane_ids
    if added and not removed:
        side = split_side(earlier, tab, next(iter(added))) if len(added) == 1 else None
        return TEXT[f"split_{side}"] if side else TEXT["pane_new"]
    if removed:
        return TEXT["pane_closed"]
    if tab.zoomed != earlier.zoomed:
        return TEXT["zoom_on" if tab.zoomed else "zoom_off"]
    if tab.focused_pane_id != earlier.focused_pane_id:
        origin, target = earlier.pane(earlier.focused_pane_id), tab.pane(tab.focused_pane_id)
        side = adjacent_side(origin.rect, target.rect) if origin and target else None
        return TEXT[f"focus_{side}" if side else "focus_other"]
    pane, old_pane = tab.pane(tab.focused_pane_id), earlier.pane(tab.focused_pane_id)
    if pane and old_pane:
        if pane.rect.width != old_pane.rect.width:
            return TEXT["wider" if pane.rect.width > old_pane.rect.width else "narrower"]
        if pane.rect.height != old_pane.rect.height:
            return TEXT["taller" if pane.rect.height > old_pane.rect.height else "shorter"]
    return None


def _describe_yazi(before: YaziWorld, after: YaziWorld) -> str | None:
    if after.copied != before.copied and after.copied:
        return TEXT["yazi_copied"].format(text=after.copied.partition(":")[2])
    if after.tab_count != before.tab_count or after.tab_index != before.tab_index:
        return TEXT["yazi_tabs"].format(position=after.tab_index + 1, count=after.tab_count)
    if after.cwd != before.cwd:
        return TEXT["yazi_folder"].format(name=after.cwd.rstrip("/").rsplit("/", 1)[-1] or after.cwd)
    if after.show_hidden != before.show_hidden:
        return TEXT["yazi_hidden_on" if after.show_hidden else "yazi_hidden_off"]
    if after.filter != before.filter:
        return TEXT["yazi_filter"]
    if after.selected != before.selected:
        return TEXT["yazi_selected"]
    if after.hovered != before.hovered and after.hovered:
        return TEXT["yazi_hover"].format(name=after.hovered)
    return None


def _describe_desktop(before: DesktopWorld, after: DesktopWorld) -> str | None:
    if after.workspace != before.workspace and after.workspace is not None:
        return TEXT["desktop_workspace"].format(number=after.workspace)
    opened, closed = after.shown - before.shown, before.shown - after.shown
    for names, key in ((opened, "desktop_special_on"), (closed, "desktop_special_off")):
        if names:
            name = min(names)
            return TEXT[key].format(name=TEXT.get("special_" + name, TEXT["special_other"].format(name=name)))
    return None
