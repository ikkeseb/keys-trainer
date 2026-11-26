import unittest

from practice import Card, History, shortcut_text


class PracticeTest(unittest.TestCase):
    def setUp(self):
        self.cards = [Card("herdr", str(i), f"Action {i}", (str(i),)) for i in range(4)]
        self.history = History(":memory:")
        self.addCleanup(self.history.close)

    def test_spells_a_rebound_prefix(self):
        self.assertEqual(shortcut_text("prefix+t", "ctrl+a"), "ctrl+a → t")
        self.assertEqual(shortcut_text("alt+t", "ctrl+a"), "alt+t")
        self.assertEqual(shortcut_text("prefix+t", ""), "")

    def test_spaced_review_early_practice_and_lapse(self):
        card, record = self.cards[0], self.history.record_outcome
        tomorrow = record(card, "clean", 100)
        self.assertEqual(tomorrow, 86500)
        self.assertEqual(record(card, "clean", 500), tomorrow)
        self.assertEqual(record(card, "clean", tomorrow), tomorrow + 3 * 86400)
        retry = record(card, "shown", tomorrow + 5)
        self.assertEqual(retry, tomorrow + 65)
        self.assertEqual(record(card, "clean", retry), retry + 86400)

    def test_rebind_gives_fresh_history(self):
        card = self.cards[0]
        changed = Card(card.track, card.action, card.title, ("shift+f1",))
        self.history.record_outcome(card, "clean")
        self.assertNotIn(changed.key, self.history.progress())

    def test_first_version_history_keeps_due_date_and_learns_unaided_times(self):
        import sqlite3
        from pathlib import Path
        from tempfile import TemporaryDirectory

        card = self.cards[0]
        with TemporaryDirectory() as folder:
            path = Path(folder) / "practice.sqlite3"
            old = sqlite3.connect(path)
            old.execute("CREATE TABLE practice (card TEXT PRIMARY KEY, streak INTEGER, due REAL)")
            old.execute("INSERT INTO practice VALUES (?, 2, 12345)", (card.key,))
            old.commit()
            old.close()
            history = History(path)
            try:
                self.assertIsNone(history.best_seconds(card))
                self.assertEqual(history.record_outcome(card, "clean", 100, seconds=2.1), 12345)
                self.assertEqual(history.best_seconds(card), 2.1)
                history.record_outcome(card, "shown", 110, seconds=10)
                self.assertEqual(history.best_seconds(card), 2.1)
                self.assertEqual(history.record_outcome(card, "clean", 170, seconds=1.4), 86570)
                self.assertEqual(history.best_seconds(card), 1.4)
            finally:
                history.close()


