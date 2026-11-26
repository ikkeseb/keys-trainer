import io
import json
import os
import pty
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import termios
import textwrap
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import doctor
from router import (POP, PUSH, Click, Input, KeyboardMode, Router, ambiguous_in_legacy, canonical,
                    key_name)
from screen import Terminal

HERE = Path(__file__).resolve().parent

# The same chords as the outer terminal sends them without and with kitty disambiguate mode.
ENCODINGS = {
    "legacy": {"ctrl+c": b"\x03", "prefix": b"\x00", "esc": b"\x1b", "alt+enter": b"\x1b\r",
               "alt+shift+enter": b"\x1b\r", "alt+esc": b"\x1b\x1b", "ctrl+alt+left": b"\x1b[1;7D",
               "ctrl+alt+right": b"\x1b[1;7C", "ctrl+alt+up": b"\x1b[1;7A"},
    "kitty": {"ctrl+c": b"\x1b[99;5u", "prefix": b"\x1b[32;5u", "esc": b"\x1b[27u",
              "alt+enter": b"\x1b[13;3u", "alt+shift+enter": b"\x1b[13;4u", "alt+esc": b"\x1b[27;3u",
              "ctrl+alt+left": b"\x1b[1;7D", "ctrl+alt+right": b"\x1b[1;7C", "ctrl+alt+up": b"\x1b[1;7A"},
}
ROWS = [{"name": "ctrl+space", "value": "prefix"},
       {"name": "alt+enter", "value": "split horizontal"},
       {"name": "alt+shift+enter", "value": "split vertical"},
       {"name": "alt+esc", "value": "close pane"},
       {"name": "prefix+r", "value": "rename tab"},
       {"name": "prefix+q", "value": "reload config"}]


class KeyNameTests(unittest.TestCase):
    def test_both_encodings_name_the_same_chord(self):
        for encoding, keys in ENCODINGS.items():
            for chord, raw in keys.items():
                if (encoding, chord) == ("legacy", "alt+shift+enter"):
                    continue  # Legacy sends Alt+Enter's bytes; that is the ambiguity.
                expected = "ctrl+space" if chord == "prefix" else chord
                with self.subTest(encoding=encoding, chord=chord):
                    self.assertEqual(key_name(raw), expected)
        for raw, name in ((b"?", "?"), (b"q", "q"), (b"Q", "shift+q"), (b"\r", "enter"),
                          (b"\x1b[Z", "shift+tab"), (b"\x1b[3;5~", "ctrl+delete"), ("ø".encode(), "ø"),
                          (b"\x1b[99;69u", "ctrl+c"), (b"\x1b[97:65;4u", "alt+shift+a"), (b"\x1bOA", "up")):
            self.assertEqual(key_name(raw), name, raw)

    def test_configured_chords_match_input_names(self):
        self.assertEqual(canonical("Ctrl+Alt+Left"), "ctrl+alt+left")
        self.assertEqual(canonical("prefix+R"), "prefix+shift+r")
        self.assertEqual(canonical("shift+alt+return"), "alt+shift+enter")
        self.assertEqual(canonical("ctrl++"), "ctrl++")

    def test_legacy_ambiguity(self):
        for chord in ("alt+esc", "alt+shift+enter", "ctrl+enter", "ctrl+1"):
            self.assertTrue(ambiguous_in_legacy(chord), chord)
        for chord in ("alt+enter", "ctrl+space", "ctrl+alt+left", "prefix+x", "shift+tab", "ctrl+c"):
            self.assertFalse(ambiguous_in_legacy(chord), chord)


class InputTests(unittest.TestCase):
    def test_kitty_keys_and_utf8_survive_split_reads(self):
        reader = Input()
        self.assertEqual(list(reader.events(b"\x1b[13;")), [])
        self.assertEqual(list(reader.events(b"4u" + "æ".encode()[:1])), [b"\x1b[13;4u"])
        self.assertEqual(list(reader.events("æ".encode()[1:] + b"q")), ["æ".encode(), b"q"])
        self.assertEqual(list(reader.events(b"\x1b[<0;4;9M")), [Click(4, 9)])


