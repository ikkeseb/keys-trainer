import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import curriculum
import keyscan
import practice

DATA = Path(__file__).with_name("test_data") / "scan"


def fixture(name):
    return (DATA / name).read_text(encoding="utf-8")


def pairs(rows):
    return [(row["name"], row["value"]) for row in rows]


class HerdrScanTest(unittest.TestCase):
    def setUp(self):
        self.own, self.configured = keyscan.parse_herdr_config(fixture("herdr_config.toml"))
        self.defaults = keyscan.herdr_defaults(fixture("herdr_default_config.toml"))

    def test_config_rows_keep_rebinds_and_skip_unbound_chords(self):
        self.assertEqual(pairs(self.own), [
            ("ctrl+a", "prefix"), ("prefix+backslash", "split vertical"), ("alt+v", "split vertical"),
            ("prefix+z", "toggle sidebar"), ("alt+1..9", "switch tab"), ("prefix+g", "lazygit"),
        ])
        self.assertIn("close pane", self.configured)
        self.assertNotIn("indexed", self.configured)

    def test_defaults_read_the_commented_keys_table_only(self):
        self.assertEqual(self.defaults["prefix"], ["ctrl+b"])
        self.assertEqual(self.defaults["split horizontal"], ["prefix+minus"])
        self.assertEqual(self.defaults["navigate pane left"], ["h"])
        self.assertNotIn("open worktree", self.defaults)  # "" is unset
        self.assertNotIn("key", self.defaults)  # the commented [[keys.command]] example
        self.assertNotIn("channel", self.defaults)  # [update], before [keys]

    def test_defaults_fill_only_free_actions_on_free_chords(self):
        rows = keyscan.herdr_rows(self.own, self.configured, self.defaults)
        found = pairs(rows)
        self.assertIn(("prefix+minus", "split horizontal"), found)
        self.assertIn(("ctrl+a", "prefix"), found)
        self.assertNotIn(("ctrl+b", "prefix"), found)
        self.assertNotIn(("prefix+v", "split vertical"), found)  # rebound
        self.assertFalse([row for row in rows if row["value"] == "close pane"])  # unbound stays unbound
        self.assertFalse([row for row in rows if row["value"] == "zoom"])  # its chord is the user's toggle sidebar
        self.assertFalse([row for row in rows if row["value"] == "goto"])  # its chord is the user's command
        self.assertIn(("prefix+w, h", "navigate pane left"), found)
        self.assertEqual(set(rows[0]), {"name", "value"})

    def test_herdr_spread(self):
        self.assertEqual(keyscan.herdr_spread("prefix+1..3"), {"prefix+1..3", "prefix+1", "prefix+2", "prefix+3"})
        self.assertEqual(keyscan.herdr_spread("prefix+x"), {"prefix+x"})

    def test_config_path_follows_environment(self):
        self.assertEqual(keyscan.herdr_config_path({"HERDR_CONFIG_PATH": "/a/c.toml", "HOME": "/h"}), Path("/a/c.toml"))
        self.assertEqual(keyscan.herdr_config_path({"XDG_CONFIG_HOME": "/x", "HOME": "/h"}), Path("/x/herdr/config.toml"))
        self.assertEqual(keyscan.herdr_config_path({"HOME": "/h"}), Path("/h/.config/herdr/config.toml"))


class HerdrAliasTest(unittest.TestCase):
    def test_fullscreen_is_read_as_zoom(self):
        rows, configured = keyscan.parse_herdr_config('[keys]\nfullscreen = "prefix+f"\n')
        self.assertEqual(pairs(rows), [("prefix+f", "zoom")])
        self.assertIn("zoom", configured)


