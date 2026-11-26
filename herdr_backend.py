"""A private herdr for lessons and the layout workshop, driven over its Unix socket API."""

import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time
import tomllib
from collections.abc import Iterable, Mapping
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from pathlib import Path

from world import HerdrWorld


LAYOUT_ACTIONS = frozenset({"split_horizontal", "split_vertical"}).union(
    f"focus_pane_{direction}" for direction in ("left", "right", "up", "down")
)
API_TIMEOUT = 2
LAYOUT_POLL_INTERVAL = 0.1
NATIVE_FRAME_OPEN = b"\x1b[?2026h"
NATIVE_FRAME_CLOSE = b"\x1b[?2026l"
NATIVE_BUFFER_LIMIT = 2 * 1024 * 1024
FIXTURES = ("one_pane", "right_stack", "four_tabs", "two_tabs", "two_workspaces", "three_workspaces")
_RIGHT_STACK = {
    "type": "split", "direction": "right", "ratio": 0.5, "first": {"type": "pane"},
    "second": {"type": "split", "direction": "down", "ratio": 0.5,
               "first": {"type": "pane"}, "second": {"type": "pane"}},
}


def _more_panes(before, after):
    return bool(before.tab and after.tab) and len(after.tab.panes) > len(before.tab.panes)


def _more_tabs(before, after):
    return bool(before.workspace and after.workspace) and len(after.workspace.tabs) > len(before.workspace.tabs)


# The self-test plays the first of these the session binds, from its one-pane start.
_SELF_TESTS = (
    ("split_vertical", _more_panes),
    ("split_horizontal", _more_panes),
    ("new_tab", _more_tabs),
    ("new_workspace", lambda before, after: len(after.workspaces) > len(before.workspaces)),
)


class HerdrUnavailable(RuntimeError):
    """The Herdr socket, protocol or private client is unavailable."""


def _request(method, params):
    return json.dumps({"id": "keys-trainer", "method": method, "params": params}).encode() + b"\n"


class _ApiStream:
    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.buffer = bytearray()
        try:
            self.socket.settimeout(API_TIMEOUT)
            self.socket.connect(str(path))
        except OSError:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.socket.close()

    def send(self, method, params):
        self.socket.sendall(_request(method, params))

    def receive(self, wait=0):
        end = time.monotonic() + wait
        while (newline := self.buffer.find(b"\n")) < 0:
            readable, _, _ = select.select([self.socket], [], [], max(0, end - time.monotonic()))
            if not readable:
                return None
            chunk = self.socket.recv(65536)
            if not chunk:
                raise HerdrUnavailable("Herdr closed its API connection.")
            self.buffer.extend(chunk)
            if len(self.buffer) > 8 * 1024 * 1024:
                raise HerdrUnavailable("Herdr returned an oversized API message.")
        try:
            message = json.loads(self.buffer[:newline])
        except (ValueError, UnicodeError) as exc:
            raise HerdrUnavailable("Herdr returned an invalid API message.") from exc
        del self.buffer[:newline + 1]
        if not isinstance(message, dict):
            raise HerdrUnavailable("Herdr returned an invalid API message.")
        if "error" in message:
            raise HerdrUnavailable(message["error"].get("message", "Herdr API error."))
        return message

    def result(self):
        message = self.receive(API_TIMEOUT)
        if not message or message.get("id") != "keys-trainer" or "result" not in message:
            raise HerdrUnavailable("Herdr did not acknowledge its API request.")
        return message["result"]


def _call(path, method, **params):
    with _ApiStream(path) as stream:
        stream.send(method, params)
        return stream.result()


def _subscribe(path, events):
    stream = _ApiStream(path)
    try:
        stream.send("events.subscribe", {"subscriptions": [{"type": event} for event in events]})
        if stream.result().get("type") != "subscription_started":
            raise HerdrUnavailable("Herdr did not start the event subscription.")
    except BaseException:
        stream.close()
        raise
    return stream


@dataclass(frozen=True)
class LayoutPane:
    id: str
    x: float
    y: float
    width: float
    height: float
    focused: bool


@dataclass(frozen=True)
class HerdrLayout:
    tab_id: str
    focused_id: str
    panes: tuple[LayoutPane, ...]
    width: int
    height: int


