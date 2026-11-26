"""Find the user's key bindings from the tools themselves, for a user without `rigpage`.

`scan()` returns a payload in the shape of `rigpage keys`: groups `herdr`, `yazi` and `desktop`,
each a list of rows `{"name": chord, "value": action}`, plus `source` ("scan") and `notes`
(plain sentences on what could not be read). The herdr and yazi groups also carry the tool's
own `defaults`, so `bind_advice()` can suggest the line that adds a missing key. Everything here only reads: config files, the
output of `herdr --default-config`, the keymap built into the yazi binary, and two queries on
Hyprland's control socket (`j/binds` and `binds`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import hypr

# ── herdr ──
# The user's config [keys] table wins; herdr's defaults (`herdr --default-config`, its [keys] table
# commented out one `# action = "chord"` per line) fill the actions the config does not set. An
# action set to "" is unbound and stays unbound; a default whose chord the user already uses for
# something else is dropped, as herdr's own key on it would never fire.

HERDR_PICKER = "workspace picker"


def herdr_config_path(env) -> Path:
    if env.get("HERDR_CONFIG_PATH"):
        return Path(env["HERDR_CONFIG_PATH"])
    return _config_home(env) / "herdr" / "config.toml"


# herdr's current action names whose legacy alias the curriculum uses.
HERDR_ALIASES = {"fullscreen": "zoom"}


def _herdr_action(name: str) -> str:
    return HERDR_ALIASES.get(name, name).replace("_", " ")


def parse_herdr_config(text: str) -> tuple[list[dict], set[str]]:
    """The user's herdr config.toml -> (rows, configured actions). Rows: one per chord of each [keys]
    action (value: the action with `_` as space) and one per [[keys.command]] (value: its command).
    Configured: every action [keys] sets, an unbound ("") one included. Raises ValueError (tomllib's
    TOMLDecodeError) for text that is not TOML."""
    keys = tomllib.loads(text).get("keys", {})
    rows, configured = [], set()
    if not isinstance(keys, dict):
        return rows, configured
    for name, setting in keys.items():
        if name == "command":
            continue
        if isinstance(setting, str):
            chords = [setting]
        elif isinstance(setting, list) and all(isinstance(chord, str) for chord in setting):
            chords = setting
        else:
            continue
        action = _herdr_action(name)
        configured.add(action)
        rows += [{"name": chord, "value": action} for chord in chords if chord]
    commands = keys.get("command", [])
    for command in commands if isinstance(commands, list) else []:
        if (isinstance(command, dict) and isinstance(command.get("key"), str) and command["key"]
                and isinstance(command.get("command"), str)):
            rows.append({"name": command["key"], "value": command["command"], "command": True})
    return rows, configured


def herdr_defaults(text: str) -> dict[str, list[str]]:
    """`herdr --default-config` -> {action: [chord]} for its commented-out [keys] lines; an action
    whose default is "" (unset) is left out."""
    defaults, table = {}, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            table = line.split("#", 1)[0].strip()
            continue
        if line.startswith("# ["):  # a commented-out table, such as [[keys.command]], ends [keys]
            table = None
            continue
        if table != "[keys]":
            continue
        entry = re.fullmatch(r'#\s*([a-z_]+)\s*=\s*"([^"]*)"\s*(?:#.*)?', line)
        if entry and entry.group(2):
            defaults.setdefault(_herdr_action(entry.group(1)), [entry.group(2)])
    return defaults


def herdr_spread(chord: str) -> set[str]:
    """A chord and, for an indexed one (`prefix+1..9`), each chord it stands for."""
    indexed = re.fullmatch(r"(.*?)(\d)\.\.(\d)", chord)
    if not indexed:
        return {chord}
    first, last = int(indexed.group(2)), int(indexed.group(3))
    return {chord} | {f"{indexed.group(1)}{n}" for n in range(first, last + 1)}


def herdr_rows(own: list[dict], configured: set[str], defaults: dict[str, list[str]]) -> list[dict]:
    """The user's rows, then herdr's defaults for actions not set, on chords not already used.
    Navigate-mode keys work inside the workspace picker, so they read `picker chord, key`; herdr's
    own navigate keys are dropped when no picker chord opens navigate mode."""
    keys = [row for row in own if not row.get("command")]
    set_actions = {row["value"] for row in keys} | configured
    used = set().union(*(herdr_spread(row["name"]) for row in own))
    added = [{"name": chord, "value": action} for action, chords in defaults.items() if action not in set_actions
             for chord in chords if not herdr_spread(chord) & used]
    picker = next((row["name"] for row in keys + added if row["value"] == HERDR_PICKER), None)
    out = []
    for row, default in [(row, False) for row in own] + [(row, True) for row in added]:
        name = row["name"]
        if row["value"].startswith("navigate ") and not row.get("command"):
            if picker:
                name = f"{picker}, {name}"
            elif default:
                continue
        out.append({"name": name, "value": row["value"]})
    return out


def scan_herdr(env, notes, defaults_out=None) -> list[dict]:
    found = shutil.which("herdr", path=env.get("PATH"))
    if not found:
        notes.append("herdr was not found on PATH, so there are no herdr keys.")
        return []
    path = herdr_config_path(env)
    own, configured = [], set()
    try:
        own, configured = parse_herdr_config(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        notes.append("herdr config not found; using herdr's defaults.")
    except (OSError, UnicodeError) as error:
        notes.append(f"herdr config could not be read ({error.__class__.__name__}); using herdr's defaults.")
    except tomllib.TOMLDecodeError:
        notes.append("herdr config is not valid TOML; using herdr's defaults.")
    defaults = {}
    try:
        run = subprocess.run([found, "--default-config"], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=10, env=dict(env))
        if run.returncode:
            notes.append(f"herdr --default-config failed (exit {run.returncode}); only your own herdr keys are used.")
        else:
            defaults = herdr_defaults(run.stdout)
            if defaults_out is not None:
                defaults_out.update({action.replace(" ", "_"): chords[0] for action, chords in defaults.items()})
    except (OSError, subprocess.SubprocessError) as error:
        notes.append(f"herdr --default-config failed ({error.__class__.__name__}); only your own herdr keys are used.")
    return herdr_rows(own, configured, defaults)


# ── yazi ──
# yazi builds its keymap.toml into its binary, so the defaults are read from there and follow an
# update. The user's own keymap.toml is read as yazi reads it: a [mgr] keymap replaces the built-in one, a
# prepend_keymap entry goes before it and wins its chord, an append_keymap one goes after it and
# fires only on a chord nothing before it holds. A row's value is the literal command it runs.

YAZI_SCHEMA = b"#:schema https://yazi-rs.github.io/schemas/keymap.json"
YAZI_HERE = {"unix", "linux"}  # yazi's `for`: the platforms an entry is on
YAZI_KEY_NAMES = {"esc": "esc", "enter": "enter", "space": "space", "tab": "tab", "backspace": "backspace",
                  "delete": "delete", "pageup": "pageup", "pagedown": "pagedown"}
YAZI_MODS = (("D", "super"), ("C", "ctrl"), ("A", "alt"), ("S", "shift"))


def yazi_keymap_path(env) -> Path:
    if env.get("YAZI_CONFIG_HOME"):
        return Path(env["YAZI_CONFIG_HOME"]) / "keymap.toml"
    return _config_home(env) / "yazi" / "keymap.toml"


def yazi_step(step: str) -> str:
    """One yazi key, `<C-u>` or `G`, as a chord step: `ctrl+u`, `G`."""
    special = re.fullmatch(r"<((?:[CSADcsad]-)*)(.+)>", step)
    if not special:
        return step
    held = special.group(1).upper().split("-")
    key = special.group(2)
    return "+".join([name for mod, name in YAZI_MODS if mod in held] + [YAZI_KEY_NAMES.get(key.lower(), key.lower())])


def yazi_chord(on) -> str | None:
    """An entry's `on` (one key or a list of steps) -> `g, g`; None for what is not keys, or for
    `<C-[>`, yazi's second spelling of the escape it already lists."""
    steps = [on] if isinstance(on, str) else on
    if not isinstance(steps, list) or not steps or not all(isinstance(step, str) and step for step in steps):
        return None
    if any(step.upper() == "<C-[>" for step in steps):
        return None
    return ", ".join(map(yazi_step, steps))


