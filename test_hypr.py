import os
import select
import socket
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from hypr import HyprFixture, HyprObserver, WorkspaceEvents, fold, workspace_chords
from world import DesktopWorld


class WorkspaceChordTests(unittest.TestCase):
    def test_reads_actual_bindings_and_expands_digit_range(self):
        payload = {"groups": [
            {"id": "herdr", "rows": [{"name": "ctrl+1", "value": "Switch to workspace 1"}]},
            {"id": "desktop", "rows": [
                {"name": "alt+1…0", "value": "Switch to workspace 1…10"},
                {"name": "alt+zero", "value": "Switch to workspace 10"},
                {"name": "super+shift+1…0", "value": "Move window to workspace 1…10"},
            ]},
        ]}
        self.assertEqual(workspace_chords(payload), {**{i: f"alt+{i}" for i in range(1, 10)}, 10: "alt+zero"})

    def test_skips_ambiguous_binding_and_retains_individual_binding(self):
        payload = {"groups": [{"id": "desktop", "rows": [
            {"name": "super+1…8", "value": "Switch to workspace 1…10"},
            {"name": "super+return", "value": "Switch to workspace 5"},
        ]}]}
        self.assertEqual(workspace_chords(payload), {5: "super+return"})

    def test_omits_chord_shared_by_multiple_targets(self):
        payload = {"groups": [{"id": "desktop", "rows": [
            {"name": "super+alt+1", "value": "Switch to workspace 1"},
            {"name": "Alt+Super+1", "value": "Switch to workspace 2"},
            {"name": "super+3", "value": "Switch to workspace 3"},
        ]}]}
        self.assertEqual(workspace_chords(payload), {3: "super+3"})


class WorkspaceEventTests(unittest.TestCase):
    def setUp(self):
        fd, self.source = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self.observer = WorkspaceEvents(fd, lambda: 1)
        self.addCleanup(self.observer.close)
        self.addCleanup(self.close_source)

    def close_source(self):
        if self.source is not None:
            os.close(self.source)
            self.source = None

    def test_arm_drops_old_lines_and_a_split_frame_and_lines_waits_for_the_newline(self):
        os.write(self.source, b"workspacev2>>2,old\nworkspacev2>>2,par")
        self.observer.arm()
        os.write(self.source, b"tial\nfocusedmonv2>>DP-1,2\nworkspacev2>>3,T")
        self.assertEqual(list(self.observer.lines()), ["focusedmonv2>>DP-1,2"])
        os.write(self.source, b"wo, renamed\n")
        self.assertEqual(list(self.observer.lines()), ["workspacev2>>3,Two, renamed"])
        self.assertEqual(list(self.observer.lines()), [])

    def test_reports_disconnected_event_stream(self):
        self.close_source()
        with self.assertRaises(ConnectionError):
            list(self.observer.lines())

    def test_fixture_drives_the_same_stream(self):
        with HyprFixture() as fixture:
            events = fixture.observer
            self.assertEqual(events.active_workspace(), 1)
            events.arm()
            fixture.go(1)  # no change, no event
            fixture.go(10)
            self.assertEqual(list(events.lines()), ["workspacev2>>10,10"])
            self.assertEqual(events.active_workspace(), 10)


class FoldTests(unittest.TestCase):
    def fold_all(self, *lines, world=DesktopWorld(7)):
        for line in lines:
            world = fold(world, line)
        return world

    def test_workspace_events_move_the_focused_workspace(self):
        self.assertEqual(self.fold_all("workspacev2>>4,4").workspace, 4)
        self.assertEqual(self.fold_all("workspacev2>>4,four, with comma").workspace, 4)
        for ignored in ("workspacev2>>-98,special:quake", "workspacev2>>x,4", "workspace>>4",
                        "focusedmonv2>>DP-2,3", "openwindow>>abc,1,foot,title", "garbage"):
            with self.subTest(line=ignored):
                self.assertEqual(self.fold_all(ignored), DesktopWorld(7))

    def test_quake_shows_and_hides_in_each_format_as_one_change(self):
        for event, prefix in (("activespecial", ""), ("activespecialv2", "-98,")):
            with self.subTest(event=event):
                shown = self.fold_all(f"{event}>>{prefix}special:quake,DP-1")
                self.assertEqual(shown.shown, {"quake"})
                self.assertEqual(shown.workspace, 7)
                self.assertEqual(fold(shown, f"{event}>>{prefix},DP-1"), DesktopWorld(7))
        both = self.fold_all("activespecial>>special:quake,DP-1", "activespecialv2>>-98,special:quake,DP-1")
        self.assertEqual(both, self.fold_all("activespecial>>special:quake,DP-1"))

    def test_specials_are_kept_per_monitor(self):
        world = self.fold_all("activespecial>>special:quake,DP-1", "activespecial>>special:music,DP-2")
        self.assertEqual(world.shown, {"quake", "music"})
        world = fold(world, "activespecial>>,DP-2")
        self.assertEqual(world.special, (("DP-1", "quake"),))
        self.assertEqual(fold(world, "activespecial>>special:quake"), world)  # no monitor: not an event


