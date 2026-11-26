"""The desktop lesson on the user's live Hyprland: the Session protocol over socket2, read-only, and its checks.

`DesktopSession` never sends a key and never writes to a Hyprland socket: it reads event
lines (`hypr.WorkspaceEvents`) and makes the one existing query, the active workspace,
when it starts. Every event that changes what it tracks becomes one `World(desktop=…)`,
handed out one per `world()` call, so the engine sees every transition in order even when
two arrive between ticks. `arm()` drops what happened before a step starts.

The engine knows a live session by `live = True`: «Show me» does not exist there (the
trainer cannot press the user's keys), a miss is told only once the user is back where it
can be read, and `vary` gives each workspace step a target other than the current one.
"""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Iterable
from dataclasses import replace

import checks
from hypr import HyprObserver, fold
from strings import DESKTOP as TEXT
from world import DesktopWorld, World

FIXTURES = ("live",)  # the user's desktop as it is: the trainer cannot set a start state
NUMBERS = tuple(range(1, 10))  # workspaces a numbered chord reaches (super+1..9)


class DesktopSession:
    """The user's live desktop, watched. `observer` is a `HyprObserver` (or anything whose context
    gives `hypr.WorkspaceEvents`); `events` passes a ready stream instead (`HyprFixture`)."""

    tool = "desktop"
    live = True
    herdr = None  # no private terminal: the runner draws a card with the bar

    def __init__(self, observer=None, *, events=None, numbers: Iterable[int] = NUMBERS, rng=None):
        if (observer is None) == (events is None):
            raise ValueError("Pass a Hyprland observer or an event stream.")
        self._observer = observer
        self._events = events
        self._numbers = tuple(numbers)
        self._rng = rng or random.Random()
        self._latest = self._shown = DesktopWorld()
        self._pending: deque[DesktopWorld] = deque()
        self._target: int | None = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # The Session protocol.

    def start(self, actions: Iterable[str], fixture: str = "live") -> World:
        if fixture not in FIXTURES:
            raise ValueError(f"Unknown desktop fixture: {fixture}")
        if self._observer is not None and self._events is None:
            self._events = self._observer.__enter__()
        self._events.arm()
        workspace = self._events.active_workspace()
        if workspace is None:
            raise RuntimeError(TEXT["no_workspace"])
        self._latest = self._shown = DesktopWorld(workspace)
        self._pending.clear()
        return self.world()

    def apply_fixture(self, name: str) -> World:
        """Nothing to build on the user's desktop: the step starts from where they are."""
        if name not in FIXTURES:
            raise ValueError(f"Unknown desktop fixture: {name}")
        self.arm()
        return self.world()

    def arm(self) -> None:
        """Take in everything that happened so far as the present, not as the user's moves."""
        self._pull(queue=False)
        self._pending.clear()
        self._shown = self._latest

    def world(self) -> World:
        self._pull()
        if self._pending:
            self._shown = self._pending.popleft()
        return World(desktop=self._shown)

    def vary(self, scenario):
        """A numbered scenario (`{number}` in its goal) gets a target the user is not on, and not
        the one they just had; any other scenario is returned as it is."""
        if not scenario.template:
            return scenario
        here = self._latest.workspace
        choices = [n for n in self._numbers if n != here and n != self._target] or \
                  [n for n in self._numbers if n != here]
        self._target = self._rng.choice(choices)
        return scenario.with_number(self._target)

    def chords(self, card: str) -> tuple[str, ...]:
        return ()

    def play(self, card: str, **kw) -> str:
        raise RuntimeError(TEXT["no_keys"])

    def press(self, chord: str) -> None:
        raise RuntimeError(TEXT["no_keys"])

    def type_text(self, text: str) -> None:
        raise RuntimeError(TEXT["no_keys"])

    def feed(self, raw: bytes) -> None:
        """Keys on the trainer's own window go nowhere: Hyprland takes the desktop chords."""

    def typing(self) -> bool:
        return False

    def close(self) -> None:
        if self._observer is not None and self._events is not None:
            self._observer.__exit__(None, None, None)
            self._events = None

    def _pull(self, queue=True):
        for line in self._events.lines():
            world = fold(self._latest, line)
            if world != self._latest:
                self._latest = replace(world, seq=self._latest.seq + 1)
                if queue:
                    self._pending.append(self._latest)


def open_live(**kw) -> DesktopSession:
    """A session over the user's running Hyprland (read-only)."""
    return DesktopSession(HyprObserver.from_environment(), **kw)


# Checks over `World.desktop` (registered in `checks`, read from curriculum/desktop.toml).

def _desktop(world: World) -> DesktopWorld:
    return world.desktop if world.desktop is not None else DesktopWorld()


@checks.check("workspace_is", tool="desktop", number=checks._int(1, 10))
def workspace_is(context, number):
    """The user is on workspace `number`."""
    return _desktop(context.now).workspace == number


@checks.check("workspace_origin", tool="desktop")
def workspace_origin(context):
    """The user is back on the workspace the step started on, after being somewhere else."""
    start, ref, now = _desktop(context.start), _desktop(context.ref), _desktop(context.now)
    return now.workspace == start.workspace and ref.place != start.place


@checks.check("special_shown", tool="desktop", special=checks._text())
def special_shown(context, special):
    """The special workspace (`quake` for `special:quake`) is shown on a monitor."""
    return special in _desktop(context.now).shown


@checks.check("special_hidden", tool="desktop", special=checks._text())
def special_hidden(context, special):
    """The special workspace was shown where the previous check latched and is hidden now."""
    return special in _desktop(context.ref).shown and special not in _desktop(context.now).shown


# The user's desktop chords, from the `desktop` rows of `rigpage keys` (curriculum's resolver).

ACTIONS = {"former_workspace": "Former workspace", "toggle_quake": "Toggle Quake terminal",
           "toggle_qconsole": "Toggle Quake console"}


def desktop_chords(rows_payload, action: str) -> tuple[str, ...]:
    """The user's chords for a desktop card. `switch_workspace` is one indexed chord
    (`super+1..9`) when workspaces 1 to 9 share its modifiers; others match a row's value."""
    from hypr import workspace_chords

    if action == "switch_workspace":
        found = workspace_chords(rows_payload)
        mods = {found.get(n, "").removesuffix(str(n)) for n in NUMBERS}
        mod = next(iter(mods))
        return (mod + "1..9",) if len(mods) == 1 and mod.endswith("+") else ()
    value = ACTIONS.get(action)
    return tuple(row["name"] for group in rows_payload.get("groups", ()) if group.get("id") == "desktop"
                 for row in group.get("rows", ()) if value and row.get("value") == value and row.get("name"))
