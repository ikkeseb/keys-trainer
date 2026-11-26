"""keys-trainer: the guided path (Home, warm-up, lessons), the layout workshop, `check`, `demo`.

One command opens Home (design § 5.1, 5.2). Enter runs today's plan: the warm-up of due
shortcuts (one private session per tool, herdr then Yazi), then the chosen lesson's intro
(§ 5.3), the lesson in the private herdr's own terminal with the coach bar (`runner`), and
the lesson-complete card (§ 5.10), whose Enter opens the next intro. `q` there ends the day
(§ 5.11). Started inside herdr, Home only explains, and offers its own window (decision 4).
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import time
from collections import deque
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import curriculum
import doctor
import layout_lab
import lesson
import practice
import runner
from hypr import HyprObserver
from practice import History, read_keys, rows_for
from router import Click, Input, KeyboardMode, ambiguous_in_legacy, key_name, query_keyboard
from screen import BOLD, CARD_WIDTH, GOLD, INK, MINT, MUTED, Card, Terminal, Text
from strings import DESKTOP, LESSON, START, SHELL as TEXT

LAUNCHER = Path(__file__).resolve().with_name("keys-trainer")
MODES = ("layouts", "check", "demo")
EXECUTABLES = {"herdr": ("herdr",), "yazi": ("herdr", "yazi"), "shell": ("herdr", "bash"), "strata": ("strata",)}  # what a lesson's tool needs on PATH
WARMUP_TOOLS = ("herdr", "yazi", "shell", "strata", "desktop")  # one session each, in this order: the live desktop last
ROUND_CAP = 8  # «Today's round»: due cards, then the weakest, up to this many
INTRO_KEYS = 30  # the intro's keys column, at most
DEMO_PRESS = 1.2  # seconds of «the user's turn» before the demo presses the hint key


# The command line.

def parse_args(argv):
    parser = argparse.ArgumentParser(prog="keys-trainer", description=TEXT["cli_description"])
    parser.add_argument("mode", nargs="?", metavar="{layouts,check,demo}", help=TEXT["cli_mode"])
    parser.add_argument("--keys", metavar="FILE", help=TEXT["cli_keys"])
    parser.add_argument("--lesson", metavar="ID", help=TEXT["cli_lesson"])
    parser.add_argument("--all", action="store_true", help=TEXT["cli_all"])
    parser.add_argument("--layout", choices=sorted(curriculum.LAYOUTS), default=curriculum.DEFAULT_LAYOUT,
                        help=TEXT["cli_layout"])
    ns = parser.parse_args(argv)
    if ns.mode == "sandbox":  # the old name of the private path, kept one release
        ns.mode = None
    if ns.mode not in (None, *MODES):
        parser.error(TEXT["cli_unknown_mode"].format(mode=repr(ns.mode)))
    if ns.all and ns.mode != "demo":
        parser.error(TEXT["cli_all_demo"])
    if ns.lesson and ns.mode:
        parser.error(TEXT["cli_lesson_mode"])
    return ns


def _interactive():
    return sys.stdin.isatty() and sys.stdout.isatty()


def main(argv=None, *, spawn=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        args = parse_args(argv)
        data = read_keys(args.keys)
        if args.mode == "layouts":
            return layouts(data)
        if args.mode == "check":
            answer = query_keyboard() if _interactive() else (None, False, b"")
            for line in check_lines(curriculum.load(), data, answer, layout=args.layout):
                print(line)
            return 0
        if args.mode == "demo":
            return demo(args, data)
        return guided(args, data, argv, spawn or spawn_window)
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error, EOFError, subprocess.TimeoutExpired) as exc:
        print(f"keys-trainer: {exc}", file=sys.stderr)
        return 1


def layouts(data):
    if not _interactive():
        raise ValueError(TEXT["layouts_tty"])
    history = History()
    try:
        with Terminal() as ui:
            layout_lab.run(ui, rows_for(data, "herdr"), history)
    finally:
        history.close()
    return 0


def check_lines(lessons, data, answer, env=None, *, layout=curriculum.DEFAULT_LAYOUT):
    """`keys-trainer check`: the terminal, the tools, the user's chords per card, every lesson."""
    ambiguous = ambiguous_in_legacy if answer[0] is None else None
    desktop = HyprObserver.running(env)
    resolved = curriculum.resolve(lessons, data, ambiguous, desktop=desktop, layout=layout)
    lines = [TEXT["check_terminal"], *("  " + line for line in doctor.terminal_report(answer, env)), "",
             TEXT["check_tools"]]
    for tool in ("herdr", "yazi", "bash", "fzf", "zoxide", "fd", "rg", "strata", "foot"):
        path = shutil.which(tool)
        if path:
            lines.append(TEXT["check_tool"].format(tool=tool, path=path))
    inside = (os.environ if env is None else env).get("HERDR_ENV") == "1"
    if not shutil.which("foot") and not inside:  # inside herdr, the start check says it
        lines.append("  " + START["foot_optional"])
    if desktop and any(state.lesson.tool == "desktop" for state in resolved.lessons):
        lines.append("  " + START["hyprland_running"])
    issues = doctor.start_check(data, resolved, env, which=shutil.which)
    if not doctor.key_notes(data):  # with notes, the start check names the source itself
        issues = doctor.key_source(data) + issues
    lines += ["  " + line for line in issues]
    lines += ["", TEXT["check_cards"]]
    for card, binding in sorted(resolved.bindings.items()):
        line = TEXT["check_card"].format(card=card, chords=TEXT["list"].join(binding.labels()))
        if binding.ambiguous:
            shown = TEXT["list"].join(curriculum.chord_text(c, binding.prefix) for c in binding.ambiguous)
            line += TEXT["check_ambiguous"].format(chords=shown)
        lines.append(line)
    return lines + ["", TEXT["check_lessons"], *curriculum.report(resolved)]


