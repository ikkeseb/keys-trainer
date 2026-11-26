"""Lesson 8 over synthetic socket2 events: the engine on a DesktopSession, and the proof
that nothing but the active-workspace query is ever sent to a Hyprland socket.

No compositor is started: `HyprFixture` is a pipe, and the transport test serves two
Unix sockets from a temporary directory.
"""

import io
import os
import random
import socket
import tempfile
import threading
import unittest
import weakref
from pathlib import Path
from unittest.mock import patch

import curriculum
import desktop_session
import lesson
import runner
from coach import Bar
from desktop_session import DesktopSession
from hypr import HyprFixture
from lesson import BEAT, DONE, RUNNING, Engine, plan_lesson

DESKTOP_ROWS = [
    {"name": "super+1…0", "value": "Switch to workspace 1…10"},
    {"name": "super+shift+1…0", "value": "Move window to workspace 1…10"},
    {"name": "super+ctrl+tab", "value": "Former workspace"},
    {"name": "super+tab", "value": "Next workspace"},
    {"name": "super+|", "value": "Toggle Quake terminal"},
    {"name": "super+`", "value": "Toggle Quake console"},
]
DATA = {"groups": [{"id": "desktop", "rows": DESKTOP_ROWS}]}
LESSONS = curriculum.load()
HOME = 7  # the workspace the trainer's window is on


class Live:
    """The engine over a DesktopSession fed by a HyprFixture, with a hand-held clock."""

    def __init__(self, test, events=None, plan=None, seed=3, data=DATA):
        self.resolved = curriculum.resolve(LESSONS, data, desktop=True)
        if events is None:
            self.fixture = test.enterContext(HyprFixture())
            self.fixture.go(HOME)  # before the session: stale, never a move
            self.fixture.go(4)
            self.fixture.go(HOME)
            events = self.fixture.observer
        self.session = DesktopSession(events=events, rng=random.Random(seed))
        self.plan = plan or plan_lesson(self.resolved, "desktop")
        self.engine = Engine(self.plan, self.session, sleep=lambda seconds: None)
        self.now = 100.0
        self.engine.begin(self.now)

    def wait(self, seconds, step=0.05):
        end = self.now + seconds
        while self.now < end - 1e-9:
            self.now += min(step, end - self.now)
            self.engine.tick(self.now)

    def target(self):
        """The workspace the current step asks for, from its numbered solution move."""
        return next(m.number for m in self.engine.step.scenario.solution if m.number)

    def go(self, *workspaces, pause=0.0):
        for workspace in workspaces:
            self.fixture.go(workspace)
            if pause:
                self.wait(pause)

    def quake(self, shown):
        self.fixture.emit(f"activespecial>>{'special:quake' if shown else ''},DP-1")


