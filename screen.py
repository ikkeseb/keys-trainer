"""A small terminal surface: cards (Home, intro, lesson complete, session end) painted
row by row, and `Terminal`, which owns the raw alternate screen. Each refresh paints only
rows that changed."""

import os
import select
import shutil
import sys
import termios
import textwrap
import tty
import unicodedata
from dataclasses import dataclass

INK = "38;5;253"
MUTED = "38;5;249"
MINT = "38;5;116"
GOLD = "38;5;222"
BOLD = "1;" + INK


def styled(text, style):
    return f"\x1b[{style}m{text}\x1b[0m"


CARD_WIDTH = 70
FOOTER_GAP = "      "


@dataclass(frozen=True)
class Text:
    """A wrapped paragraph; `label` sits in a left column `indent` cells wide."""

    text: str
    style: str = INK
    label: str = ""
    indent: int = 0
    label_style: str = MUTED


@dataclass(frozen=True)
class Card:
    """A centred card (design § 5): header and its right-hand note over a rule, then lines.

    Each line is "" (a blank row), a `Text`, or a tuple of (text, style) segments clipped to
    the card width. `footer` is the key row: a string, or a tuple of items («Enter  Start»).
    `scroll` is a (start, stop) slice of `lines` that may scroll and `focus` the index of
    the line in it that must stay on screen (Home's path and its selection): when the card
    is too tall, the slice scrolls so `focus` stays in view (see `_scrolled`).
    """

    header: str
    right: str = ""
    lines: tuple = ()
    footer: str = ""
    header_style: str = MUTED
    right_style: str = MUTED
    scroll: tuple = ()
    focus: int | None = None


def cells(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in text)


def _printable(text):
    return "".join(c for c in str(text) if c.isprintable())


def _segments(parts, width):
    """Styled segments clipped to `width` cells."""
    out, used = [], 0
    for text, style in parts:
        text = _printable(text)
        room = width - used
        if room <= 0:
            break
        if cells(text) > room:
            kept = ""
            for c in text:
                if cells(kept + c) > room:
                    break
                kept += c
            text = kept
        if text:
            out.append(styled(text, style) if style else text)
            used += cells(text)
    return "".join(out)


def _wrap(item, width):
    room = max(8, width - item.indent)
    parts = textwrap.wrap(_printable(item.text), room) or [""]
    rows = []
    for index, part in enumerate(parts):
        lead = (item.label.ljust(item.indent) if index == 0 else " " * item.indent) if item.indent else ""
        rows.append(_segments([(lead, item.label_style), (part, item.style)], width))
    return rows