def demo(args, data):
    lessons = curriculum.load()
    if args.all or not _interactive():
        try:
            import selftest
        except ImportError as exc:
            raise ValueError(TEXT["no_selftest"]) from exc
        results = selftest.solve_all(curriculum.resolve(lessons, data), data)
        if not args.all:  # one evidence line per active scenario
            results = [r for r in results if getattr(r, "kind", "solution") == "solution"]
        for line in selftest.lines(results):
            print(line, flush=True)
        return 0 if all(getattr(r, "passed", True) for r in results) else 1
    _need_herdr()
    with Terminal() as ui, KeyboardMode() as mode:
        resolved = curriculum.resolve(lessons, data, mode.ambiguous, layout=args.layout)
        first = next(state for state in resolved.visible if state.lesson.tool == "herdr")  # never the live one
        keys, mode.pending = Keys(ui, mode.pending), b""
        shell = Shell(ui, keys, resolved, data, None, mode=mode)
        shell.play(lesson.plan_lesson(resolved, first.lesson.id), "herdr", autoplay=DEMO_PRESS)
    return 0


def _need_herdr():
    if not shutil.which("herdr"):
        raise ValueError(TEXT["no_herdr"])


# Inside herdr (decision 4): explain, and offer a window of its own.

def window_argv(argv):
    return ["foot", "--app-id", "keys-trainer", "--", str(LAUNCHER), *argv]


def window_env(env):
    """The environment without herdr's markers, so the new window starts the guided path."""
    return {name: value for name, value in env.items() if not name.startswith("HERDR_")}


def window_available(env):
    return bool(shutil.which("foot") and env.get("WAYLAND_DISPLAY"))


def spawn_window(argv, env):
    subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


def guided(args, data, argv, spawn, env=None):
    env = os.environ if env is None else env
    if not _interactive():
        raise ValueError(TEXT["needs_tty"])
    lessons = curriculum.load()
    ids = [item.id for item in lessons.lessons]
    if args.lesson and args.lesson not in ids:
        raise ValueError(TEXT["unknown_lesson"].format(id=args.lesson, ids=", ".join(ids)))
    plain = curriculum.resolve(lessons, data, desktop=HyprObserver.running(env), layout=args.layout)
    issues, fatal = doctor.start_check(data, plain, env, which=shutil.which), doctor.fatal(shutil.which)
    if issues and not start_check(issues, fatal=fatal):
        return 1 if fatal else 0
    history = History()
    try:
        if env.get("HERDR_ENV") == "1":
            with Terminal() as ui:
                resolved = curriculum.resolve(lessons, data, desktop=HyprObserver.running(env), layout=args.layout)
                shell = Shell(ui, Keys(ui), resolved, data, history)
                shell.inside_herdr(argv, env, spawn)
            return 0
        with Terminal() as ui, KeyboardMode() as mode:
            resolved = curriculum.resolve(lessons, data, mode.ambiguous, desktop=HyprObserver.running(env),
                                          layout=args.layout)
            if args.lesson and not resolved.lesson(args.lesson).visible:
                raise ValueError(TEXT["hidden_lesson"].format(id=args.lesson))
            keys, mode.pending = Keys(ui, mode.pending), b""
            Shell(ui, keys, resolved, data, history, mode=mode).run(args.lesson)
    finally:
        history.close()
    return 0