class RouterRuleTests(unittest.TestCase):
    def test_controls_move_off_keys_the_bindings_claim(self):
        router = Router.from_rows(ROWS)
        self.assertEqual((router.hint_label, router.pause_label), ("?", "q"))
        router = Router.from_rows(ROWS + [{"name": "?", "value": "help"}, {"name": "q", "value": "quit"}])
        self.assertEqual((router.hint_label, router.pause_label), ("!", ";"))
        self.assertEqual(router.route(b"!"), ("trainer", "hint"))
        self.assertEqual(router.route(b"?"), ("forward", b"?"))

    def test_a_dropped_token_leaves_neither_prefix_nor_prompt_behind(self):
        # The engine drops keys outside the user's turn (the success beat): the router forgets them.
        router = Router.from_rows(ROWS)
        router.route(b"\x00")
        router.dropped()
        self.assertEqual(router.route(b"?"), ("trainer", "hint"))
        router.route(b"\x00")
        router.route(b"r")  # rename tab: the router opens the prompt itself
        self.assertTrue(router.typing)
        router.dropped()
        self.assertFalse(router.typing)
        router.typing = True  # a Yazi prompt the session reports stays open
        router.route(b"a")
        router.dropped()
        self.assertTrue(router.typing)

    def test_claims_names_the_trainer_keys_unless_a_prefix_takes_them(self):
        # The runner waits for a late prompt report only before a key the trainer would take.
        router = Router.from_rows(ROWS)
        self.assertTrue(router.claims(b"?") and router.claims(b"q"))
        self.assertFalse(router.claims(b"f") or router.claims(Click(1, 2)) or router.claims(b"\x03"))
        router.route(b"\x00")
        self.assertFalse(router.claims(b"q"))  # prefix+q goes to herdr
        router.route(b"x")
        router.typing = True
        self.assertTrue(router.claims(b"q"))  # a prompt may be closing: still worth the wait

    def test_mouse_reports_stay_with_the_trainer(self):
        self.assertEqual(Router().route(Click(1, 2)), ("click", Click(1, 2)))


def _pty_terminal(test):
    master, slave = pty.openpty()
    test.addCleanup(os.close, master)
    test.addCleanup(os.close, slave)
    stream = Mock()
    stream.fileno.return_value = slave
    return master, slave, stream