class DesktopLessonTests(unittest.TestCase):
    def test_lesson_resolves_from_the_desktop_rows_and_is_hidden_without_hyprland(self):
        shown = curriculum.resolve(LESSONS, DATA, desktop=True)
        state = shown.lesson("desktop")
        self.assertTrue(state.visible)
        self.assertEqual([f"{s.kind}:{s.scenario}" for s in state.steps],
                         ["learn:desktop-workspace", "recall:desktop-workspace",
                          "learn:desktop-quake", "recall:desktop-quake"])
        self.assertEqual(shown.bindings["desktop:switch_workspace"].chords, ("super+1..9",))
        self.assertEqual(shown.bindings["desktop:former_workspace"].label(), "Super + Ctrl + Tab")
        self.assertEqual(shown.bindings["desktop:toggle_quake"].label(), "Super + |")
        hidden = curriculum.resolve(LESSONS, DATA)
        self.assertFalse(hidden.lesson("desktop").visible)
        self.assertIn("Hyprland is not running", "\n".join(curriculum.report(hidden)))
        no_rows = curriculum.resolve(LESSONS, {"groups": []}, desktop=True)
        self.assertFalse(no_rows.lesson("desktop").visible)

    def test_workspace_round_trip_latches_in_order_and_the_bar_is_live(self):
        live = Live(self)
        target = live.target()
        self.assertNotIn(target, (HOME,))
        bar = live.engine.bar()
        self.assertEqual(bar.tag, "LIVE")
        self.assertEqual(bar.goal, f"Go to workspace {target}, then come back here")
        self.assertEqual(bar.chord, f"Super + {target}, Super + Ctrl + Tab")
        self.assertEqual(bar.line, "This happens on your real desktop. Back here: Super + Ctrl + Tab.")
        self.assertEqual(bar.hint, "Show keys")
        live.wait(0.5)
        self.assertEqual(live.engine.state, RUNNING)  # the stale trip before the session counts for nothing
        live.go(target, HOME)  # both events between two ticks: neither transition is lost
        live.wait(0.3)
        self.assertEqual(live.engine.state, BEAT)
        result = live.engine.drain()[-1][1]
        self.assertEqual((result.scenario, result.outcome), ("desktop-workspace", "met"))

    def test_coming_back_first_does_not_count_and_the_number_varies(self):
        live = Live(self)
        first = live.target()
        live.go(first, HOME, pause=0.3)
        live.wait(1.0)
        second = live.target()  # the RECALL twin
        self.assertNotIn(second, (HOME, first))
        self.assertEqual(live.engine.step.kind, "recall")
        self.assertIn(f"workspace {second}", live.engine.bar().goal)
        live.go(second, pause=0.3)
        self.assertEqual(live.engine.state, RUNNING)
        live.go(HOME, pause=0.3)
        self.assertEqual(live.engine.state, BEAT)

    def test_a_wrong_workspace_is_a_miss_told_once_the_user_is_back(self):
        live = Live(self)
        live.go(live.target(), HOME, pause=0.3)
        live.wait(1.0)
        target = live.target()
        wrong = next(n for n in range(1, 10) if n not in (HOME, target))
        live.go(wrong, pause=0.3)
        bar = live.engine.bar()
        self.assertEqual((bar.line, bar.kind), (live.engine._live_line(), "reason"))  # away: nothing yet
        live.go(HOME, pause=0.3)
        bar = live.engine.bar()
        self.assertEqual(bar.line, f"You are on workspace {wrong}. Try again from here.")
        self.assertEqual(bar.kind, "miss")
        live.go(target, HOME, pause=0.3)
        self.assertEqual(live.engine.state, BEAT)
        self.assertEqual(live.engine.drain()[-1][1].outcome, "nudged")

    def test_quake_open_then_closed_and_a_stale_toggle_before_the_step_never_counts(self):
        live = Live(self)
        for _ in range(2):
            live.go(live.target(), HOME, pause=0.3)
            live.wait(0.2)
            # During the beat, before the quake step starts: open and close it.
            live.quake(True)
            live.quake(False)
            live.quake(True)
            live.wait(1.0)
        self.assertEqual(live.engine.step.scenario.id, "desktop-quake")
        self.assertEqual(live.engine.bar().line, "This happens on your real desktop. Back here: Super + |.")
        self.assertEqual(live.engine.bar().chord, "Super + | ×2")
        live.quake(False)  # closing what was open before the step is not «open, then closed»
        live.wait(0.3)
        self.assertEqual(live.engine.state, RUNNING)
        live.quake(True)
        live.wait(0.3)
        self.assertEqual(live.engine.state, RUNNING)
        live.quake(False)
        live.wait(0.3)
        self.assertEqual(live.engine.state, BEAT)

    def test_quake_teaches_and_observes_the_selected_workspace(self):
        console_only = [row for row in DESKTOP_ROWS if row["value"] != "Toggle Quake terminal"]
        for rows, name, chord, workspace, other in (
            (DESKTOP_ROWS, "terminal", "Super + |", "quake", "qconsole"),
            (console_only, "console", "Super + `", "qconsole", "quake"),
        ):
            with self.subTest(name=name):
                live = Live(self, data={"groups": [{"id": "desktop", "rows": rows}]})
                for _ in range(2):
                    live.go(live.target(), HOME, pause=0.3)
                    live.wait(1.0)
                self.assertEqual(live.engine.bar().goal, f"Open the Quake {name}, then close it again")
                self.assertEqual(live.engine.bar().chord, f"{chord} ×2")
                for target, expected in ((other, RUNNING), (workspace, BEAT)):
                    live.fixture.emit(f"activespecial>>special:{target},DP-1")
                    live.wait(0.3)
                    live.fixture.emit("activespecial>>,DP-1")
                    live.wait(0.3)
                    self.assertEqual(live.engine.state, expected)

    def test_quake_without_either_bind_shows_a_missing_console_key_notice(self):
        rows = [row for row in DESKTOP_ROWS if "Quake" not in row["value"]]
        data = {"groups": [{"id": "desktop", "rows": rows}]}
        plan = plan_lesson(curriculum.resolve(LESSONS, data, desktop=True), "desktop")
        self.assertEqual(plan.steps[-1].missing[0]["action"], "Toggle Quake console")

    def test_a_wrong_special_workspace_is_named_on_return(self):
        live = Live(self)
        for _ in range(2):
            live.go(live.target(), HOME, pause=0.3)
            live.wait(1.0)
        live.fixture.emit("activespecial>>special:qconsole,DP-1")
        live.wait(0.3)
        live.fixture.emit("activespecial>>,DP-1")
        live.wait(0.3)
        self.assertEqual(live.engine.bar().line, "The Quake console opened. Try again from here.")

    def test_hint_in_live_shows_the_keys_and_never_plays(self):
        live = Live(self)
        target = live.target()
        self.assertTrue(live.engine.hint())
        bar = live.engine.bar()
        self.assertEqual(bar.line, f"Keys · Super + {target}, then Super + Ctrl + Tab")
        self.assertEqual((bar.kind, bar.hint), ("hint", "Show keys"))
        self.assertTrue(live.engine.hint())  # again: the same keys, never «Show me»
        live.wait(2.0)
        self.assertEqual(live.engine.state, RUNNING)
        self.assertFalse(live.engine._show(live.now))
        with self.assertRaises(RuntimeError):  # and the session could not if asked
            live.session.play("desktop:toggle_quake")
        live.go(target, HOME, pause=0.3)
        self.assertEqual(live.engine.drain()[-1][1].hint_level, 2)

    def test_the_whole_lesson_runs_to_its_end(self):
        live = Live(self)
        for _ in range(2):
            live.go(live.target(), HOME, pause=0.3)
            live.wait(1.0)
        for _ in range(2):
            live.quake(True)
            live.wait(0.3)
            live.quake(False)
            live.wait(1.3)  # settle and the beat
        self.assertEqual(live.engine.state, DONE)
        result = live.engine.result
        self.assertTrue(result.complete)
        self.assertEqual([s.outcome for s in result.steps], ["met", "clean", "met", "clean"])
        self.assertEqual(live.engine.rebuilds, 0)

    def test_warm_up_of_desktop_cards_is_a_live_plan(self):
        resolved = curriculum.resolve(LESSONS, DATA, desktop=True)
        plan = lesson.plan_warmup(resolved, ["desktop:toggle_quake", "desktop:switch_workspace"], tool="desktop")
        self.assertEqual([s.scenario.id for s in plan.steps], ["desktop-quake", "desktop-workspace"])
        self.assertEqual(plan.fixture, "live")