def start_check(issues, *, fatal=False, read=None, width=None):
    """Print the start check's lines before Home. True to go on: Enter; q (or end of input) quits.

    A fatal check (no herdr) prints and returns False without asking.
    """
    width = min(width or shutil.get_terminal_size().columns, 100)
    print(START["header"])
    for line in issues:
        print(textwrap.fill(line, width, initial_indent=START["bullet"],
                            subsequent_indent=" " * len(START["bullet"])))
    if fatal:
        return False
    try:
        answer = (read or input)("\n" + START["prompt"])
    except EOFError:
        return False
    return answer.strip().lower() != "q"


# Keys on the cards.

class Keys:
    """Name the keys pressed on a card the same way in legacy and kitty encodings."""

    def __init__(self, ui, pending=b""):
        self.ui = ui
        self.inputs = Input()
        self.queue = deque(self.inputs.events(pending)) if pending else deque()

    def next(self, delay=0.1):
        if not self.queue:
            self.queue.extend(self.inputs.events(self.ui.read(delay, forward_ctrl_c=True)))
        while self.queue:
            token = self.queue.popleft()
            name = None if isinstance(token, Click) else key_name(token)
            if name == "ctrl+c":
                raise KeyboardInterrupt
            if name:
                return name
        return None

    def clear(self):
        self.queue.clear()


# Models and cards (pure: tested without a terminal).

@dataclass(frozen=True)
class PathRow:
    lesson: str
    number: int
    title: str
    tool: str
    stars: int | None  # best stars of a finished run; None before the first one
    ready: bool = True


@dataclass(frozen=True)
class Home:
    rows: tuple[PathRow, ...]
    recommended: int  # index into rows, where the cursor starts
    first_run: bool
    last_practised: float | None = None
    warmup: int = 0  # due shortcuts the warm-up covers
    round: int = 0  # shortcuts in «Today's round», offered once every lesson is done (0: not offered)
    round_build: bool = False  # the round ends with a layout build

    @property
    def all_done(self) -> bool:
        return bool(self.rows) and all(row.stars for row in self.rows if row.ready)


ROUND = -1  # Home's cursor on «Today's round», above the path


def home_model(resolved, records, *, practised=False, warmup=0, ready=lambda tool: True, round_=(0, False)):
    """Home's plan and path: the visible lessons with their stars, and the lesson to start.

    `records` is `History.lessons()`; `practised` says whether any card has history. The
    recommended lesson is the first ready one never finished, else the first below three
    stars, else the first. `round_` is («Today's round» shortcuts, ends with a build); it is
    offered only once every ready lesson has stars.
    """
    rows = []
    for number, state in enumerate(resolved.visible, 1):
        record = records.get(state.lesson.id)
        stars = record.stars if record and record.stars > 0 else None
        rows.append(PathRow(state.lesson.id, number, state.lesson.title, state.lesson.tool, stars,
                            bool(ready(state.lesson.tool))))
    open_ = [i for i, row in enumerate(rows) if row.ready and row.stars is None]
    better = [i for i, row in enumerate(rows) if row.ready and (row.stars or 0) < 3]
    recommended = (open_ or better or [0])[0]
    last = max((r.last_practised for r in records.values() if r.last_practised), default=None)
    home = Home(tuple(rows), recommended, not records and not practised, last, warmup)
    if home.all_done and (round_[0] or round_[1]):
        home = Home(home.rows, recommended, home.first_run, last, warmup, *round_)
    return home


def minutes(steps):
    return max(2, round(steps * 0.45))


def _plural(count, word):
    one, many = TEXT[word]
    return one if count == 1 else many


def _last_text(last, now):
    if last is None:
        return ""
    days = (date.fromtimestamp(now) - date.fromtimestamp(last)).days
    if days <= 0:
        return TEXT["last_today"]
    return TEXT["last_yesterday"] if days == 1 else TEXT["last_days"].format(days=days)


def _stars(stars):
    if stars is None:
        return [("○○○", MUTED)]
    return [("★" * stars, GOLD), ("☆" * (3 - stars), MUTED)]


