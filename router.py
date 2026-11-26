"""Split the outer terminal's input between the trainer and the private session.

`Input` keeps escape sequences, mouse reports and UTF-8 characters whole across reads.
`key_name` names a token the same way in legacy xterm and kitty (`CSI … u`) encodings, so
the `Router` can recognise Ctrl+C, the herdr prefix and its own two keys in both, while the
bytes it forwards stay exactly as the terminal sent them. `KeyboardMode` asks the outer
terminal for the kitty keyboard protocol's disambiguate flag and always gives it back.
"""

from dataclasses import dataclass
import os
import re
import select
import signal
import sys
import termios
import time
import tty


@dataclass(frozen=True)
class Click:
    x: int
    y: int


class Input:
    """Keep mouse reports, terminal escape sequences and UTF-8 characters whole across reads."""
    def __init__(self):
        self.held = b""
        self.since = 0

    def events(self, data):
        now = time.monotonic()
        raw = self.held + data
        can_wait = not self.held or now-self.since < .10
        self.held = b""
        while raw:
            if raw.startswith(b"\x1b["):
                match = re.match(rb"\x1b\[[0-?]*[ -/]*[@-~]", raw)
                size = len(match[0]) if match else 0
            elif raw.startswith(b"\x1bO"):
                size = 3 if len(raw) >= 3 else 0
            elif raw.startswith(b"\x1b"):
                size = 2 if len(raw) >= 2 else 0
            elif raw[0] >= 0xC0:
                # A typed æ, ø or å arrives as one multi-byte character.
                size = 2 if raw[0] < 0xE0 else 3 if raw[0] < 0xF0 else 4
                size = size if len(raw) >= size else 0
            else:
                size = 1
            if not size and can_wait and len(raw) < 64:
                self.held = raw
                if data:
                    self.since = now
                break
            size = size or 1
            key, raw = raw[:size], raw[size:]
            mouse = re.fullmatch(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])", key)
            if mouse:
                if mouse[1] == b"0" and mouse[4] == b"M":
                    yield Click(int(mouse[2]), int(mouse[3]))
            else:
                yield key


MODIFIERS = ("ctrl", "alt", "shift", "super")
_ALIASES = {"control": "ctrl", "meta": "alt", "option": "alt", "cmd": "super", "win": "super"}
_KEYS = {"return": "enter", "escape": "esc", "minus": "-", "comma": ",", "plus": "+",
         "backtick": "`", " ": "space", "period": ".", "slash": "/"}
_CODES = {9: "tab", 13: "enter", 27: "esc", 32: "space", 127: "backspace"}
_CSI_FINAL = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end",
              "P": "f1", "Q": "f2", "R": "f3", "S": "f4"}
_TILDE = {2: "insert", 3: "delete", 5: "pageup", 6: "pagedown", 15: "f5", 17: "f6", 18: "f7",
          19: "f8", 20: "f9", 21: "f10", 23: "f11", 24: "f12"}


def _join(mods, key):
    if len(key) == 1 and key.isalpha() and key.isupper():
        mods, key = set(mods) | {"shift"}, key.lower()
    return "+".join([m for m in MODIFIERS if m in mods] + [key])


def canonical(chord):
    """Name a configured chord (`ctrl+alt+left`, `prefix+R`) the way `key_name` names input."""
    chord = chord.strip()
    if chord.startswith("prefix+"):
        rest = canonical(chord.removeprefix("prefix+"))
        return "prefix+" + rest if rest else ""
    if not chord:
        return ""
    parts = chord.split("+")
    if chord.endswith("++"):
        parts = parts[:-2] + ["+"]
    *mods, key = parts
    mods = {_ALIASES.get(m.lower(), m.lower()) for m in mods}
    key = key if len(key) == 1 else key.lower()
    return _join(mods, _KEYS.get(key, key))


def _modifiers(value):
    bits = max(0, int(value or 1) - 1) & ~(64 | 128)  # Caps Lock and Num Lock never change a chord.
    return {name for bit, name in ((4, "ctrl"), (2, "alt"), (1, "shift"), (8, "super")) if bits & bit}