class RouterPtyTests(unittest.TestCase):
    """Every routing rule with bytes written to a real PTY and read the way the engine reads."""

    def route_through_pty(self, router, writes):
        master, _, stream = _pty_terminal(self)
        inputs, routes = Input(), []
        with patch("screen.sys.stdin", stream), redirect_stdout(io.StringIO()):
            with Terminal() as ui:
                for raw in writes:
                    os.write(master, raw)
                    got = list(inputs.events(ui.read(.3, forward_ctrl_c=True)))
                    deadline = time.monotonic() + 1
                    while inputs.held and time.monotonic() < deadline:
                        got += inputs.events(ui.read(.05, forward_ctrl_c=True))
                    routes += [router.route(token) for token in got]
        return routes

    def test_every_rule_in_both_encodings(self):
        for encoding, keys in ENCODINGS.items():
            with self.subTest(encoding=encoding):
                router = Router.from_rows(ROWS)
                plain = [b"?", b"q", keys["alt+enter"], keys["alt+esc"], keys["ctrl+alt+left"], b"x"]
                after_prefix = [keys["prefix"], b"?", keys["prefix"], b"q", keys["prefix"], keys["ctrl+c"]]
                prompt = [keys["prefix"], b"r", b"q", b"?", "ø".encode(), keys["ctrl+c"], keys["esc"], b"q"]
                routes = self.route_through_pty(router, plain + after_prefix + prompt)
                F = lambda raw: ("forward", raw)
                self.assertEqual(routes, [
                    ("trainer", "hint"), ("trainer", "pause"),
                    F(keys["alt+enter"]), F(keys["alt+esc"]), F(keys["ctrl+alt+left"]), F(b"x"),
                    # After the prefix the next key is the user's, even ? and q; Ctrl+C still quits.
                    F(keys["prefix"]), F(b"?"), F(keys["prefix"]), F(b"q"),
                    F(keys["prefix"]), ("trainer", "quit"),
                    # The rename prompt opens: q, ? and ø are typed, Ctrl+C quits, Esc closes it.
                    F(keys["prefix"]), F(b"r"), F(b"q"), F(b"?"), F("ø".encode()),
                    ("trainer", "quit"), F(keys["esc"]), ("trainer", "pause"),
                ])

    def test_engine_typing_forwards_q_until_the_engine_clears_it(self):
        for encoding, keys in ENCODINGS.items():
            with self.subTest(encoding=encoding):
                router = Router.from_rows(ROWS)
                router.typing = True  # a Yazi filter prompt is open
                routes = self.route_through_pty(router, [b"q", b"?", keys["esc"], b"q", keys["ctrl+c"]])
                self.assertEqual(routes[:4], [("forward", b"q"), ("forward", b"?"),
                                              ("forward", keys["esc"]), ("forward", b"q")])
                self.assertEqual(routes[4], ("trainer", "quit"))
                self.assertTrue(router.typing)
                router.typing = False
                self.assertEqual(router.route(b"q"), ("trainer", "pause"))


CHILD = textwrap.dedent("""
    import os, signal, sys, time
    sys.path.insert(0, sys.argv[1])
    from router import KeyboardMode
    from screen import Terminal
    how = sys.argv[2]
    if how == "launcher":
        def leave(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, leave)
    try:
        with Terminal(), KeyboardMode(timeout=2) as mode:
            sys.stdout.write(f"<inside {mode.protocol} {mode.pending!r} {mode.ambiguous('alt+esc')}>")
            sys.stdout.flush()
            if how == "raise":
                raise RuntimeError("boom")
            if how in ("signal", "launcher"):
                time.sleep(10)
    except KeyboardInterrupt:
        sys.exit(130)
""")