class YaziScanTest(unittest.TestCase):
    def setUp(self):
        binary = b"\x7fELF\x00junk" + fixture("yazi_builtin.toml").encode() + b"\x00\x01more binary\n]\x00"
        self.builtin = keyscan.yazi_builtin(binary)

    def test_builtin_keymap_is_found_in_the_binary(self):
        self.assertIn("tasks", self.builtin)
        self.assertEqual(len(self.builtin["mgr"]["keymap"]), 18)

    def test_defaults_spell_chords_as_yazi_backend_reads_them(self):
        found = pairs(keyscan.yazi_rows({}, self.builtin))
        for row in (("esc", "escape"), ("j", "arrow next"), ("g, g", "arrow top"), ("G", "arrow bot"),
                    ("space", "toggle; arrow 1"), ("ctrl+u", "arrow -50%"), ("f", "filter --smart")):
            self.assertIn(row, found)
        self.assertEqual([name for name, _ in found].count("esc"), 1)  # <C-[> is the same escape
        self.assertNotIn("ctrl+z", [name for name, _ in found])  # windows only

    def test_prepend_wins_and_append_fills(self):
        own = keyscan.tomllib.loads(fixture("yazi_keymap.toml"))
        found = pairs(keyscan.yazi_rows(own, self.builtin))
        self.assertIn(("f", "find --smart"), found)
        self.assertNotIn(("f", "filter --smart"), found)
        self.assertIn(("ctrl+n", "arrow next"), found)
        self.assertIn(("j", "arrow next"), found)  # the appended noop never fires
        self.assertIn(("g, h", "cd ~"), found)
        self.assertNotIn("alt+x", [name for name, _ in found])  # a noop shows nowhere

    def test_own_keymap_replaces_builtin(self):
        own = {"mgr": {"keymap": [{"on": "n", "run": "arrow next"}]}}
        self.assertEqual(pairs(keyscan.yazi_rows(own, self.builtin)), [("n", "arrow next")])

    def test_rows_resolve_in_yazi_backend(self):
        from yazi_backend import yazi_bindings

        found = yazi_bindings(keyscan.yazi_rows({}, self.builtin))
        self.assertEqual([chord for chord, _ in found["mark_one"]], ["space"])
        self.assertEqual([chord for chord, _ in found["top"]], ["g, g"])
        self.assertIn("escape", found)


class DesktopScanTest(unittest.TestCase):
    def setUp(self):
        binds = json.loads(fixture("hypr_binds.json"))
        self.binds = keyscan.fill_keys(binds, keyscan.parse_text_binds(fixture("hypr_binds.txt")))
        self.rows = pairs(keyscan.desktop_rows(self.binds))

    def test_workspace_dispatch_reads_as_a_switch(self):
        self.assertIn(("super+1", "Switch to workspace 1"), self.rows)
        self.assertIn(("super+0", "Switch to workspace 10"), self.rows)
        import hypr

        payload = {"groups": [{"id": "desktop", "rows": keyscan.desktop_rows(self.binds)}]}
        self.assertEqual(hypr.workspace_chords(payload)[1], "super+1")
        self.assertEqual(len(hypr.workspace_chords(payload)), 10)

    def test_missing_keys_come_from_the_plain_reply(self):
        self.assertIn(("super+shift+1", "Move window to workspace 1"), self.rows)
        self.assertIn(("super+shift+2", "Move window to workspace 2"), self.rows)

    def test_names_descriptions_and_submaps(self):
        self.assertIn(("super+ctrl+tab", "Former workspace"), self.rows)
        self.assertIn(("super+|", "Toggle Quake terminal"), self.rows)
        self.assertIn(("audio raise volume", "Volume up"), self.rows)
        self.assertIn(("super+left click", "movewindow"), self.rows)
        self.assertNotIn("resizeactive -20 0", [value for _, value in self.rows])


