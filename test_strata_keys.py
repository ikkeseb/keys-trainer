"""Strata's keys: reading settings.toml, and the session that counts key presses."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import checks
import strata_keys
from strata_keys import KeysSession, accelerator, chords, rows

ROWS = [
    {"name": "alt+up", "value": "parent_folder"},
    {"name": "ctrl+k", "value": "search"},
    {"name": "space", "value": "preview"},
    {"name": "ctrl+shift+c", "value": "copy_path"},
]


def of(found, action):
    return [row["name"] for row in found if row["value"] == action]


class AcceleratorTest(unittest.TestCase):
    def test_gtk_spelling_becomes_a_chord(self):
        for text, chord in (("<Shift><Control>c", "ctrl+shift+c"), ("<Primary>k", "ctrl+k"), ("y", "y"),
                            ("<Alt>Up", "alt+up"), ("<Control>period", "ctrl+."), ("<Control>Return", "ctrl+enter"),
                            ("<Control>Page_Down", "ctrl+pagedown"), ("<Control><", "ctrl+<"), ("F2", "f2")):
            with self.subTest(text):
                self.assertEqual(accelerator(text), chord)

    def test_unreadable_text_is_none(self):
        for text in ("", "<Control>", "<Hyper>x", "<Control><a>b", "a>b"):
            with self.subTest(text):
                self.assertIsNone(accelerator(text))


class RowsTest(unittest.TestCase):
    def test_defaults_leave_out_typed_keys_while_type_to_search_is_on(self):
        found = rows({})
        self.assertEqual(of(found, "parent_folder"), ["alt+up"])
        self.assertEqual(of(found, "toggle_hidden_files"), ["ctrl+h", "ctrl+."])
        self.assertEqual(of(found, "preview"), ["space"])
        self.assertEqual(of(found, "copy_path"), [])
        self.assertEqual(of(rows({"type_to_search": False}), "copy_path"), ["y"])

    def test_an_entry_replaces_the_defaults(self):
        found = rows({"keymap": {"default": {"copy_path": ["y", "<Shift><Control>c"], "search": []}}})
        self.assertEqual(of(found, "copy_path"), ["ctrl+shift+c"])
        self.assertEqual(of(found, "search"), [])

    def test_odd_shapes_fall_back_to_the_defaults(self):
        for settings in ({"keymap": "x"}, {"keymap": {"default": 3}}, {"keymap": {"default": {"search": "x"}}}):
            with self.subTest(settings=settings):
                self.assertEqual(of(rows(settings), "search"), ["ctrl+k"])

    def test_chords_accept_the_id_or_the_export_description(self):
        export = [{"name": "ctrl+k", "value": strata_keys.ACTIONS["search"][0]}, *ROWS]
        self.assertEqual(chords(export, "search"), ("ctrl+k",))


class ScanTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.env = {"HOME": directory.name}
        self.path = Path(directory.name, ".config", "strata", "settings.toml")
        self.path.parent.mkdir(parents=True)

    def scan(self, installed=True):
        notes = []
        with patch.object(strata_keys.shutil, "which", return_value="/usr/bin/strata" if installed else None):
            return strata_keys.scan(self.env, notes), notes

    def test_no_strata_no_rows(self):
        self.assertEqual(self.scan(installed=False), ([], []))

    def test_without_a_settings_file_the_defaults_count(self):
        self.assertEqual(self.scan(), (rows({}), []))

    def test_the_settings_file_wins(self):
        self.path.write_text('[keymap.default]\nsearch = ["<Control>p"]\n', encoding="utf-8")
        found, notes = self.scan()
        self.assertEqual((of(found, "search"), notes), (["ctrl+p"], []))

    def test_a_broken_file_is_noted_and_the_defaults_used(self):
        self.path.write_text("[keymap.default\n", encoding="utf-8")
        found, notes = self.scan()
        self.assertEqual(found, rows({}))
        self.assertIn("could not be read", notes[0])


class KeysSessionTest(unittest.TestCase):
    def session(self, *actions):
        session = KeysSession(ROWS)
        session.start([f"strata:{action}" for action in actions])
        return session

    def test_terminal_bytes_become_the_action(self):
        session = self.session("parent_folder", "search", "preview")
        for raw in (b"\x1b[1;3A", b"\x0b", b" ", b"\x1b[107;5u"):
            session.feed(raw)
        self.assertEqual(session.world().strata, ("parent_folder", "search", "preview", "search"))

    def test_another_key_is_recorded_as_itself(self):
        session = self.session("search")
        session.feed(b"x")
        self.assertEqual(session.world().strata, ("x",))

    def test_enter_stands_in_only_for_a_chord_the_terminal_keeps(self):
        session = self.session("search", "copy_path")
        session.feed(b"\r")
        self.assertEqual(session.world().strata, ("copy_path",))
        session = self.session("search")
        session.feed(b"\r")
        self.assertEqual(session.world().strata, ("enter",))

    def test_played_pressed_and_rebuilt(self):
        session = self.session("search")
        session.press("ctrl+k")
        self.assertEqual(session.world().strata, ("search",))
        self.assertEqual(session.chords("strata:search"), ("ctrl+k",))
        self.assertEqual(session.play("strata:search"), "ctrl+k")
        self.assertEqual(session.apply_fixture("keys").strata, ())

    def test_pressed_looks_at_the_latest_key_only(self):
        def met(*pressed):
            context = type("Context", (), {"now": strata_keys.World(strata=pressed)})
            return strata_keys.pressed(context, "search")

        self.assertTrue(met("x", "search"))
        self.assertFalse(met("search", "x"))
        self.assertFalse(met())


if __name__ == "__main__":
    unittest.main()
