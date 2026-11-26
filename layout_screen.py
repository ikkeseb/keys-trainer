"""Cell-accurate blueprints and hit testing on the same terminal grid."""

from dataclasses import dataclass
from itertools import groupby
import shutil
import textwrap

from screen import BOLD, GOLD, INK, MINT, MUTED, styled
from strings import LAYOUTS as TEXT


FOCUS = "1;38;5;159;48;5;24"
SHADE = "38;5;117;48;5;24"
MATCH = "38;5;151"
EDGE = "38;5;73"
DIM = "38;5;245"
# Each mask describes the lines connecting a cell to its left, right, up and down.
_JOINT = " ╴╶─╵┘└┴╷┐┌┬│┤├┼"


@dataclass(frozen=True)
class Hit:
    left: int
    top: int
    right: int
    bottom: int
    item: object

    def contains(self, x, y):
        return self.left <= x < self.right and self.top <= y < self.bottom


class Canvas:
    def __init__(self, width, height):
        self.width, self.height = width, height
        self.cells = [[(" ", "") for _ in range(width)] for _ in range(height)]
        self.hits = []

    def write(self, x, y, value, style=INK):
        if 0 <= y < self.height:
            for offset, char in enumerate(value):
                if 0 <= x + offset < self.width and char.isprintable():
                    self.cells[y][x + offset] = (char, style)

    def ansi_rows(self):
        rows = []
        for row in self.cells:
            parts = []
            for style, run in groupby(row, lambda cell: cell[1]):
                text = "".join(char for char, _ in run)
                parts.append(styled(text, style) if style else text)
            rows.append("".join(parts).rstrip())
        return rows

    def board(self, x, y, width, height, shapes, *, focus=None, matched=(), target=False, cue=None):
        """Paint every shared edge once, including tees and four-way joins."""
        if width < 3 or height < 3:
            return
        edges = [[0] * width for _ in range(height)]
        for number, (key, rect) in enumerate(shapes, 1):
            rx, ry, rw, rh = rect
            l = max(0, min(width - 2, round(rx * (width - 1))))
            r = max(l + 1, min(width - 1, round((rx + rw) * (width - 1))))
            t = max(0, min(height - 2, round(ry * (height - 1))))
            b = max(t + 1, min(height - 1, round((ry + rh) * (height - 1))))
            for col in range(l, r):
                for row in (t, b):
                    edges[row][col] |= 2
                    edges[row][col + 1] |= 1
            for row in range(t, b):
                for col in (l, r):
                    edges[row][col] |= 8
                    edges[row + 1][col] |= 4
            if target:
                continue
            self.hits.append(Hit(x+l+1, y+t+1, x+r, y+b, key))
            inner = r - l - 1
            if focus == key:
                for row in range(t+1, b):
                    self.write(x+l+1, y+row, " " * inner, SHADE)
            if cue and cue[0] == key:
                if cue[1] == "right":
                    for row in range(t+1, b):
                        self.write(x+(l+r)//2, y+row, "┆", GOLD)
                else:
                    self.write(x+l+1, y+(t+b)//2, "┄" * inner, GOLD)
            label = ("●" if key == focus else "") + str(number) + ("✓" if key in matched else "")
            if b - t > 1 and len(label) <= inner:
                color = FOCUS if key == focus else MATCH if key in matched else MUTED
                self.write(x+l+1+(inner-len(label))//2, y+(t+b)//2, label, color)
        for row, values in enumerate(edges):
            for col, mask in enumerate(values):
                if mask:
                    self.write(x+col, y+row, _JOINT[mask], EDGE if target else MINT)


def layout_shapes(layout):
    # Herdr reserves a physical cell between adjacent panes. Share its midpoint
    # so a small preview draws a single border on either side of a rounding tie.
    def align(edges, cells):
        groups = []
        for edge in sorted(set(edges)):
            if groups and (edge-groups[-1][0])*cells <= 1.001:
                groups[-1].append(edge)
            else:
                groups.append([edge])
        return {edge: (group[0]+group[-1])/2 for group in groups for edge in group}

    xs = align((v for p in layout.panes for v in (p.x, p.x+p.width)), layout.width)
    ys = align((v for p in layout.panes for v in (p.y, p.y+p.height)), layout.height)
    return [(p.id, (xs[p.x], ys[p.y], xs[p.x+p.width]-xs[p.x], ys[p.y+p.height]-ys[p.y]))
            for p in layout.panes]


def goal_shapes(rects):
    return tuple(enumerate(rects))


def _surface():
    tw, th = shutil.get_terminal_size()
    width = max(1, min(tw - 2, 94))
    height = max(1, min(th, 29))
    return Canvas(width, height), (tw-width)//2, (th-height)//2, (tw, th)


def _present(ui, canvas, left, top, size):
    ui.paint({i+top+1: line for i, line in enumerate(canvas.ansi_rows())}, left, size)
    return [Hit(h.left+left+1, h.top+top+1, h.right+left+1, h.bottom+top+1, h.item)
            for h in canvas.hits]


def _heading(canvas, title, right):
    brand = TEXT["brand"] if canvas.width > 44 else TEXT["brand_short"]
    canvas.write(0, 0, brand, MUTED)
    canvas.write(max(0, canvas.width-len(right)), 0, right, MINT)
    canvas.write(0, 1, "─" * canvas.width, DIM)
    canvas.write(0, 3, title, BOLD)


def gallery(ui, blueprints, selected, completed):
    canvas, left, top, size = _surface()
    w, h = canvas.width, canvas.height
    bp = blueprints[selected]
    if h < 10 or w < 28:
        canvas.write(0, 0, TEXT["too_small"], GOLD)
        canvas.write(0, h-1, TEXT["back"], MUTED)
        return _present(ui, canvas, left, top, size)
    built = sum(bp.id in completed for bp in blueprints)
    _heading(canvas, TEXT["choose"], TEXT["built"].format(built=built, total=len(blueprints)))
    if w >= 55 and h >= 18:
        split, y = min(32, w//2), 5
        for i, item in enumerate(blueprints):
            row = 5 + i * (2 if h >= 21 else 1)
            marker = "› " if i == selected else "  "
            badge = " ✓" if item.id in completed else ""
            label = f"{marker}{i+1}  {item.title}"[:split-1-len(badge)] + badge
            canvas.write(0, row, label, FOCUS if i == selected else MUTED)
            canvas.hits.append(Hit(0,row,split,row+1,i))
    else:
        split, y = 0, 4
        # A carousel leaves room for the actual blueprint even at 40×12.
        canvas.write(0, 2, " " * w)
        canvas.write(0, 3, " " * w)
        canvas.write(0, 2, "‹", MINT)
        canvas.write(2, 2, bp.title[:max(0,w-4)], BOLD)
        canvas.write(w-1, 2, "›", MINT)
        canvas.hits.extend([Hit(0,2,2,3,(selected-1)%len(blueprints)),
                            Hit(w-2,2,w,3,(selected+1)%len(blueprints)),
                            Hit(2,2,w-2,3,selected)])
    height = min(12, h-y-3)
    canvas.board(split, y, w-split, height, goal_shapes(bp.rects), target=True)
    caption = TEXT["panes"].format(count=len(bp.rects))
    if completed.get(bp.id) is not None:
        caption += TEXT["best"].format(seconds=completed[bp.id])
    canvas.write(split, y+height, caption, MUTED)
    footer = TEXT["footer"] if w >= 42 else TEXT["footer_short"]
    canvas.write(0, h-1, footer, MUTED)
    return _present(ui, canvas, left, top, size)


def puzzle(ui, bp, layout, result, message, controls, elapsed, *, solved=False, hint=False, guide=None):
    canvas, left, top, size = _surface()
    w, h = canvas.width, canvas.height
    if h < 10 or w < 28:
        canvas.write(0, 0, TEXT["too_small"], GOLD)
        canvas.write(0, h-1, TEXT["back_layouts"].format(back=controls[3]), MUTED)
        return _present(ui, canvas, left, top, size)
    small = h < 17
    if small:
        canvas.write(0, 0, bp.title, BOLD)
    else:
        _heading(canvas, bp.title, TEXT["seconds"].format(seconds=elapsed))
    state = TEXT["matched"].format(matched=len(result.matched_ids), total=len(bp.rects))
    if not small and len(bp.title) + len(state) + 2 <= w:
        canvas.write(w-len(state), 3, state, MATCH if solved else MUTED)
    y = 2 if small else 6 if h >= 20 else 5
    bw = (w-3)//2
    right = w-bw
    bh = min(12, max(3, h-y-(4 if small else 5)))
    canvas.write(0, y-1, TEXT["target"], EDGE)
    canvas.write(right, y-1, TEXT["yours"], MINT)
    canvas.board(0, y, bw, bh, goal_shapes(bp.rects), target=True)
    cue = (result.pane_id, result.direction) if hint and result.recoverable else None
    canvas.board(right, y, bw, bh, layout_shapes(layout), focus=layout.focused_id, matched=result.matched_ids, cue=cue)
    message_y = y+bh+(0 if small else 1)
    hint_key, undo_key, focus_key, back_key, reset_key, guide_key, terminal_key = controls
    space = max(1, h-1-message_y-(1 if guide else 0))
    for i, text in enumerate(textwrap.wrap(message, max(1,w))[:space]):
        canvas.write(0, message_y+i, text, MATCH if solved else GOLD if hint or not result.recoverable else MUTED)
    if guide:
        button = TEXT["play"].format(key=guide_key, guide=guide)
        row = h-2
        canvas.write(0, row, button, FOCUS)
        canvas.hits.append(Hit(0,row,min(w,len(button)),row+1,"guide"))

    keys = {"terminal": terminal_key, "hint": hint_key, "undo": undo_key, "focus": focus_key,
            "reset": reset_key, "back": back_key}
    footer = "footer_solved" if solved else "footer_wide" if w >= 65 else "footer_mid" if w >= 48 else "footer_narrow"
    canvas.write(0, h-1, TEXT[footer].format(**keys), MUTED)
    return _present(ui, canvas, left, top, size)