def key_name(token):
    """Name one input token (`ctrl+c`, `alt+shift+enter`, `?`), or None when unknown."""
    if not isinstance(token, bytes) or not token:
        return None
    if len(token) == 1 or token[0] >= 0xC0:
        byte = token[0]
        if byte >= 0xC0:
            try:
                return _join((), token.decode())
            except UnicodeDecodeError:
                return None
        if byte in _CODES:
            return _CODES[byte]
        if byte == 0:
            return "ctrl+space"
        if byte < 32:
            return "ctrl+" + (chr(byte + 96) if byte <= 26 else "\\]^_"[byte - 28])
        return _join((), chr(byte)) if byte < 127 else None
    if token.startswith(b"\x1b[") and (match := re.fullmatch(rb"\x1b\[([0-9:;]*)([u~A-DFHPQRSZ])", token)):
        fields = [field.split(b":")[0] for field in match[1].split(b";")]
        final = match[2].decode()
        try:
            numbers = [int(field) if field else 1 for field in fields]
        except ValueError:
            return None
        mods = _modifiers(numbers[1] if len(numbers) > 1 else 1)
        if final == "u":
            code = numbers[0]
            key = _CODES.get(code) or (chr(code) if 32 < code < 0x110000 else None)
            return _join(mods, key) if key else None
        if final == "Z":
            return _join(mods | {"shift"}, "tab")
        if final == "~":
            key = _TILDE.get(numbers[0])
            return _join(mods, key) if key else None
        return _join(mods, _CSI_FINAL[final])
    if token.startswith(b"\x1bO") and len(token) == 3:
        key = _CSI_FINAL.get(chr(token[2]))
        return key
    if token[0] == 0x1b and len(token) == 2:
        inner = key_name(token[1:])
        if inner is None:
            return None
        *mods, key = inner.split("+") if inner != "+" else ["+"]
        return _join(set(mods) | {"alt"}, key)
    return None


HINT, PAUSE, SKIP, QUIT = "hint", "pause", "skip", "quit"
_FALLBACKS = "!;:|~@#%&*_=,." + "abcdefghijklmnopqrstuvwxyz"
PROMPT_ACTIONS = ("rename tab", "rename workspace", "rename pane", "close workspace")


class Router:
    """Decide, per input token, whether the trainer or the private session gets it.

    `route` returns `("trainer", "hint" | "pause" | "quit")` or `("forward", bytes)` (and
    `("click", Click)` for mouse reports). The engine sets `typing` while a Yazi prompt is
    open; the router sets it itself after a chord in `prompt_chords` and clears that on
    Enter or Esc.
    """

    def __init__(self, prefix="", direct=(), prompt_chords=()):
        self.prefix = canonical(prefix) or None
        taken = {canonical(chord) for chord in direct} - {""}
        self.hint_key = next(c for c in "?" + _FALLBACKS if canonical(c) not in taken)
        self.pause_key = next(c for c in "q" + _FALLBACKS
                              if canonical(c) not in taken and c != self.hint_key)
        self.skip_key = next(c for c in "s" + _FALLBACKS
                             if canonical(c) not in taken and c not in (self.hint_key, self.pause_key))
        self.skippable = False  # the step is awkward on the keyboard layout: the skip key is the trainer's
        self.prompt_chords = frozenset(canonical(chord) for chord in prompt_chords) - {""}
        self.typing = False
        self._auto_typing = False
        self._after_prefix = False
        self._opened = False  # the last routed token opened a prompt

    @classmethod
    def from_rows(cls, rows, prompt_actions=PROMPT_ACTIONS):
        """Build from rigpage rows (`{"name": chord, "value": action}`) of the active tools."""
        rows = [row for row in rows if isinstance(row.get("name"), str)]
        prefix = next((row["name"] for row in rows if row.get("value") == "prefix"), "")
        direct = [row["name"] for row in rows
                  if row.get("value") != "prefix" and not row["name"].startswith("prefix+")]
        prompts = [row["name"] for row in rows if row.get("value") in prompt_actions]
        return cls(prefix, direct, prompts)

    @property
    def hint_label(self):
        return self.hint_key

    @property
    def pause_label(self):
        return self.pause_key

    def reset(self):
        """Forget prefix and prompt state, as when a step is set up again."""
        self.typing = self._auto_typing = self._after_prefix = self._opened = False

    def forget_prefix(self):
        """Forget a pending prefix only, as when a new step begins: the engine starts its own
        key count afresh, so a prefix from the step before must not take the next key."""
        self._after_prefix = False

    def dropped(self):
        """The session never got the token just routed (not the user's turn): forget the prefix it
        may have started, and the prompt it may have opened."""
        self._after_prefix = False
        if self._opened:
            self.typing = self._auto_typing = self._opened = False

    @property
    def follows_session(self):
        """True unless a prompt the router opened itself decides `typing` (herdr's prompts)."""
        return not self._auto_typing

    def claims(self, token):
        """True when `token` is a trainer key (hint, pause, or skip while offered) and no
        prefix waits for it: the trainer takes it unless a prompt is open."""
        name = key_name(token)
        keys = (self.hint_key, self.pause_key, *((self.skip_key,) if self.skippable else ()))
        return name is not None and not self._after_prefix and name in map(canonical, keys)

    def route(self, token):
        if isinstance(token, Click):
            return ("click", token)
        self._opened = False
        name = key_name(token)
        if name == "ctrl+c":
            self._after_prefix = False
            return ("trainer", QUIT)
        if self._after_prefix:
            self._after_prefix = False
            self._open_prompt(f"prefix+{name}")
            return ("forward", token)
        if self.typing:
            if self._auto_typing and name in ("enter", "esc"):
                self.typing = self._auto_typing = False
            return ("forward", token)
        if name is not None and name == self.prefix:
            self._after_prefix = True
            return ("forward", token)
        if name == canonical(self.hint_key):
            return ("trainer", HINT)
        if name == canonical(self.pause_key):
            return ("trainer", PAUSE)
        if self.skippable and name == canonical(self.skip_key):
            return ("trainer", SKIP)
        self._open_prompt(name)
        return ("forward", token)

    def _open_prompt(self, chord):
        if chord in self.prompt_chords and not self.typing:
            self.typing = self._auto_typing = self._opened = True