def yazi_run(run) -> str:
    """An entry's `run`, a list joined by "; "; "" where it is not commands."""
    runs = run if isinstance(run, list) else [run]
    if not runs or not all(isinstance(each, str) and each.strip() for each in runs):
        return ""
    return "; ".join(each.strip() for each in runs)


def yazi_entries(table) -> list[tuple[str, str]]:
    """A keymap list -> [(chord, run)], skipping what is not a key or is for another platform."""
    out = []
    for entry in table if isinstance(table, list) else []:
        if isinstance(entry, dict) and entry.get("for", "unix") in YAZI_HERE:
            chord, run = yazi_chord(entry.get("on")), yazi_run(entry.get("run"))
            if chord and run:
                out.append((chord, run))
    return out


def yazi_builtin(binary: bytes) -> dict:
    """The keymap.toml built into the yazi binary, parsed, or {}. The text starts at its schema line
    and ends at the last line opening with `]` that still leaves valid TOML."""
    start, best = binary.find(YAZI_SCHEMA), {}
    if start < 0:
        return {}
    for end in re.finditer(rb"\n\][^\n\x00]*\n", binary[start:start + 1_000_000]):
        try:
            best = tomllib.loads(binary[start:start + end.end()].decode("utf-8"))
        except (UnicodeError, ValueError):
            if best:
                break
    return best


