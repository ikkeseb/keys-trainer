import errno
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import checks
import curriculum
import keyscan
import landlock
from world import YaziWorld
from yazi_backend import (
    FIXTURES,
    KEYMAP,
    OBSERVE,
    PRACTICE_TREE,
    YaziSession,
    YaziState,
    YaziUnavailable,
    _session_event,
    chord_keys,
    session_bindings,
    session_keymap,
    yazi_bindings,
)


ROWS = [
    {"name": "l", "value": "inn i mappa"},
    {"name": "right", "value": "enter"},
    {"name": "h", "value": "opp til mappa over"},
    {"name": "j", "value": "neste fil"},
    {"name": "space", "value": "marker eller avmarker fila, og gå til neste"},
    {"name": ";", "value": "shell --interactive"},
]


class YaziUnitTests(unittest.TestCase):
    def test_only_fixed_action_commands_and_real_aliases_are_exported(self):
        bindings = yazi_bindings(ROWS)
        self.assertEqual(bindings["enter_folder"], [("l", b"l"), ("right", b"\x1b[C")])
        keymap = tomllib.loads(session_keymap(session_bindings(ROWS, KEYMAP)))["mgr"]["keymap"]
        self.assertEqual(keymap[1], {"on": ["<Right>"], "run": "plugin trainer-enter"})
        self.assertEqual(keymap[2]["run"], "plugin trainer-leave")
        self.assertNotIn("shell --interactive", [entry["run"] for entry in keymap])

    def test_play_takes_the_protocols_number_and_refuses_a_real_one(self):
        with self.assertRaisesRegex(ValueError, "does not take a number"):
            YaziSession(ROWS).play("yazi:top", number=3)
        with self.assertRaisesRegex(ValueError, "No chord"):  # number=None is accepted
            YaziSession(ROWS).play("yazi:top", number=None)


DATA = Path(__file__).with_name("test_data")
YAZI_ROWS = json.loads((DATA / "rigpage_yazi.json").read_text(encoding="utf-8"))
HERDR_ROWS = json.loads((DATA / "rigpage_herdr.json").read_text(encoding="utf-8"))