def home_card(home, cursor, resolved, *, now, hint_key="?", inside_herdr=False, window=False):
    rows = home.rows
    chosen = rows[cursor] if rows and cursor != ROUND else None
    steps = len(resolved.lesson(chosen.lesson).steps) if chosen else 0
    lines = []
    if home.first_run:
        right = TEXT["first_right"].format(minutes=minutes(steps))
        lines += [((TEXT["first_title"], BOLD),), ""]
    else:
        right = _last_text(home.last_practised, now)
        lines += [((TEXT["back_title"], BOLD),), ""]
    if inside_herdr:
        lines += [Text(TEXT["herdr_line"], GOLD), ""]
    if home.first_run:
        lines += [Text(TEXT["first_private"]), "",
                  Text(TEXT["first_how"], INK, TEXT["first_how_label"], 16), ""]
    elif cursor == ROUND:
        total = minutes(home.round + 2 * home.round_build)
        what = TEXT["round_what"].format(count=home.round, shortcuts=_plural(home.round, "shortcuts"))
        if home.round_build:
            what = TEXT["round_build"].format(what=what) if home.round else TEXT["round_only_build"]
        lines += [((TEXT["today"], BOLD), ("  " + TEXT["minutes"].format(minutes=total), MUTED)),
                  ((f"  1  ", MUTED), (LESSON["round_title"].ljust(15), INK), (what, MUTED)), ""]
    elif chosen:
        total = minutes(steps) + (max(1, round(home.warmup * 0.2)) if home.warmup else 0)
        lines += [((TEXT["today"], BOLD), ("  " + TEXT["minutes"].format(minutes=total), MUTED))]
        plan = []
        if home.warmup:
            plan.append((TEXT["today_warmup"], TEXT["today_warmup_what"].format(
                count=home.warmup, shortcuts=_plural(home.warmup, "shortcuts"))))
        plan.append((TEXT["today_lesson"].format(number=chosen.number), chosen.title))
        lines += [((f"  {i}  ", MUTED), (what.ljust(15), INK), (detail, MUTED)) for i, (what, detail) in enumerate(plan, 1)]
        lines.append("")
    lines.append(((TEXT["path"], MUTED),))
    path = (len(lines), len(lines) + len(rows))  # the rows that scroll on a short screen
    column = min(26, max((len(row.title) for row in rows), default=0) + 3)
    digits = len(str(max((row.number for row in rows), default=0)))
    for index, row in enumerate(rows):
        here = index == cursor
        mark = ("› ", BOLD) if here else ("✓ ", MINT) if row.stars else ("  ", "")
        note, note_style = "", MUTED
        if not row.ready:
            note = TEXT["note_tool"].format(tool=row.tool.capitalize() if row.tool in ("yazi", "strata", "shell") else row.tool)
        elif index == home.recommended:
            note, note_style = TEXT["note_start" if home.first_run else "note_next"], MINT
        elif row.stars == 2:
            note = TEXT["note_one_star"]
        elif row.stars == 1:
            note = TEXT["note_two_stars"]
        lines.append((("  ", ""), mark, (f"{row.number:>{digits}}  ", BOLD if here else INK),
                      (row.title.ljust(column), BOLD if here else INK), *_stars(row.stars),
                      ("   " + note if note else "", note_style)))
    if home.first_run and not inside_herdr:
        lines += ["", Text(TEXT["first_stuck"].format(key=hint_key), MUTED)]
    if inside_herdr:
        footer = [TEXT["herdr_window"]] * window + [TEXT["home_quit"]]
    elif home.first_run:
        footer = [TEXT["home_start_first"].format(number=chosen.number if chosen else 1),
                  TEXT["home_pick_first"], TEXT["home_layouts"], TEXT["home_quit"]]
    else:
        footer = [TEXT["home_start"], TEXT["home_pick"], TEXT["home_layouts"], TEXT["home_quit"]]
    return Card(TEXT["brand"], right, tuple(lines), tuple(footer), scroll=path,
                focus=path[0] + cursor if chosen else None)


def intro_card(resolved, lesson_id, number, total):
    state = resolved.lesson(lesson_id)
    item = state.lesson
    plan = lesson.plan_lesson(resolved, lesson_id)
    scenarios = resolved.curriculum.scenarios
    lines = [((item.title, BOLD),), "", Text(item.intro), ""]
    if item.tool == "desktop":  # plainly: the real desktop, and the trainer only watches
        lines += [Text(TEXT["intro_live"], GOLD), ""]
    lines += [Text(item.pattern, INK, TEXT["intro_pattern"], 14), ""]
    learned = [scenarios[step.scenario] for step in state.steps if step.kind == "learn"]
    if learned:
        # Keys in a gold column, each goal wrapped in its own column beside them; keys too
        # wide for the column get their own row, with the goal under them.
        shown = [(lesson.keys_line(plan, scenario), scenario.goal) for scenario in learned]
        column = min(INTRO_KEYS, max(len(keys) for keys, _ in shown) + 4)
        lines.append(((TEXT["intro_learn"], MUTED),))
        for keys, goal in shown:
            said = textwrap.wrap(goal, CARD_WIDTH - 2 - column) or [""]
            if len(keys) + 2 > column:
                lines.append((("  " + keys, GOLD),))
                keys = ""
            lines += [(("  " + (keys if index == 0 else "").ljust(column), GOLD), (text, INK))
                      for index, text in enumerate(said)]
    build = next((scenarios[step.scenario] for step in state.steps if step.kind == "build"), None)
    if build and item.builds:
        lines += ["", ((TEXT["intro_build"], MUTED),)]
        width = max(len(row) for row in item.builds)
        said = textwrap.wrap(build.goal, CARD_WIDTH - width - 8)
        for index in range(max(len(item.builds), len(said))):
            row = item.builds[index] if index < len(item.builds) else ""
            text = said[index] if index < len(said) else ""
            lines.append((("  " + row.ljust(width), MINT), ("    " + text, INK)))
    footer = (TEXT["intro_start"], TEXT["intro_test"], TEXT["intro_back"])
    right = TEXT["minutes"].format(minutes=minutes(len(state.steps)))
    return Card(TEXT["intro_header"].format(number=number, total=total), right, tuple(lines), footer)