def _mgr(data: dict) -> dict:
    table = data.get("mgr", data.get("manager", {}))  # yazi before 25.x named it [manager]
    return table if isinstance(table, dict) else {}


def yazi_rows(own: dict, builtin: dict) -> list[dict]:
    """The user's parsed keymap.toml and yazi's built-in one -> rows, each chord once: the entry that fires
    on it. A `noop` holds its chord and does nothing, so it shows nowhere."""
    mine = _mgr(own)
    base = mine["keymap"] if "keymap" in mine else _mgr(builtin).get("keymap")
    entries = yazi_entries(mine.get("prepend_keymap")) + yazi_entries(base) + yazi_entries(mine.get("append_keymap"))
    holder = {}
    for chord, run in entries:
        holder.setdefault(chord, run)
    return [{"name": chord, "value": run} for chord, run in holder.items() if run != "noop"]


def yazi_defaults(builtin: dict) -> dict[str, list[str]]:
    """{run: on} for yazi's built-in [mgr] keymap, each command's first entry, `on` as yazi spells it."""
    out = {}
    for entry in _mgr(builtin).get("keymap") or ():
        if isinstance(entry, dict) and entry.get("for", "unix") in YAZI_HERE and yazi_chord(entry.get("on")):
            run, on = yazi_run(entry.get("run")), entry["on"]
            if run:
                out.setdefault(run, [on] if isinstance(on, str) else list(on))
    return out


def scan_yazi(env, notes, defaults_out=None) -> list[dict]:
    found = shutil.which("yazi", path=env.get("PATH"))
    if not found:
        notes.append("yazi was not found on PATH, so there are no yazi keys.")
        return []
    builtin = {}
    try:
        builtin = yazi_builtin(Path(found).read_bytes())
    except OSError as error:
        notes.append(f"yazi's built-in keymap could not be read ({error.__class__.__name__}).")
    else:
        if not builtin:
            notes.append("yazi's built-in keymap was not found in its binary; only your own yazi keys are used.")
    own = {}
    path = yazi_keymap_path(env)
    try:
        own = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        notes.append("yazi keymap.toml not found; using yazi's defaults.")
    except (OSError, UnicodeError) as error:
        notes.append(f"yazi keymap.toml could not be read ({error.__class__.__name__}); using yazi's defaults.")
    except tomllib.TOMLDecodeError:
        notes.append("yazi keymap.toml is not valid TOML; using yazi's defaults.")
    if defaults_out is not None:
        defaults_out.update(yazi_defaults(builtin))
    return yazi_rows(own, builtin)


# ── desktop (Hyprland) ──
# The live binds, from the control socket. Modifiers come from `modmask`; the key from `key`, or
# `keycode` for a bind by code. Hyprland 0.56 with a Lua config reports a `code:N` bind with an
# empty key and keycode 0 in `j/binds`, while plain `binds` still shows the spec it was bound with
# (`SUPER + code:10`); such a bind takes its key from there.

