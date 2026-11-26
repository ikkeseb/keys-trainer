"""Show the private herdr client's own terminal above the trainer's bar.

`show_bar(bar)` draws the coach bar (`coach.Bar`, three or two rows); `show(title,
help_text)` keeps the older two status lines. The private terminal is sized to the rows
the bar leaves, scrolling is confined to those rows, and the bar is redrawn inside each
relayed synchronized frame, so a frame can never overdraw it.
"""

import shutil
import sys

import coach

FRAME_CLOSE = b"\x1b[?2026l"


def _status_rows(title, help_text):
    def rows(width, height):
        safe = [("".join(c for c in line if c.isprintable())[:max(0, width-1)], style)
                for line, style in ((title, "1;38;5;121"), (help_text, "38;5;248"))]
        if width < 40 or height < 12:
            safe[0] = ("Enlarge to 40 × 12"[:width], "")
        return [f"\x1b[0m\x1b[{style}m{line}\x1b[0m" for line, style in safe]
    return rows


class NativeView:
    def __init__(self, ui, source):
        self.ui = ui
        self.source = source
        self.active = False
        self.size = self.footer = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.active:
            try:
                self.source.end_native()
            finally:
                self.active = False
                self.size = self.footer = None
                self.ui.cache = {}
                self.ui.size = None
                sys.stdout.write("\x1b[0m\x1b[r\x1b[2J\x1b[H\x1b[?25l")
                sys.stdout.flush()

    def show(self, title, help_text):
        """Two plain status lines under the frame (the layout workshop and older drills)."""
        self._present(_status_rows(title, help_text),
                      lambda w, h: 2 if w >= 40 and h >= 12 else 0, ("status", title, help_text))

    def show_bar(self, bar):
        """The coach bar under the frame; `bar` is a `coach.Bar`."""
        self._present(lambda w, h: coach.render(bar, w, h), coach.bar_rows, bar)

    def _present(self, rows_for, count_for, model):
        self.active = True
        width, height = shutil.get_terminal_size()
        output = sys.stdout.buffer
        count = count_for(width, height)
        footer = (model, width, height)
        if count == 0:
            if self.size is not None:
                self.source.end_native()
            if self.footer != footer:
                output.write(b"\x1b[0m\x1b[r\x1b[2J\x1b[H")
                for y, row in enumerate(rows_for(width, height), 1):
                    output.write(f"\x1b[{y};1H{row}".encode())
                output.flush()
            self.size, self.footer = None, footer
            return
        size = (width, height - count)
        if size != self.size:
            self.source.begin_native(*size)
            # Keep any scrolling in the frame's rows, away from the bar.
            output.write(f"\x1b[0m\x1b[r\x1b[2J\x1b[1;{size[1]}r\x1b[H".encode())
            self.size = size
            self.footer = None
        frame = self.source.native_output()
        if frame or footer != self.footer:
            # Herdr uses relative cursor moves between successive diff frames.
            bar = bytearray(b"\x1b7")
            for y, row in enumerate(rows_for(width, height), size[1] + 1):
                bar += f"\x1b[{y};1H\x1b[0m\x1b[2K{row}".encode()
            bar += b"\x1b[0m\x1b8\x1b[?25l"
            if frame.endswith(FRAME_CLOSE):
                # Inside the synchronized update, so the bar never flickers or lags a frame.
                output.write(frame[:-len(FRAME_CLOSE)] + bar + FRAME_CLOSE)
            else:
                output.write(frame + bar)
            self.footer = footer
        output.flush()
