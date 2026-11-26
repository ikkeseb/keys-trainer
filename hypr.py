"""Read the user's live Hyprland, never write to it (the «Desktop (live)» lesson).

The trainer reads socket2's event lines and makes read-only queries on the control socket:
`j/activeworkspace` here, `j/binds` and `binds` from keyscan.py; no command that changes
Hyprland is ever sent. `fold` turns event
lines into a `world.DesktopWorld`; `HyprFixture` feeds synthetic lines for tests.
"""

import json
import os
import re
import socket
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

from strings import DESKTOP as TEXT
from world import DesktopWorld


def workspace_chords(rigpage_payload: dict) -> dict[int, str]:
    """The user's chord per workspace number, expanded from the `desktop` rows of `rigpage keys`."""
    individual = {}
    expanded = {}
    for group in rigpage_payload.get("groups", []):
        if group.get("id") != "desktop":
            continue
        for row in group["rows"]:
            chord = row["name"]
            label = re.fullmatch(r"Switch to workspace ([1-9]|10)(?:…([1-9]|10))?", row["value"], re.IGNORECASE)
            if not label or not chord:
                continue
            first = int(label[1])
            if label[2] is None:
                individual.setdefault(first, chord)
                continue
            last = int(label[2])
            match = re.fullmatch(r"(.*\+)([0-9])…([0-9])", chord)
            if not match:
                continue
            start_key, end_key = int(match[2]), int(match[3])
            if last < first or (start_key + last - first) % 10 != end_key:
                continue
            for index in range(first, last + 1):
                key = (start_key + index - first) % 10
                expanded.setdefault(index, f"{match[1]}{key}")
    drills = expanded | individual
    chord_counts = Counter(_chord_signature(chord) for chord in drills.values())
    return {workspace: chord for workspace, chord in sorted(drills.items())
            if chord_counts[_chord_signature(chord)] == 1}


def _chord_signature(chord: str) -> tuple[frozenset[str], str]:
    *mods, key = (part.strip() for part in chord.casefold().split("+"))
    return frozenset(mods), key


def _request(path: Path, command: bytes) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(path))
        client.sendall(command)
        reply = bytearray()
        while chunk := client.recv(8192):
            reply.extend(chunk)
        return bytes(reply)


