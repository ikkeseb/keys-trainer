"""Build a blueprint by manipulating a real private herdr tab."""

from contextlib import contextmanager
import shutil
import sys
import termios
import tty
import time

from herdr_backend import HerdrBackend, demo_keys
from layouts import BLUEPRINTS, assess
from native_view import NativeView
from router import Click, Input
from layout_screen import gallery, puzzle
from screen import chord_label


@contextmanager
def mouse_input():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd, termios.TCSANOW)
    sys.stdout.write("\x1b[?1000h\x1b[?1006h")
    sys.stdout.flush()
    try:
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, old)
        sys.stdout.write("\x1b[?1006l\x1b[?1000l")
        sys.stdout.flush()


def split_keys(rows):
    prefix = next((r["name"] for r in rows if r["value"] == "prefix"), "")
    labels, raw_keys, reserved = {}, {}, set()
    directions = {"split horizontal": "down", "split vertical": "right"}
    for row in rows:
        action, name = row["value"], row["name"]
        if action not in directions and not action.startswith("focus pane "):
            continue
        if name.startswith("prefix+") and not prefix:
            continue
        encoded = demo_keys(name, prefix)
        if encoded:
            for token in Input().events(encoded):
                if isinstance(token, bytes) and len(token) == 1:
                    reserved.add(token[0])
        # Extended keyboard encodings may differ, so also reserve printable tails.
        for part in (name, prefix):
            tail = part.rsplit("+", 1)[-1]
            if len(tail) == 1 and tail.isascii():
                reserved.update(tail.encode())
        if action in directions:
            display = name.replace("prefix+", prefix + " → ", 1) if name.startswith("prefix+") else name
            if directions[action] not in labels or (encoded and not raw_keys[directions[action]]):
                labels[directions[action]] = chord_label(display)
                raw_keys[directions[action]] = encoded
    controls = []
    for preferred in ("?", "u", "\t", "q", "r", "g", "t"):
        key = next((c for c in preferred + "/!;@~:abcdefgknopwxyz"
                    if c not in controls and ord(c) not in reserved), None)
        if key is None:
            return labels, raw_keys, ()
        controls.append(key)
    return labels, raw_keys, tuple(controls)


def available(rows):
    labels, _, controls = split_keys(rows)
    return [bp for bp in BLUEPRINTS if bp.directions <= labels.keys()] if controls else []


def clicked(event, hits):
    return next((hit.item for hit in hits if hit.contains(event.x, event.y)), None)


def _pick(ui, blueprints, index, records, inputs):
    while True:
        hits = gallery(ui, blueprints, index, records)
        for key in inputs.events(ui.read(.1)):
            if isinstance(key, Click):
                candidate = clicked(key, hits)
                if candidate is not None:
                    if candidate == index:
                        return index
                    index = candidate
            elif key in (b"q", b"Q", b"\x1b", b"\x03"):
                return None
            elif key in (b"\r", b"\n", b" "):
                return index
            elif key in (b"\x1b[A", b"\x1b[D", b"k"):
                index = (index-1) % len(blueprints)
            elif key in (b"\x1b[B", b"\x1b[C", b"j"):
                index = (index+1) % len(blueprints)
            elif len(key) == 1 and b"1" <= key <= b"9" and int(key) <= len(blueprints):
                index = int(key)-1


def _build(ui, source, bp, history, labels, raw_keys, controls, inputs):
    source.start_layout()
    with NativeView(ui, source) as native:
        return _play_layout(ui, source, bp, history, labels, raw_keys, controls, inputs, native)