def _seconds(seconds):
    return lesson.seconds_text(seconds) if seconds is not None else ""


def complete_card(resolved, result, number, upcoming=None):
    """The lesson-complete card; `upcoming` is the next lesson's (title, steps) or None."""
    scenarios = resolved.curriculum.scenarios
    plan = lesson.plan_lesson(resolved, result.lesson)
    final, first = {}, {}
    for step in result.steps:
        if step.kind != "learn" or step.notice:
            first.setdefault(step.scenario, step)
            final[step.scenario] = step  # a step that came back: its last try, after the first
    lines = [((result.title, BOLD),), ""]
    missing = {plan_step.scenario.id: plan_step.missing for plan_step in plan.steps if plan_step.missing}
    labels = {sid: TEXT["done_no_key"].format(actions=", ".join(item["action"] for item in missing.get(sid, ())))
              if step.notice
              else TEXT["done_build"] if step.kind == "build"  # the goal is a long shape; the title is above
              else lesson.keys_line(plan, scenarios[sid]) for sid, step in final.items()}
    width = min(max((len(label) for label in labels.values()), default=0) + 2, 42)
    labels = {sid: label if len(label) < width else label[:width - 3] + "…" for sid, label in labels.items()}
    for sid, step in final.items():
        label = labels[sid]
        said = lambda outcome: TEXT.get("done_" + outcome, "")
        help_word = TEXT["done_skipped"] if step.notice else said(step.outcome)
        if first[sid] is not step and first[sid].outcome != step.outcome:
            help_word = TEXT["done_then"].format(first=said(first[sid].outcome), final=help_word)
        tail = [("    " + TEXT["done_again"], MUTED)] if step.outcome == "shown" and step.kind == "recall" else []
        lines.append((("  " + label.ljust(max(26, width)), INK), (_seconds(step.seconds).ljust(10), MUTED),
                      (help_word, MINT if first[sid].outcome == "clean" else GOLD), *tail))
    earned = practice.stars(lesson.scored(result.steps), result.complete)
    lines.append("")
    if earned >= 3:
        lines.append(Text(TEXT["star_full"], MUTED))
    elif earned == 2:
        missed = next((sid for sid, step in final.items() if not step.notice
                       and ((step.outcome != "clean") if step.kind == "recall" else step.hint_level)), None)
        lines.append(Text(TEXT["star_one_more"].format(goal=scenarios[missed].goal) if missed
                          else TEXT["star_two_more"], MUTED))
    else:
        lines.append(Text(TEXT["star_two_more"], MUTED))
    if upcoming:
        first = TEXT["done_next"].format(title=upcoming[0], minutes=TEXT["minutes"].format(minutes=minutes(upcoming[1])))
    else:
        first = TEXT["done_finish"]
    footer = (first, TEXT["done_replay"], TEXT["done_quit"])
    stars = "★" * earned + "☆" * (3 - earned)
    return Card(TEXT["done_header"].format(number=number), stars, tuple(lines), footer,
                header_style=MINT, right_style=GOLD)