class YaziSessionUnitTests(unittest.TestCase):
    def test_export_rows_resolve_every_action(self):
        bindings = session_bindings(YAZI_ROWS, KEYMAP)
        chords = {action: [chord for chord, _ in pairs] for action, pairs in bindings.items()}
        self.assertEqual(chords, {
            "enter_folder": ["l", "right"], "leave_folder": ["h", "left"], "next_file": ["j", "down"],
            "prev_file": ["k", "up"], "top": ["g, g", "home"], "bottom": ["G", "end"],
            "toggle_hidden": ["."], "filter": ["f"], "mark_one": ["space"], "visual": ["v"],
            "escape": ["esc"], "yank": ["y"], "cut": ["x"], "paste": ["p"],
            "copy_path": ["c, c"], "copy_filename": ["c, f"], "copy_dirpath": ["c, d"], "select_all": ["ctrl+a"],
            "create": ["a"], "rename": ["r"], "trash": ["d"], "back": ["H"], "forward": ["L"], "new_tab": ["t, t"],
        })
        self.assertEqual(bindings["top"][0], ("g, g", ("g", "g")))
        self.assertEqual(bindings["mark_one"][0], ("space", ("<Space>",)))

    def test_a_rebound_literal_command_resolves_and_a_missing_action_is_inactive(self):
        rows = [{"name": "ctrl+t", "value": "arrow top"}, {"name": "J", "value": "neste fil"},
                {"name": "q", "value": "quit"}]
        bindings = session_bindings(rows, KEYMAP)
        self.assertEqual(set(bindings), {"top", "next_file"})
        self.assertEqual(bindings["top"], [("ctrl+t", ("<C-t>",))])
        # Only the session's actions are bound, even when the export has more.
        self.assertEqual(set(session_bindings(rows, ["top"])), {"top"})

    def test_private_keymap_runs_only_table_commands(self):
        keymap = tomllib.loads(session_keymap(session_bindings(YAZI_ROWS, KEYMAP)))["mgr"]["keymap"]
        runs = {json.dumps(entry["run"]) for entry in keymap}
        self.assertLessEqual(runs, {json.dumps(command) for _, _, command in KEYMAP.values()})
        self.assertIn({"on": ["g", "g"], "run": "arrow top"}, keymap)
        self.assertIn({"on": ["G"], "run": "arrow bot"}, keymap)
        self.assertIn({"on": ["l"], "run": "plugin trainer-enter"}, keymap)
        self.assertNotIn("q", [entry["on"][0] for entry in keymap])  # the quit key stays unbound

    def test_chords_encode_for_the_kitty_client(self):
        self.assertEqual(chord_keys("G"), (b"G",))
        self.assertEqual(chord_keys("g, g"), (b"g", b"g"))
        self.assertEqual(chord_keys("."), (b".",))
        self.assertEqual(chord_keys("space"), (b" ",))
        self.assertEqual(chord_keys("esc"), (b"\x1b[27u",))
        self.assertEqual(chord_keys("home"), (b"\x1b[H",))
        self.assertEqual(chord_keys("ctrl+a"), (b"\x1b[97;5u",))
        self.assertIsNone(chord_keys("super+nothing"))

    def test_hook_events_become_worlds_inside_the_tree_only(self):
        session = YaziSession(YAZI_ROWS)
        session._home = Path("/tmp/private/home")
        body = {"cwd": "/tmp/private/home/Practice/Projects", "hovered": "", "entries": {}, "selected": {},
                "show_hidden": True, "filter": "no", "layer": "input", "tab_count": 1, "tab_index": 0,
                "yanked": 2, "cut": True, "visual": False}
        state = session._as_state(body)
        self.assertIsInstance(state, YaziWorld)
        self.assertEqual((state.cwd, state.hovered, state.entries, state.selected), ("Practice/Projects", None, (), frozenset()))
        self.assertEqual((state.show_hidden, state.filter, state.layer, state.yanked, state.cut),
                         (True, "no", "input", 2, True))
        for cwd in ("/tmp/private/home", "/tmp/private/home/Practice/Projects/deeper", "/tmp/elsewhere/Practice"):
            with self.assertRaises(YaziUnavailable):
                session._as_state({**body, "cwd": cwd})
        line = b"keys-trainer-state,1,1," + json.dumps(body).encode()
        self.assertEqual(_session_event(line), body)
        self.assertIsNone(_session_event(b"keys-trainer-state,1,2," + json.dumps(body).encode()))
        self.assertIsNone(_session_event(b"hover,1,1,{}"))
        self.assertIsNone(_session_event(b"keys-trainer-state,1,1,{broken"))

    def test_two_hook_events_in_one_poll_give_two_worlds_in_order(self):
        import tempfile

        class Stream:
            def __init__(self, data):
                self.data = data

            def read(self):
                data, self.data = self.data, b""
                return data

        class FakeHerdr:
            def world(self, max_age=0):
                return None

        with tempfile.TemporaryDirectory() as home:
            session = YaziSession(YAZI_ROWS)
            session._home = Path(home)
            session.herdr = FakeHerdr()
            lines = b""
            for hovered in ("notes.txt", "draft.txt"):
                body = {"cwd": f"{home}/Practice", "hovered": hovered, "entries": {}, "selected": {},
                        "show_hidden": False, "filter": "", "layer": "mgr", "tab_count": 1, "tab_index": 0,
                        "yanked": 0, "cut": False, "visual": False}
                lines += b"keys-trainer-state,1,1," + json.dumps(body).encode() + b"\n"
            session._events = Stream(lines)
            self.assertEqual(session.world().yazi.hovered, "notes.txt")
            self.assertEqual(session.world().yazi.hovered, "draft.txt")
            self.assertEqual(session.world().yazi.hovered, "draft.txt")  # then the latest, stable

    def test_the_hook_reports_every_world_field(self):
        for name in YaziWorld.__dataclass_fields__:
            self.assertIn(f"{name} = ", OBSERVE)
        self.assertNotIn("is_visual", OBSERVE)  # M9: a deprecation toast; visual is not is_normal

    def test_private_keymap_overrides_input_without_clipboard_commands(self):
        parsed = tomllib.loads(session_keymap(session_bindings(YAZI_ROWS, KEYMAP)))
        inp = parsed["input"]["keymap"]
        runs = [cmd for entry in inp for cmd in (entry["run"] if isinstance(entry["run"], list) else [entry["run"]])]
        for forbidden in ("delete", "kill", "yank", "paste", "copy", "help"):
            self.assertFalse(any(cmd == forbidden or cmd.startswith(forbidden + " ") for cmd in runs), forbidden)
        for required in ("close", "close --submit", "escape", "backspace", "move -1", "move 1"):
            self.assertIn(required, runs)
        # Measure the installed Yazi binary's built-in keymap for other reachable layers.
        if binary := shutil.which("yazi"):
            builtin = keyscan.yazi_builtin(Path(binary).read_bytes())
            for layer in ("confirm", "cmp", "help"):
                layer_runs = [
                    cmd for entry in builtin.get(layer, {}).get("keymap", ())
                    for cmd in (entry["run"] if isinstance(entry["run"], list) else [entry["run"]])
                ]
                for forbidden in ("copy", "yank", "paste", "delete"):
                    self.assertFalse(
                        any(cmd == forbidden or cmd.startswith(forbidden + " ") for cmd in layer_runs),
                        f"{layer} has {forbidden}",
                    )

    def test_landlock_write_rights_follow_abi(self):
        abi1 = landlock.write_rights(1)
        self.assertTrue(abi1 & landlock.FS_WRITE_FILE)
        self.assertFalse(abi1 & (landlock.FS_REFER | landlock.FS_TRUNCATE))
        self.assertFalse(abi1 & ((1 << 0) | (1 << 2) | (1 << 3)))  # execute, read_file, read_dir unhandled
        self.assertTrue(landlock.write_rights(2) & landlock.FS_REFER)
        self.assertFalse(landlock.write_rights(2) & landlock.FS_TRUNCATE)
        self.assertTrue(landlock.write_rights(3) & landlock.FS_TRUNCATE)

    def test_a_kernel_that_cannot_block_truncation_fails_closed(self):
        with patch.object(landlock, "_libc") as libc:
            libc.return_value.syscall.return_value = 2  # Landlock ABI 2: truncate is not handled
            with self.assertRaisesRegex(OSError, "Linux 6.2"):
                landlock.abi_version()
            libc.return_value.syscall.return_value = 3
            self.assertEqual(landlock.abi_version(), 3)

    def test_landlock_leaves_the_rest_of_dev_closed(self):
        launcher = Path(__file__).with_name("landlock.py")
        probe = "import os, sys\nos.truncate(sys.argv[1], 0)\n"
        with tempfile.TemporaryDirectory(prefix="keys-ll-root-", dir="/tmp") as root, \
             tempfile.NamedTemporaryFile(prefix="keys-ll-", dir="/dev/shm") as other:
            other.write(b"keep")
            other.flush()
            result = subprocess.run(
                [sys.executable, "-IS", str(launcher), "--root", root, "--",
                 sys.executable, "-c", probe, other.name],
                capture_output=True, text=True, timeout=10,
            )
            self.assertIn("PermissionError", result.stderr)
            self.assertEqual(Path(other.name).read_bytes(), b"keep")

    def test_landlock_unavailable_fails_closed(self):
        with patch.object(landlock, "abi_version", side_effect=OSError(errno.ENOSYS, "Landlock unavailable")):
            with self.assertRaisesRegex(YaziUnavailable, "did not start"):
                YaziSession(YAZI_ROWS).start(["yazi:top"])
        with tempfile.TemporaryDirectory(prefix="keys-landlock-") as tmp:
            err_file = Path(tmp) / "err"
            stderr = io.StringIO()
            with patch.object(landlock, "restrict_writes", side_effect=OSError(errno.ENOSYS, "no landlock")), \
                 patch.object(os, "execv") as execv, redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as exited:
                    landlock.main(["--root", tmp, "--error-file", str(err_file), "--", "/bin/true"])
                self.assertEqual(exited.exception.code, 126)
                execv.assert_not_called()
            self.assertIn("no landlock", err_file.read_text())
            self.assertIn("no landlock", stderr.getvalue())

    def test_landlock_launcher_blocks_writes_outside_root_and_allows_inside(self):
        launcher = Path(__file__).with_name("landlock.py")
        probe = (
            "import os, sys\n"
            "from pathlib import Path\n"
            "root, outside = Path(sys.argv[1]), Path(sys.argv[2])\n"
            "# Inside root: create, write, truncate, cross-dir rename, remove all succeed.\n"
            "(root / 'sub').mkdir()\n"
            "f = root / 'a.txt'\n"
            "f.write_text('hello')\n"
            "f.write_text('hi')\n"
            "moved = root / 'sub' / 'b.txt'\n"
            "f.rename(moved)\n"
            "assert moved.read_text() == 'hi'\n"
            "moved.unlink()\n"
            "# /dev/null write and outside read stay allowed.\n"
            "with open('/dev/null', 'w') as devnull: devnull.write('ok')\n"
            "assert (outside / 'existing.txt').read_text() == 'keep'\n"
            "# Outside root: create, overwrite, truncate, remove, rename all fail.\n"
            "for op in (\n"
            "    lambda: (outside / 'new.txt').write_text('x'),\n"
            "    lambda: (outside / 'existing.txt').write_text('overwrite'),\n"
            "    lambda: os.truncate(outside / 'existing.txt', 0),\n"
            "    lambda: (outside / 'existing.txt').unlink(),\n"
            "    lambda: (outside / 'existing.txt').rename(root / 'stolen.txt'),\n"
            "):\n"
            "    try:\n"
            "        op()\n"
            "    except PermissionError:\n"
            "        pass\n"
            "    else:\n"
            "        raise AssertionError('outside write was not blocked')\n"
            "inside = root / 'stay.txt'\n"
            "inside.write_text('ok')\n"
            "try:\n"
            "    inside.rename(outside / 'escaped.txt')\n"
            "except PermissionError:\n"
            "    pass\n"
            "else:\n"
            "    raise AssertionError('rename out of root was not blocked')\n"
        )
        with tempfile.TemporaryDirectory(prefix="keys-ll-root-", dir="/tmp") as root, \
             tempfile.TemporaryDirectory(prefix="keys-ll-out-", dir="/tmp") as outside:
            (Path(outside) / "existing.txt").write_text("keep")
            result = subprocess.run(
                [sys.executable, "-IS", str(launcher), "--root", root, "--",
                 sys.executable, "-c", probe, root, outside],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((Path(outside) / "existing.txt").read_text(), "keep")
            self.assertFalse((Path(outside) / "new.txt").exists())
            self.assertFalse((Path(outside) / "escaped.txt").exists())

    def test_unknown_cards_and_fixtures_are_refused_before_starting(self):
        session = YaziSession(YAZI_ROWS)
        with self.assertRaises(ValueError):
            session.start(["yazi:delete_everything"])
        with self.assertRaises(ValueError):
            session.start(["herdr:split_vertical"])
        with self.assertRaises(ValueError):
            session.start(["yazi:top"], "elsewhere")
        self.assertIn("practice_tree", FIXTURES)


def _yazi_pids():
    pids = set()
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit():
            try:
                if (proc / "comm").read_text().strip() == "yazi":
                    pids.add(int(proc.name))
            except OSError:
                pass
    return pids


def _running_in(root):
    """Processes whose working directory is inside `root` (the private herdr runs in its home)."""
    found = []
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit():
            try:
                if Path(os.readlink(proc / "cwd")).is_relative_to(root):
                    found.append(int(proc.name))
            except OSError:
                pass
    return found


def settled(session):
    """The newest world: world() hands out one hook event per call, so drain what is queued."""
    world = session.world()
    while session._queue:
        world = session.world()
    return world


@unittest.skipUnless(
    os.environ.get("KEYS_TRAINER_TEST_YAZI") == "1" and os.environ.get("KEYS_TRAINER_TEST_HERDR") == "1"
    and shutil.which("yazi") and shutil.which("herdr"),
    "set KEYS_TRAINER_TEST_YAZI=1 and KEYS_TRAINER_TEST_HERDR=1 to test Yazi inside a private herdr",
)
class YaziSessionTests(unittest.TestCase):
    def test_back_and_forward_typed_with_no_gap_still_count(self):
        scenario = curriculum.load().scenarios["yazi-history"]
        cards = ["yazi:enter_folder", "yazi:back", "yazi:forward"]
        for attempt in range(5):
            with self.subTest(attempt=attempt), YaziSession(YAZI_ROWS, HERDR_ROWS) as session:
                start = session.start(cards, "practice_tree")
                session.play("yazi:enter_folder")
                history = [settled(session)]
                for card in cards[1:]:
                    for raw in chord_keys(session.chords(card)[0]):
                        session.feed(raw)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not checks.evaluate(scenario.checks, start, history).done:
                    history.append(session.world())
                    time.sleep(0.02)
                self.assertTrue(checks.evaluate(scenario.checks, start, history).done, history[-1].yazi)

    def test_yazi_runs_in_the_one_pane_and_close_leaves_nothing(self):
        before = _yazi_pids()
        with YaziSession(YAZI_ROWS, HERDR_ROWS) as session:
            world = session.start(["yazi:top"], "practice_tree")
            self.assertEqual((world.yazi.cwd, world.yazi.hovered), ("Practice", "Projects"))
            self.assertEqual(world.yazi.entries, ("Projects", "draft.txt", "notes.txt", "report.md"))
            self.assertEqual(world.yazi.tree, frozenset(PRACTICE_TREE))
            self.assertEqual(len(world.herdr.tab.panes), 1)
            self.assertEqual(world.herdr.tab.label, "Files")
            yazi, root, herdr_root = session._pid, session._root, session.herdr._home.parent
            self.assertIn(yazi, _yazi_pids() - before)
            self.assertTrue(_running_in(herdr_root))  # the private herdr server and client
            # A rebuild starts a new Yazi and ends the old one.
            session.play("yazi:next_file")
            world = session.apply_fixture("practice_tree@report.md")
            self.assertEqual(world.yazi.hovered, "report.md")
            self.assertNotIn(yazi, _yazi_pids())
            self.assertEqual(len(_yazi_pids() - before), 1)
        self.assertEqual(_yazi_pids() - before, set())
        self.assertEqual(_running_in(herdr_root), [])
        self.assertFalse(root.exists())
        self.assertFalse(herdr_root.exists())
        with self.assertRaises(YaziUnavailable):
            session.world()

    def test_the_tree_cannot_be_left(self):
        with YaziSession(YAZI_ROWS, HERDR_ROWS) as session:
            session.start(["yazi:top"], "practice_tree@draft.txt")
            session.play("yazi:leave_folder")          # at the top of the tree: nothing
            session.play("yazi:enter_folder")          # on a file: nothing
            time.sleep(0.4)
            self.assertEqual((settled(session).yazi.cwd, settled(session).yazi.hovered), ("Practice", "draft.txt"))
            session.play("yazi:top")
            session.play("yazi:enter_folder")
            session.play("yazi:enter_folder")          # Projects holds files only
            time.sleep(0.4)
            self.assertEqual(settled(session).yazi.cwd, "Practice/Projects")
            session.play("yazi:leave_folder")
            session.play("yazi:leave_folder")
            time.sleep(0.4)
            self.assertEqual(settled(session).yazi.cwd, "Practice")

    def test_playback_spaces_chords_and_plays_the_second_chord(self):
        with YaziSession(YAZI_ROWS, HERDR_ROWS) as session:
            session.start(["yazi:bottom", "yazi:top"], "practice_tree@notes.txt")
            self.assertEqual(session.chords("yazi:bottom"), ("G", "end"))
            started = time.monotonic()
            self.assertEqual(session.play("yazi:bottom", index=1), "end")
            self.assertEqual(session.play("yazi:top", number=None), "g, g")  # as session.perform calls it
            self.assertGreaterEqual(time.monotonic() - started, 0.3)
            time.sleep(0.4)
            self.assertEqual(settled(session).yazi.hovered, "Projects")
            with self.assertRaises(ValueError):
                YaziSession([]).chords("yazi:nothing")

    def test_yazi_cannot_write_outside_private_root(self):
        with tempfile.TemporaryDirectory(prefix="keys-yazi-outside-", dir="/tmp") as outside:
            target = Path(outside) / "escape.txt"
            with YaziSession(YAZI_ROWS, HERDR_ROWS) as session:
                session.start(["yazi:create", "yazi:rename"], "practice_tree@notes.txt")
                session.play("yazi:create")
                session.type_text(str(target))
                session.press("enter")
                time.sleep(0.4)
                self.assertFalse(target.exists())
                # Creating inside Practice still works under the jail.
                session.apply_fixture("practice_tree@notes.txt")
                session.play("yazi:create")
                session.type_text("inside.txt")
                session.press("enter")
                time.sleep(0.4)
                self.assertIn("Practice/inside.txt", settled(session).yazi.tree)


if __name__ == "__main__":
    unittest.main()