class LiveViewTests(unittest.TestCase):
    class UI:
        def __init__(self):
            self.cache, self.size, self.painted = {}, None, []

        def paint(self, rows, margin, size):
            self.painted.append(rows)
            self.size = size

    def test_the_card_and_the_bar_are_drawn_once_per_change(self):
        ui, out = self.UI(), io.StringIO()
        view = runner.LiveView(ui, out)
        bar = Bar("LIVE", "Go to workspace 4, then come back here", line="This happens on your real desktop.",
                  title="Desktop (live)", steps=("current", "todo"))
        view.show_bar(bar, size=(100, 30))
        card = "\n".join(ui.painted[-1].values())
        self.assertIn("DESKTOP · LIVE", card)
        self.assertIn("Go to workspace 4", card)
        self.assertIn("The trainer only watches", card)
        self.assertTrue(all(row < 28 for row in ui.painted[-1]))  # above the bar's three rows
        self.assertIn("\x1b[30;1H", out.getvalue())
        written = out.tell()
        view.show_bar(bar, size=(100, 30))
        self.assertEqual(out.tell(), written)  # nothing changed, nothing painted
        view.show_bar(bar, size=(30, 10))  # too small: the notice, no card
        self.assertIn("Enlarge the window", out.getvalue())

    def test_run_lesson_picks_the_live_view_for_a_session_without_herdr(self):
        self.assertIsNone(DesktopSession(events=object()).herdr)


