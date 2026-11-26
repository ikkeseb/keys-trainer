"""Read the user's shortcuts, and keep spaced review and lesson stars in one SQLite history."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time


@dataclass(frozen=True)
class Card:
    track: str
    action: str
    title: str
    shortcuts: tuple[str, ...]

    @property
    def key(self):
        # Rebinding a shortcut starts a fresh learning history.
        return json.dumps([self.track, self.action, self.shortcuts], ensure_ascii=False)


def read_keys(path=None):
    """The user's bindings: a `--keys FILE` export, else `rigpage keys` where it is installed, else a scan of
    herdr, yazi and Hyprland themselves (`keyscan.scan`). `source` says which ("file", "rigpage", "scan")."""
    if path:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        source = "file"
    elif shutil.which("rigpage"):
        run = subprocess.run(["rigpage", "keys"], capture_output=True, timeout=15)
        if run.returncode:
            raise ValueError("rigpage keys failed. Run it in your terminal to check its config.")
        data = json.loads(run.stdout)
        source = "rigpage"
        import shell_backend

        shell_group = next((g for g in data.get("groups", ()) if g.get("id") == "shell"), None)
        if shell_group is None or not shell_backend.shell_bindings(shell_group.get("rows", ())):
            extra = shell_backend.scan()
            if shell_group is not None:
                shell_group["rows"] = [*shell_group["rows"], *extra]
            elif extra:
                data["groups"].append({"id": "shell", "rows": extra})
    else:
        import keyscan

        data, source = keyscan.scan(), "scan"
    if isinstance(data, dict):
        data["source"] = source
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        raise ValueError("Expected a rigpage keys export with a groups array.")
    for group in data["groups"]:
        if not (isinstance(group, dict) and isinstance(group.get("id"), str)
                and isinstance(group.get("rows"), list)
                and all(isinstance(row, dict) and isinstance(row.get("value"), str)
                        and isinstance(row.get("name"), str) for row in group["rows"])):
            raise ValueError("Expected rigpage groups with named shortcut rows.")
    return data


def rows_for(data, group_id):
    return [row for group in data["groups"] if group.get("id") == group_id
            for row in group["rows"]]


def shortcut_text(chord, prefix):
    """Today's history spelling of a chord: `prefix+h` with prefix `ctrl+space` is `ctrl+space → h`."""
    if chord.startswith("prefix+"):
        return prefix + " → " + chord.removeprefix("prefix+") if prefix else ""
    return chord


OUTCOMES = ("clean", "nudged", "shown")
DAYS = (1, 3, 7, 14, 30)
SCHEMA = 3


@dataclass(frozen=True)
class LessonRecord:
    stars: int
    best_seconds: float | None
    last_practised: float | None


def stars(results, complete=True):
    """Stars for a lesson run (design § 3): 3 when every recall was clean and every build had
    no hint, 2 when nothing was shown, 1 for finishing, 0 for a run left before the end.

    `results` carry `kind` (learn, recall, build), `outcome` and `hint_level`.
    """
    if not complete:
        return 0
    scored = [r for r in results if r.kind != "learn"]
    if all((r.outcome == "clean") if r.kind == "recall" else (r.hint_level == 0 and r.outcome != "shown")
           for r in scored):
        return 3
    return 2 if all(r.outcome != "shown" for r in scored) else 1