def _tab_layout(snapshot):
    tab = snapshot.get("focused_tab_id")
    if tab is None:
        return None
    layout = next((item for item in snapshot["layouts"] if item["tab_id"] == tab), None)
    if layout is None:
        return None
    area = layout["area"]
    width, height = area["width"], area["height"]
    if width <= 0 or height <= 0:
        return None
    focused_id = layout["focused_pane_id"]
    panes = []
    for pane in layout["panes"]:
        pane_id, rect = pane["pane_id"], pane["rect"]
        panes.append(LayoutPane(
            pane_id,
            (rect["x"] - area["x"]) / width,
            (rect["y"] - area["y"]) / height,
            rect["width"] / width,
            rect["height"] / height,
            pane_id == focused_id,
        ))
    return HerdrLayout(tab, focused_id, tuple(panes), width, height)


# A rigpage keys export lists herdr's own defaults under this group. They are not in the
# user's config, and a private [keys] built from them left every binding dead (M6).
UNCONFIGURED_GROUP = "Mer å lære"
# Except these: herdr's own moving-about keys, live whenever the config leaves them alone.
UNCONFIGURED_NAVIGATION = frozenset({"workspace picker", "cycle pane next"})


def _unconfigured(row):
    return str(row.get("group", "")).startswith(UNCONFIGURED_GROUP)


def _bindable(row):
    chord = row.get("name", "")
    return (
        (not _unconfigured(row) or str(row.get("value", "")) in UNCONFIGURED_NAVIGATION)
        and isinstance(chord, str) and 0 < len(chord) <= 80
        and chord.isascii() and chord.isprintable()
        # Sequences such as "prefix+w, up" are navigate-mode notes, not bindings.
        and not any(char in chord for char in ", ")
    )


def selected_bindings(rows, actions):
    """Map each allowed herdr action, and the prefix, to its configured chords."""
    selected = {}
    rows = tuple(rows)
    own = {row.get("name") for row in rows if not _unconfigured(row)}
    for row in rows:
        action = row.get("value", "").replace(" ", "_")
        if ((action in actions or action == "prefix") and _bindable(row)
                and not (_unconfigured(row) and row.get("name") in own)):
            chords = selected.setdefault(action, [])
            if row["name"] not in chords:
                chords.append(row["name"])
    return selected


def _key_config(default_config, rows, actions):
    names = set()
    in_keys = False
    for line in default_config.splitlines():
        if line == "[keys]":
            in_keys = True
            continue
        if in_keys and line.startswith(("[", "# [")):
            break
        if in_keys and (match := re.fullmatch(
            r'\s*(?:#\s*)?(\w+)\s*=\s*(?:"(?:[^"\\]|\\.)*"|\[[^\]]*\])\s*(?:#.*)?', line
        )):
            names.add(match[1])
    selected = selected_bindings(rows, actions)
    lines = ["[keys]"]
    for name in sorted(names | selected.keys()):
        if name == "prefix":
            if choices := selected.get(name):
                lines.append(f"prefix = {json.dumps(choices[0])}")
        elif choices := selected.get(name):
            lines.append(f"{name} = {json.dumps(choices)}")
        elif not name.startswith("navigate_"):
            lines.append(f'{name} = ""')
    return "\n".join(lines)


def herdr_actions(default_config):
    """Return the action names herdr's default config declares under [keys]."""
    return frozenset(
        name for name in tomllib.loads(_key_config(default_config, (), ()))["keys"] if name != "prefix"
    )


