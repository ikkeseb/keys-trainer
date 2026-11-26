"""The coach bar under the private herdr view: a plain model in, finished ANSI rows out.

Three rows from 80 × 20, two rows from 40 × 12, and below that a notice asking for a
bigger window (design § 5.4 to 5.12). Every row fits in `width - 1` cells and ends with
an SGR reset, so the engine never formats and the bar can never bleed into the frame.
"""

from dataclasses import dataclass
import re
import unicodedata

from screen import BOLD, GOLD, INK, MINT, MUTED
from strings import COACH as TEXT  # the bar's own copy; lesson text arrives in the model
from strings import LESSON  # only to read the engine's keys hint back (`_taught`)
DOTS = {"clean": "●", "done": "●", "nudged": "◐", "shown": "◐", "current": "◉", "todo": "○"}
DOT_STYLES = {"●": MINT, "◐": GOLD, "◉": BOLD, "○": MUTED}
LINE_STYLES = {"reason": MUTED, "hint": GOLD, "miss": GOLD, "success": MINT, "checklist": MINT, "warn": GOLD}
# On two rows the middle line is folded into the first one while one of these is showing;
# a checklist never takes the goal's place (it shows beside the key labels while it fits).
EVENT_KINDS = ("hint", "miss")
TAG_STYLES = {"LIVE": GOLD, "PAUSE": GOLD, "NO KEY": GOLD}
TAG_CELLS = 7  # the widest tag, RECALL, and a space
MAP_GAP = 3


@dataclass(frozen=True)
class Bar:
    """What the bar shows. Every field is plain text; the coach adds every style.

    tag       LEARN, RECALL, BUILD, LIVE, PAUSE, NO KEY (a notice step), or ✓ during the beat.
    goal      the bold goal (or the success title next to ✓).
    chord     right-aligned in gold on row 1 after «Press» (LEARN steps).
    line      the changing middle line; `kind` picks its style: reason (muted), hint and
              miss (gold), success and checklist (mint).
    hint      label after the hint key («Hint», «Show keys», «Show me»); "" hides the hint
              key alone (a notice step); None hides both key labels, as while «Show me»
              plays and during the beat.
    hint_key, pause_key   the router's chosen keys (`Router.hint_label`, `pause_label`).
    offer     paint the hint label gold: help is on offer (the engine's `offer`, after 10 s
              without progress).
    controls  replaces the key labels on the bottom row, e.g. «Typing · Enter confirms».
    title     lesson title, bottom right; `steps` one state per step: clean, nudged, shown,
              current, todo. The count is the current step over the total.
    target    up to three lines of a BUILD target map, drawn at the right end of each row.
    """

    tag: str = ""
    goal: str = ""
    chord: str = ""
    line: str = ""
    kind: str = "reason"
    hint: str | None = TEXT["hint"]
    hint_key: str = "?"
    pause_key: str = "q"
    controls: str = ""
    title: str = ""
    steps: tuple[str, ...] = ()
    target: tuple[str, ...] = ()
    offer: bool = False
    skip_key: str = ""  # shown while the step can be skipped with one key


def bar_rows(width, height):
    """Rows the bar takes at this size: 3, 2, or 0 when the window is too small."""
    if width < 40 or height < 12:
        return 0
    return 3 if width >= 80 and height >= 20 else 2


def cells(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in text)


def _clean(text):
    return "".join(c for c in str(text or "") if c.isprintable())


def _clip(text, room):
    if cells(text) <= room:
        return text
    out, used = "", 0
    for c in text:
        if used + cells(c) > max(0, room - 1):
            break
        out, used = out + c, used + cells(c)
    return out + "…" if room > 0 else ""


def _taught(line):
    """A keys hint cut to the first, taught chord of each move: «Keys · A or B, then C or
    D» becomes «Keys · A, then C». Any other line comes back unchanged."""
    lead, _, tail = LESSON["keys"].partition("{keys}")
    if not (line.startswith(lead) and line.endswith(tail) and LESSON["or"] in line):
        return line
    times = LESSON["times"].partition("{count}")[0]
    moves = []
    for move in line[len(lead):len(line) - len(tail)].split(LESSON["then"]):
        options = move.split(LESSON["or"])
        repeat = options[-1][options[-1].rfind(times):] if times and times in options[-1] else ""
        moves.append(options[0] + (repeat if len(options) > 1 else ""))
    return lead + LESSON["then"].join(moves) + tail


def _fit(line, space):
    """The line as given while it fits; a keys hint too wide for `space` keeps each move's
    taught chord rather than clipping the way back off its end."""
    return line if cells(line) <= space else _taught(line)


def _width(segments):
    return sum(cells(text) for text, _ in segments)


def _paint(segments):
    return "".join(f"\x1b[{style}m{text}\x1b[0m" if style else text for text, style in segments if text)


def _row(left, right_options, room):
    """Join left and the richest right part that fits; the left side is clipped last."""
    right = []
    for option in right_options:
        right = option
        if _width(left) + 2 + _width(option) <= room or not option:
            break
    gap = 2 if right else 0
    space = max(0, room - _width(right) - gap)
    if _width(left) > space:
        clipped, used = [], 0
        for text, style in left:
            take = _clip(text, space - used) if used + cells(text) > space else text
            clipped.append((take, style))
            used += cells(take)
            if take != text:
                break
        left = clipped
    if _width(right) > room:
        right = [(_clip("".join(t for t, _ in right), room), right[-1][1])]
        left = []
    pad = room - _width(left) - _width(right)
    return "\x1b[0m" + _paint(left) + " " * max(0, pad) + _paint(right) + "\x1b[0m"