class HyprObserverTransportTests(unittest.TestCase):
    def test_reads_only_from_explicit_unix_socket_fixtures(self):
        runtime = Path(self.enterContext(tempfile.TemporaryDirectory()))
        signature = "trainer-fixture"
        instance = runtime / "hypr" / signature
        instance.mkdir(parents=True)
        env = {"XDG_RUNTIME_DIR": str(runtime), "HYPRLAND_INSTANCE_SIGNATURE": signature}
        self.assertFalse(HyprObserver.running(env))  # no event socket yet

        events_socket = self.listen(instance / ".socket2.sock")
        control_socket = self.listen(instance / ".socket.sock")
        self.assertTrue(HyprObserver.running(env))
        with patch.dict(os.environ, env):
            observer = HyprObserver.from_environment()

        def serve_workspace_query():
            connection, _ = control_socket.accept()
            with connection:
                connection.settimeout(1)
                request = connection.recv(4096)
                connection.sendall(b'{"id": 7, "name": "home"}')
                return request

        self.drop_probes(events_socket)
        with observer as stream, ThreadPoolExecutor(max_workers=1) as pool:
            events, _ = events_socket.accept()
            with events:
                query = pool.submit(serve_workspace_query)
                self.assertEqual(stream.active_workspace(), 7)
                self.assertEqual(query.result(timeout=1), b"j/activeworkspace")
                stream.arm()
                events.sendall(b"workspacev2>>10,ten\n")
                select.select([stream], [], [], 1)
                self.assertEqual(list(stream.lines()), ["workspacev2>>10,ten"])

    def test_a_stale_signature_falls_back_to_the_live_instance(self):
        runtime = Path(self.enterContext(tempfile.TemporaryDirectory()))
        dead = runtime / "hypr" / "dead-instance"
        live = runtime / "hypr" / "live-instance"
        dead.mkdir(parents=True)
        live.mkdir(parents=True)
        stale = self.listen(dead / ".socket2.sock")
        stale.close()  # the file stays, nobody listens: connect is refused
        events_socket = self.listen(live / ".socket2.sock")
        control_socket = self.listen(live / ".socket.sock")
        env = {"XDG_RUNTIME_DIR": str(runtime), "HYPRLAND_INSTANCE_SIGNATURE": "dead-instance"}
        self.assertTrue(HyprObserver.running(env))
        observer = HyprObserver.from_environment(env)
        self.assertEqual(observer.instance_dir, live)

        def serve():
            connection, _ = control_socket.accept()
            with connection:
                connection.settimeout(1)
                connection.recv(4096)
                connection.sendall(b'{"id": 3}')

        with observer as stream, ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(serve)
            self.assertEqual(stream.active_workspace(), 3)
        events_socket.close()

    def test_no_live_instance_means_not_running(self):
        runtime = Path(self.enterContext(tempfile.TemporaryDirectory()))
        dead = runtime / "hypr" / "dead-instance"
        dead.mkdir(parents=True)
        self.listen(dead / ".socket2.sock").close()
        env = {"XDG_RUNTIME_DIR": str(runtime), "HYPRLAND_INSTANCE_SIGNATURE": "dead-instance"}
        self.assertFalse(HyprObserver.running(env))

    def drop_probes(self, server):
        """Connect-and-close probes (`running`, `from_environment`) queue up on a fixture socket."""
        server.settimeout(0.05)
        try:
            while True:
                server.accept()[0].close()
        except (TimeoutError, BlockingIOError):
            server.settimeout(1)

    def listen(self, path):
        try:
            server = self.enterContext(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
            server.bind(str(path))
        except PermissionError:
            self.skipTest("The runtime does not permit filesystem Unix sockets")
        server.settimeout(1)
        server.listen()
        return server