class GuidedHistoryTest(unittest.TestCase):
    """Outcomes, lesson stars, best times and the in-place migration of today's file."""

    def setUp(self):
        self.history = History(":memory:")
        self.addCleanup(self.history.close)
        self.card = Card("herdr", "split_vertical", "split vertical", ("ctrl+space → v", "alt+shift+enter"))

    def test_outcome_table(self):
        from practice import DAYS
        history, card = self.history, self.card
        self.assertEqual(history.record_outcome(card, "clean", 100, seconds=2.0), 100 + DAYS[0] * 86400)
        self.assertEqual(history.record_outcome(card, "nudged", 200), 200 + 86400)
        self.assertEqual(history.progress()[card.key][0], 1)  # streak kept
        self.assertEqual(history.record_outcome(card, "shown", 300), 360)
        self.assertEqual(history.progress()[card.key][0], 0)
        history.record_outcome(card, "clean", 400, seconds=1.5)
        history.record_outcome(card, "clean", 400 + 86400, seconds=3.0)
        self.assertEqual(history.best_seconds(card), 1.5)
        self.assertEqual(history.last_practised(card), 400 + 86400)

    def test_learn_marks_met_due_now_without_wiping_a_streak(self):
        from types import SimpleNamespace
        history, card = self.history, self.card
        history.record_step(SimpleNamespace(kind="learn", outcome="met", cards=(card,), seconds=None), 50)
        self.assertEqual(history.progress()[card.key], (0, 50))
        history.record_outcome(card, "clean", 60)
        history.record_step(SimpleNamespace(kind="learn", outcome="met", cards=(card,), seconds=None), 70)
        self.assertEqual(history.progress()[card.key][0], 1)
        history.record_step(SimpleNamespace(kind="build", outcome="shown", cards=(card,), seconds=None), 80)
        self.assertEqual(history.progress()[card.key][0], 1)  # builds score stars only

    def test_stars_and_lesson_records(self):
        from types import SimpleNamespace as R
        from practice import stars
        clean = [R(kind="learn", outcome="met", hint_level=3), R(kind="recall", outcome="clean", hint_level=0),
                 R(kind="build", outcome="clean", hint_level=0)]
        nudged = clean[:1] + [R(kind="recall", outcome="nudged", hint_level=1)] + clean[2:]
        hinted_build = clean[:2] + [R(kind="build", outcome="nudged", hint_level=1)]
        shown = clean[:1] + [R(kind="recall", outcome="shown", hint_level=2)] + clean[2:]
        self.assertEqual([stars(clean), stars(nudged), stars(hinted_build), stars(shown), stars(clean, False)],
                         [3, 2, 2, 1, 0])
        self.assertEqual(self.history.finish_lesson("split", shown, 10, seconds=90), 1)
        self.assertEqual(self.history.finish_lesson("split", clean, 20, seconds=120), 3)
        self.history.finish_lesson("split", nudged, 30, seconds=80, complete=False)
        record = self.history.lessons()["split"]
        self.assertEqual((record.stars, record.best_seconds, record.last_practised), (3, 90, 30))

    def test_warmup_picks_due_most_overdue_first_capped(self):
        from practice import pick_warmup
        cards = {f"herdr:a{i}": Card("herdr", f"a{i}", "", (str(i),)) for i in range(9)}
        for i, card in enumerate(cards.values()):
            self.history.meet(card, 1000 - i)  # a8 is the most overdue
        self.history.record_outcome(cards["herdr:a0"], "clean", 1000)  # due tomorrow
        picked = pick_warmup(self.history, cards, now=2000)
        self.assertEqual(picked, ["herdr:a8", "herdr:a7", "herdr:a6", "herdr:a5", "herdr:a4", "herdr:a3"])
        self.assertEqual(pick_warmup(self.history, cards, now=10), [])

    def test_migrates_todays_file_in_place(self):
        import sqlite3
        from pathlib import Path
        from tempfile import TemporaryDirectory

        old_card = Card("herdr", "split_vertical", "Split this pane into two columns",
                        ("ctrl+space → v", "alt+shift+enter"))
        lapsed = Card("herdr", "new_tab", "Open a new tab", ("ctrl+space → c",))
        with TemporaryDirectory() as folder:
            path = Path(folder) / "practice.sqlite3"
            # Today's schema, exactly as the shipped History creates it.
            old = sqlite3.connect(path)
            old.execute("""CREATE TABLE practice (card TEXT PRIMARY KEY, streak INTEGER NOT NULL,
                           due REAL NOT NULL, recall_seconds REAL)""")
            old.execute("CREATE TABLE layout_scores (blueprint TEXT PRIMARY KEY, best_seconds REAL)")
            old.execute("INSERT INTO practice VALUES (?, 3, 5000, 1.8)", (old_card.key,))
            old.execute("INSERT INTO practice VALUES (?, 0, 4000, NULL)", (lapsed.key,))
            old.execute("INSERT INTO layout_scores VALUES ('grid', 7.5)")
            old.commit()
            old.close()
            for _ in range(2):  # a second open changes nothing
                history = History(path)
                try:
                    self.assertEqual(history.progress(), {old_card.key: (3, 5000), lapsed.key: (0, 4000)})
                    self.assertEqual(history.layout_records(), {"grid": 7.5})
                    self.assertEqual(history.best_seconds(old_card), 1.8)
                    outcomes = dict(history.db.execute("SELECT card, outcome FROM practice"))
                    self.assertEqual(outcomes, {old_card.key: "clean", lapsed.key: "shown"})
                    self.assertEqual(history.db.execute("PRAGMA user_version").fetchone()[0], 3)
                finally:
                    history.close()
            # The guided trainer's card for split_vertical is the same history row.
            import curriculum
            import json
            rows = json.loads(Path(__file__).with_name("test_data").joinpath("rigpage_herdr.json").read_text())
            resolved = curriculum.resolve(curriculum.loads({"herdr.toml": curriculum.FOLDER.joinpath(
                "herdr.toml").read_text()}), {"groups": [{"id": "herdr", "rows": rows}]})
            self.assertEqual(resolved.bindings["herdr:split_vertical"].practice.key, old_card.key)
            self.assertEqual(resolved.bindings["herdr:new_tab"].practice.key, lapsed.key)


class OldRowsFeedCurrentCardsTest(unittest.TestCase):
    """Rows from the one-shot drill engine have no current card; migration aliases them."""

    def old_file(self, folder, rows):
        import sqlite3
        from pathlib import Path
        path = Path(folder) / "practice.sqlite3"
        old = sqlite3.connect(path)
        old.execute("""CREATE TABLE practice (card TEXT PRIMARY KEY, streak INTEGER NOT NULL,
                       due REAL NOT NULL, recall_seconds REAL, best_seconds REAL,
                       last_practised REAL, outcome TEXT)""")
        for card, streak, due in rows:
            old.execute("INSERT INTO practice VALUES (?, ?, ?, NULL, NULL, 1, 'clean')", (card, streak, due))
        old.execute("PRAGMA user_version = 2")
        old.commit()
        old.close()
        return path

    def test_old_mark_notes_and_workspace_rows_make_current_cards_due(self):
        import json
        from tempfile import TemporaryDirectory
        from practice import pick_warmup
        old_mark = json.dumps(["Yazi", "mark_notes", ["space"]])
        old_ws = json.dumps(["Hyprland", "hyprland:workspace:4", ["alt+4"]])
        old_ws2 = json.dumps(["Hyprland", "hyprland:workspace:5", ["alt+5"]])
        mark = Card("Yazi", "mark_one", "mark one", ("space",))
        workspace = Card("Hyprland", "switch_workspace", "switch workspace", ("alt+1..9",))
        with TemporaryDirectory() as folder:
            path = self.old_file(folder, [(old_mark, 2, 100), (old_ws, 1, 200), (old_ws2, 3, 150)])
            for _ in range(2):  # a second open changes nothing
                history = History(path)
                progress = history.progress()
                self.assertEqual(progress[mark.key], (2, 100))
                self.assertIn(workspace.key, progress)
                for key in (old_mark, old_ws, old_ws2):  # old rows are kept
                    self.assertIn(key, progress)
                picked = pick_warmup(history, {"yazi:mark_one": mark, "desktop:switch_workspace": workspace}, now=1000)
                self.assertEqual(sorted(picked), ["desktop:switch_workspace", "yazi:mark_one"])
                history.close()