# Chords a legacy (non-kitty) terminal cannot send apart from another chord or at all.
def ambiguous_in_legacy(chord):
    name = canonical(chord)
    if name.startswith("prefix+"):
        return False
    *mods, key = name.split("+") if name != "+" else ["+"]
    mods = set(mods)
    if key == "esc":
        return bool(mods)
    if key in ("enter", "backspace", "tab"):
        return bool(mods - {"alt"}) and name != "shift+tab"
    if key == "space":
        return bool(mods - {"ctrl"})
    if len(key) == 1 and "ctrl" in mods:
        return "shift" in mods or not ("a" <= key <= "z" or key in "@[\\]^_")
    return "super" in mods


QUERY = "\x1b[?u\x1b[c"       # kitty flags query, then primary device attributes as a sentinel
PUSH = "\x1b[>1u"             # disambiguate escape codes
POP = "\x1b[<u"
_REPLY_FLAGS = re.compile(rb"\x1b\[\?(\d+)u")
_REPLY_DA1 = re.compile(rb"\x1b\[\?[0-9;]*c")


def query_keyboard(fd=None, timeout=0.5):
    """Ask the terminal on `fd` for its kitty keyboard flags.

    Returns `(flags, answered, leftover)`: flags is an int when the protocol is supported,
    else None; answered says whether the terminal replied at all; leftover holds keys the user
    typed during the query, for the router.
    """
    fd = sys.stdin.fileno() if fd is None else fd
    if not os.isatty(fd):
        return None, False, b""
    old = termios.tcgetattr(fd)
    data = bytearray()
    try:
        tty.setraw(fd, termios.TCSANOW)
        sys.stdout.write(QUERY)
        sys.stdout.flush()
        deadline = time.monotonic() + timeout
        while not _REPLY_DA1.search(data):
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                break
            chunk = os.read(fd, 1024)
            if not chunk:
                break
            data.extend(chunk)
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, old)
    flags = _REPLY_FLAGS.search(data)
    answered = bool(flags or _REPLY_DA1.search(data))
    leftover = _REPLY_DA1.sub(b"", _REPLY_FLAGS.sub(b"", bytes(data)))
    return (int(flags[1]) if flags else None), answered, leftover


class KeyboardMode:
    """Push kitty disambiguate mode on the outer terminal for the native view's lifetime.

    Enter it after the alternate screen is entered and leave it before that screen is left:
    kitty keeps one mode stack per screen. SIGTERM and SIGHUP raise KeyboardInterrupt while
    it is active (unless a handler is already installed, as the launcher does), so the mode
    is popped on those exits too.
    """

    SIGNALS = (signal.SIGTERM, signal.SIGHUP)

    def __init__(self, fd=None, timeout=0.5):
        self.fd = fd
        self.timeout = timeout
        self.flags = None
        self.answered = False
        self.pending = b""
        self.pushed = False
        self._signals = {}

    @property
    def protocol(self):
        return "kitty" if self.flags is not None else "legacy"

    @property
    def legacy(self):
        return self.flags is None

    def ambiguous(self, chord):
        """True when the engine should teach the prefix twin of this chord instead."""
        return self.legacy and ambiguous_in_legacy(chord)

    def __enter__(self):
        self._catch_signals()
        try:
            self.flags, self.answered, self.pending = query_keyboard(self.fd, self.timeout)
            if self.flags is not None:
                self.pushed = True
                sys.stdout.write(PUSH)
                sys.stdout.flush()
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_):
        try:
            if self.pushed:
                self.pushed = False
                try:
                    sys.stdout.write(POP)
                    sys.stdout.flush()
                except (OSError, ValueError):
                    pass
        finally:
            for sig, previous in self._signals.items():
                signal.signal(sig, previous)
            self._signals.clear()

    def _catch_signals(self):
        def leave(signum, _frame):
            raise KeyboardInterrupt

        for sig in self.SIGNALS:
            try:
                if signal.getsignal(sig) == signal.SIG_DFL:
                    self._signals[sig] = signal.signal(sig, leave)
            except ValueError:  # not the main thread
                break