class KeyboardModeTests(unittest.TestCase):
    def run_child(self, how, kitty, typed=b""):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        process = subprocess.Popen([sys.executable, "-c", CHILD, str(HERE), how],
                                   stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        self.addCleanup(lambda: process.poll() is None and (process.kill(), process.wait()))
        seen, answered = bytearray(), False
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if not answered and b"\x1b[c" in seen:
                # Answer as the outer terminal would, after a key typed meanwhile.
                os.write(master, typed + (b"\x1b[?0u" if kitty else b"") + b"\x1b[?62;22c")
                answered = True
            if b"<inside" in seen and how in ("signal", "launcher") and process.poll() is None:
                time.sleep(.1)
                process.send_signal(signal.SIGTERM)
                how = "sent"
            if select.select([master], [], [], .05)[0]:
                try:
                    seen.extend(os.read(master, 65536))
                except OSError:
                    break
            elif process.poll() is not None:
                break
        return process.wait(timeout=3), bytes(seen)

    def assert_popped(self, out):
        inside = out.index(b"<inside")
        self.assertEqual(out.count(PUSH.encode()), 1)
        self.assertLess(out.index(PUSH.encode()), inside)
        pop = out.index(POP.encode(), inside)
        self.assertLess(pop, out.rindex(b"\x1b[?1049l"), "pop before leaving the alternate screen")

    def test_pushed_and_popped_on_normal_exit(self):
        code, out = self.run_child("normal", kitty=True, typed=b"x")
        self.assertEqual(code, 0)
        self.assert_popped(out)
        self.assertIn(b"<inside kitty b'x' False>", out)

    def test_popped_after_an_exception(self):
        code, out = self.run_child("raise", kitty=True)
        self.assertEqual(code, 1)
        self.assert_popped(out)
        self.assertIn(b"boom", out[out.index(POP.encode()):])

    def test_popped_on_sigterm_with_and_without_the_launcher_handler(self):
        for how in ("signal", "launcher"):
            with self.subTest(how=how):
                code, out = self.run_child(how, kitty=True)
                self.assertEqual(code, 130)
                self.assert_popped(out)

    def test_legacy_terminal_is_left_alone_and_reports_ambiguity(self):
        code, out = self.run_child("normal", kitty=False)
        self.assertEqual(code, 0)
        self.assertIn(b"<inside legacy b'' True>", out)
        self.assertNotIn(PUSH.encode(), out)
        self.assertNotIn(POP.encode(), out)

    def test_not_a_terminal_reports_legacy_without_writing(self):
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        with redirect_stdout(io.StringIO()) as out, KeyboardMode(fd=read) as mode:
            self.assertTrue(mode.legacy)
            self.assertFalse(mode.answered)
        self.assertEqual(out.getvalue(), "")


class DoctorTests(unittest.TestCase):
    def test_report_names_protocol_env_size_and_term(self):
        lines = doctor.terminal_report((0, True, b""), {"TERM": "foot"}, (100, 30))
        self.assertIn("kitty (flags 0)", lines[0])
        self.assertEqual(lines[1:], ["HERDR_ENV: not set", "Terminal size: 100 × 30", "TERM: foot"])
        lines = doctor.terminal_report((None, True, b""), {"HERDR_ENV": "1"}, (60, 15))
        self.assertIn("not supported", lines[0])
        self.assertIn("prefix twin", lines[0])
        self.assertIn("herdr's", lines[1])
        self.assertIn("two rows", lines[2])
        self.assertIn("no answer", doctor.terminal_report((None, False, b""), {}, (30, 10))[0])


class PrivateHerdr:
    """A herdr server and client of our own: socket, home and config under /tmp."""

    KEYS = {
        "prefix": "ctrl+space",
        "split_horizontal": ["prefix+h", "alt+enter"],
        "split_vertical": ["prefix+v", "alt+shift+enter"],
        "close_pane": ["prefix+x", "alt+esc"],
        "focus_pane_left": ["ctrl+alt+left"], "focus_pane_right": ["ctrl+alt+right"],
        "focus_pane_up": ["ctrl+alt+up"], "focus_pane_down": ["ctrl+alt+down"],
        "new_tab": ["prefix+c", "prefix+?"], "new_workspace": ["prefix+q"], "rename_tab": ["prefix+r"],
    }

    def __enter__(self):
        binary = shutil.which("herdr")
        self.tmp = tempfile.TemporaryDirectory(prefix="keys-router-", dir="/tmp")
        self.server = self.client = self.master = self.log = None
        try:
            self._start(binary, Path(self.tmp.name))
        except BaseException:
            self.__exit__()
            raise
        return self

    def _start(self, binary, root):
        idle = root / "idle"
        idle.write_text("#!/bin/sh\ntrap '' INT QUIT TSTP\nexec sleep 86400\n")
        idle.chmod(0o700)
        paths = {n: root / n.lower() for n in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME",
                                               "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "TMPDIR")}
        for path in paths.values():
            path.mkdir(mode=0o700)
        self.api, config = root / "api.sock", root / "config.toml"
        env = {n: str(p) for n, p in paths.items()} | {
            "TERM": "xterm-256color", "PATH": os.defpath, "SHELL": str(idle), "HERDR_CONFIG_PATH": str(config),
            "HERDR_SOCKET_PATH": str(self.api), "HERDR_SESSION": root.name}
        default = subprocess.check_output([binary, "--default-config"], text=True, env=env,
                                          cwd=paths["HOME"], timeout=2)
        section = re.split(r"^#? ?\[", default.split("\n[keys]\n", 1)[1], maxsplit=1, flags=re.M)[0]
        names = set(re.findall(r"^#?\s*(\w+)\s*=", section, re.M)) | self.KEYS.keys()
        keys = [f"prefix = {json.dumps(self.KEYS['prefix'])}"] + [
            f"{name} = {json.dumps(self.KEYS.get(name, ''))}"
            for name in sorted(names) if name != "prefix" and not name.startswith("navigate_")]
        config.write_text(
            f'onboarding = false\n[terminal]\ndefault_shell = {json.dumps(str(idle))}\n'
            'shell_mode = "non_login"\nkitty_graphics = false\n'
            '[update]\nversion_check = false\nmanifest_check = false\n'
            '[ui]\nprompt_new_tab_name = false\nhide_tab_bar_when_single_tab = false\n'
            '[ui.sound]\nenabled = false\n[ui.toast]\ndelivery = "off"\n'
            '[keys]\n' + "\n".join(keys) + "\n")
        self.log = (root / "server.log").open("w+b")
        self.server = subprocess.Popen([binary, "server"], env=env, cwd=paths["HOME"], stdin=subprocess.DEVNULL,
                                       stdout=self.log, stderr=self.log, start_new_session=True)
        deadline = time.monotonic() + 3
        while not self.api.is_socket():
            if self.server.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("The private herdr server did not start.")
            time.sleep(.02)
        self.call("workspace.create", cwd=str(paths["HOME"]), focus=True, label="Practice")
        self.master, slave = pty.openpty()
        os.set_blocking(self.master, False)
        termios.tcsetwinsize(slave, (28, 100))
        try:
            self.client = subprocess.Popen(
                [sys.executable, "-I", "-c", "import fcntl, os, sys, termios;"
                 "fcntl.ioctl(0, termios.TIOCSCTTY, 0); os.execv(sys.argv[1], sys.argv[1:])", binary],
                env=env, cwd=paths["HOME"], stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        finally:
            os.close(slave)
        seen, deadline = bytearray(), time.monotonic() + 3
        while b"\x1b[?2026l" not in seen and time.monotonic() < deadline:
            seen += self.drain(.05)
        if b"\x1b[?2026l" not in seen:
            raise RuntimeError("The private herdr client drew no frame.")

    def __exit__(self, *_):
        for process in (self.client, self.server):
            if process is None or process.poll() is not None:
                continue
            if process is self.server:
                try:
                    self.call("server.stop")
                    process.wait(1)
                except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
                    pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        if self.master is not None:
            os.close(self.master)
        if self.log is not None:
            self.log.close()
        self.tmp.cleanup()

    def call(self, method, **params):
        with socket.socket(socket.AF_UNIX) as stream:
            stream.settimeout(2)
            stream.connect(str(self.api))
            stream.sendall(json.dumps({"id": "t", "method": method, "params": params}).encode() + b"\n")
            reply = b""
            while b"\n" not in reply and (chunk := stream.recv(65536)):
                reply += chunk
        message = json.loads(reply.split(b"\n")[0])
        if "error" in message:
            raise RuntimeError(message["error"])
        return message["result"]

    def drain(self, wait=0):
        out = bytearray()
        if select.select([self.master], [], [], wait)[0]:
            while True:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
        return bytes(out)

    def feed(self, data):
        self.drain()
        os.write(self.master, data)

    def state(self):
        snapshot = self.call("session.snapshot")["snapshot"]
        layout = next(item for item in snapshot["layouts"] if item["tab_id"] == snapshot["focused_tab_id"])
        rects = {pane["pane_id"]: pane["rect"] for pane in layout["panes"]}
        tab = next(tab for tab in snapshot["tabs"] if tab["tab_id"] == snapshot["focused_tab_id"])
        return {"panes": len(rects), "rects": rects, "focus": layout["focused_pane_id"],
                "tabs": len(snapshot["tabs"]), "workspaces": len(snapshot["workspaces"]), "label": tab["label"]}

    def wait(self, check, timeout=1.5):
        deadline = time.monotonic() + timeout
        while not check(state := self.state()) and time.monotonic() < deadline:
            self.drain(.05)
        return check(state)


@unittest.skipUnless(os.getenv("KEYS_TRAINER_TEST_HERDR") == "1" and shutil.which("herdr"),
                     "opt in to private Herdr integration")
class PrivateHerdrChordTests(unittest.TestCase):
    """Exported chords, routed from both encodings, act in a private herdr (never the default socket)."""

    def play(self, herdr, router, inputs, *chunks):
        for raw in chunks:
            tokens = list(inputs.events(raw))
            if inputs.held:
                time.sleep(.12)
                tokens += inputs.events(b"")
            for token in tokens:
                kind, value = router.route(token)
                self.assertEqual(kind, "forward", token)
                # Measured: herdr can drop a key that lands within ~20 ms of the previous one.
                time.sleep(.1)
                herdr.feed(value)

    def test_listed_chords_act_in_both_encodings(self):
        for encoding, keys in ENCODINGS.items():
            with self.subTest(encoding=encoding), PrivateHerdr() as herdr:
                router, inputs = Router.from_rows(ROWS), Input()
                play = lambda *chunks: self.play(herdr, router, inputs, *chunks)
                def side_by_side(state):
                    focus = state["rects"][state["focus"]]
                    return any(r["y"] == focus["y"] and r["x"] != focus["x"] for r in state["rects"].values())
                # Ctrl+Space + letter: split into columns with the prefix twin.
                play(keys["prefix"], b"v")
                self.assertTrue(herdr.wait(lambda s: s["panes"] == 2 and side_by_side(s)))
                start = herdr.state()["focus"]
                play(keys["ctrl+alt+left"])
                self.assertTrue(herdr.wait(lambda s: s["focus"] != start))
                play(keys["ctrl+alt+right"])
                self.assertTrue(herdr.wait(lambda s: s["focus"] == start))
                play(keys["alt+enter"])
                self.assertTrue(herdr.wait(lambda s: s["panes"] == 3 and not side_by_side(s)))
                below = herdr.state()["focus"]
                play(keys["ctrl+alt+up"])
                self.assertTrue(herdr.wait(lambda s: s["focus"] != below))
                # Alt+Shift+Enter: columns with kitty; legacy sends Alt+Enter's bytes (rows).
                play(keys["alt+shift+enter"])
                self.assertTrue(herdr.wait(lambda s: s["panes"] == 4))
                self.assertEqual(side_by_side(herdr.state()), encoding == "kitty")
                # Alt+Esc closes the pane with kitty; legacy Esc Esc does nothing (M5).
                play(keys["alt+esc"])
                closed = herdr.wait(lambda s: s["panes"] == 3, timeout=2)
                self.assertEqual(closed, encoding == "kitty")
                # Ctrl+Space ? and Ctrl+Space q belong to herdr, not the trainer.
                play(keys["prefix"], b"?")
                self.assertTrue(herdr.wait(lambda s: s["tabs"] == 2))
                play(keys["prefix"], b"q")
                self.assertTrue(herdr.wait(lambda s: s["workspaces"] == 2))
                # Typing q in a rename prompt types a q.
                label = herdr.state()["label"]
                play(keys["prefix"], b"r")
                self.assertTrue(router.typing)
                time.sleep(.3)
                play(b"q", b"\r")
                self.assertFalse(router.typing)
                self.assertTrue(herdr.wait(lambda s: s["label"] == label + "q"), herdr.state()["label"])


if __name__ == "__main__":
    unittest.main()