class History:
    """Spaced review per card, lesson stars and personal bests, in one SQLite file.

    Older files are migrated in place: rows are kept and columns or tables only added.
    """

    def __init__(self, path=None):
        if path is None:
            root = Path(os.getenv("XDG_STATE_HOME") or Path.home() / ".local/state")
            folder = root / "keys-trainer"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / "practice.sqlite3"
        self.db = sqlite3.connect(path)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("""CREATE TABLE IF NOT EXISTS practice (
                card TEXT PRIMARY KEY, streak INTEGER NOT NULL, due REAL NOT NULL,
                recall_seconds REAL
            )""")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(practice)")}
            for column, kind in (("recall_seconds", "REAL"), ("best_seconds", "REAL"),
                                 ("last_practised", "REAL"), ("outcome", "TEXT")):
                if column not in columns:
                    self.db.execute(f"ALTER TABLE practice ADD COLUMN {column} {kind}")
            if self.db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA:
                # Old rows: a streak means the last drill was recalled, none means it needed help.
                self.db.execute("""UPDATE practice SET
                    outcome = COALESCE(outcome, CASE WHEN streak > 0 THEN 'clean' ELSE 'shown' END),
                    best_seconds = COALESCE(best_seconds, recall_seconds)""")
            if self.db.execute("PRAGMA user_version").fetchone()[0] < 3:
                self._alias_old_cards()
            self.db.execute(f"PRAGMA user_version = {SCHEMA}")

            self.db.execute("""CREATE TABLE IF NOT EXISTS layout_scores (
                blueprint TEXT PRIMARY KEY, best_seconds REAL
            )""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS lessons (
                lesson TEXT PRIMARY KEY, stars INTEGER NOT NULL, best_seconds REAL, last_practised REAL
            )""")

    def _alias_old_cards(self):
        """Old rows no current card matches get a twin under the current key (the old row stays):
        Yazi's `mark_notes` is `mark_one`, and `hyprland:workspace:N` rows are `switch_workspace`
        with its one indexed chord (`alt+4` becomes `alt+1..9`). A twin is never overwritten."""
        columns = "streak, due, recall_seconds, best_seconds, last_practised, outcome"
        for key, *values in self.db.execute(
                f"SELECT card, {columns} FROM practice ORDER BY streak DESC, due").fetchall():
            try:
                track, action, shortcuts = json.loads(key)
            except (ValueError, TypeError):
                continue
            if track == "Yazi" and action == "mark_notes":
                twin = [track, "mark_one", shortcuts]
            elif track == "Hyprland" and isinstance(action, str) and action.startswith("hyprland:workspace:") \
                    and shortcuts and shortcuts[0][-1:].isdigit():
                twin = [track, "switch_workspace", [shortcuts[0][:-1] + "1..9"]]
            else:
                continue
            self.db.execute(f"INSERT OR IGNORE INTO practice (card, {columns}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (json.dumps(twin, ensure_ascii=False), *values))

    def layout_records(self):
        return dict(self.db.execute("SELECT blueprint, best_seconds FROM layout_scores"))

    def finish_layout(self, blueprint, seconds, assisted):
        with self.db:
            self.db.execute("""INSERT INTO layout_scores VALUES (?, ?)
                ON CONFLICT(blueprint) DO UPDATE SET best_seconds = excluded.best_seconds
                WHERE excluded.best_seconds IS NOT NULL AND
                    (best_seconds IS NULL OR excluded.best_seconds < best_seconds)""",
                (blueprint, None if assisted else seconds))

    def best_seconds(self, card):
        """The fastest clean recall of the card, or None before the first one."""
        row = self.db.execute("SELECT best_seconds FROM practice WHERE card=?", (card.key,)).fetchone()
        return row[0] if row else None

    def progress(self):
        return {key: (streak, due) for key, streak, due in
                self.db.execute("SELECT card, streak, due FROM practice")}

    def last_practised(self, card):
        row = self.db.execute("SELECT last_practised FROM practice WHERE card=?", (card.key,)).fetchone()
        return row[0] if row else None

    def record_outcome(self, card, outcome, now=None, *, seconds=None):
        """Schedule a card after a scored step (design § 3) and return when it is due.

        clean: streak +1, due in 1, 3, 7, 14, 30 days (a clean early review keeps the date);
        nudged: streak kept, due tomorrow; shown: streak 0, due in a minute.
        """
        if outcome not in OUTCOMES:
            outcome = "shown"
        now = time.time() if now is None else now
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            old = self.db.execute("SELECT streak, due, recall_seconds, best_seconds FROM practice WHERE card=?",
                                  (card.key,)).fetchone()
            streak, due, previous, best = old if old else (0, None, None, None)
            if outcome == "clean":
                if seconds is not None:
                    previous = seconds
                    best = seconds if best is None else min(best, seconds)
                if not (old and streak > 0 and due > now):
                    streak += 1
                    due = now + DAYS[min(streak, len(DAYS)) - 1] * 86400
            elif outcome == "nudged":
                due = now + 86400
            else:
                streak, due = 0, now + 60
            self.db.execute("""INSERT OR REPLACE INTO practice
                (card, streak, due, recall_seconds, best_seconds, last_practised, outcome)
                VALUES (?, ?, ?, ?, ?, ?, ?)""", (card.key, streak, due, previous, best, now, outcome))
        return due

    def meet(self, card, now=None):
        """A LEARN step was finished: a card never practised before becomes due now.

        A card with history keeps its schedule: replaying a lesson does not wipe a streak.
        """
        now = time.time() if now is None else now
        with self.db:
            self.db.execute("""INSERT INTO practice (card, streak, due, last_practised) VALUES (?, 0, ?, ?)
                ON CONFLICT(card) DO UPDATE SET last_practised = excluded.last_practised""",
                (card.key, now, now))

    def record_step(self, result, now=None):
        """Apply a lesson step (`lesson.StepResult`) to every card it practised.

        LEARN done: met. RECALL (lesson or warm-up): its outcome. BUILD and skipped LEARN: stars only.
        """
        for card in result.cards:
            if result.outcome == "met":
                self.meet(card, now)
            elif result.kind == "recall" and result.outcome in OUTCOMES:
                self.record_outcome(card, result.outcome, now, seconds=result.seconds)

    def finish_lesson(self, lesson, results, now=None, *, seconds=None, complete=True):
        """Store a lesson run; keep the best stars and the fastest complete run. Returns the run's stars."""
        now = time.time() if now is None else now
        earned = stars(results, complete)
        with self.db:
            self.db.execute("""INSERT INTO lessons VALUES (?, ?, ?, ?)
                ON CONFLICT(lesson) DO UPDATE SET
                    stars = MAX(stars, excluded.stars),
                    best_seconds = CASE WHEN excluded.best_seconds IS NULL THEN best_seconds
                        WHEN best_seconds IS NULL THEN excluded.best_seconds
                        ELSE MIN(best_seconds, excluded.best_seconds) END,
                    last_practised = excluded.last_practised""",
                (lesson, earned, seconds if complete else None, now))
        return earned

    def lessons(self):
        return {lesson: LessonRecord(stars, best, last) for lesson, stars, best, last in
                self.db.execute("SELECT lesson, stars, best_seconds, last_practised FROM lessons")}

    def close(self):
        self.db.close()


def pick_warmup(history, cards, now=None, cap=6):
    """The warm-up: cards already met that are due, most overdue first, at most `cap`.

    `cards` maps a card id (`herdr:split_vertical`) to its practice Card; returns card ids.
    """
    now = time.time() if now is None else now
    progress = history.progress()
    due = [(progress[card.key][1], card_id) for card_id, card in cards.items()
           if card.key in progress and progress[card.key][1] <= now]
    return [card_id for _, card_id in sorted(due)[:cap]]
