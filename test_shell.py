"""Unit and live tests for shell_backend.py (bash bind scanner and private ShellSession)."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import landlock
import shell_backend
from shell_backend import ShellSession, ShellUnavailable, parse_scan, readline_chord, shell_bindings


SAMPLE_SCAN = """\
"\\C-r" "__fzf_history__"
"\\C-t" "fzf-file-widget"
===S===
"\\ec": " \\C-b\\C-k \\C-u`__fzf_cd__`\\e\\C-e\\e(\\C-m"
===P===
"\\ep": non-incremental-reverse-search-history
# reverse-search-history (not bound)
===CMD===
z=function
zi=function
open=function
xdg-open=file
nvim=file
n=function
fd=file
rg=file
fzf=file
zoxide=file
"""

STOCK_BASH_SCAN = """\
===S===
===P===
"\\C-r": reverse-search-history
===CMD===
z=
zi=
open=
xdg-open=file
nvim=file
n=
fd=file
rg=file
fzf=
zoxide=
"""


class ShellScanUnitTests(unittest.TestCase):
    def test_readline_chord_parses_ctrl_alt_and_sequences(self):
        self.assertEqual(readline_chord(r"\C-r"), "ctrl+r")
        self.assertEqual(readline_chord(r"\C-t"), "ctrl+t")
        self.assertEqual(readline_chord(r"\ec"), "alt+c")
        self.assertEqual(readline_chord(r"\M-c"), "alt+c")
        self.assertEqual(readline_chord(r"\e\C-r"), "ctrl+alt+r")
        self.assertIsNone(readline_chord(""))

    def test_parse_scan_reads_omarchy_fzf_zoxide_and_open_tools(self):
        rows = parse_scan(SAMPLE_SCAN)
        bindings = shell_bindings(rows)
        self.assertEqual(bindings["history_search"], ("ctrl+r",))
        self.assertEqual(bindings["file_widget"], ("ctrl+t",))
        self.assertEqual(bindings["cd_widget"], ("alt+c",))
        self.assertEqual(bindings["zoxide_jump"], ("z",))
        self.assertEqual(bindings["open_default"], ("open", "xdg-open"))
        self.assertEqual(bindings["open_editor"], ("nvim", "n"))
        self.assertEqual(bindings["find_file"], ("fd",))
        self.assertEqual(bindings["search_content"], ("rg",))

    def test_parse_scan_without_fzf_or_zoxide_only_binds_what_exists(self):
        rows = parse_scan(STOCK_BASH_SCAN)
        bindings = shell_bindings(rows)
        self.assertNotIn("history_search", bindings)
        self.assertNotIn("file_widget", bindings)
        self.assertNotIn("cd_widget", bindings)
        self.assertNotIn("zoxide_jump", bindings)
        self.assertEqual(bindings["open_default"], ("xdg-open",))
        self.assertEqual(bindings["open_editor"], ("nvim",))
        self.assertEqual(bindings["find_file"], ("fd",))
        self.assertEqual(bindings["search_content"], ("rg",))

    def test_parse_scan_ignores_stale_fzf_bindings_when_fzf_missing(self):
        stale = SAMPLE_SCAN.replace("fzf=file", "fzf=")
        bindings = shell_bindings(parse_scan(stale))
        self.assertNotIn("history_search", bindings)
        self.assertNotIn("file_widget", bindings)
        self.assertNotIn("cd_widget", bindings)
        self.assertEqual(bindings["find_file"], ("fd",))

    def test_fixture_reset_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory(prefix="keys-sh-root-", dir="/tmp") as root_str, \
             tempfile.TemporaryDirectory(prefix="keys-sh-outside-", dir="/tmp") as outside_str:
            root = Path(root_str)
            outside = Path(outside_str)
            keep = outside / "keep.txt"
            keep.write_text("untouched", encoding="utf-8")
            outside_hist = outside / "hist.txt"
            outside_hist.write_text("real history\n", encoding="utf-8")
            home = root / "home"
            home.mkdir()
            (home / "Practice").symlink_to(outside)
            (root / "bash_history").symlink_to(outside_hist)
            (root / "zoxide").symlink_to(outside)
            shell_backend._reset_shell_fixture(home, root, root / "zoxide")
            self.assertEqual(keep.read_text(encoding="utf-8"), "untouched")
            self.assertEqual(outside_hist.read_text(encoding="utf-8"), "real history\n")
            self.assertFalse((home / "Practice").is_symlink())
            self.assertTrue((home / "Practice" / "notes.txt").is_file())
            self.assertFalse((root / "bash_history").is_symlink())
            self.assertIn("echo ship-42", (root / "bash_history").read_text(encoding="utf-8"))
            self.assertFalse((root / "zoxide").is_symlink())

    def test_opener_stubs_refuse_files_outside_practice_and_allow_inside(self):
        import subprocess
        with tempfile.TemporaryDirectory(prefix="keys-sh-stub-", dir="/tmp") as root_str, \
             tempfile.TemporaryDirectory(prefix="keys-sh-out-", dir="/tmp") as outside_str:
            root = Path(root_str)
            outside_file = Path(outside_str) / "secret.txt"
            outside_file.write_text("secret", encoding="utf-8")
            private = shell_backend._private_shell(root, {})
            home = private["HOME"]
            inside_file = home / "Practice" / "notes.txt"
            inside_file.parent.mkdir(parents=True)
            inside_file.write_text("hello", encoding="utf-8")
            events = root / "events.jsonl"
            env = {"HOME": str(home), "KT_EVENTS": str(events), "PATH": os.defpath}
            blocked = subprocess.run(
                [str(root / "bin" / "open"), str(outside_file)],
                env=env, capture_output=True, text=True, timeout=5,
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("outside Practice", blocked.stderr)
            self.assertFalse(events.exists() and events.read_text(encoding="utf-8").strip())
            allowed = subprocess.run(
                [str(root / "bin" / "open"), str(inside_file)],
                env=env, capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.assertIn("Opened", allowed.stdout)
            logged = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(logged), 1)
            self.assertEqual(logged[0]["kind"], "open")
            self.assertEqual(logged[0]["opener"], "default")

    def test_scan_write_jail_blocks_bashrc_writes(self):
        with tempfile.TemporaryDirectory(prefix="keys-sh-scan-", dir="/tmp") as home_str:
            fake_home = Path(home_str)
            (fake_home / ".bashrc").write_text(
                'echo pwned > "$HOME/pwned.txt"\n'
                'export HISTFILE="$HOME/real_hist"\n'
                'echo leaked > "$HISTFILE"\n',
                encoding="utf-8",
            )
            shell_backend.scan({"HOME": str(fake_home), "PATH": os.environ.get("PATH", os.defpath)})
            self.assertFalse((fake_home / "pwned.txt").exists())
            self.assertFalse((fake_home / "real_hist").exists())

    def test_rebound_fzf_chord_is_picked_up_from_bash(self):
        custom = SAMPLE_SCAN.replace(r'"\C-t" "fzf-file-widget"', r'"\C-f" "fzf-file-widget"')
        bindings = shell_bindings(parse_scan(custom))
        self.assertEqual(bindings["file_widget"], ("ctrl+f",))

    def test_landlock_unavailable_fails_closed(self):
        import errno
        rows = parse_scan(SAMPLE_SCAN)
        with patch.object(landlock, "abi_version", side_effect=OSError(errno.ENOSYS, "no landlock")):
            with self.assertRaisesRegex(ShellUnavailable, "did not start"):
                ShellSession(rows).start(["shell:find_file"])

    def test_unknown_fixture_and_card_refused(self):
        rows = parse_scan(SAMPLE_SCAN)
        session = ShellSession(rows)
        with self.assertRaises(ValueError):
            session.start(["shell:nonexistent"])
        with self.assertRaises(ValueError):
            session.start(["shell:find_file"], "unknown_fixture")

    def test_shell_plan_router_uses_function_keys_so_typing_letters_passes_through(self):
        import curriculum
        import lesson
        import runner
        data = {"groups": [{"id": "shell", "rows": parse_scan(SAMPLE_SCAN)}]}
        resolved = curriculum.resolve(curriculum.load(), data)
        plan = lesson.plan_lesson(resolved, "shell-search")
        router = runner.router_for(plan)
        self.assertEqual((router.hint_key, router.pause_key, router.skip_key), ("F1", "F2", "F3"))
        self.assertEqual(router.route(b"q"), ("forward", b"q"))
        self.assertEqual(router.route(b"?"), ("forward", b"?"))
        self.assertEqual(router.route(b"\x1bOP"), ("trainer", "hint"))
        self.assertEqual(router.route(b"\x1bOQ"), ("trainer", "pause"))


LIVE = os.environ.get("KEYS_TRAINER_TEST_HERDR") == "1" and shutil.which("herdr") and shutil.which("bash")


@unittest.skipUnless(LIVE, "set KEYS_TRAINER_TEST_HERDR=1 to test ShellSession live")
class ShellSessionLiveTests(unittest.TestCase):
    def test_private_shell_isolates_history_zoxide_and_writes(self):
        rows = parse_scan(SAMPLE_SCAN)
        herdr_rows = json.loads((Path(__file__).with_name("test_data") / "rigpage_herdr.json").read_text("utf-8"))
        with tempfile.TemporaryDirectory(prefix="keys-sh-outside-", dir="/tmp") as outside:
            escape = Path(outside) / "escape.txt"
            with ShellSession(rows, herdr_rows) as session:
                world = session.start(["shell:open_default", "shell:find_file"], "shell_tree")
                self.assertEqual(world.shell.cwd, "Practice")
                root = session._root
                session.type_text(f"touch {escape}")
                session.press("enter")
                import time
                time.sleep(0.4)
                self.assertFalse(escape.exists())
                # Opening a file hits the recording stub, never a real desktop app.
                session.play("shell:open_default")
                session.press("space")
                session.type_text("notes.txt")
                session.press("enter")
                end = time.monotonic() + 3
                while time.monotonic() < end and not session.world().shell.opened:
                    time.sleep(0.05)
                self.assertEqual(session.world().shell.opened, (("default", "Practice/notes.txt"),))
            self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