def end_card(resolved, results, history, *, known, started, now):
    """Session end (§ 5.11): what the user practised today, what comes back, and what to do next."""
    labels = {binding.practice.key: binding.label() for binding in resolved.bindings.values()}
    steps = [step for run in results for step in run.steps]
    keys, recall, met, fastest = [], {}, [], None
    for step in steps:
        for card in step.cards:
            if card.key not in keys:
                keys.append(card.key)
            if step.kind == "recall":
                recall[card.key] = step.outcome
                if step.outcome == "clean" and step.seconds is not None and (fastest is None or step.seconds < fastest[0]):
                    fastest = (step.seconds, card.key)
            if step.outcome == "met" and card.key not in known and card.key not in met:
                met.append(card.key)
    finished = {run.lesson for run in results if run.lesson and run.complete}
    clean = sum(1 for key in keys if recall.get(key) == "clean")
    joined = lambda found: TEXT["list"].join(labels.get(key, "?") for key in found)

    def row(label, value, style=INK):
        return Text(value, style, label, 18)

    lines = [row(TEXT["end_today"], TEXT["end_today_line"].format(
        practised=len(keys), shortcuts=_plural(len(keys), "shortcuts"), clean=clean,
        lessons=len(finished), lesson_word=_plural(len(finished), "lessons")))]
    if met:
        lines.append(row(TEXT["end_new"], joined(met)))
    more = [key for key in keys if recall.get(key) == "shown"]
    if more:
        lines.append(row(TEXT["end_more"], joined(more), GOLD))
    if fastest:
        lines.append(row(TEXT["end_fastest"], f"{labels.get(fastest[1], '?')}   {lesson.seconds_text(fastest[0])}", MINT))
    records = history.lessons() if history is not None else {}
    home = home_model(resolved, records, practised=True)
    done = sum(1 for item in home.rows if item.stars)
    dots = "".join("●" if item.stars else "○" for item in home.rows)
    lines += ["", row(TEXT["end_path"], TEXT["end_path_line"].format(dots=dots, done=done, total=len(home.rows)))]
    progress = history.progress() if history is not None else {}
    buckets = {}
    for key in keys:
        if key in progress:
            days = max(0, round((progress[key][1] - now) / 86400))
            buckets[days] = buckets.get(days, 0) + 1
    if buckets:
        said = [TEXT["due_now" if d == 0 else "due_tomorrow" if d == 1 else "due_days"].format(count=n, days=d)
                for d, n in sorted(buckets.items())]
        lines.append(row(TEXT["end_back"], TEXT["list"].join(said)))
    soon = sum(n for d, n in buckets.items() if d <= 1)
    if home.rows:
        chosen = home.rows[home.recommended]
        steps_next = len(resolved.lesson(chosen.lesson).steps)
        total = minutes(steps_next) + (max(1, round(soon * 0.2)) if soon else 0)
        plan = (TEXT["next_warmup"].format(count=soon, number=chosen.number, title=chosen.title) if soon
                else TEXT["next_lesson"].format(number=chosen.number, title=chosen.title))
        # the minutes get their own muted row, so a long plan never splits «≈ 5 min»
        lines += [row(TEXT["end_next"], plan), row("", TEXT["minutes"].format(minutes=total), MUTED)]
    spent = TEXT["end_minutes"].format(minutes=max(1, round((now - started) / 60)))
    return Card(TEXT["end_header"], spent, tuple(lines), TEXT["end_quit"], header_style=MINT)


def error_card(problem, what="session"):
    """Why a session or the workshop did not run. A reason written for the user (a string from
    `strings`) is the headline; an exception gets a plain headline for `what` (session,
    layouts) and its own message as a muted detail, the first row to go on a short screen."""
    if isinstance(problem, str):
        return Card(TEXT["error_header"], "", (Text(problem, GOLD),), TEXT["error_back"], header_style=GOLD)
    key = "error_timeout" if isinstance(problem, subprocess.TimeoutExpired) else "error_" + what
    detail = str(problem).strip()
    lines = (Text(TEXT[key], GOLD),) + (("", Text(detail, MUTED)) if detail else ())
    return Card(TEXT["error_header"], "", lines, TEXT["error_back"], header_style=GOLD)


def _cards(resolved, ready):
    usable = {card for state in resolved.visible if state.lesson.tool in WARMUP_TOOLS and ready(state.lesson.tool)
              for step in state.steps for card in resolved.curriculum.scenarios[step.scenario].cards}
    return {card: resolved.bindings[card].practice for card in sorted(usable) if card in resolved.bindings}


def _per_tool(resolved, picked, *, cap=lesson.WARMUP_CAP, build=None, title=None):
    plans = []
    for tool in WARMUP_TOOLS:
        plan = lesson.plan_warmup(resolved, [card for card in picked if card.startswith(tool + ":")], tool=tool,
                                  cap=cap, build=build if tool == "herdr" else None, title=title)
        if plan:
            plans.append((tool, plan))
    covered = {card for _, plan in plans for step in plan.steps if step.kind == "recall"
               for card in step.scenario.cards} & set(picked)
    return plans, len(covered)


def warmup_plans(resolved, history, *, now=None, ready=lambda tool: True):
    """Today's warm-up: the due cards of the visible lessons (most overdue first, at most six),
    one plan per tool, herdr first and the live desktop last. Returns ([(tool, plan)], count
    of shortcuts covered)."""
    return _per_tool(resolved, practice.pick_warmup(history, _cards(resolved, ready), now))


def round_cards(history, cards, now=None, cap=ROUND_CAP):
    """«Today's round»'s cards: the due ones, most overdue first, then the weakest the user has met
    (lowest streak, then soonest due), up to `cap`."""
    due = practice.pick_warmup(history, cards, now, cap=cap)
    progress = history.progress()
    weak = sorted((progress[card.key][0], progress[card.key][1], card_id) for card_id, card in cards.items()
                  if card.key in progress and card_id not in due)
    return due + [card_id for *_, card_id in weak][:cap - len(due)]


