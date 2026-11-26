"""What the trainer needs: the start check before Home, and the terminal part of `keys-trainer check`."""

import os
import shutil

import curriculum
from router import query_keyboard
from strings import DOCTOR as TEXT, REPORT, START


def terminal_report(answer=None, env=None, size=None):
    """Lines for the check report. `answer` is `query_keyboard()`'s result, asked when None."""
    env = os.environ if env is None else env
    flags, answered, _ = query_keyboard() if answer is None else answer
    if flags is not None:
        protocol = TEXT["kitty"].format(flags=flags, ambiguous=TEXT["ambiguous"])
    else:
        protocol = TEXT["legacy" if answered else "silent"].format(ambiguous=TEXT["ambiguous"])
    inside = env.get("HERDR_ENV") == "1"
    columns, rows = size or shutil.get_terminal_size()
    fit = (TEXT["size_small"] if columns < 40 or rows < 12
           else TEXT["size_two"] if columns < 80 or rows < 20 else "")
    return [
        protocol,
        TEXT["herdr_inside"] if inside else TEXT["herdr_env"].format(value=env.get("HERDR_ENV", TEXT["unset"])),
        TEXT["size"].format(columns=columns, rows=rows) + fit,
        TEXT["term"].format(value=env.get("TERM", TEXT["unset"])),
    ]


# The start check: what the trainer needs, said once per missing or mismatched piece.

def start_check(data, resolved, env=None, which=shutil.which):
    """Plain lines for what is missing or mismatched; empty when all is well.

    `resolved` is `curriculum.resolve(...)` for these keys. Only a missing herdr stops the
    trainer (see `fatal`); every other line names the lessons that are off because of it.
    """
    env = os.environ if env is None else env
    lines, off = [], set()
    if not which("herdr"):
        lines.append(START["herdr"])
        off.update(state.lesson.tool for state in resolved.lessons)
    if not which("yazi") and _lessons_of(resolved, "yazi"):
        lines.append(START["yazi"].format(**_named(_lessons_of(resolved, "yazi"))))
        off.add("yazi")
    if not which("strata") and _lessons_of(resolved, "strata"):
        lines.append(START["strata"].format(**_named(_lessons_of(resolved, "strata"))))
        off.add("strata")
    if not which("bash") and _lessons_of(resolved, "shell"):
        lines.append(START["shell"].format(**_named(_lessons_of(resolved, "shell"))))
        off.add("shell")
    if _no_hyprland(resolved) and _lessons_of(resolved, "desktop"):
        lines.append(START["hyprland"].format(**_named(_lessons_of(resolved, "desktop"))))
        off.add("desktop")
    if env.get("HERDR_ENV") == "1" and not which("foot"):
        lines.append(START["foot"])
    notes = key_notes(data)
    if notes:
        lines += [*key_source(data), *notes]
    return lines + unbound_lines(data, resolved, skip=off)


def fatal(which=shutil.which):
    """Whether the trainer cannot start at all: without herdr no lesson can run."""
    return not which("herdr")


def key_source(data):
    """Where the user's keys came from (`data["source"]`), as one line; none when not said."""
    source = data.get("source")
    if not source:
        return []
    return [START["source"].format(source=START["sources"].get(source, source))]


def key_notes(data):
    return [START["note"].format(note=note) for note in data.get("notes") or ()]


def unbound_lines(data, resolved, skip=()):
    """One line per lesson with cards the user has no key for: its steps for them show how to add
    the key (and `s` skips them), or the lesson is off when too few steps are left to run."""
    lines = []
    for state in resolved.lessons:
        lesson = state.lesson
        if lesson.tool in skip or state.active == state.total:
            continue
        missing = []
        for sid in dict.fromkeys(step.scenario for step in lesson.steps):
            if resolved.active(sid):
                continue
            scenario = resolved.curriculum.scenarios[sid]
            needed = (*scenario.cards, *(m.card for m in (*scenario.solution, *scenario.setup) if m.card))
            missing += [card for card in needed if card not in resolved.bindings and _unbound(data, card)]
        actions = [card.partition(":")[2].replace("_", " ") for card in dict.fromkeys(missing)]
        if not actions:
            continue  # off for another reason (the terminal, Hyprland): said elsewhere
        shown = actions[0] if len(actions) == 1 else START["comma"].join(actions[:-1]) + START["or"] + actions[-1]
        name = START["lesson_start"].format(lesson=START["lesson"].format(order=lesson.order, title=lesson.title))
        tool = START["tools"].get(lesson.tool, lesson.tool)
        lines.append(START["notice" if state.visible else "no_key"].format(
            lesson=name, tool=tool, actions=shown, active=state.active, total=state.total))
    return lines


def _unbound(data, card):
    """True when the user's keys have no chord at all for the card (not merely one the terminal cannot send)."""
    tool, _, action = card.partition(":")
    resolver = curriculum.RESOLVERS.get(tool)
    return resolver is None or not resolver(data, action)[0]


def _no_hyprland(resolved):
    return any(not item.active and item.reason == REPORT["no_hyprland"] for item in resolved.scenarios.values())


def _lessons_of(resolved, tool):
    return [state.lesson for state in resolved.lessons if state.lesson.tool == tool]


def _named(lessons):
    """{lessons, off} for a START line: «lesson 8 “Desktop (live)”» or «lessons 6 “…” and 7 “…”»."""
    names = [START["lesson"].format(order=lesson.order, title=lesson.title) for lesson in lessons]
    if len(names) == 1:
        return {"lessons": START["lessons"][0].format(lessons=names[0]), "off": START["off"][0]}
    return {"lessons": START["lessons"][1].format(lessons=START["comma"].join(names[:-1]), last=names[-1]),
            "off": START["off"][1]}