MODMASK = ((64, "super"), (4, "ctrl"), (8, "alt"), (1, "shift"))
# Key names whose own name reads worse than the key, and Hyprland's mouse names (rigpage's spelling).
KEY_LABELS = {
    "bar": "|", "section": "§", "grave": "`", "comma": ",", "period": ".", "slash": "/", "plus": "+", "minus": "-",
    "equal": "=", "backslash": "\\", "aring": "å", "ae": "æ", "oslash": "ø", "dead_diaeresis": "¨", "diaeresis": "¨",
    "return": "enter", "escape": "esc", "alt_r": "right alt", "mouse_down": "scroll down", "mouse_up": "scroll up",
    "mouse:272": "left click", "mouse:273": "right click",
}
# The number row by keycode (Omarchy binds its workspaces by code, so any layout reaches them).
DIGIT_CODES = {10 + i: str((i + 1) % 10) for i in range(10)}
WORKSPACE = "Switch to workspace {}"


def key_label(key: str) -> str:
    code = re.fullmatch(r"code:(\d+)", key)
    if code:
        key = DIGIT_CODES.get(int(code.group(1)), key)
    if key.lower().startswith("xf86") and len(key) > 4:  # XF86AudioRaiseVolume -> audio raise volume
        key = " ".join(re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", key[4:]))
    return KEY_LABELS.get(key.lower(), key.lower())


def bind_chord(bind: dict) -> str | None:
    """A `j/binds` entry -> `super+shift+1`, or None where its key cannot be named."""
    mask = bind.get("modmask", 0)
    key = bind.get("key") or ""
    if "+" in key and not key.endswith("+"):  # a spec such as `SUPER + code:10`: its last part is the key
        key = key.rsplit("+", 1)[1].strip()
    if not key:
        code = bind.get("keycode") or 0
        key = f"code:{code}" if code else ""
    if not key or not isinstance(mask, int):
        return None
    return "+".join([name for bit, name in MODMASK if mask & bit] + [key_label(key)])


def parse_text_binds(text: str) -> list[dict]:
    """Hyprland's plain `binds` reply -> one dict per bind with its fields as strings."""
    binds = []
    for block in re.split(r"\n\s*\n", text):
        fields = dict(re.findall(r"^\t(\w+): ?(.*)$", block, re.MULTILINE))
        if fields:
            binds.append(fields)
    return binds


def _typed(bind: dict) -> dict:
    """A plain `binds` entry with its number fields as numbers, as `j/binds` gives them."""
    out = dict(bind)
    for name in ("modmask", "keycode"):
        try:
            out[name] = int(out.get(name) or 0)
        except ValueError:
            out[name] = 0
    return out


def _identity(bind: dict) -> tuple[str, ...]:
    return tuple(str(bind.get(name, "")) for name in ("modmask", "submap", "description", "dispatcher", "arg"))


def fill_keys(binds: list[dict], text_binds: list[dict]) -> list[dict]:
    """Give a `j/binds` entry with no key and no keycode the key its plain `binds` twin shows."""
    spare = {}
    for bind in text_binds:
        spare.setdefault(_identity(bind), []).append(bind.get("key", ""))
    out = []
    for bind in binds:
        if not bind.get("key") and not bind.get("keycode") and spare.get(_identity(bind)):
            bind = {**bind, "key": spare[_identity(bind)].pop(0)}
        out.append(bind)
    return out


def desktop_rows(binds: list[dict]) -> list[dict]:
    """Hyprland binds -> rows valued by the bind's description. A `workspace` dispatch to 1..10
    always also reads «Switch to workspace N», so `hypr.workspace_chords` finds it. A bind with no
    description and no workspace reads as its dispatcher and argument; binds inside a submap, which
    fire only in that mode, are left out."""
    rows = []
    for bind in binds:
        if not isinstance(bind, dict) or bind.get("submap"):
            continue
        chord = bind_chord(bind)
        if not chord:
            continue
        description = (bind.get("description") or "").strip()
        arg = str(bind.get("arg", "")).strip()
        values = [description] if description else []
        if bind.get("dispatcher") == "workspace" and re.fullmatch(r"[1-9]|10", arg):
            workspace = WORKSPACE.format(arg)
            if workspace.casefold() != description.casefold():
                values.append(workspace)
        if not values:
            values = [f"{bind.get('dispatcher', '')} {arg}".strip()]
        rows += [{"name": chord, "value": value} for value in values if value]
    return rows


def scan_desktop(env, notes) -> list[dict]:
    try:
        observer = hypr.HyprObserver.from_environment(env)
        control = observer.instance_dir / ".socket.sock"
        try:
            binds = json.loads(hypr._request(control, b"j/binds"))
        except ValueError:  # some Hyprland releases answer j/binds with broken JSON
            binds = [_typed(bind) for bind in
                     parse_text_binds(hypr._request(control, b"binds").decode("utf-8", "replace"))]
        if not isinstance(binds, list):
            raise ValueError("j/binds is not a list")
        if any(isinstance(bind, dict) and not bind.get("key") and not bind.get("keycode") for bind in binds):
            binds = fill_keys(binds, parse_text_binds(hypr._request(control, b"binds").decode("utf-8", "replace")))
    except (RuntimeError, OSError, ValueError):
        notes.append("Hyprland is not running or did not answer, so there are no desktop keys.")
        return []
    return desktop_rows(binds)


# ── the payload ──

def _config_home(env) -> Path:
    if env.get("XDG_CONFIG_HOME") and Path(env["XDG_CONFIG_HOME"]).is_absolute():
        return Path(env["XDG_CONFIG_HOME"])
    return Path(env.get("HOME") or Path.home()) / ".config"


def scan(env=None) -> dict:
    """The user's bindings in the shape of `rigpage keys`, plus `source` and `notes`."""
    env = os.environ if env is None else env
    notes: list[str] = []
    herdr_defaults_, yazi_defaults_ = {}, {}
    groups = [
        {"id": "herdr", "rows": scan_herdr(env, notes, herdr_defaults_), "defaults": herdr_defaults_},
        {"id": "yazi", "rows": scan_yazi(env, notes, yazi_defaults_), "defaults": yazi_defaults_},
        {"id": "desktop", "rows": scan_desktop(env, notes)},
    ]
    return {"groups": groups, "source": "scan", "notes": notes}


# ── how to add a missing key ──

def _home_path(path: Path, env) -> str:
    home = env.get("HOME") or str(Path.home())
    text = str(path)
    return "~" + text[len(home):] if home and (text == home or text.startswith(home.rstrip("/") + "/")) else text


def _defaults(data, tool) -> dict:
    return next((group.get("defaults") or {} for group in data.get("groups", ()) if group.get("id") == tool), {})


def _holder(data, tool, chords: set[str]) -> str:
    """The action (row value) that already holds one of `chords` in the user's keys, or ""."""
    for group in data.get("groups", ()):
        if group.get("id") == tool:
            for row in group.get("rows", ()):
                if herdr_spread(str(row.get("name", ""))) & chords:
                    return str(row.get("value", ""))
    return ""


def bind_advice(data, card: str, env=None) -> dict:
    """How to add a key for `card` («herdr:split_vertical») the user has no binding for:
    {tool, action, path, line, default, taken}. `line` is the line to add (a placeholder key
    when no free default is known), `default` the tool's own chord for it, `taken` the action
    that already holds that default. Desktop cards get no path or line (Hyprland binds vary)."""
    env = os.environ if env is None else env
    tool, _, action = card.partition(":")
    advice = {"tool": tool, "action": action.replace("_", " "), "path": "", "line": "", "default": "", "taken": ""}
    if tool == "herdr":
        default = _defaults(data, "herdr").get(action, "")
        taken = _holder(data, "herdr", herdr_spread(default)) if default else ""
        key = default if default and not taken else "<key>"
        advice.update(path=_home_path(herdr_config_path(env), env), line=f'{action} = "{key}"',
                      default=default, taken=taken)
    elif tool == "yazi":
        from yazi_backend import KEYMAP
        literal = KEYMAP.get(action, ("", action, action))[1]  # the user's command, not the trainer's
        runs = [part.strip() for part in literal.split(";")]
        on = _defaults(data, "yazi").get(yazi_run(runs)) or []
        default = yazi_chord(on) or ""
        taken = _holder(data, "yazi", {default}) if default else ""
        if not default or taken:
            on = ["<key>"]
        spell = lambda items: "[" + ", ".join(json.dumps(item) for item in items) + "]"
        shown_run = json.dumps(runs[0]) if len(runs) == 1 else spell(runs)
        advice.update(path=_home_path(yazi_keymap_path(env), env), line=f"{{ on = {spell(on)}, run = {shown_run} }}",
                      default=default, taken=taken)
    elif tool == "desktop":
        import desktop_session
        advice["action"] = desktop_session.ACTIONS.get(action, advice["action"])
    return advice


if __name__ == "__main__":
    print(json.dumps(scan(), ensure_ascii=False, indent=1))