class NeverWritesTests(unittest.TestCase):
    """The hard limit: only `j/activeworkspace`, on the control socket, ever leaves the trainer."""

    def test_a_whole_live_lesson_sends_only_the_active_workspace_query(self):
        runtime = Path(self.enterContext(tempfile.TemporaryDirectory()))
        instance = runtime / "hypr" / "trainer-spy"
        instance.mkdir(parents=True)
        servers = {}
        for name in (".socket.sock", ".socket2.sock"):
            server = self.enterContext(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
            try:
                server.bind(str(instance / name))
            except PermissionError:
                self.skipTest("The runtime does not permit filesystem Unix sockets")
            server.listen()
            server.settimeout(2)
            servers[name] = server
        queries, stop = [], threading.Event()

        def control():
            servers[".socket.sock"].settimeout(0.1)
            while not stop.is_set():
                try:
                    connection, _ = servers[".socket.sock"].accept()
                except TimeoutError:
                    continue
                with connection:
                    connection.settimeout(1)
                    queries.append(connection.recv(4096))
                    connection.sendall(b'{"id": 7, "name": "7"}')

        thread = threading.Thread(target=control, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(stop.set)

        # Spy on every client socket the trainer opens (the test's own servers accept, never connect).
        sent, connected, clients = [], [], weakref.WeakSet()
        real_connect, real_sendall, real_send = socket.socket.connect, socket.socket.sendall, socket.socket.send

        def spy_connect(sock, address):
            connected.append(address)
            clients.add(sock)
            return real_connect(sock, address)

        def spy_sendall(sock, data, *args):
            if sock in clients:
                sent.append(bytes(data))
            return real_sendall(sock, data, *args)

        def spy_send(sock, data, *args):
            if sock in clients:
                sent.append(bytes(data))
            return real_send(sock, data, *args)

        env = {"XDG_RUNTIME_DIR": str(runtime), "HYPRLAND_INSTANCE_SIGNATURE": "trainer-spy"}
        with patch.dict(os.environ, env), patch.object(socket.socket, "connect", spy_connect), \
                patch.object(socket.socket, "sendall", spy_sendall), patch.object(socket.socket, "send", spy_send), \
                patch.object(socket.socket, "sendto", side_effect=AssertionError("sendto")), \
                patch.object(socket.socket, "sendmsg", side_effect=AssertionError("sendmsg")):
            session = desktop_session.open_live(rng=random.Random(1))
            plan = plan_lesson(curriculum.resolve(LESSONS, DATA, desktop=True), "desktop")
            with session:
                engine = Engine(plan, session, sleep=lambda seconds: None)
                engine.begin(0.0)
                events = _event_stream(servers[".socket2.sock"])
                self.addCleanup(events.close)
                clock = [0.0]

                def wait(seconds):
                    end = clock[0] + seconds
                    while clock[0] < end:
                        clock[0] += 0.05
                        threading.Event().wait(0.002)
                        engine.tick(clock[0])

                for _ in range(2):
                    target = next(m.number for m in engine.step.scenario.solution if m.number)
                    events.sendall(f"workspacev2>>{target},{target}\nworkspacev2>>7,7\n".encode())
                    wait(1.5)
                for _ in range(2):
                    events.sendall(b"activespecial>>special:quake,DP-1\n")
                    wait(0.3)
                    events.sendall(b"activespecialv2>>-98,,DP-1\n")
                    wait(1.5)
                self.assertEqual(engine.state, DONE)
                self.assertTrue(engine.hint() is False)  # done: nothing more to show
        events.settimeout(0.2)
        try:
            to_socket2 = events.recv(4096)
        except TimeoutError:
            to_socket2 = b""
        self.assertEqual(to_socket2, b"")  # nothing ever written to the event socket
        self.assertTrue(queries)
        self.assertEqual(set(queries), {b"j/activeworkspace"})
        self.assertEqual(set(sent), {b"j/activeworkspace"})
        self.assertEqual({Path(address).name for address in connected}, {".socket.sock", ".socket2.sock"})


def _event_stream(server):
    """The connection that stays open: `hypr` probes socket2 with connect-and-close first."""
    while True:
        conn, _ = server.accept()
        conn.settimeout(0.05)
        try:
            closed = conn.recv(1, socket.MSG_PEEK) == b""
        except TimeoutError:
            closed = False
        if not closed:
            conn.settimeout(None)
            return conn
        conn.close()


if __name__ == "__main__":
    unittest.main()