def _progress(bar):
    steps = [DOTS.get(step, "○") for step in bar.steps]
    if not steps:
        return "", ""
    current = bar.steps.index("current") + 1 if "current" in bar.steps else sum(s != "todo" for s in bar.steps)
    return "".join(steps), f"{current}/{len(steps)}"


def _keys(bar, gap):
    if bar.controls:
        return [(_clean(bar.controls), MUTED)]
    if bar.hint is None:
        return []
    skip = [(_clean(bar.skip_key), BOLD), (" " + TEXT["skip"] + " " * gap, MUTED)] if bar.skip_key else []
    hint = [(_clean(bar.hint_key), BOLD), (" " + _clean(bar.hint) + " " * gap, GOLD if bar.offer else MUTED)]
    return [*(hint if bar.hint else []), *skip, (_clean(bar.pause_key), BOLD), (" " + TEXT["pause"], MUTED)]


def _tag(bar):
    tag = _clean(bar.tag)
    return (tag.ljust(TAG_CELLS - 1) + " ") if tag else "", TAG_STYLES.get(tag, MINT)


def _split(line, space, below):
    """A line too wide for `space`, cut after the last move («…, then …», «…, …») that fits:
    (head, rest), the rest at most `below` cells. Rest is "" when the line fits, or when no
    such cut exists (the row then clips)."""
    if cells(line) <= space:
        return line, ""
    cut = max((m.start() for m in re.finditer(", ", line) if cells(line[:m.start() + 1]) <= space), default=None)
    return (line, "") if cut is None or cells(line[cut + 2:]) > below else (line[:cut + 1], line[cut + 2:])


def _moved(rest, style, chord):
    """What goes to the bottom row's right end: a line's rest, then a chord that did not fit."""
    return [(rest, style)] * bool(rest) + [("  ", "")] * bool(rest and chord) + chord


def render(bar, width, height):
    """ANSI rows for the bar, top to bottom; each fits `width - 1` cells and ends reset.

    Nothing the user needs gives way: the goal keeps the first row (a checklist never takes it),
    and a chord or a keys hint too wide for its row moves whole, or from a move on, to the
    bottom row's right end, in place of the lesson's progress."""
    room = max(0, width - 1)
    count = bar_rows(width, height)
    keys = _keys(bar, 5 if count == 3 else 2)
    if count == 0:
        return [_row([(TEXT["too_small"], BOLD)], [[]], room), _row(_keys(bar, 2), [[]], room)]
    tag, tag_style = _tag(bar)
    goal, line = _clean(bar.goal), _clean(bar.line)
    chord = [(TEXT["press"] + "  ", MUTED), (_clean(bar.chord), GOLD)] if bar.chord else []
    dots, counted = _progress(bar)
    line_style = LINE_STYLES.get(bar.kind, MUTED)
    if count == 2:
        event = bool(line) and bar.kind in EVENT_KINDS
        space, below = room - cells(tag), room - _width(keys) - 2
        first, rest = _split(_fit(line, space), space, below) if event else (goal, "")
        moved = bool(chord) and (bool(rest) or cells(tag) + cells(first) + 2 + _width(chord) > room)
        extra = _moved(rest, line_style, chord if moved else [])
        top = [] if moved else chord
        if moved and not rest and _width(chord) > below:
            # Too wide for either row: its first moves stay up top, the rest go below.
            head, tail = _split(chord[1][0], room - cells(tag) - cells(first) - 2 - cells(chord[0][0]), below)
            if tail:
                top, extra = [chord[0], (head, GOLD)], [(tail, GOLD)]
            else:  # no cut fits: the chord keeps the top row and the goal clips
                top, extra = chord, []
        counter = [(("  " if extra else "") + counted, MUTED)] if counted else []
        options = [extra + counter, extra] if extra else []
        if bar.kind in ("checklist", "warn") and line:  # beside the key labels while it fits
            options += [[(line, line_style)] + [("  " + counted, MUTED)] * bool(counted), [(line, line_style)]]
        return [_row([(tag, tag_style), (first, line_style if event else BOLD)], [top], room),
                _row(keys, options + [counter, []], room)]
    target = [_clean(item) for item in bar.target[:3]]
    target += [""] * (3 - len(target)) if target else []
    map_cells = max(map(cells, target), default=0)
    beside = map_cells + MAP_GAP + 2 if target else 0  # a row's share for the target map
    space = room - TAG_CELLS - beside
    below = room - _width(keys) - 2 - beside
    head, rest = _split(_fit(line, space), space, below) if bar.kind in EVENT_KINDS else (_fit(line, space), "")
    moved = bool(chord) and TAG_CELLS + cells(goal) + _width(chord) + (beside or 2) > room
    extra = _moved(rest, line_style, chord if moved else [])

    def mapped(index, parts):
        if not target:
            return parts
        piece = (target[index].ljust(map_cells), MUTED)
        return [part + [(" " * MAP_GAP, ""), piece] if part else [piece] for part in parts]

    title = [(_clean(bar.title), MUTED)] if bar.title else []
    counted = [("  " + counted, MUTED)] if dots else []
    dotted = [(c, DOT_STYLES.get(c, MUTED)) for c in dots]
    richest = title + [("  ", "")] * bool(title and dots) + dotted + counted
    progress = [richest, dotted + counted, counted, []]
    return [
        _row([(tag, tag_style), (goal, BOLD)], mapped(0, [[] if moved else chord]), room),
        _row([(" " * TAG_CELLS, ""), (head, line_style)], mapped(1, [[]]), room),
        _row(keys, mapped(2, [extra + counted, extra] + progress if extra else progress), room),
    ]
