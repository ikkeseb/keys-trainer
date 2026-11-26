"""Practise Yazi in a private process, observed through its local state events.

`YaziSession` (the lesson engine's Session for Yazi) runs a private Yazi as the
command of the one pane of a private herdr session, so the user sees Yazi's own
screen through the native relay.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import deque
from collections.abc import Iterable, Mapping
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path

import landlock
from herdr_backend import HerdrBackend, HerdrUnavailable, demo_keys, kitty_keys
from strings import YAZI as TEXT
from world import World, YaziWorld


# The Yazi action table. Each action: (the description a rigpage keys export gives it, the
# literal command it shows for a rebound key, the command the private keymap runs).
# Rows resolve by description or literal command; an action the export lacks has no
# chord and its cards are inactive. Each command was measured in Yazi 26.9.1.
KEYMAP = {
    "enter_folder": ("inn i mappa", "enter", "plugin trainer-enter"),
    "leave_folder": ("opp til mappa over", "leave", "plugin trainer-leave"),
    "next_file": ("neste fil", "arrow next", "arrow next"),
    "prev_file": ("forrige fil", "arrow prev", "arrow prev"),
    "top": ("til toppen", "arrow top", "arrow top"),
    "bottom": ("til bunnen", "arrow bot", "arrow bot"),
    "toggle_hidden": ("vis eller skjul skjulte filer", "hidden toggle", "hidden toggle"),
    "filter": ("filtrer lista mens du skriver", "filter --smart", "filter --smart"),
    "mark_one": ("marker eller avmarker fila, og gå til neste", "toggle; arrow 1", ("toggle", "arrow 1")),
    "visual": ("marker et område: flytt, så y, x eller d", "visual_mode", "visual_mode"),
    "escape": ("avslutt markeringsmodus, fjern markeringen eller avbryt søket", "escape", "escape"),
    "yank": ("kopier det markerte", "yank", "yank"),
    "cut": ("klipp ut det markerte", "yank --cut", "yank --cut"),
    "paste": ("lim inn her", "paste", "paste"),
    "copy_path": ("kopier stien til fila", "copy path", "plugin trainer-copy path"),
    "copy_filename": ("kopier filnavnet", "copy filename", "plugin trainer-copy filename"),
    "copy_dirpath": ("kopier stien til mappa", "copy dirpath", "plugin trainer-copy dirpath"),
    "select_all": ("marker alle", "toggle_all --state=on", "toggle_all --state=on"),
    "create": ("ny fil (avslutt navnet med / for en mappe)", "create", "create"),
    "rename": ("gi nytt navn (med flere markert: alle samtidig i editoren)", "rename --cursor=before_ext",
               "rename --cursor=before_ext"),
    "trash": ("flytt til papirkurven", "remove", "remove"),
    "back": ("tilbake til forrige mappe", "back", "plugin trainer-history back"),
    "forward": ("fram igjen", "forward", "plugin trainer-history forward"),
    "new_tab": ("ny fane i samme mappe", "tab_create --current", "tab_create --current"),
    "search_name": ("søk etter filnavn i alle undermapper (fd)", "search --via=fd", "plugin trainer-search fd"),
    "search_content": ("søk i innholdet i filene (ripgrep)", "search --via=rg", "plugin trainer-search rg"),
    "jump_zoxide": ("hopp til en mappe du har vært i (zoxide)", "plugin zoxide", "plugin trainer-jump zoxide"),
    "jump_fzf": ("finn og hopp til en fil eller mappe herfra og ned (fzf)", "plugin fzf", "plugin trainer-jump fzf"),
    "open": ("åpne", "open", "plugin trainer-open"),
    "open_with": ("åpne med et program du velger", "open --interactive", "plugin trainer-open interactive"),
}
# Bound in every session so the user can always move about, whatever the lesson teaches.
BASE_ACTIONS = ("enter_folder", "leave_folder", "next_file", "prev_file", "escape")
ACTION_TOOLS = {"search_name": "fd", "search_content": "rg", "jump_fzf": "fzf", "jump_zoxide": "zoxide"}

STATE_EVENT = b"keys-trainer-state"
STARTUP_TIMEOUT = 5
CHORD_GAP = 0.3   # playback spaces chords (herdr drops a key ~1 ms after another)
STEP_GAP = 0.1    # between the keys of a sequence such as "g, g"

# Publish the active tab's state to the private --local-events stream whenever it
# changes. No filesystem writes, remote subscription or key matching happens here.
# M9: `mode.is_visual` raises a deprecation toast, so visual is `not is_normal`.
OBSERVE = """
local previous = ""
trainer_history = ""
trainer_copied = ""
trainer_search_via = ""
trainer_jump_via = ""
pending_jump_via = ""
pending_jump_cwd = ""
pending_jump_hovered = ""
trainer_open_interactive = false
ps.sub("keys-trainer-copy", function(body)
    trainer_copied = body.what .. ":" .. body.text
    ui.render()
end)
Status:children_add(function(self)
    local tab = cx.active
    local current = tab.current
    local cwd_str = tostring(current.cwd)
    local hovered_name = current.hovered and current.hovered.name or ""
    if pending_jump_via ~= "" then
        if cwd_str ~= pending_jump_cwd or hovered_name ~= pending_jump_hovered then
            trainer_jump_via = pending_jump_via
            pending_jump_via = ""
            os.remove(@TYPING_FLAG@)
            os.remove(@JUMP_RC@)
        else
            local rf = io.open(@JUMP_RC@, "r")
            if rf then
                local rc = (rf:read("*l") or ""):match("^%s*(.-)%s*$")
                rf:close()
                if rc ~= "0" then
                    pending_jump_via = ""
                    trainer_jump_via = ""
                    os.remove(@JUMP_RC@)
                end
            end
        end
    end
    local entries, selected = {}, {}
    for i = 1, #current.files do
        entries[#entries + 1] = current.files[i].name
    end
    for _, file in pairs(tab.selected) do
        selected[#selected + 1] = file.name
    end
    table.sort(selected)
    local filter = current.files.filter
    local is_search = current.cwd.is_search
    local state = {
        cwd = cwd_str,
        hovered = hovered_name,
        entries = entries,
        selected = selected,
        show_hidden = tab.pref.show_hidden,
        copied = trainer_copied or "",
        filter = filter and tostring(filter) or "",
        layer = tostring(cx.layer),
        tab_count = #cx.tabs,
        tab_index = cx.tabs.idx - 1,
        yanked = #cx.yanked,
        cut = cx.yanked.is_cut,
        visual = not tab.mode.is_normal,
        history = trainer_history,
        search_via = is_search and trainer_search_via or "",
        search_query = is_search and tostring(current.cwd.domain or "") or "",
        jump_via = trainer_jump_via,
        open_interactive = trainer_open_interactive,
    }
    local signature = table.concat({
        state.cwd, state.hovered, table.concat(entries, "|"), table.concat(selected, "|"),
        tostring(state.show_hidden), state.filter, state.layer, state.tab_count, state.tab_index,
        state.yanked, tostring(state.cut), tostring(state.visual), state.copied, state.history,
        state.search_via, state.search_query, state.jump_via, tostring(state.open_interactive),
    }, "\\n")
    if signature ~= previous then
        previous = signature
        ps.pub("keys-trainer-state", state)
    end
    return ""
end, 5000, Status.LEFT)
"""

# Directory moves are limited to the practice tree: in only from Practice onto its known
# subfolders, out only from a subfolder back to Practice. Pasting cannot open a third level.
_ENTER_PLUGIN = """--- @sync entry
local ALLOWED = { Archive = true, Docs = true, Projects = true }
return { entry = function()
  local current = cx.active.current
  if tostring(current.cwd) == @PRACTICE@ and current.hovered and ALLOWED[current.hovered.name] then
    pending_jump_via = ""
    trainer_jump_via = ""
    ya.emit("enter", {})
  end
end }
"""
_LEAVE_PLUGIN = """--- @sync entry
return { entry = function()
  local cwd = tostring(cx.active.current.cwd)
  if cwd:sub(1, #@PRACTICE@ + 1) == @PRACTICE@ .. "/" then
    pending_jump_via = ""
    trainer_jump_via = ""
    ya.emit("leave", {})
  end
end }
"""
# History jumps are logged as they are asked for («back:Projects», the folder they left), so
# checks tell them from leave/enter however fast they are typed: no screen in between is needed.
_HISTORY_PLUGIN = """--- @sync entry
return { entry = function(_, job)
  local dir = job.args[1]
  if dir ~= "back" and dir ~= "forward" then return end
  local from = tostring(cx.active.current.cwd):match("[^/]+$") or ""
  pending_jump_via = ""
  trainer_jump_via = ""
  trainer_history = trainer_history .. dir .. ":" .. from .. " "
  ya.emit(dir, {})
  ui.render()
end }
"""
# Search via fd or rg: log which tool was invoked so checks tell name search from content search.
_SEARCH_PLUGIN = """--- @sync entry
return { entry = function(_, job)
  local via = job.args[1]
  if via ~= "fd" and via ~= "rg" then return end
  trainer_search_via = via
  ya.emit("search", { via = via })
  ui.render()
end }
"""
# Jump via zoxide or fzf: stage the pending jump and touch typing.flag immediately; promote to
# trainer_jump_via only once cwd or hovered actually changes after the picker closes.
_JUMP_PLUGIN = """--- @sync entry
return { entry = function(_, job)
  local via = job.args[1]
  if via ~= "zoxide" and via ~= "fzf" then return end
  local current = cx.active.current
  pending_jump_via = via
  pending_jump_cwd = tostring(current.cwd)
  pending_jump_hovered = current.hovered and current.hovered.name or ""
  os.remove(@JUMP_RC@)
  local f = io.open(@TYPING_FLAG@, "w")
  if f then f:close() end
  ya.emit("plugin", { via })
end }
"""
# Open (default or interactive "open with"): persist mode for trainer-open stub before emitting.
_OPEN_PLUGIN = """--- @sync entry
return { entry = function(_, job)
  local inter = job.args[1] == "interactive"
  trainer_open_interactive = inter
  local f = io.open(@OPEN_MODE@, "w")
  if f then
    f:write(inter and "1" or "0")
    f:close()
  end
  ya.emit("open", inter and { interactive = true } or {})
  ui.render()
end }
"""
# «copy» writes the system clipboard. The practice Yazi must never touch the user's, so its copy
# keys run this instead: it works out the same text and only reports it.
_COPY_PLUGIN = """--- @sync entry
return { entry = function(_, job)
  local current = cx.active.current
  local file = current.hovered
  if not file then return end
  local what, url = job.args[1], tostring(file.url)
  local name = file.name
  local text = ({
    path = url,
    filename = name,
    dirpath = tostring(current.cwd),
  })[what]
  ps.pub("keys-trainer-copy", { what = what, text = text })
end }
"""
# Safe [input] editing keys (measured from Yazi 26.9.1's built-in keymap): typing, submit,
# cancel, cursor motion, backspace/delete and undo/redo. Everything that touches the clipboard
# (delete --cut, kill, yank, paste) or opens help is omitted.
_INPUT_KEYMAP = (
    ("<C-c>", "close"),
    ("<Enter>", "close --submit"),
    ("<Esc>", "escape"),
    ("<C-[>", "escape"),
    ("i", "insert"),
    ("I", ["move first-char", "insert"]),
    ("a", "insert --append"),
    ("A", ["move eol", "insert --append"]),
    ("h", "move -1"),
    ("l", "move 1"),
    ("<Left>", "move -1"),
    ("<Right>", "move 1"),
    ("<C-b>", "move -1"),
    ("<C-f>", "move 1"),
    ("b", "backward"),
    ("B", "backward wide"),
    ("w", "forward"),
    ("W", "forward wide"),
    ("e", "forward --end-of-word"),
    ("E", "forward wide --end-of-word"),
    ("<A-b>", "backward lean"),
    ("<A-f>", "forward lean --end-of-word"),
    ("<C-Left>", "backward lean"),
    ("<C-Right>", "forward lean --end-of-word"),
    ("0", "move bol"),
    ("$", "move eol"),
    ("_", "move first-char"),
    ("^", "move first-char"),
    ("<C-a>", "move bol"),
    ("<C-e>", "move eol"),
    ("<Home>", "move bol"),
    ("<End>", "move eol"),
    ("<Backspace>", "backspace"),
    ("<Delete>", "backspace --under"),
    ("<C-h>", "backspace"),
    ("<C-d>", "backspace --under"),
    ("u", "undo"),
    ("<C-r>", "redo"),
)
_PICK_KEYMAP = (
    ("<Esc>", "close"),
    ("<C-c>", "close"),
    ("<Enter>", "close --submit"),
    ("k", "arrow prev"),
    ("j", "arrow next"),
    ("<Up>", "arrow prev"),
    ("<Down>", "arrow next"),
)
_YAZI_CONFIG = (
    '[mgr]\nsort_by = "alphabetical"\nsort_dir_first = true\nshow_hidden = false\nmouse_events = []\n'
    '[plugin]\npreloaders = []\npreviewers = []\nfetchers = []\n'
)
_YAZI_OPEN_CONFIG = (
    '[mgr]\nsort_by = "alphabetical"\nsort_dir_first = true\nshow_hidden = false\nmouse_events = []\n'
    '[plugin]\npreloaders = []\npreviewers = []\n'
    'fetchers = [\n  { url = "local://*", run = "mime.local", prio = "high", group = "mime" },\n]\n'
    '[opener]\n'
    'open = [{ run = "@STUB@ default %s1", desc = "Default app (xdg-open)", for = "unix" }]\n'
    'edit = [{ run = "@STUB@ editor %s1", desc = "Editor (nvim)", for = "unix", block = true }]\n'
    '[open]\n'
    'rules = [\n  { url = "*", use = ["open", "edit"] },\n]\n'
)


def _private_yazi(root: Path, keymap: str, *, enable_open: bool = False) -> tuple[dict[str, Path], Path]:
    """Create a private home, XDG dirs, stub opener, fzf wrapper and Yazi config under `root`."""
    private = {
        "HOME": root / "home",
        "XDG_CONFIG_HOME": root / "config",
        "XDG_DATA_HOME": root / "data",
        "XDG_CACHE_HOME": root / "cache",
        "XDG_STATE_HOME": root / "state",
        "XDG_RUNTIME_DIR": root / "run",
        "TMPDIR": root / "tmp",
        "_ZO_DATA_DIR": root / "zoxide",
    }
    for path in private.values():
        path.mkdir(mode=0o700)
    bin_dir = root / "bin"
    bin_dir.mkdir(mode=0o700)
    opened_log = root / "opened.log"
    open_mode = root / "open_mode"
    typing_flag = root / "typing.flag"
    jump_rc = root / "jump_rc"
    stub = bin_dir / "trainer-open"
    stub.write_text(
        "#!/bin/sh\n"
        "inter=0\n"
        f"if [ -f {_sh(open_mode)} ]; then\n"
        f'  inter="$(cat {_sh(open_mode)} 2>/dev/null || echo 0)"\n'
        "fi\n"
        f'printf "%s\\t%s\\t%s\\n" "$1" "$inter" "$2" >> {_sh(opened_log)}\n'
    )
    stub.chmod(0o700)
    if real_fzf := shutil.which("fzf"):
        fzf_wrap = bin_dir / "fzf"
        fzf_wrap.write_text(
            "#!/bin/sh\n"
            f"FLAG={_sh(typing_flag)}\n"
            f"RCFILE={_sh(jump_rc)}\n"
            'touch "$FLAG"\n'
            "rc=130\n"
            'trap \'rm -f "$FLAG"; printf "%s\\n" "$rc" > "$RCFILE"\' EXIT INT TERM\n'
            f"{_sh(real_fzf)} \"$@\"\n"
            "rc=$?\n"
            "exit $rc\n"
        )
        fzf_wrap.chmod(0o700)
    config = private["XDG_CONFIG_HOME"] / "yazi"
    practice = private["HOME"] / "Practice"
    plugins = {
        "trainer-enter": _ENTER_PLUGIN,
        "trainer-leave": _LEAVE_PLUGIN,
        "trainer-history": _HISTORY_PLUGIN,
        "trainer-copy": _COPY_PLUGIN,
        "trainer-search": _SEARCH_PLUGIN,
        "trainer-jump": _JUMP_PLUGIN,
        "trainer-open": _OPEN_PLUGIN,
    }
    def _subst(text: str) -> str:
        return (
            text.replace("@PRACTICE@", json.dumps(str(practice)))
            .replace("@PROJECTS@", json.dumps(str(practice / "Projects")))
            .replace("@TYPING_FLAG@", json.dumps(str(typing_flag)))
            .replace("@OPEN_MODE@", json.dumps(str(open_mode)))
            .replace("@JUMP_RC@", json.dumps(str(jump_rc)))
        )
    for name, source in plugins.items():
        plugin = config / "plugins" / f"{name}.yazi"
        plugin.mkdir(parents=True)
        (plugin / "main.lua").write_text(_subst(source))
    (config / "keymap.toml").write_text(keymap)
    # A plugin loads on its first key unless required here, and keys typed meanwhile overtake it.
    preload = "".join(f'require("{name}")\n' for name in plugins)
    (config / "init.lua").write_text(_subst(OBSERVE) + preload)
    yazi_cfg = _YAZI_OPEN_CONFIG.replace("@STUB@", str(stub)) if enable_open else _YAZI_CONFIG
    (config / "yazi.toml").write_text(yazi_cfg)
    return private, config


class YaziUnavailable(RuntimeError):
    """Yazi or its private practice process is unavailable."""


def _key_step(step):
    if len(step) == 1:
        return (step, step.encode("ascii")) if step.isascii() and step.isprintable() else None
    *modifiers, key = step.lower().split("+")
    mod_names = {"ctrl": "C", "alt": "A", "shift": "S"}
    special = {
        "enter": "Enter", "space": "Space", "tab": "Tab", "esc": "Esc", "backspace": "Backspace",
        "up": "Up", "down": "Down", "left": "Left", "right": "Right", "home": "Home", "end": "End",
        "insert": "Insert", "delete": "Delete", "pageup": "PageUp", "pagedown": "PageDown",
    }
    special.update({f"f{n}": f"F{n}" for n in range(1, 13)})
    if set(modifiers) - mod_names.keys() or len(set(modifiers)) != len(modifiers):
        return None
    if key in special:
        name = special[key]
    elif modifiers and len(key) == 1 and key.isascii() and key.isprintable():
        name = key
    else:
        return None
    raw = demo_keys(step, "")
    if raw is None:
        return None
    yazi = "<" + "-".join([*(mod_names[mod] for mod in modifiers), name]) + ">"
    return yazi, raw


def _bindings(rows, actions=tuple(KEYMAP), which=shutil.which):
    """Yield (action, rigpage chord, Yazi key specs, legacy PTY bytes) for each usable row."""
    wanted = set(actions)
    exact = {value: action for action, names in KEYMAP.items() if action in wanted for value in names[:2]}
    prefixes = tuple((KEYMAP[action][1] + " ", action) for action in ACTION_TOOLS if action in wanted)
    taken = set()
    for row in rows:
        value = row.get("value")
        action = exact.get(value)
        if action is None and isinstance(value, str):
            action = next((act for prefix, act in prefixes if value.startswith(prefix)), None)
        chord = row.get("name", "")
        if not action or not isinstance(chord, str) or len(chord) > 80:
            continue
        if action in ACTION_TOOLS and not which(ACTION_TOOLS[action]):
            continue
        steps = tuple(_key_step(step) for step in chord.split(", "))
        if not all(steps):
            continue
        keys, encoded = zip(*steps)
        if encoded not in taken:
            taken.add(encoded)
            yield action, chord, keys, b"".join(encoded)


def yazi_bindings(rows, actions=tuple(KEYMAP), which=shutil.which):
    """Map each action of the table to (rigpage chord, legacy PTY bytes) alternatives;
    the curriculum and the router read the chords from here."""
    result = {}
    for action, chord, _, encoded in _bindings(rows, actions, which=which):
        result.setdefault(action, []).append((chord, encoded))
    return result


def _names(value):
    if value == {}:
        return ()
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise ValueError("Expected a list of file names.")
    return tuple(value)


# The lesson tree (Yazi lessons). Paths are relative to the private home.
PRACTICE_TREE = (
    "Practice/.env", "Practice/draft.txt", "Practice/notes.txt", "Practice/report.md",
    "Practice/Projects/planning.txt", "Practice/Projects/timeline.txt",
)
PRACTICE_FOLDERS = ("Practice", "Practice/Projects")
# Richer multi-folder tree with distinct file names and contents for search, jump and open lessons.
SEARCH_TREE = {
    "Practice/Archive/old.txt": "archived quarterly memo\n",
    "Practice/Docs/guide.txt": "onboarding guide with lantern keyword\n",
    "Practice/Projects/deploy.md": "release checklist: run checks before ship\n",
    "Practice/notes.txt": "daily standup notes\n",
}
SEARCH_FOLDERS = ("Practice", "Practice/Archive", "Practice/Docs", "Practice/Projects")
ALL_FOLDERS = frozenset(PRACTICE_FOLDERS) | frozenset(SEARCH_FOLDERS)
# A start state per cursor: «practice_tree» hovers Projects, «practice_tree@NAME» hovers NAME.
# «search_tree» hovers Archive, «search_tree@NAME» hovers NAME.
FIXTURES = (
    "practice_tree", "practice_tree@draft.txt", "practice_tree@notes.txt", "practice_tree@report.md",
    "search_tree", "search_tree@notes.txt",
)


@dataclass(frozen=True)
class YaziState(YaziWorld):
    """YaziWorld plus the practice tree, private trash and stub-opened files on disk.

    `cwd` is relative to the private home ("Practice", "Practice/Projects").
    `tree` holds the file paths under the private home ("Practice/Projects/notes.txt").
    `trashed` holds the file names currently in the private XDG trash.
    `opened` holds (opener, interactive, relative_path) tuples recorded by the private opener stub.
    """

    tree: frozenset[str] = field(default_factory=frozenset)
    trashed: frozenset[str] = field(default_factory=frozenset)
    opened: tuple[tuple[str, bool, str], ...] = ()


def _remove_path(path: Path) -> None:
    """Remove a directory, file or symlink without following symlinks."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _tree(home: Path) -> frozenset[str]:
    practice = home / "Practice"
    return frozenset(
        str(path.relative_to(home)) for path in practice.rglob("*") if path.is_file()
    ) if practice.is_dir() else frozenset()


def _trashed(data: Path | None) -> frozenset[str]:
    files = data / "Trash" / "files" if data is not None else None
    return frozenset(path.name for path in files.iterdir()) if files and files.is_dir() else frozenset()


def _opened(log: Path | None, home: Path | None) -> tuple[tuple[str, bool, str], ...]:
    if log is None or home is None or not log.is_file():
        return ()
    prefix = str(home).rstrip("/") + "/"
    found = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        mode, inter, target = parts
        if not mode or not target:
            continue
        if target.startswith(prefix):
            target = target[len(prefix):]
        found.append((mode, inter == "1", target))
    return tuple(found)


def _card_action(card: str) -> str:
    tool, _, action = card.partition(":")
    if tool != "yazi" or action not in KEYMAP:
        raise ValueError(f"Unknown Yazi card {card!r}.")
    return action


def chord_keys(chord: str) -> tuple[bytes, ...] | None:
    """Encode a rigpage chord ("j", "G", "g, g", "space", "esc") as key presses for the
    private herdr client, which speaks the kitty keyboard protocol (M5): one bytes per key."""
    keys = []
    for step in chord.split(", "):
        if len(step) == 1 and step.isascii() and step.isprintable():
            keys.append(step.encode())  # text keys, upper case included, arrive as text
        elif (raw := kitty_keys(step)) is not None and _key_step(step) is not None:
            keys.append(raw)
        else:
            return None
    return tuple(keys)


def session_bindings(
    rows: Iterable[Mapping], actions: Iterable[str], which=shutil.which,
) -> dict[str, list[tuple[str, tuple[str, ...]]]]:
    """Map each action to the user's (rigpage chord, Yazi key specs) pairs, first row first."""
    result = {}
    for action, chord, keys, _ in _bindings(rows, frozenset(actions), which=which):
        if chord_keys(chord) is not None:
            result.setdefault(action, []).append((chord, keys))
    return result


def session_keymap(bindings: Mapping[str, list[tuple[str, tuple[str, ...]]]]) -> str:
    mgr = [
        f'  {{ on = {json.dumps(list(keys))}, run = {json.dumps(KEYMAP[action][2])} }},'
        for action, pairs in bindings.items() for _, keys in pairs
    ]
    inp = [
        f'  {{ on = {json.dumps(on)}, run = {json.dumps(run)} }},'
        for on, run in _INPUT_KEYMAP
    ]
    pick = [
        f'  {{ on = {json.dumps(on)}, run = {json.dumps(run)} }},'
        for on, run in _PICK_KEYMAP
    ]
    return "\n".join([
        "[mgr]", "keymap = [", *mgr, "]", "",
        "[input]", "keymap = [", *inp, "]", "",
        "[pick]", "keymap = [", *pick, "]", "",
    ])


class YaziSession:
    """A private Yazi inside the one pane of a private herdr session (the Session protocol).

    `start` binds the user's chords for the lesson's actions (plus moving about), builds the
    fixture and returns the world. `world()` is `World(herdr=…, yazi=YaziState)`. Every
    fixture restarts Yazi on a fresh practice tree; the tree cannot be left.
    """

    def __init__(self, rows: Iterable[Mapping], herdr_rows: Iterable[Mapping] = ()):
        self.rows = tuple(rows)
        self.herdr = HerdrBackend(herdr_rows)
        self._resources = ExitStack()
        self._root = self._home = self._data = self._config = self._env = None
        self._zoxide = self._opened_log = self._open_mode = self._typing_flag = self._jump_rc = None
        self._bindings = {}
        self._binary = None
        self._events = None
        self._buffer = bytearray()
        self._state = None
        self._queue: deque[YaziState] = deque()  # one world per hook event not yet handed out
        self._pid = None
        self._generation = 0
        self._last_key = float("-inf")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self, actions: Iterable[str], fixture: str = "practice_tree") -> World:
        wanted = {_card_action(card) for card in actions}
        if fixture not in FIXTURES:
            raise ValueError(f"Unknown Yazi fixture: {fixture}")
        self.close()
        self._binary = shutil.which("yazi")
        if self._binary is None:
            raise YaziUnavailable(TEXT["missing"])
        try:
            landlock.abi_version()  # fail closed before starting if kernel lacks Landlock
            self.herdr.start_session((), "one_pane")
            self._root = Path(self._resources.enter_context(
                tempfile.TemporaryDirectory(prefix="keys-yazi-", dir="/tmp")))
            self._bindings = session_bindings(self.rows, wanted | set(BASE_ACTIONS))
            private, self._config = _private_yazi(
                self._root, session_keymap(self._bindings), enable_open=bool(wanted & {"open", "open_with"}),
            )
            self._home = private["HOME"]
            self._data = private["XDG_DATA_HOME"]
            self._zoxide = private["_ZO_DATA_DIR"]
            self._opened_log = self._root / "opened.log"
            self._open_mode = self._root / "open_mode"
            self._typing_flag = self._root / "typing.flag"
            self._jump_rc = self._root / "jump_rc"
            # No inherited YAZI ids, display sessions, shell settings or credentials.
            self._env = {name: str(path) for name, path in private.items()} | {
                "YAZI_CONFIG_HOME": str(self._config),
                "PATH": f"{self._root / 'bin'}:{os.defpath}",
            }
            return self.apply_fixture(fixture)
        except (OSError, HerdrUnavailable) as exc:
            self.close()
            raise YaziUnavailable(TEXT["no_start"].format(detail=exc)) from exc
        except BaseException:
            self.close()
            raise

    def apply_fixture(self, name: str) -> World:
        """Restart Yazi in the pane on a fresh practice tree, the cursor on the fixture's entry."""
        if name not in FIXTURES:
            raise ValueError(f"Unknown Yazi fixture: {name}")
        if self._home is None:
            raise YaziUnavailable(TEXT["not_started"])
        practice = self._home / "Practice"
        is_search = name.startswith("search_tree")
        default_hover = "Archive" if is_search else "Projects"
        hovered = name.partition("@")[2] or default_hover
        try:
            self._stop_yazi()
            _remove_path(practice)
            if self._data is not None:
                _remove_path(self._data / "Trash")
            if self._opened_log is not None:
                self._opened_log.unlink(missing_ok=True)
            if self._open_mode is not None:
                self._open_mode.unlink(missing_ok=True)
            if self._typing_flag is not None:
                self._typing_flag.unlink(missing_ok=True)
            if self._jump_rc is not None:
                self._jump_rc.unlink(missing_ok=True)
            if is_search:
                for path, content in SEARCH_TREE.items():
                    (self._home / path).parent.mkdir(parents=True, exist_ok=True)
                    (self._home / path).write_text(content)
                self._seed_zoxide()
            else:
                for path in PRACTICE_TREE:
                    (self._home / path).parent.mkdir(parents=True, exist_ok=True)
                    (self._home / path).write_text("Practice file.\n")
            self._generation += 1
            events = self._root / f"events-{self._generation}"
            pid = self._root / f"pid-{self._generation}"
            error = self._root / f"error-{self._generation}"
            events.touch(mode=0o600)
            entry = practice if hovered == default_hover else practice / hovered
            script = self._root / "yazi.sh"
            launcher = Path(__file__).with_name("landlock.py")
            exports = " ".join(f"{key}={_sh(value)}" for key, value in sorted(self._env.items()))
            script.write_text(
                "#!/bin/sh\n"
                f"echo $$ > {_sh(pid)}\n"
                f'exec /usr/bin/env -i TERM="${{TERM:-xterm-256color}}" {exports} '
                f"{_sh(sys.executable)} -IS {_sh(launcher)} --root {_sh(self._root)} "
                f"--error-file {_sh(error)} -- "
                f"{_sh(self._binary)} --local-events=keys-trainer-state {_sh(entry)} >> {_sh(events)}\n"
            )
            if self._events is not None:
                self._events.close()
            self._events = open(events, "rb")
            self._buffer.clear()
            self._queue.clear()
            self._state = None
            tab = self.herdr.world().tab
            result = self.herdr._call(
                "layout.apply", tab_id=tab.id, tab_label=TEXT["tab_label"], focus=True,
                root={"type": "pane", "command": ["/bin/sh", str(script)], "cwd": str(practice)},
            )
            pane = result["layout"]["root"]["pane_id"]
            self._wait_for(lambda state: (state.cwd, state.hovered) == ("Practice", hovered), pid, error)
            # M8: layout.apply moves the server's focus, not the client's view. Focus the
            # pane and wait for its painted frame (plus 50 ms quiet) before any key.
            self.herdr._focus_and_paint(pane)
            self._queue.clear()  # startup events are the present, not the user's moves
        except (OSError, KeyError, TypeError, HerdrUnavailable) as exc:
            raise YaziUnavailable(TEXT["no_start"].format(detail=exc)) from exc
        return self.world()

    def _seed_zoxide(self) -> None:
        if self._zoxide is None or self._home is None:
            return
        _remove_path(self._zoxide)
        self._zoxide.mkdir(mode=0o700, parents=True, exist_ok=True)
        binary = shutil.which("zoxide")
        if not binary:
            return
        env = {"HOME": str(self._home), "_ZO_DATA_DIR": str(self._zoxide), "PATH": os.defpath}
        for folder in SEARCH_FOLDERS:
            if folder != "Practice":
                subprocess.run([binary, "add", str(self._home / folder)], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)

    def _wait_for(self, ready, pid_file, error_file=None):
        end = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < end:
            self.herdr.world(max_age=60)  # drains the client so herdr keeps painting
            if error_file is not None and error_file.exists() and (detail := error_file.read_text().strip()):
                raise YaziUnavailable(TEXT["no_start"].format(detail=detail))
            if self._pid is None and pid_file.exists() and (text := pid_file.read_text().strip()):
                self._pid = int(text)
            if (state := self._pump()) is not None and ready(state) and state.entries:
                return state
            time.sleep(0.02)
        raise YaziUnavailable(TEXT["no_report"])

    def _pump(self) -> YaziState | None:
        if self._events is None:
            return None
        chunk = self._events.read()
        if chunk:
            self._buffer.extend(chunk)
            while (newline := self._buffer.find(b"\n")) >= 0:
                line = bytes(self._buffer[:newline])
                del self._buffer[:newline + 1]
                if (body := _session_event(line)) is not None:
                    self._state = self._as_state(body)
                    self._queue.append(self._state)
        if self._state is not None:
            tree = _tree(self._home)
            trashed = _trashed(self._data)
            opened = _opened(self._opened_log, self._home)
            if tree != self._state.tree or trashed != self._state.trashed or opened != self._state.opened:
                self._state = replace(self._state, tree=tree, trashed=trashed, opened=opened)
        return self._state

    def _as_state(self, body: Mapping) -> YaziState:
        home = str(self._home)
        cwd = str(body.get("cwd", ""))
        search_query = str(body.get("search_query") or "")
        if cwd.startswith("search://"):
            prefix = f"search://{search_query}/"
            cwd = cwd[len(prefix):] if cwd.startswith(prefix) else ""
        relative = cwd[len(home) + 1:] if cwd.startswith(home + "/") else ""
        if relative not in ALL_FOLDERS:
            raise YaziUnavailable(TEXT["left_tree"])
        copied = str(body.get("copied", "")).replace(home + "/", "")  # paths as the practice home sees them
        world = YaziWorld.from_event({**body, "cwd": relative, "copied": copied, "entries": _names(body.get("entries", [])),
                                      "selected": _names(body.get("selected", []))})
        return YaziState(**{name: getattr(world, name) for name in world.__dataclass_fields__},
                         tree=_tree(self._home), trashed=_trashed(self._data),
                         opened=_opened(self._opened_log, self._home))

    def world(self) -> World:
        if self._home is None:
            raise YaziUnavailable(TEXT["not_started"])
        herdr = self.herdr.world(max_age=0.25)
        latest = self._pump()
        # A round trip faster than a poll leaves several events: one world per call, oldest first.
        return World(herdr=herdr, yazi=self._queue.popleft() if self._queue else latest)

    def typing(self) -> bool:
        """True while a Yazi prompt, app picker, or fzf/zoxide sub-picker takes the user's text."""
        state = self._pump()
        return (state is not None and state.layer != "mgr") or (
            self._typing_flag is not None and self._typing_flag.exists()
        )

    def chords(self, card: str) -> tuple[str, ...]:
        return tuple(chord for chord, _ in self._bindings.get(_card_action(card), ()))

    def play(self, card: str, *, index: int = 0, number: int | None = None) -> str:
        """Press the user's chord for the card. `number` is the Session protocol's indexed-chord
        argument (`session.perform` always passes it); no Yazi card takes one."""
        if number is not None:
            raise ValueError(f"{card} does not take a number.")
        chords = self.chords(card)
        if not chords:
            raise ValueError(f"No chord for {card} is bound in this private session.")
        chord = chords[index]
        self.press(chord)
        return chord

    def press(self, chord: str) -> None:
        """Type one chord in rigpage spelling ("j", "G", "g, g", "enter", "esc")."""
        keys = chord_keys(chord)
        if keys is None:
            raise ValueError(f"Cannot type the chord {chord!r} into private Yazi.")
        self._pace()
        for number, raw in enumerate(keys):
            if number:
                self._sleep(STEP_GAP)
            self.herdr.feed(raw)
        self._last_key = time.monotonic()

    def type_text(self, text: str) -> None:
        if not text.isprintable():
            raise ValueError("Only printable text can be typed into private Yazi.")
        self._pace()
        for number, char in enumerate(text):
            if number:
                self._sleep(STEP_GAP)
            self.herdr.feed(char.encode())
        self._last_key = time.monotonic()

    def feed(self, raw: bytes) -> None:
        self.herdr.feed(raw)

    def _pace(self):
        self._sleep(self._last_key + CHORD_GAP - time.monotonic())

    def _sleep(self, seconds):
        # Keep draining herdr's frames while waiting, so its client never blocks on output.
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            self.herdr.world(max_age=60)
            time.sleep(min(left, 0.02))

    def _stop_yazi(self):
        pid, self._pid = self._pid, None
        if pid is None:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with suppress(ProcessLookupError):
                if _is_yazi(pid):
                    os.kill(pid, sig)
            end = time.monotonic() + 1
            while time.monotonic() < end and _is_yazi(pid):
                time.sleep(0.02)
            if not _is_yazi(pid):
                return

    def close(self) -> None:
        try:
            with ExitStack() as cleanup:
                cleanup.callback(self._resources.close)
                cleanup.callback(self._stop_yazi)  # herdr's close normally ends it first
                cleanup.callback(self.herdr.close)
                if self._events is not None:
                    cleanup.callback(self._events.close)
        finally:
            self._resources = ExitStack()
            self._root = self._home = self._data = self._config = self._env = self._events = None
            self._zoxide = self._opened_log = self._open_mode = self._typing_flag = self._jump_rc = None
            self._state = None
            self._queue.clear()
            self._bindings = {}
            self._buffer.clear()
            self._last_key = float("-inf")


def _sh(value) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


def _is_yazi(pid: int) -> bool:
    """Whether `pid` is still a Yazi process (not reaped, not reused by another program)."""
    try:
        status = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    name, state = status[status.index("(") + 1:status.rindex(")")], status[status.rindex(")") + 2]
    return name == "yazi" and state not in "ZX"


def _session_event(line: bytes) -> dict | None:
    fields = line.split(b",", 3)
    if len(fields) != 4 or fields[0] != STATE_EVENT or fields[1] != fields[2]:
        return None
    try:
        body = json.loads(fields[3])
    except ValueError:
        return None
    return body if isinstance(body, dict) else None