class FakeHyprland:
    """A Hyprland runtime folder whose control socket answers `j/binds` and `binds` from fixtures."""

    def __init__(self, runtime: Path):
        self.folder = runtime / "hypr" / "test"
        self.folder.mkdir(parents=True)
        self.replies = {b"j/binds": fixture("hypr_binds.json").encode(), b"binds": fixture("hypr_binds.txt").encode()}
        self.events = self._listen(".socket2.sock")
        self.control = self._listen(".socket.sock")
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _listen(self, name):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.folder / name))
        server.listen()
        return server

    def _serve(self):
        while True:
            try:
                client, _ = self.control.accept()
            except OSError:
                return
            with client:
                client.sendall(self.replies.get(client.recv(64), b""))

    def close(self):
        self.control.close()
        self.events.close()


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ks-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        herdr = bin_dir / "herdr"
        herdr.write_text(f"#!/bin/sh\nexec '{shutil.which('cat')}' '{DATA / 'herdr_default_config.toml'}'\n")
        yazi = bin_dir / "yazi"
        yazi.write_bytes(b"\x7fELF\x00" + fixture("yazi_builtin.toml").encode() + b"\x00")
        for tool in (herdr, yazi):
            tool.chmod(0o755)
        (self.root / "home").mkdir()
        self.env = {"PATH": str(bin_dir), "HOME": str(self.root / "home")}

    def test_broken_json_binds_fall_back_to_the_plain_reply(self):
        runtime = self.root / "run"
        hyprland = FakeHyprland(runtime)
        hyprland.replies[b"j/binds"] = b'[{"modmask": 64, "key": "1",'
        self.addCleanup(hyprland.close)
        payload = keyscan.scan(self.env | {"XDG_RUNTIME_DIR": str(runtime), "HYPRLAND_INSTANCE_SIGNATURE": "test"})
        rows = pairs(next(group["rows"] for group in payload["groups"] if group["id"] == "desktop"))
        self.assertIn(("super+shift+1", "Move window to workspace 1"), rows)  # the plain fixture's binds

    def test_default_configs_and_no_hyprland(self):
        payload = keyscan.scan(self.env)
        self.assertEqual(payload["source"], "scan")
        self.assertEqual([group["id"] for group in payload["groups"]], ["herdr", "yazi", "shell", "strata", "desktop"])
        groups = {group["id"]: group["rows"] for group in payload["groups"]}
        self.assertIn(("prefix+minus", "split horizontal"), pairs(groups["herdr"]))
        self.assertIn(("j", "arrow next"), pairs(groups["yazi"]))
        self.assertEqual(groups["desktop"], [])
        self.assertEqual(payload["notes"], [
            "herdr config not found; using herdr's defaults.",
            "yazi keymap.toml not found; using yazi's defaults.",
            "Hyprland is not running or did not answer, so there are no desktop keys.",
        ])

    def test_missing_tools_give_empty_groups_with_notes(self):
        payload = keyscan.scan({"PATH": str(self.root / "nowhere"), "HOME": str(self.root / "home")})
        self.assertTrue(all(group["rows"] == [] for group in payload["groups"]))
        self.assertEqual(len(payload["notes"]), 3)
        self.assertIn("herdr was not found", payload["notes"][0])

    def test_lesson_one_resolves_with_herdr_defaults(self):
        runtime = self.root / "run"
        hyprland = FakeHyprland(runtime)
        self.addCleanup(hyprland.close)
        payload = keyscan.scan(self.env | {"XDG_RUNTIME_DIR": str(runtime), "HYPRLAND_INSTANCE_SIGNATURE": "test"})
        self.assertEqual(len(payload["notes"]), 2)
        resolved = curriculum.resolve(curriculum.load(), payload, desktop=True)
        lesson = resolved.lesson("split")
        self.assertEqual(lesson.active, lesson.total)
        for sid in ("split-rows", "split-cols", "close-pane", "build-split-bottom"):
            self.assertTrue(resolved.active(sid), sid)
        self.assertEqual(resolved.bindings["herdr:split_horizontal"].chords, ("prefix+minus",))
        self.assertEqual(resolved.bindings["herdr:close_pane"].chords, ("prefix+x",))
        self.assertEqual(resolved.bindings["herdr:split_horizontal"].prefix, "ctrl+b")
        self.assertEqual(resolved.bindings["desktop:switch_workspace"].chords, ("super+1..9",))
        self.assertEqual(resolved.bindings["desktop:former_workspace"].chords, ("super+ctrl+tab",))


    def test_a_missing_key_gets_its_file_and_the_line_with_the_default_chord(self):
        (self.root / "home" / ".config" / "yazi").mkdir(parents=True)
        (self.root / "home" / ".config" / "yazi" / "keymap.toml").write_text(
            '[mgr]\nkeymap = [{ on = "j", run = "arrow next" }]\n', encoding="utf-8")
        payload = keyscan.scan(self.env)
        groups = {group["id"]: group for group in payload["groups"]}
        self.assertEqual(groups["herdr"]["defaults"]["split_horizontal"], "prefix+minus")
        self.assertEqual(groups["yazi"]["defaults"]["arrow top"], ["g", "g"])
        herdr = keyscan.bind_advice(payload, "herdr:split_horizontal", self.env)
        self.assertEqual((herdr["path"], herdr["default"], herdr["taken"]),
                         ("~/.config/herdr/config.toml", "prefix+minus", "split horizontal"))  # still bound: taken
        yazi = keyscan.bind_advice(payload, "yazi:top", self.env)
        self.assertEqual(yazi["path"], "~/.config/yazi/keymap.toml")
        self.assertEqual(yazi["line"], '{ on = ["g", "g"], run = "arrow top" }')
        mark = keyscan.bind_advice(payload, "yazi:mark_one", self.env)
        self.assertIn('run = ["toggle", "arrow 1"]', mark["line"])
        resolved = curriculum.resolve(curriculum.load(), payload)
        self.assertTrue(resolved.scenarios["yazi-bottom-top"].notice)
        env = self.env | {"HERDR_CONFIG_PATH": "/etc/herdr.toml"}
        free = keyscan.bind_advice({"groups": [{"id": "herdr", "rows": [], "defaults": {"zoom": "prefix+z"}}]},
                                   "herdr:zoom", env)
        self.assertEqual((free["path"], free["line"], free["taken"]), ("/etc/herdr.toml", 'zoom = "prefix+z"', ""))
        enter = keyscan.bind_advice(payload, "yazi:enter_folder", self.env)
        self.assertIn('run = "enter"', enter["line"])  # Yazi's own command, never the trainer's plugin
        desktop = keyscan.bind_advice(payload, "desktop:toggle_quake", self.env)
        self.assertEqual((desktop["action"], desktop["line"]), ("Toggle Quake terminal", ""))


class ReadKeysTest(unittest.TestCase):
    def test_falls_back_to_scan_without_rigpage(self):
        scanned = {"groups": [{"id": "herdr", "rows": [{"name": "prefix+v", "value": "split vertical"}]}],
                   "source": "scan", "notes": ["a note"]}
        with patch("practice.shutil.which", return_value=None), \
                patch("keyscan.scan", return_value=scanned) as scan, patch("practice.subprocess.run") as run:
            data = practice.read_keys()
        scan.assert_called_once_with()
        run.assert_not_called()
        self.assertEqual(data["source"], "scan")
        self.assertEqual(data["notes"], ["a note"])

    def test_file_wins_and_says_so(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as out:
            json.dump({"groups": []}, out)
        self.addCleanup(os.unlink, out.name)
        with patch("keyscan.scan") as scan:
            self.assertEqual(practice.read_keys(out.name)["source"], "file")
        scan.assert_not_called()

    def test_rigpage_is_used_where_installed(self):
        reply = json.dumps({"groups": []}).encode()
        with patch("practice.shutil.which", return_value="/bin/rigpage"), patch("keyscan.scan") as scan, \
                patch("practice.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = reply
            self.assertEqual(practice.read_keys()["source"], "rigpage")
        scan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