def render_card(card, w, h):
    """The card's rows for a w × h screen: ({row number: styled text}, left margin)."""
    width = max(10, min(CARD_WIDTH, w - 2))
    margin = max(0, (w - width) // 2)
    right = _printable(card.right)
    gap = max(1, width - cells(_printable(card.header)) - cells(right))
    head = [_segments([(card.header, card.header_style), (" " * gap, ""), (right, card.right_style)], width),
            styled("─" * width, MUTED)]
    body, owner = [], []  # each body row and the index of the line it came from
    for index, line in enumerate(card.lines):
        if isinstance(line, Text):
            rows = _wrap(line, width)
        else:
            rows = [_segments(line, width) if line else ""]
        body += rows
        owner += [index] * len(rows)
    # The key row may run past the card, as the design's does, while the screen has room;
    # it breaks between its items, never inside one.
    foot_width = max(width, w - margin - 1)
    items = (card.footer,) if isinstance(card.footer, str) else card.footer
    packed = []
    for item in (_printable(item) for item in items if item):
        if packed and cells(packed[-1]) + len(FOOTER_GAP) + cells(item) <= foot_width:
            packed[-1] += FOOTER_GAP + item
        else:
            packed.append(item)
    foot = [styled(part, MUTED) for line in packed for part in textwrap.wrap(line, foot_width)]
    gaps = 3 if foot else 1  # a blank after the rule, two before the footer
    if len(head) + len(body) + len(foot) + gaps > h:
        kept = [i for i, row in enumerate(body) if row]  # tight: drop the blank rows first
        body, owner = [body[i] for i in kept], [owner[i] for i in kept]
        gaps = 1 if foot else 0
    room = max(0, h - len(head) - len(foot) - gaps)
    body = _scrolled(body, owner, room, card.scroll, card.focus)
    rows = head + ([""] if gaps else []) + body + ([""] * (gaps - 1) if foot else []) + foot
    top = max(1, (h - len(rows)) // 2 + 1)
    return {top + i: row for i, row in enumerate(rows) if row and top + i <= h}, margin


def _scrolled(body, owner, room, scroll, focus):
    """At most `room` rows of the body, the `focus` line always among them.

    Too tall, whole lines below the `scroll` slice go first, from the bottom; then the slice
    shrinks to a window that holds the focus (scrolled no further than it needs); then whole
    lines above it go, from the top. Without a focus the body is simply cut at the bottom."""
    if len(body) <= room or focus not in owner:
        return body[:room]
    first, stop = scroll if scroll and scroll[0] <= focus < scroll[1] else (focus, focus + 1)
    rows = list(zip(owner, body))
    while len(rows) > room and rows[-1][0] >= stop:
        line = rows[-1][0]
        rows = [row for row in rows if row[0] != line]
    region = [i for i, (line, _) in enumerate(rows) if first <= line < stop]
    at = next(i for i, (line, _) in enumerate(rows) if line == focus)
    keep = max(1, len(region) - (len(rows) - room))
    start = min(max(region[0], at - keep + 1), region[-1] + 1 - keep)
    rows = rows[:region[0]] + rows[start:start + keep] + rows[region[-1] + 1:]
    while len(rows) > room and rows[0][0] < first:
        line = rows[0][0]
        rows = [row for row in rows if row[0] != line]
    return [row for _, row in rows][:room]


def chord_label(chord):
    def key_name(part):
        return part if len(part) == 1 else part.capitalize()

    return " → ".join(" + ".join(key_name(part) for part in step.split("+"))
                      for step in chord.replace(", ", " → ").split(" → "))


class Terminal:
    def __enter__(self):
        self.fd = sys.stdin.fileno()
        self.old = termios.tcgetattr(self.fd)
        tty.setraw(self.fd, termios.TCSANOW)
        self.cache = {}
        self.size = None
        sys.stdout.write("\x1b[?1049h\x1b[?25l")
        sys.stdout.flush()
        return self

    def __exit__(self, *_):
        termios.tcsetattr(self.fd, termios.TCSANOW, self.old)
        sys.stdout.write("\x1b[0m\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()

    def read(self, delay, *, forward_ctrl_c=False):
        if select.select([self.fd], [], [], delay)[0]:
            data = os.read(self.fd, 4096)
            if not data:
                raise EOFError("Terminal input closed.")
            if not forward_ctrl_c and data == b"\x03":
                raise KeyboardInterrupt
            return data
        return b""

    def card(self, card):
        """Paint a `Card`, centred; only rows that changed are written."""
        w, h = shutil.get_terminal_size()
        rows, margin = render_card(card, w, h)
        self.paint(rows, margin, (w, h))

    def paint(self, lines, margin, size):
        """Paint pre-styled rows and clear rows left behind by a previous view."""
        if self.size != size or getattr(self, "_margin", margin) != margin:
            self.cache = {}
            sys.stdout.write("\x1b[2J")
        self.size, self._margin = size, margin
        for n in sorted(self.cache.keys() | lines.keys()):
            value = lines.get(n, "")
            if self.cache.get(n, "") != value:
                sys.stdout.write(f"\x1b[{n};1H\x1b[2K\x1b[{n};{margin + 1}H{value}")
        self.cache = lines
        sys.stdout.flush()