class HerdrBackend:
    """Run practice sessions and layout exercises in an isolated Herdr.

    A practice session (`start_session`) binds only the actions it is given,
    rebuilds start states with `apply_fixture`, reads `world()` and types the
    user's chords with `play`. `start_layout` serves the layout workshop.
    """

    def __init__(self, bindings: Iterable[Mapping[str, str]]):
        self._rows = tuple(bindings)
        self._resources = ExitStack()
        self._client = None
        self._master = None
        self._key_configs = {}
        self._api = None
        self._layout_mode = False
        self._layout_cache = None
        self._layout_checked = float("-inf")
        self._layout_scope = None
        self._layout_events = None
        self._split_panes = {}
        self._undo_candidate = None
        self._native_active = False
        self._native_overflow = False
        self._native_buffer = bytearray()
        self._native_original_size = None
        self._home = None
        self._session_bindings = None
        self._world_cache = None
        self._world_checked = float("-inf")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def start_layout(self):
        """Start a new layout exercise with a single pane."""
        self._start_private(None)

    def start_session(self, actions: Iterable[str], fixture: str = "one_pane") -> HerdrWorld:
        """Start a private session whose [keys] bind only these herdr actions.

        One bound chord (a split, new tab or new workspace, whichever is bound
        first) is played as a self-test before the fixture is built; with none
        of those bound there is no self-test. Raises ValueError for an action herdr does not know, HerdrUnavailable when
        herdr cannot start or ignores the self-test chord.
        """
        actions = frozenset(actions)
        if fixture not in FIXTURES:
            raise ValueError(f"Unknown Herdr fixture: {fixture}")
        self._start_private(actions)
        try:
            self._self_test()
            return self.apply_fixture(fixture)
        except BaseException:
            self.close()
            raise

    def _self_test(self):
        action, verified = next(
            ((action, verified) for action, verified in _SELF_TESTS if self._session_bindings.get(action)),
            (None, None),
        )
        if action is None:
            return
        self._focus_and_paint(self._call("session.snapshot")["snapshot"]["focused_pane_id"])
        before = self.world()
        chord = self.play(action)
        deadline = time.monotonic() + API_TIMEOUT
        while time.monotonic() < deadline:
            if verified(before, self.world()):
                return
            time.sleep(0.02)
        raise HerdrUnavailable(
            f"Private Herdr ignored {chord} ({action.replace('_', ' ')}): its shortcut bindings are dead."
        )

    def apply_fixture(self, name: str) -> HerdrWorld:
        """Rebuild the session as a declared start state in a fresh «Practice» workspace."""
        if name not in FIXTURES:
            raise ValueError(f"Unknown Herdr fixture: {name}")
        if self._session_bindings is None:
            raise HerdrUnavailable("Start a private Herdr session before building a start state.")
        try:
            # Leave any prompt, confirm box or pending prefix before rebuilding under it.
            # Esc on the close-workspace box leaves herdr in prefix mode (measured):
            # the second Esc ends that, or reaches the old pane, which is closed below.
            self.feed(kitty_keys("esc") * 2)
            old = [item["workspace_id"] for item in self._call("session.snapshot")["snapshot"]["workspaces"]]
            created = self._call("workspace.create", cwd=str(self._home), focus=True, label="Practice")
            workspace, tab = created["workspace"]["workspace_id"], created["tab"]["tab_id"]
            focus = created["root_pane"]["pane_id"]
            for workspace_id in old:
                self._call("workspace.close", workspace_id=workspace_id)
            self._call("tab.rename", tab_id=tab, label="Start")
            if name == "right_stack":
                result = self._call("layout.apply", tab_id=tab, tab_label="Start", focus=True, root=_RIGHT_STACK)
                focus = _leaf_ids(result["layout"]["root"])[2]  # bottom right
            elif name in ("four_tabs", "two_tabs"):
                labels = ("api", "web", "db") if name == "four_tabs" else ("api",)
                created_tabs = [
                    self._call("tab.create", workspace_id=workspace, cwd=str(self._home), focus=False, label=label)
                    for label in labels
                ]
                if name == "four_tabs":
                    focus = created_tabs[0]["root_pane"]["pane_id"]  # tab 2
            elif name in ("two_workspaces", "three_workspaces"):
                # Focus on the last: with three, previous and next workspace differ (M7).
                labels = ("api",) if name == "two_workspaces" else ("api", "docs")
                for label in labels:
                    created = self._call("workspace.create", cwd=str(self._home), focus=True, label=label)
                    self._call("tab.rename", tab_id=created["tab"]["tab_id"], label="Start")
                    focus = created["root_pane"]["pane_id"]
            # layout.apply's own focus moves the server's focus but not the attached
            # client's view: the next key lands in the old tab (the M8 cause). An
            # explicit pane.focus moves both.
            self._focus_and_paint(focus)
        except (OSError, KeyError, TypeError, IndexError) as exc:
            raise HerdrUnavailable(f"Could not build the private Herdr start state {name}: {exc}") from exc
        return self.world()

    def world(self, max_age: float = 0.0) -> HerdrWorld:
        """Return the session's state, read now or at most `max_age` seconds ago."""
        if self._api is None:
            raise HerdrUnavailable("Start a private Herdr session before reading it.")
        self._drain_client()
        if self._world_cache is None or time.monotonic() - self._world_checked > max_age:
            try:
                self._world_cache = HerdrWorld.from_snapshot(self._call("session.snapshot")["snapshot"])
            except (OSError, KeyError, TypeError, ValueError) as exc:
                raise HerdrUnavailable(f"Could not read the private Herdr session: {exc}") from exc
            self._world_checked = time.monotonic()
        return self._world_cache

    def chords(self, action: str) -> tuple[str, ...]:
        """The user's chords bound for this action in the private session."""
        return tuple((self._session_bindings or {}).get(action, ()))

    def play(self, action: str, *, index: int = 0, number: int | None = None) -> str:
        """Type one of the user's chords for `action` into the private client; return the chord.

        `number` fills an indexed chord such as "alt+1..9".
        """
        chords = self.chords(action)
        if not chords:
            raise ValueError(f"No chord for {action} is bound in this private session.")
        chord = chords[index]
        if "1..9" in chord:
            if number is None or not 1 <= number <= 9:
                raise ValueError(f"{action} needs a number from 1 to 9.")
            chord = chord.replace("1..9", str(number))
        self.press(chord)
        return chord

    def press(self, chord: str) -> None:
        """Type one chord (for example "enter", "backspace" or "prefix+z") into the private client."""
        prefix = next(iter(self.chords("prefix")), "")
        data = kitty_keys(chord, prefix)
        if data is None:
            raise ValueError(f"Cannot type the chord {chord!r} into private Herdr.")
        self.feed(data)

    def type_text(self, text: str) -> None:
        """Type plain text, as into a rename prompt."""
        if not text.isprintable():
            raise ValueError("Only printable text can be typed into private Herdr.")
        self.feed(text.encode())

    def _call(self, method, **params):
        self._world_cache = None
        return _call(self._api, method, **params)

    def _focus_and_paint(self, pane_id):
        self._drain_client()
        self._call("pane.focus", pane_id=pane_id)
        self._wait_for_focus_paint(quiet=0.05)
        self._layout_checked = float("-inf")

    def _start_private(self, actions):
        self.close()
        binary = shutil.which("herdr")
        if binary is None:
            raise HerdrUnavailable("Install Herdr to run its private practice session.")
        try:
            self._start(binary, actions)
            if self._layout_mode:
                self.layout()
        except (OSError, subprocess.SubprocessError, KeyError) as exc:
            self.close()
            raise HerdrUnavailable(f"Could not start private Herdr: {exc}") from exc
        except BaseException:
            self.close()
            raise

    def _start(self, binary, session_actions):
        # Keep the Unix socket path short regardless of TMPDIR and the checkout path.
        root = Path(self._resources.enter_context(tempfile.TemporaryDirectory(prefix="keys-herdr-", dir="/tmp")))
        config = root / "config.toml"
        idle = root / "idle"
        idle.write_text(
            "#!/bin/sh\n"
            "trap '' INT QUIT TSTP\n"
            "printf 'Practice pane  %s\\n' \"${HERDR_PANE_ID:-}\"\n"
            "exec sleep 86400\n"
        )
        idle.chmod(0o700)
        paths = {
            "HOME": root / "home",
            "XDG_CONFIG_HOME": root / "config",
            "XDG_STATE_HOME": root / "state",
            "XDG_DATA_HOME": root / "data",
            "XDG_CACHE_HOME": root / "cache",
            "XDG_RUNTIME_DIR": root / "run",
            "TMPDIR": root / "tmp",
        }
        for path in paths.values():
            path.mkdir(mode=0o700)
        api = root / "api.sock"
        env = {name: str(path) for name, path in paths.items()} | {
            "TERM": "xterm-256color",
            "PATH": os.defpath,
            "SHELL": str(idle),
            "HERDR_CONFIG_PATH": str(config),
            "HERDR_SOCKET_PATH": str(api),
            "HERDR_SESSION": root.name,
        }
        layout_mode = session_actions is None
        actions = LAYOUT_ACTIONS if layout_mode else frozenset(session_actions)
        if actions not in self._key_configs:
            default_config = subprocess.check_output(
                [binary, "--default-config"], text=True, env=env, cwd=paths["HOME"], timeout=API_TIMEOUT,
            )
            if unknown := actions - herdr_actions(default_config):
                raise ValueError(f"Herdr has no action named {', '.join(sorted(unknown))}.")
            self._key_configs[actions] = _key_config(default_config, self._rows, actions)
        config.write_text(
            'onboarding = false\n'
            '[terminal]\n'
            f'default_shell = {json.dumps(str(idle))}\n'
            'shell_mode = "non_login"\n'
            'kitty_graphics = false\n'
            '[update]\nversion_check = false\nmanifest_check = false\n'
            '[ui]\nprompt_new_tab_name = false\nhide_tab_bar_when_single_tab = false\ncopy_on_select = false\n'
            '[ui.sound]\nenabled = false\n'
            '[ui.toast]\ndelivery = "off"\n'
            '[session]\nresume_agents_on_restore = false\n'
            + self._key_configs[actions] + "\n",
            encoding="utf-8",
        )
        log = self._resources.enter_context((root / "server.log").open("w+b"))
        server = subprocess.Popen(
            [binary, "server"], env=env, cwd=paths["HOME"], stdin=subprocess.DEVNULL,
            stdout=log, stderr=log, start_new_session=True,
        )
        self._resources.callback(self._stop_server, server, api)
        end = time.monotonic() + API_TIMEOUT
        while not api.is_socket():
            if server.poll() is not None or time.monotonic() >= end:
                log.seek(0)
                detail = log.read(512).decode(errors="replace").strip()
                raise HerdrUnavailable(f"Private Herdr server did not start. {detail}".rstrip())
            time.sleep(0.02)

        workspace = _call(api, "workspace.create", cwd=str(paths["HOME"]), focus=True, label="Practice")
        origin = (
            workspace["workspace"]["workspace_id"],
            workspace["tab"]["tab_id"],
            workspace["root_pane"]["pane_id"],
        )
        _call(api, "tab.rename", tab_id=origin[1], label="Start")

        master, slave = pty.openpty()
        self._resources.callback(os.close, master)
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 100, 0, 0))
            os.set_blocking(master, False)
            self._client = subprocess.Popen(
                [
                    sys.executable, "-I", "-c",
                    "import fcntl, os, sys, termios;"
                    "fcntl.ioctl(0, termios.TIOCSCTTY, 0);"
                    "os.execv(sys.argv[1], sys.argv[1:])",
                    binary,
                ],
                env=env, cwd=paths["HOME"], stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True,
            )
        finally:
            os.close(slave)
        self._resources.callback(self._stop_process, self._client)
        self._master = master
        self._wait_for_client()

        if layout_mode:
            self._layout_scope = origin
            self._layout_events = self._resources.enter_context(_subscribe(api, ("pane.created",)))
        else:
            self._home = paths["HOME"]
            self._session_bindings = selected_bindings(self._rows, actions)
        self._api = api
        self._layout_mode = layout_mode

    def _wait_for_client(self):
        end = time.monotonic() + API_TIMEOUT
        output = bytearray()
        while time.monotonic() < end:
            if self._client.poll() is not None:
                break
            if select.select([self._master], [], [], 0.03)[0]:
                output.extend(self._drain())
                if b"config.toml invalid" in output:
                    raise HerdrUnavailable("Herdr rejected the private shortcut configuration.")
                # Herdr reads terminal capability replies before its first completed frame.
                if NATIVE_FRAME_CLOSE in output:
                    return
        raise HerdrUnavailable("Private Herdr client did not initialize its terminal.")

    def _drain(self):
        output = bytearray()
        for _ in range(16):
            try:
                output.extend(os.read(self._master, 65536))
            except BlockingIOError:
                break
        if self._native_active and not self._native_overflow:
            if len(self._native_buffer) + len(output) <= NATIVE_BUFFER_LIMIT:
                self._native_buffer.extend(output)
            else:
                self._native_buffer.clear()
                self._native_overflow = True
        return output

    def feed(self, raw):
        if self._master is None:
            raise HerdrUnavailable("Start a private Herdr session before sending keys.")
        try:
            self._drain()
            pending = memoryview(raw)
            while pending:
                if not select.select([], [self._master], [], API_TIMEOUT)[1]:
                    raise HerdrUnavailable("The private Herdr terminal stopped accepting input.")
                pending = pending[os.write(self._master, pending):]
        except OSError as exc:
            raise HerdrUnavailable(f"Could not send a key to private Herdr: {exc}") from exc

    def _drain_client(self):
        try:
            self._drain()
        except OSError as exc:
            raise HerdrUnavailable("The private Herdr terminal closed.") from exc

    def begin_native(self, columns, rows):
        """Start relaying the private client's own ANSI frames at this size."""
        if self._master is None:
            raise HerdrUnavailable("Start a private Herdr session before viewing its terminal.")
        if not (0 < columns <= 65535 and 0 < rows <= 65535 and columns * rows <= 400000):
            raise ValueError("The native Herdr terminal size is outside the supported range.")
        self._native_active = self._native_overflow = False
        self._native_buffer.clear()
        self._drain_client()
        self._native_active = True
        try:
            size = struct.pack("HHHH", rows, columns, 0, 0)
            previous = fcntl.ioctl(self._master, termios.TIOCGWINSZ, bytes(8))
            if self._native_original_size is None:
                self._native_original_size = previous
            fcntl.ioctl(self._master, termios.TIOCSWINSZ, size)
            if previous == size:
                self._client.send_signal(signal.SIGWINCH)
        except OSError as exc:
            self.end_native()
            raise HerdrUnavailable(f"Could not resize the private Herdr terminal: {exc}") from exc
        self._layout_checked = float("-inf")

    def native_output(self):
        """Read complete rendered frames currently available, without waiting."""
        if not self._native_active:
            return b""
        self._drain_client()
        if self._native_overflow:
            self._native_overflow = False
            self._client.send_signal(signal.SIGWINCH)
            return b""
        frames = bytearray()
        while True:
            start = self._native_buffer.find(NATIVE_FRAME_OPEN)
            if start < 0:
                # Preserve only a possible partial opening marker.
                del self._native_buffer[:-(len(NATIVE_FRAME_OPEN) - 1)]
                break
            end = self._native_buffer.find(NATIVE_FRAME_CLOSE, start + len(NATIVE_FRAME_OPEN))
            if end < 0:
                del self._native_buffer[:start]
                break
            end += len(NATIVE_FRAME_CLOSE)
            frames.extend(self._native_buffer[start:end])
            del self._native_buffer[:end]
        return bytes(frames)

    def end_native(self):
        """Stop relaying frames. The private session stays active."""
        self._native_active = self._native_overflow = False
        self._native_buffer.clear()
        original, self._native_original_size = self._native_original_size, None
        if original is not None and self._master is not None:
            try:
                fcntl.ioctl(self._master, termios.TIOCSWINSZ, original)
            except OSError as exc:
                raise HerdrUnavailable(f"Could not restore the private Herdr terminal size: {exc}") from exc
            self._layout_checked = float("-inf")

    def layout(self):
        """Return the private client's focused tab, sampled at up to 10 Hz."""
        if self._api is None:
            return None
        self._drain_client()
        if time.monotonic() - self._layout_checked >= LAYOUT_POLL_INTERVAL:
            self._sample_layout()
        return self._layout_cache

    def _sample_layout(self):
        try:
            snapshot = _call(self._api, "session.snapshot")["snapshot"]
            current = _tab_layout(snapshot)
        except (OSError, KeyError, TypeError) as exc:
            raise HerdrUnavailable(f"Could not read the private Herdr layout: {exc}") from exc
        if self._layout_mode:
            self._poll_split_events()
            self._undo_candidate = self._eligible_split(current, snapshot.get("focused_workspace_id"))
        self._layout_cache = current
        self._layout_checked = time.monotonic()
        return current

    def _poll_split_events(self):
        if self._layout_events is None:
            return
        workspace, tab, root = self._layout_scope
        try:
            for _ in range(128):
                if (message := self._layout_events.receive()) is None:
                    return
                if message.get("event") != "pane_created":
                    raise ValueError("Expected a pane creation event.")
                pane = message["data"]["pane"]
                pane_id = pane["pane_id"]
                if (pane["workspace_id"], pane["tab_id"]) == (workspace, tab) and pane_id != root:
                    if not isinstance(pane_id, str) or not pane_id or pane_id in self._split_panes:
                        raise ValueError("Ambiguous pane creation history.")
                    self._split_panes[pane_id] = None
        except (OSError, HerdrUnavailable, KeyError, TypeError, ValueError):
            # A broken event stream invalidates the creation order for this exercise.
            self._layout_events.close()
            self._layout_events = None
            self._split_panes.clear()

    def _eligible_split(self, current, workspace):
        if self._layout_events is None or current is None:
            return None
        expected_workspace, expected_tab, root = self._layout_scope
        if (workspace, current.tab_id) != (expected_workspace, expected_tab):
            return None
        # Snapshot and event delivery can race. Wait for both to agree before offering undo.
        visible = {pane.id for pane in current.panes}
        if visible != self._split_panes.keys() | {root}:
            return None
        candidate = next(reversed(self._split_panes), None)
        if any(pane.id == candidate and pane.width > 0 and pane.height > 0 for pane in current.panes):
            return candidate
        return None

    def focus_pane(self, pane_id):
        """Focus a pane visible in the current private layout exercise."""
        if not self._layout_mode:
            return False
        self._drain_client()
        current = self._sample_layout()
        if current is None or not any(pane.id == pane_id for pane in current.panes):
            return False
        if current.focused_id == pane_id:
            return True
        try:
            _call(self._api, "pane.focus", pane_id=pane_id)
            self._wait_for_focus_paint()
        except OSError as exc:
            raise HerdrUnavailable(f"Could not focus the private Herdr pane: {exc}") from exc
        self._layout_checked = float("-inf")
        return True

    def _wait_for_focus_paint(self, quiet=0.0):
        # The server acknowledges pane.focus before its attached client paints
        # the new focus. Let the client catch up before forwarding another key.
        # With `quiet`, also wait until the client has stopped painting: a frame
        # from an earlier call can close before the one that shows this focus.
        end = time.monotonic() + API_TIMEOUT
        tail = bytearray()
        painted = False
        while time.monotonic() < end:
            wait = quiet if painted else max(0, end - time.monotonic())
            if select.select([self._master], [], [], wait)[0]:
                tail.extend(self._drain())
                if NATIVE_FRAME_CLOSE in tail:
                    if not quiet:
                        return
                    painted = True
                del tail[:-(len(NATIVE_FRAME_CLOSE) - 1)]
            elif painted:
                return
        raise HerdrUnavailable("The private Herdr client did not repaint its focused pane.")

    @property
    def undo_id(self):
        """Return the pane eligible for undo in the latest sampled layout."""
        return self._undo_candidate

    def undo_split(self, pane_id):
        """Close the newest provable split pane in the current exercise."""
        if not self._layout_mode or pane_id is None or pane_id != self._undo_candidate:
            return False
        current = self._sample_layout()
        if current is None or len(current.panes) <= 1 or pane_id != self._undo_candidate:
            return False
        try:
            _call(self._api, "pane.close", pane_id=pane_id)
        except OSError as exc:
            raise HerdrUnavailable(f"Could not undo the private Herdr split: {exc}") from exc
        self._split_panes.pop(pane_id)
        self._undo_candidate = next(reversed(self._split_panes), None)
        self._layout_checked = float("-inf")
        return True

    def close(self):
        self._native_original_size = None
        self.end_native()
        try:
            self._resources.close()
        finally:
            self._client = self._master = None
            self._api = self._layout_cache = self._undo_candidate = None
            self._layout_mode = False
            self._layout_checked = float("-inf")
            self._layout_scope = self._layout_events = None
            self._split_panes.clear()
            self._home = self._session_bindings = self._world_cache = None

    @staticmethod
    def _stop_process(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    @classmethod
    def _stop_server(cls, process, path):
        try:
            if path.is_socket():
                with suppress(OSError, HerdrUnavailable, subprocess.TimeoutExpired):
                    _call(path, "server.stop")
                    process.wait(timeout=1)
        finally:
            cls._stop_process(process)


def _leaf_ids(node):
    if node["type"] == "pane":
        return [node["pane_id"]]
    return _leaf_ids(node["first"]) + _leaf_ids(node["second"])


def demo_keys(chord, prefix):
    """Encode a Herdr chord as input from a traditional xterm terminal."""
    if chord.startswith("prefix+"):
        lead = demo_keys(prefix, "") if prefix and not prefix.startswith("prefix+") else None
        tail = demo_keys(chord.removeprefix("prefix+"), "")
        return lead + tail if lead and tail else None
    *modifiers, key = chord.lower().split("+")
    if set(modifiers) - {"shift", "alt", "ctrl"}:
        return None
    ctrl, alt, shift = (item in modifiers for item in ("ctrl", "alt", "shift"))
    modifier = 1 + shift + 2 * alt + 4 * ctrl
    arrows = {"up": "A", "down": "B", "right": "C", "left": "D", "home": "H", "end": "F"}
    if key in arrows:
        code = arrows[key]
        return (f"\x1b[{code}" if modifier == 1 else f"\x1b[1;{modifier}{code}").encode()
    if key in {"f1", "f2", "f3", "f4"}:
        code = "PQRS"[int(key[1:]) - 1]
        return (f"\x1bO{code}" if modifier == 1 else f"\x1b[1;{modifier}{code}").encode()
    tilde_keys = {
        "insert": 2, "delete": 3, "pageup": 5, "pagedown": 6,
        "f5": 15, "f6": 17, "f7": 18, "f8": 19, "f9": 20, "f10": 21, "f11": 23, "f12": 24,
    }
    if key in tilde_keys:
        code = tilde_keys[key]
        return (f"\x1b[{code}~" if modifier == 1 else f"\x1b[{code};{modifier}~").encode()
    if modifier == 2 and key == "tab":
        return b"\x1b[Z"
    keys = {
        "enter": "\r", "esc": "\x1b", "tab": "\t", "backspace": "\x7f",
        "space": " ", "minus": "-", "comma": ",", "plus": "+", "backtick": "`",
    }
    value = keys.get(key, key)
    if len(value) != 1 or not value.isascii():
        return None
    if ctrl and not shift and ("a" <= value <= "z" or value in " @"):
        value = chr(ord(value) & 31)
    elif ctrl or (shift and not value.isalpha()):
        return f"\x1b[{ord(value)};{modifier}u".encode()
    elif shift:
        value = value.upper()
    return ("\x1b" * alt + value).encode()


_KITTY_FUNCTIONAL = {
    "enter": 13, "esc": 27, "tab": 9, "backspace": 127, "space": 32,
}
_KITTY_NAMED = {"minus": "-", "comma": ",", "plus": "+", "backtick": "`", "ampersand": "&"}
_KITTY_LETTER_KEYS = {"up": "A", "down": "B", "right": "C", "left": "D", "home": "H", "end": "F",
                      "f1": "P", "f2": "Q", "f3": "R", "f4": "S"}
_KITTY_TILDE_KEYS = {
    "insert": 2, "delete": 3, "pageup": 5, "pagedown": 6,
    "f5": 15, "f6": 17, "f7": 18, "f8": 19, "f9": 20, "f10": 21, "f11": 23, "f12": 24,
}


def kitty_keys(chord, prefix=""):
    """Encode a herdr chord as a kitty-keyboard-protocol terminal sends it.

    The private herdr client requests the protocol (CSI > 7 u). Legacy bytes
    are ambiguous there: alt+esc as ESC ESC is ignored (M5).
    """
    if chord.startswith("prefix+"):
        lead = kitty_keys(prefix) if prefix and not prefix.startswith("prefix+") else None
        tail = kitty_keys(chord.removeprefix("prefix+"))
        return lead + tail if lead and tail else None
    *modifiers, key = chord.lower().split("+")
    if not key or set(modifiers) - {"shift", "alt", "ctrl", "super"}:
        return None
    modifier = 1 + sum(bit for name, bit in (("shift", 1), ("alt", 2), ("ctrl", 4), ("super", 8))
                       if name in modifiers)
    if key in _KITTY_LETTER_KEYS:
        code = _KITTY_LETTER_KEYS[key]
        return (f"\x1b[{code}" if modifier == 1 else f"\x1b[1;{modifier}{code}").encode()
    if key in _KITTY_TILDE_KEYS:
        code = _KITTY_TILDE_KEYS[key]
        return (f"\x1b[{code}~" if modifier == 1 else f"\x1b[{code};{modifier}~").encode()
    if key in _KITTY_FUNCTIONAL:
        code = _KITTY_FUNCTIONAL[key]
        if modifier == 1 and key != "esc":
            return chr(code).encode()
        return (f"\x1b[{code}u" if modifier == 1 else f"\x1b[{code};{modifier}u").encode()
    value = _KITTY_NAMED.get(key, key)
    if len(value) != 1 or not value.isascii() or not value.isprintable():
        return None
    if modifier == 1:
        return value.encode()
    return f"\x1b[{ord(value.lower())};{modifier}u".encode()
