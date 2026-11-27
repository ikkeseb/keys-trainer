"""Strata's shortcuts, practised as key presses.

Strata is a window, so there is no private copy to practise in: the trainer reads the key in
its own terminal and checks it against the user's shortcuts. Nothing in Strata is touched.
"""

from __future__ import annotations

import os
import re
import shutil
import tomllib
from pathlib import Path

import checks
from router import canonical, key_name
from world import World

# Config id: (the description a rigpage keys export gives it, Strata 1.2.2's default keys).
ACTIONS = {
    "parent_folder": ("opp til mappa over", ("alt+up",)),
    "history_back": ("tilbake", ("alt+left",)),
    "history_forward": ("fram igjen", ("alt+right",)),
    "location": ("skriv eller lim inn en sti", ("ctrl+l",)),
    "search": ("søk etter filer overalt (hjemmemappa og diskene)", ("ctrl+k",)),
    "filter": ("filtrer mappa du står i", ("ctrl+f",)),
    "preview": ("forhåndsvis fila", ("space",)),
    "toggle_hidden_files": ("vis eller skjul skjulte filer", ("ctrl+h", "ctrl+.")),
    "copy_path": ("kopier stien til det markerte", ("y",)),
}
# Chords foot, Alacritty, kitty and Ghostty take for their own clipboard: they never arrive
# here, so Enter counts for them.
TERMINAL_KEEPS = frozenset({"ctrl+shift+c", "ctrl+shift+v"})


def enter_stands_in(found) -> bool:
    """Enter counts for an action bound to a chord the terminal may keep."""
    return bool({canonical(chord) for chord in found} & TERMINAL_KEEPS)


_MODIFIERS = {"control": "ctrl", "primary": "ctrl", "alt": "alt", "shift": "shift", "super": "super"}
_NAMES = {"return": "enter", "escape": "esc", "period": ".", "comma": ",", "slash": "/", "backslash": "\\",
          "page_up": "pageup", "page_down": "pagedown"}


def config_path(env) -> Path:
    base = env.get("XDG_CONFIG_HOME")
    if not (base and Path(base).is_absolute()):
        base = Path(env.get("HOME") or Path.home()) / ".config"
    return Path(base) / "strata" / "settings.toml"


def accelerator(text: str) -> str | None:
    """A GTK accelerator, `<Shift><Control>c`, as a chord, `ctrl+shift+c`; None if unreadable."""
    match = re.fullmatch(r"((?:<[A-Za-z]+>)*)([^<>]+|<|>)", text)
    if not match:
        return None
    held = [_MODIFIERS.get(name.lower()) for name in re.findall(r"<([A-Za-z]+)>", match[1])]
    if None in held:
        return None
    key = match[2] if len(match[2]) == 1 else _NAMES.get(match[2].lower(), match[2].lower())
    return "+".join([name for name in ("super", "ctrl", "alt", "shift") if name in held] + [key])


def rows(settings: dict) -> list[dict]:
    """The user's parsed settings.toml over Strata's defaults. An entry replaces the action's keys.
    With type-to-search on (the default) a plain character searches, so it is no shortcut."""
    keymap = settings.get("keymap", {})
    own = keymap.get("default", {}) if isinstance(keymap, dict) else {}
    typing = settings.get("type_to_search", True) is not False
    found = []
    for action, (_, defaults) in ACTIONS.items():
        entry = own.get(action) if isinstance(own, dict) else None
        if isinstance(entry, list):
            chords = [accelerator(item) for item in entry if isinstance(item, str)]
        else:
            chords = defaults
        found += [{"name": chord, "value": action} for chord in chords if chord and not (typing and len(chord) == 1)]
    return found


def scan(env, notes) -> list[dict]:
    if not shutil.which("strata", path=env.get("PATH")):
        return []
    settings = {}
    try:
        settings = tomllib.loads(config_path(env).read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass  # never changed: Strata's defaults
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        notes.append(f"Strata's settings could not be read ({error.__class__.__name__}); using its defaults.")
    return rows(settings)


def chords(data_rows, action: str) -> tuple[str, ...]:
    names = {action, ACTIONS.get(action, ("",))[0]}
    return tuple(dict.fromkeys(row["name"] for row in data_rows if row.get("value") in names))


class KeysSession:
    """The Session protocol over key presses: the world is the list of what was pressed."""

    tool = "strata"

    def __init__(self, data_rows):
        self.rows = tuple(data_rows)
        self._bindings: dict[str, tuple[str, ...]] = {}
        self._pressed: tuple[str, ...] = ()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self, actions, fixture="keys") -> World:
        wanted = [card.partition(":")[2] for card in actions if card.startswith("strata:")]
        self._bindings = {action: chords(self.rows, action) for action in wanted}
        return self.apply_fixture(fixture)

    def apply_fixture(self, name) -> World:
        self._pressed = ()
        return self.world()

    def world(self) -> World:
        return World(strata=self._pressed)

    def chords(self, card: str) -> tuple[str, ...]:
        return self._bindings.get(card.partition(":")[2], ())

    def enter_stands_in(self, card: str) -> bool:
        """Enter counts for this card too: the lesson says so (`lesson.Engine.bar`)."""
        return enter_stands_in(self.chords(card))

    def play(self, card: str, *, index: int = 0, number: int | None = None) -> str:
        chord = self.chords(card)[index]
        self._pressed += (card.partition(":")[2],)
        return chord

    def press(self, chord: str) -> None:
        self._receive(canonical(chord))

    def type_text(self, text: str) -> None:
        for char in text:
            self._receive(char)

    def feed(self, raw: bytes) -> None:
        if name := key_name(raw):
            self._receive(name)

    def _receive(self, name):
        """Record the action the key does, else the key itself."""
        for action, found in self._bindings.items():
            if name in {canonical(chord) for chord in found} or (name == "enter" and enter_stands_in(found)):
                self._pressed += (action,)
                return
        self._pressed += (name,)

    def typing(self) -> bool:
        return False

    def close(self) -> None:
        self._pressed = ()


@checks.check("pressed", tool="strata", action=checks._choice(*ACTIONS))
def pressed(context, action):
    """The latest key was one of the user's for this Strata action."""
    return bool(context.now.strata) and context.now.strata[-1] == action