def _play_layout(ui, source, bp, history, labels, raw_keys, controls, inputs, native):
    terminal = False
    start = time.monotonic()
    elapsed = None
    hinted = show_hint = False
    notice = ""
    shown_controls = tuple("Tab" if c == "\t" else c for c in controls)
    hint, undo, focus, back, reset, guide_key, toggle = (c.encode() for c in controls)
    previous = history.layout_records().get(bp.id)
    layout = None
    while True:
        if elapsed is None:
            snapshot = source.layout()
            if snapshot is not None and all(p.width > 0 and p.height > 0 for p in snapshot.panes):
                layout = snapshot
            if layout is None:
                raise RuntimeError("The private herdr tab did not report its layout.")
            result = assess(bp, layout.panes, layout.focused_id, size=(layout.width, layout.height))
        if result.solved:
            if elapsed is None:
                elapsed = time.monotonic()-start
                history.finish_layout(bp.id, elapsed, hinted)
            message = "Layout matched." + (" Built with a hint." if hinted else " Built without a hint.")
            if previous is not None:
                message += f" Previous best {previous:.1f} s."
        elif not result.recoverable:
            recovery = f"{controls[1]} to undo the last split" if source.undo_id else f"{controls[4]} to restart"
            message = f"This split crosses the target. Press {recovery}."
        elif show_hint:
            number = next(i for i,p in enumerate(layout.panes, 1) if p.id == result.pane_id)
            axis = "columns" if result.direction == "right" else "rows"
            select = f"Focus pane {number}, then split" if result.needs_focus else f"Split pane {number}"
            message = f"{select} into {axis} with {labels[result.direction]}."
        else:
            message = notice or "Use your herdr shortcuts to build the target. ● marks the focused pane."
        guide = (show_hint and result.recoverable and not result.needs_focus and not result.solved
                 and raw_keys.get(result.direction))
        if terminal:
            hits = []
            status = "Layout matched" if result.solved else bp.title
            feedback = "Enter  Next layout" if result.solved else f"{len(result.matched_ids)} / {len(bp.rects)} panes matched"
            if not result.solved and not result.recoverable:
                status = "Wrong split"
                feedback = f"{controls[1]} Undo" if source.undo_id else f"{controls[4]} Restart"
            elif notice and not result.solved:
                status = notice
            native.show(f"{status}   ·   {feedback}",
                        f"{controls[6]}  Target map    {controls[3]}  Layouts")
        else:
            hits = puzzle(ui, bp, layout, result, message, shown_controls,
                          elapsed if elapsed is not None else time.monotonic()-start,
                          solved=result.solved, hint=show_hint,
                          guide=labels[result.direction] if guide else None)
        current_focus = layout.focused_id
        for key in inputs.events(ui.read(.055, forward_ctrl_c=True)):
            if key == toggle:
                terminal = not terminal
                if not terminal:
                    native.close()
                continue
            if result.solved:
                if key in (b"\r", b"\n", b" "):
                    return "next"
                if key == reset:
                    return "restart"
                if key == back:
                    return "gallery"
            elif isinstance(key, Click):
                if terminal:
                    # Pane picking lives on the map, where every hit has a scoped ID.
                    continue
                pane = clicked(key, hits)
                if pane == "guide" and guide:
                    source.feed(guide)
                    guide = None
                elif pane is not None and source.focus_pane(pane):
                    current_focus = pane
                    guide = None
            elif key == back:
                return "gallery"
            elif key == reset:
                return "restart"
            elif key == hint:
                terminal = False
                native.close()
                show_hint = not show_hint
                hinted |= show_hint
            elif key == guide_key and guide:
                source.feed(guide)
                guide = None
            elif key == undo:
                if source.undo_split(source.undo_id):
                    notice = "Last split undone."
                else:
                    notice = "There is no single split to undo."
            elif key == focus:
                ids = [pane.id for pane in layout.panes]
                candidate = ids[(ids.index(current_focus)+1) % len(ids)]
                if source.focus_pane(candidate):
                    current_focus = candidate
                    guide = None
            else:
                notice = ""
                source.feed(key)


def run(ui, rows, history):
    blueprints = available(rows)
    if not blueprints or not shutil.which("herdr"):
        raise ValueError("Layouts needs herdr and at least one configured split shortcut.")
    labels, raw_keys, controls = split_keys(rows)
    # Start with a three-pane example when the keymap supports it.
    index = next((i for i, bp in enumerate(blueprints) if bp.id == "right-stack"), 0)
    inputs = Input()
    with mouse_input(), HerdrBackend(rows) as source:
        while True:
            index = _pick(ui, blueprints, index, history.layout_records(), inputs)
            if index is None:
                return
            while True:
                action = _build(ui, source, blueprints[index], history, labels, raw_keys, controls, inputs)
                source.close()
                if action == "gallery":
                    break
                if action == "next":
                    if index+1 == len(blueprints):
                        break
                    index += 1