def _accepts(path: Path) -> bool:
    """Whether a socket accepts a connection; connect and close, nothing is sent."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(path))
    except OSError:
        return False
    return True


class HyprObserver:
    """Subscribe to an explicit compositor and query its active workspace."""

    def __init__(self, instance_dir: Path):
        self.instance_dir = Path(instance_dir)
        self._events: WorkspaceEvents | None = None

    @classmethod
    def from_environment(cls, env=None) -> "HyprObserver":
        """The instance the environment names; when its event socket refuses a connection
        (a server that outlived a desktop restart still carries the dead signature), the
        newest instance under `$XDG_RUNTIME_DIR/hypr` that accepts one."""
        env = os.environ if env is None else env
        runtime = env.get("XDG_RUNTIME_DIR")
        signature = env.get("HYPRLAND_INSTANCE_SIGNATURE", "")
        if not runtime or not Path(runtime).is_absolute() or not re.fullmatch(r"[a-zA-Z0-9_-]+", signature):
            raise RuntimeError(TEXT["no_session"])
        named = Path(runtime) / "hypr" / signature
        if _accepts(named / ".socket2.sock"):
            return cls(named)
        others = [path for path in (named.parent.glob("*/") if named.parent.is_dir() else ())
                  if path != named and (path / ".socket2.sock").exists()]
        for path in sorted(others, key=lambda path: (path / ".socket2.sock").stat().st_mtime, reverse=True):
            if _accepts(path / ".socket2.sock"):
                return cls(path)
        return cls(named)

    @classmethod
    def running(cls, env=None) -> bool:
        """Whether Hyprland is up: its event socket accepts a connection (nothing is sent)."""
        try:
            observer = cls.from_environment(env)
        except RuntimeError:
            return False
        return _accepts(observer.instance_dir / ".socket2.sock")

    def __enter__(self) -> "WorkspaceEvents":
        if self._events is not None:
            raise RuntimeError(TEXT["connected"])
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(self.instance_dir / ".socket2.sock"))
            client.setblocking(False)
            self._events = WorkspaceEvents(client.detach(), self._read_active_workspace)
        return self._events

    def __exit__(self, *exc_info) -> None:
        if self._events is not None:
            self._events.close()
            self._events = None

    def _read_active_workspace(self) -> int | None:
        reply = json.loads(_request(self.instance_dir / ".socket.sock", b"j/activeworkspace"))
        if not isinstance(reply, dict):
            raise RuntimeError(TEXT["bad_reply"])
        workspace = reply.get("id")
        return workspace if type(workspace) is int and workspace > 0 else None


class WorkspaceEvents:
    """Consume newline-delimited events from a socket or an in-process fixture."""

    def __init__(self, fd: int, get_workspace: Callable[[], int | None]):
        self._fd: int | None = fd
        self._get_workspace = get_workspace
        self._partial = b""
        self._discard_partial = False

    def fileno(self) -> int:
        if self._fd is None:
            raise RuntimeError(TEXT["closed"])
        return self._fd

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def active_workspace(self) -> int | None:
        """The one query the trainer makes (`j/activeworkspace`, read-only)."""
        self.fileno()
        return self._get_workspace()

    def arm(self) -> None:
        """Discard events from before now, including a split frame."""
        for _ in self.lines():
            pass
        self._discard_partial = bool(self._partial)

    def lines(self) -> Iterator[str]:
        """Every complete event line ready now, oldest first; never blocks."""
        fd = self.fileno()
        while True:
            try:
                chunk = os.read(fd, 8192)
            except BlockingIOError:
                return
            if not chunk:
                raise ConnectionError(TEXT["ended"])
            *lines, self._partial = (self._partial + chunk).split(b"\n")
            if lines and self._discard_partial:
                lines.pop(0)
                self._discard_partial = False
            for line in lines:
                yield line.decode("utf-8", errors="replace")


def fold(world: DesktopWorld, line: str) -> DesktopWorld:
    """Apply one socket2 event to the desktop world; events it does not track change nothing.

    `workspacev2>>ID,NAME` moves the focused workspace (special workspaces, with negative
    ids, are not workspaces here). `activespecial>>special:NAME,MONITOR` and
    `activespecialv2>>ID,special:NAME,MONITOR` show a special workspace on a monitor; an
    empty name hides it. Hyprland can send one transition in both formats: the world is
    the same after the second, so it is one change.
    """
    event, _, payload = line.partition(">>")
    if event == "workspacev2":
        number, comma, _ = payload.partition(",")
        if comma and re.fullmatch(r"[0-9]+", number) and int(number) > 0:
            return replace(world, workspace=int(number))
    elif event in ("activespecial", "activespecialv2"):
        if event == "activespecialv2":
            _, _, payload = payload.partition(",")
        name, comma, monitor = payload.rpartition(",")
        if comma and monitor:
            shown = dict(world.special)
            name = name.removeprefix("special:")
            if name:
                shown[monitor] = name
            else:
                shown.pop(monitor, None)
            return replace(world, special=tuple(sorted(shown.items())))
    return world


class HyprFixture:
    """Drive the verifier with synthetic Hyprland-format events through a local pipe."""

    def __init__(self):
        self._workspace = 1
        self._source: int | None = None
        self.observer: WorkspaceEvents

    def __enter__(self) -> "HyprFixture":
        if self._source is not None:
            raise RuntimeError("The Hyprland fixture is already running")
        fd, self._source = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self._workspace = 1
        self.observer = WorkspaceEvents(fd, lambda: self._workspace)
        return self

    def __exit__(self, *exc_info) -> None:
        if self._source is not None:
            os.close(self._source)
            self._source = None
            self.observer.close()

    def go(self, workspace: int) -> None:
        if self._source is None:
            raise RuntimeError("The Hyprland fixture is closed")
        if type(workspace) is not int or not 1 <= workspace <= 10:
            raise ValueError("Expected a workspace from 1 to 10")
        if workspace != self._workspace:
            self.emit(f"workspacev2>>{workspace},{workspace}")
            self._workspace = workspace

    def emit(self, line: str) -> None:
        """Send one socket2 event, e.g. activespecial>>special:quake,DP-1."""
        if self._source is None:
            raise RuntimeError("The Hyprland fixture is closed")
        os.write(self._source, (line + "\n").encode())