def round_plans(resolved, history, *, now=None, ready=lambda tool: True, rng=random):
    """«Today's round» (design § 1): due cards, then the weakest up to eight, then one random
    layout build at the end of the herdr part. Returns ([(tool, plan)], shortcuts, has build)."""
    builds = [step.scenario for state in resolved.visible if state.lesson.tool == "herdr" and ready("herdr")
              for step in state.steps if step.kind == "build"]
    build = rng.choice(builds) if builds else None
    plans, count = _per_tool(resolved, round_cards(history, _cards(resolved, ready), now), cap=ROUND_CAP,
                             build=build, title=LESSON["round_title"])
    return plans, count, build is not None


def tool_ready(tool):
    if tool == "desktop":
        return HyprObserver.running()
    return all(shutil.which(name) for name in EXECUTABLES.get(tool, (tool,)))


def open_session(tool, data):
    if tool == "yazi":
        from yazi_backend import YaziSession
        return YaziSession(rows_for(data, "yazi"), rows_for(data, "herdr"))
    if tool == "shell":
        from shell_backend import ShellSession
        return ShellSession(rows_for(data, "shell"), rows_for(data, "herdr"))
    if tool == "strata":
        from strata_keys import KeysSession
        return KeysSession(rows_for(data, "strata"))
    if tool == "desktop":  # the user's live Hyprland, read-only
        from desktop_session import open_live
        return open_live()
    from session import HerdrSession
    return HerdrSession(rows_for(data, "herdr"))


def live_card():
    """Before the live desktop part of a warm-up or «Today's round» (design § 1)."""
    return Card(DESKTOP["header"], "", (((TEXT["live_now"], BOLD),), "", Text(DESKTOP["watching"], INK)),
                (TEXT["live_go"], TEXT["live_skip"]), header_style=GOLD)


# The flow.

class Shell:
    """Home → (warm-up) → intro → lesson → complete → next intro …, until the user ends the day."""

    def __init__(self, ui, keys, resolved, data, history, *, mode=None, ready=tool_ready,
                 session=open_session, clock=time.time, rng=random):
        self.ui, self.keys, self.resolved, self.data, self.history = ui, keys, resolved, data, history
        self.mode, self.ready, self.open_session, self.clock, self.rng = mode, ready, session, clock, rng
        self.started = clock()
        self.known = set(history.progress()) if history is not None else set()
        self.results = []
        self.warmed = False

    @property
    def visible(self):
        return self.resolved.visible

    def _number(self, lesson_id):
        return next(i for i, state in enumerate(self.visible, 1) if state.lesson.id == lesson_id)

    def _ask(self, card, names):
        """Show the card until one of `names` is pressed; repaint for a resize meanwhile."""
        while True:
            self.ui.card(card)
            name = self.keys.next()
            if name in names:
                return name

    def run(self, lesson_id=None):
        where = ("intro", lesson_id) if lesson_id else ("home",)
        while where:
            where = getattr(self, where[0])(*where[1:])
        if self.results:
            self._ask(end_card(self.resolved, self.results, self.history, known=self.known,
                               started=self.started, now=self.clock()), ("enter", "q", "esc"))

    def _home(self):
        plans, due = ([], 0) if self.warmed else warmup_plans(self.resolved, self.history, now=self.clock(),
                                                                 ready=self.ready)
        records = self.history.lessons()
        model = home_model(self.resolved, records, practised=bool(self.history.progress()),
                           warmup=due, ready=self.ready)
        self.round_plan = ([], 0, False)
        if model.all_done:
            self.round_plan = round_plans(self.resolved, self.history, now=self.clock(), ready=self.ready, rng=self.rng)
            model = home_model(self.resolved, records, practised=True, warmup=due, ready=self.ready,
                               round_=self.round_plan[1:])
        return model, plans

    def _hint_key(self, lesson_id):
        try:
            return runner.router_for(lesson.plan_lesson(self.resolved, lesson_id)).hint_key
        except (StopIteration, ValueError):
            return "?"

    def home(self):
        model, plans = self._home()
        if not model.rows:
            raise ValueError(TEXT["no_lessons"])
        top = ROUND if model.round or model.round_build else 0
        cursor = top if top == ROUND else model.recommended
        hint_key = self._hint_key(model.rows[max(cursor, 0)].lesson)
        while True:
            name = self._ask(home_card(model, cursor, self.resolved, now=self.clock(), hint_key=hint_key),
                             ("enter", "up", "down", "l", "q", "esc"))
            if name in ("up", "down"):
                cursor = max(top, min(len(model.rows) - 1, cursor + (1 if name == "down" else -1)))
            elif name == "l":
                return ("layouts",)
            elif name in ("q", "esc"):
                return None
            elif cursor == ROUND:
                return ("round", self.round_plan[0])
            else:
                chosen = model.rows[cursor]
                if plans:
                    return ("warmup", plans, chosen.lesson)
                return ("intro", chosen.lesson)

    def inside_herdr(self, argv, env, spawn):
        model, _ = self._home()
        window = window_available(env)
        while True:
            name = self._ask(home_card(model, model.recommended, self.resolved, now=self.clock(),
                                       inside_herdr=True, window=window), ("enter", "q", "esc"))
            if name != "enter":
                return
            if window:
                spawn(window_argv(argv), window_env(env))
                return

    def layouts(self):
        if self.mode is not None:
            self.mode.__exit__(None, None, None)  # the workshop reads legacy keys
        try:
            layout_lab.run(self.ui, rows_for(self.data, "herdr"), self.history)
        except ValueError as exc:
            return ("error", exc, "layouts")
        finally:
            self.ui.cache, self.ui.size = {}, None
            if self.mode is not None:
                self.mode.__enter__()
                self.keys.queue.extend(self.keys.inputs.events(self.mode.pending))
                self.mode.pending = b""
        return ("home",)

    def warmup(self, plans, lesson_id):
        self.warmed = True
        failed = self._play_all(plans)
        return failed or ("intro", lesson_id)

    def round(self, plans):
        """«Today's round»: it covers what is due, so no warm-up follows it today."""
        self.warmed = True
        return self._play_all(plans) or ("home",)

    def _play_all(self, plans):
        """Each tool's plan in its own session; the live desktop asks first (Esc skips it)."""
        for tool, plan in plans:
            if tool == "desktop" and self._ask(live_card(), ("enter", "esc", "q")) != "enter":
                continue
            result = self.play(plan, tool)
            if isinstance(result, Exception):
                return ("error", result)
            if result is None or not result.complete:
                return ("home",)
        return None

    def intro(self, lesson_id):
        state = self.resolved.lesson(lesson_id)
        if not self.ready(state.lesson.tool):
            return ("error", TEXT["note_tool"].format(tool=state.lesson.tool))
        card = intro_card(self.resolved, lesson_id, self._number(lesson_id), len(self.visible))
        name = self._ask(card, ("enter", "t", "esc", "q"))
        if name in ("esc", "q"):
            return ("home",)
        return ("lesson", lesson_id, name == "t")

    def lesson(self, lesson_id, test_out=False):
        tool = self.resolved.lesson(lesson_id).lesson.tool
        result = self.play(lesson.plan_lesson(self.resolved, lesson_id, test_out=test_out), tool)
        if isinstance(result, Exception):
            return ("error", result)
        if result is None or not result.complete:
            return ("home",)
        return ("complete", lesson_id, result)

    def complete(self, lesson_id, result):
        index = self._number(lesson_id)
        upcoming = self.visible[index] if index < len(self.visible) else None
        info = (upcoming.lesson.title, len(upcoming.steps)) if upcoming else None
        name = self._ask(complete_card(self.resolved, result, index, info), ("enter", "r", "q", "esc"))
        if name == "r":
            return ("lesson", lesson_id, False)
        if name == "enter" and upcoming:
            return ("intro", upcoming.lesson.id)
        return None

    def error(self, problem, what="session"):
        self._ask(error_card(problem, what), ("enter", "esc", "q"))
        return ("home",)

    def play(self, plan, tool, *, autoplay=None):
        """Run one plan in a fresh private session. Returns its LessonResult, or the exception
        that kept the session from starting (shown on an error card)."""
        herdr_rows = rows_for(self.data, "herdr")
        prefix = next((row["name"] for row in herdr_rows if row.get("value") == "prefix"), "")
        extra = ()
        if tool == "yazi":
            from yazi_backend import BASE_ACTIONS, yazi_bindings
            extra = [chord for options in yazi_bindings(rows_for(self.data, "yazi"), BASE_ACTIONS).values()
                     for chord, *_ in options]
        router = runner.router_for(plan, prefix=prefix, extra=extra)
        record = self.history if autoplay is None else None
        self.keys.clear()
        try:
            with self.open_session(tool, self.data) as session:
                engine = lesson.Engine(plan, session, hint_key=router.hint_key, pause_key=router.pause_key,
                                       skip_key=router.skip_key,
                                       history=record)
                result = runner.run_lesson(self.ui, session, engine, router, self.mode or _NoMode(),
                                           history=record, inputs=self.keys.inputs, autoplay=autoplay)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            return exc
        finally:
            self.ui.cache, self.ui.size = {}, None
        if result is not None and autoplay is None:
            self.results.append(result)
        return result


class _NoMode:
    pending = b""
