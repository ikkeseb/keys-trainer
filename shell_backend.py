"""Practise bash search, jump (zoxide/fzf) and file opening in a private, jailed shell.

`scan()` inspects the user's real interactive bash bindings (`bind -X`, `bind -s`, `bind -p`)
and available commands (`z`, `open`/`xdg-open`, `nvim`/`n`, `fd`, `rg`) without touching the
user's real history or zoxide database. `ShellSession` runs a private bash inside the one pane
of a private herdr session under Linux Landlock, with a seeded private HISTFILE, private
`_ZO_DATA_DIR`, a practice file tree and recording stubs for file opening.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import deque
from collections.abc import Iterable, Mapping
from contextlib import ExitStack, suppress
from dataclasses import replace
from pathlib import Path

import landlock
from herdr_backend import HerdrBackend, HerdrUnavailable, kitty_keys
from strings import SHELL_BACKEND as TEXT
from world import ShellWorld, World
from yazi_backend import SEARCH_FOLDERS, SEARCH_TREE, _remove_path, _sh


ACTIONS = {
    "history_search": "search command history",
    "file_widget": "insert a file path with fzf",
    "cd_widget": "jump to a subfolder with fzf",
    "zoxide_jump": "jump to a visited folder with zoxide",
    "open_default": "open a file with the default app",
    "open_editor": "open a file in your editor",
    "find_file": "find files by name with fd",
    "search_content": "search inside files with rg",
}
COMMAND_ACTIONS = frozenset({"zoxide_jump", "open_default", "open_editor", "find_file", "search_content"})
WIDGET_ACTIONS = frozenset({"history_search", "file_widget", "cd_widget"})
FIXTURES = ("shell_tree", "shell_tree@Docs")
SEEDED_HISTORY = (
    "git status",
    "echo ship-42",
    "df -h",
    "uname -r",
)
STARTUP_TIMEOUT = 6.0
CHORD_GAP = 0.25
STEP_GAP = 0.05


class ShellUnavailable(RuntimeError):
    """Bash or the private practice shell is unavailable."""


def readline_chord(seq: str) -> str | None:
    """Parse a bash readline key sequence (`\\C-r`, `\\ec`, `\\M-c`, `\\e\\C-r`) into a chord."""
    if not seq:
        return None
    mods: set[str] = set()
    rest = seq
    while rest:
        if rest.startswith(r"\e"):
            mods.add("alt")
            rest = rest[2:]
        elif rest.startswith(r"\M-"):
            mods.add("alt")
            rest = rest[3:]
        elif rest.startswith(r"\C-"):
            mods.add("ctrl")
            rest = rest[3:]
        else:
            break
    if len(rest) != 1 or not rest.isascii() or not rest.isprintable():
        return None
    key = rest.lower()
    if rest.isupper():
        mods.add("shift")
    ordered = [m for m in ("ctrl", "alt", "shift") if m in mods] + [key]
    return "+".join(ordered)


def chord_to_readline(chord: str) -> str | None:
    """Format a chord (`ctrl+r`, `alt+c`, `ctrl+alt+r`) as a bash readline bind sequence."""
    *mods, key = chord.lower().split("+")
    if len(key) != 1 or not key.isascii() or not key.isprintable():
        return None
    if set(mods) - {"ctrl", "alt"}:
        return None
    prefix = (r"\e" if "alt" in mods else "") + (r"\C-" if "ctrl" in mods else "")
    return prefix + key


def parse_scan(text: str) -> list[dict]:
    """Parse the combined `bind -X`, `bind -s`, `bind -p` and command-type output into rows."""
    x_part, _, rest = text.partition("===S===")
    s_part, _, rest = rest.partition("===P===")
    _p_part, _, cmd_part = rest.partition("===CMD===")

    found: dict[str, list[str]] = {}

    def add(action: str, chord: str | None) -> None:
        if chord:
            items = found.setdefault(action, [])
            if chord not in items:
                items.append(chord)

    # 1. Commands/functions/aliases present in the user's bash
    cmds: dict[str, str] = {}
    for line in cmd_part.splitlines():
        name, sep, kind = line.strip().partition("=")
        if sep and name:
            cmds[name] = kind

    # 2. fzf readline bindings (`bind -X` and `bind -s`) — only valid when `fzf` is installed.
    if cmds.get("fzf"):
        for line in x_part.splitlines():
            match = re.match(r'^"((?:\\.|[^"\\])+)"\s*:?\s*"((?:\\.|[^"\\])+)"', line.strip())
            if not match:
                continue
            seq, target = match.group(1), match.group(2)
            if "__fzf_history__" in target:
                add("history_search", readline_chord(seq))
            elif "fzf-file-widget" in target or "__fzf_select__" in target:
                add("file_widget", readline_chord(seq))
            elif "__fzf_cd__" in target:
                add("cd_widget", readline_chord(seq))

        for line in s_part.splitlines():
            match = re.match(r'^"((?:\\.|[^"\\])+)"\s*:\s*"(.*)"$', line.strip())
            if not match:
                continue
            seq, macro = match.group(1), match.group(2)
            if "__fzf_cd__" in macro:
                add("cd_widget", readline_chord(seq))
            elif "__fzf_select__" in macro and "file_widget" not in found:
                add("file_widget", readline_chord(seq))
            elif "__fzf_history__" in macro and "history_search" not in found:
                add("history_search", readline_chord(seq))

    if cmds.get("z") and cmds.get("zoxide"):
        add("zoxide_jump", "z")
    if cmds.get("open"):
        add("open_default", "open")
    if cmds.get("xdg-open"):
        add("open_default", "xdg-open")
    if cmds.get("nvim"):
        add("open_editor", "nvim")
    if cmds.get("n") and cmds.get("nvim"):
        add("open_editor", "n")
    if cmds.get("fd"):
        add("find_file", "fd")
    if cmds.get("rg"):
        add("search_content", "rg")

    return [{"name": chord, "value": action} for action in ACTIONS for chord in found.get(action, ())]


def scan(env: Mapping | None = None, notes: list[str] | None = None) -> list[dict]:
    """Read the user's actual bash readline bindings and search/jump/open commands."""
    env = os.environ if env is None else env
    bash = shutil.which("bash", path=env.get("PATH"))
    if not bash:
        return []
    probe = (
        'bind -X 2>/dev/null; printf "\\n===S===\\n"; '
        'bind -s 2>/dev/null; printf "\\n===P===\\n"; '
        'bind -p 2>/dev/null; printf "\\n===CMD===\\n"; '
        'for c in z zi open xdg-open nvim n fd rg fzf zoxide; do '
        '  printf "%s=%s\\n" "$c" "$(type -t "$c" 2>/dev/null || true)"; '
        'done'
    )
    # Never write to the user's real history, zoxide DB or home while scanning.
    scan_env = {**env, "HISTFILE": "/dev/null", "HISTSIZE": "0", "_ZO_DATA_DIR": "/dev/null"}
    use_landlock = False
    try:
        landlock.abi_version()
        use_landlock = True
    except OSError:
        use_landlock = False
    try:
        with ExitStack() as stack:
            if use_landlock:
                jail_dir = stack.enter_context(tempfile.TemporaryDirectory(prefix="keys-scan-", dir="/tmp"))
                cmd = [
                    sys.executable, "-IS", str(Path(__file__).with_name("landlock.py")),
                    "--root", jail_dir, "--", bash, "-ic", probe,
                ]
            else:
                cmd = [bash, "-ic", probe]
            run = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=5, env=scan_env,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        if notes is not None:
            notes.append(f"bash bindings could not be read ({exc.__class__.__name__}).")
        return []
    out = run.stdout.decode("utf-8", "replace") if isinstance(run.stdout, bytes) else (run.stdout or "")
    return parse_scan(out)


def shell_bindings(rows: Iterable[Mapping]) -> dict[str, tuple[str, ...]]:
    """Map each shell action to its usable chords/commands from `rows`."""
    found: dict[str, list[str]] = {}
    for row in rows:
        action = row.get("value")
        chord = row.get("name", "")
        if action not in ACTIONS or not isinstance(chord, str) or not chord or len(chord) > 40:
            continue
        if action in WIDGET_ACTIONS and kitty_keys(chord) is None:
            continue
        items = found.setdefault(action, [])
        if chord not in items:
            items.append(chord)
    return {action: tuple(chords) for action, chords in found.items()}


def _card_action(card: str) -> str:
    tool, _, action = card.partition(":")
    if tool != "shell" or action not in ACTIONS:
        raise ValueError(f"Unknown shell card {card!r}.")
    return action


def _write_exec(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)


def _private_shell(root: Path, bindings: Mapping[str, tuple[str, ...]]) -> dict[str, Path]:
    """Create private home, XDG dirs, stub binaries, wrappers and bashrc under `root`."""
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
        path.mkdir(mode=0o700, exist_ok=True)
    bin_dir = root / "bin"
    bin_dir.mkdir(mode=0o700, exist_ok=True)

    # Tiny JSON-lines event emitter (safe against arbitrary quotes/newlines in commands/paths).
    _write_exec(
        bin_dir / "_kt-emit",
        f"#!{sys.executable} -IS\n"
        "import json, os, sys\n"
        "log = os.environ.get('KT_EVENTS')\n"
        "if not log:\n"
        "    sys.exit(0)\n"
        "kind = sys.argv[1]\n"
        "data = {'kind': kind}\n"
        "args = sys.argv[2:]\n"
        "i = 0\n"
        "while i < len(args):\n"
        "    if args[i] == '--list':\n"
        "        key = args[i + 1]\n"
        "        i += 2\n"
        "        items = []\n"
        "        while i < len(args) and args[i] != '--end':\n"
        "            items.append(args[i])\n"
        "            i += 1\n"
        "        data[key] = items\n"
        "        if i < len(args) and args[i] == '--end':\n"
        "            i += 1\n"
        "        continue\n"
        "    data[args[i]] = args[i + 1]\n"
        "    i += 2\n"
        "with open(log, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps(data, ensure_ascii=False) + '\\n')\n",
    )

    # Recording stubs for default app and editor: never launch real GUI apps or trap the user in vim.
    for name, opener, label in (
        ("open", "default", "default app"),
        ("xdg-open", "default", "default app"),
        ("nvim", "editor", "editor"),
        ("n", "editor", "editor"),
    ):
        _write_exec(
            bin_dir / name,
            "#!/bin/sh\n"
            'target="$1"\n'
            'if [ -z "$target" ]; then echo "Usage: ' + name + ' <file>" >&2; exit 2; fi\n'
            'if [ ! -e "$target" ]; then echo "' + name + ': $target: No such file" >&2; exit 1; fi\n'
            'abs="$(realpath "$target" 2>/dev/null || printf "%s/%s" "$PWD" "$target")"\n'
            'practice="$(realpath "$HOME/Practice" 2>/dev/null || printf "%s/Practice" "$HOME")"\n'
            'case "$abs" in\n'
            '  "$practice/"*) ;;\n'
            '  *) echo "' + name + ': $target: outside Practice" >&2; exit 1 ;;\n'
            'esac\n'
            f'{_sh(bin_dir / "_kt-emit")} open opener {opener} target "$abs"\n'
            f'printf "Opened %s ({label})\\n" "$target"\n',
        )

    # Recording wrappers for real fd and rg.
    if real_fd := shutil.which("fd"):
        _write_exec(
            bin_dir / "fd",
            "#!/bin/sh\n"
            f'{_sh(real_fd)} "$@"\n'
            "rc=$?\n"
            "if [ $rc -eq 0 ]; then\n"
            f'  matches="$({_sh(real_fd)} --absolute-path --color=never "$@" </dev/null 2>/dev/null)"\n'
            "  IFS='\n'\n"
            "  set -f\n"
            f'  {_sh(bin_dir / "_kt-emit")} tool tool fd --list args "$@" --end --list matches $matches --end\n'
            "fi\n"
            "exit $rc\n",
        )

    if real_rg := shutil.which("rg"):
        _write_exec(
            bin_dir / "rg",
            "#!/bin/sh\n"
            f'{_sh(real_rg)} "$@"\n'
            "rc=$?\n"
            "if [ $rc -eq 0 ]; then\n"
            f'  raw="$({_sh(real_rg)} --files-with-matches --color=never "$@" </dev/null 2>/dev/null)"\n'
            '  abs=""\n'
            '  while IFS= read -r f; do\n'
            '    [ -n "$f" ] && abs="${abs}$(realpath "$f" 2>/dev/null || printf "%s/%s" "$PWD" "$f")\n"\n'
            '  done <<EOF\n'
            '$raw\n'
            'EOF\n'
            "  IFS='\n'\n"
            "  set -f\n"
            f'  {_sh(bin_dir / "_kt-emit")} tool tool rg --list args "$@" --end --list matches $abs --end\n'
            "fi\n"
            "exit $rc\n",
        )

    # Custom readline rebinds if the user rebound fzf's widgets from defaults.
    extra_binds = []
    for action, fn, is_macro in (
        ("history_search", "__fzf_history__", False),
        ("file_widget", "fzf-file-widget", False),
        ("cd_widget", r' \C-b\C-k \C-u`__fzf_cd__`\e\C-e\e(\C-m\C-y\C-h\e \C-y\ey\C-x\C-x\C-d\C-y\ey\C-_', True),
    ):
        for chord in bindings.get(action, ()):
            seq = chord_to_readline(chord)
            if not seq:
                continue
            if is_macro:
                extra_binds.append(f"bind -m emacs-standard '\"{seq}\": \"{fn}\"'")
            else:
                extra_binds.append(f"bind -m emacs-standard -x '\"{seq}\": {fn}'")

    fzf_kb = Path("/usr/share/fzf/key-bindings.bash")
    fzf_block = ""
    if fzf_kb.is_file() and shutil.which("fzf"):
        fzf_block = (
            f"source {_sh(fzf_kb)}\n"
            'eval "$(declare -f __fzf_history__ | sed \'1s/__fzf_history__/__orig_fzf_history__/\')"\n'
            "__fzf_history__() {\n"
            '  local before="$READLINE_LINE"\n'
            '  __orig_fzf_history__ "$@"\n'
            '  if [ -n "$READLINE_LINE" ] && [ "$READLINE_LINE" != "$before" ]; then\n'
            f'    {_sh(bin_dir / "_kt-emit")} widget widget history text "$READLINE_LINE"\n'
            "  fi\n"
            "}\n"
            'eval "$(declare -f fzf-file-widget | sed \'1s/fzf-file-widget/__orig_fzf_file_widget/\')"\n'
            "fzf-file-widget() {\n"
            '  local before="$READLINE_LINE"\n'
            '  __orig_fzf_file_widget "$@"\n'
            '  if [ -n "$READLINE_LINE" ] && [ "$READLINE_LINE" != "$before" ]; then\n'
            f'    {_sh(bin_dir / "_kt-emit")} widget widget file text "$READLINE_LINE"\n'
            "  fi\n"
            "}\n"
            'eval "$(declare -f __fzf_cd__ | sed \'1s/__fzf_cd__/__orig_fzf_cd__/\')"\n'
            "__fzf_cd__() {\n"
            "  local out\n"
            '  out="$(__orig_fzf_cd__ "$@")"\n'
            '  if [ -n "$out" ]; then\n'
            f'    {_sh(bin_dir / "_kt-emit")} widget widget cd text "$out"\n'
            "  fi\n"
            '  printf "%s" "$out"\n'
            "}\n"
            + ("\n".join(extra_binds) + "\n" if extra_binds else "")
        )

    zoxide_block = (
        "if command -v zoxide >/dev/null 2>&1; then\n"
        '  eval "$(zoxide init bash)"\n'
        "  if declare -F __zoxide_z >/dev/null 2>&1; then\n"
        '    eval "$(declare -f __zoxide_z | sed \'1s/__zoxide_z/__orig_zoxide_z/\')"\n'
        "    __zoxide_z() {\n"
        '      __orig_zoxide_z "$@"\n'
        "      local rc=$?\n"
        "      if [ $rc -eq 0 ]; then\n"
        f'        {_sh(bin_dir / "_kt-emit")} zoxide mode z target "$PWD"\n'
        "      fi\n"
        "      return $rc\n"
        "    }\n"
        "  fi\n"
        "fi\n"
    )

    bashrc = root / "bashrc"
    bashrc.write_text(
        "export HISTCONTROL=''\n"
        "export HISTSIZE=100\n"
        "export HISTFILESIZE=100\n"
        "history -c\n"
        "history -r\n"
        r"export PS1='\W \$ '" + "\n"
        + zoxide_block
        + fzf_block
        + "_kt_last_hist=$HISTCMD\n"
        "_kt_prompt() {\n"
        "  local st=$?\n"
        "  local cmd=''\n"
        '  if [ "$HISTCMD" != "$_kt_last_hist" ]; then\n'
        '    cmd="$(fc -ln -1 2>/dev/null | sed \'s/^[[:space:]]*//\')"\n'
        "    _kt_last_hist=$HISTCMD\n"
        "  fi\n"
        f'  {_sh(bin_dir / "_kt-emit")} prompt cwd "$PWD" cmd "$cmd" status "$st"\n'
        "}\n"
        'PROMPT_COMMAND=(_kt_prompt "${PROMPT_COMMAND[@]}")\n',
        encoding="utf-8",
    )
    return private


def _reset_shell_fixture(home: Path, root: Path, zoxide: Path | None = None) -> None:
    """Symlink-safe reset of Practice tree, seeded bash_history and optional zoxide dir."""
    _remove_path(home / "Practice")
    for path, content in SEARCH_TREE.items():
        (home / path).parent.mkdir(parents=True, exist_ok=True)
        (home / path).write_text(content, encoding="utf-8")
    hist = root / "bash_history"
    if hist.exists() or hist.is_symlink():
        hist.unlink()
    fd = os.open(hist, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(SEEDED_HISTORY) + "\n")
    if zoxide is not None:
        _remove_path(zoxide)
        zoxide.mkdir(mode=0o700, parents=True, exist_ok=True)


class ShellSession:
    """A private bash shell inside a private herdr session (the Session protocol)."""

    tool = "shell"

    def __init__(self, rows: Iterable[Mapping], herdr_rows: Iterable[Mapping] = ()):
        self.rows = tuple(rows)
        self.herdr = HerdrBackend(herdr_rows)
        self._resources = ExitStack()
        self._root = self._home = self._zoxide = self._env = None
        self._bindings: dict[str, tuple[str, ...]] = {}
        self._binary = None
        self._events = None
        self._buffer = bytearray()
        self._state: ShellWorld | None = None
        self._queue: deque[ShellWorld] = deque()
        self._pid: int | None = None
        self._generation = 0
        self._last_key = float("-inf")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self, actions: Iterable[str], fixture: str = "shell_tree") -> World:
        wanted = {_card_action(card) for card in actions}
        if fixture not in FIXTURES:
            raise ValueError(f"Unknown shell fixture: {fixture}")
        self.close()
        self._binary = shutil.which("bash")
        if self._binary is None:
            raise ShellUnavailable(TEXT["missing"])
        try:
            landlock.abi_version()  # fail closed before starting if kernel lacks Landlock
            self.herdr.start_session((), "one_pane")
            self._root = Path(self._resources.enter_context(
                tempfile.TemporaryDirectory(prefix="keys-shell-", dir="/tmp")))
            all_bindings = shell_bindings(self.rows)
            self._bindings = {action: all_bindings[action] for action in wanted if action in all_bindings}
            private = _private_shell(self._root, all_bindings)
            self._home = private["HOME"]
            self._zoxide = private["_ZO_DATA_DIR"]
            self._env = {name: str(path) for name, path in private.items()} | {
                "PATH": f"{self._root / 'bin'}:{os.defpath}",
                "HISTFILE": str(self._root / "bash_history"),
                "EDITOR": str(self._root / "bin" / "nvim"),
            }
            return self.apply_fixture(fixture)
        except (OSError, HerdrUnavailable) as exc:
            self.close()
            raise ShellUnavailable(TEXT["no_start"].format(detail=exc)) from exc
        except BaseException:
            self.close()
            raise

    def apply_fixture(self, name: str) -> World:
        """Restart bash in the pane on a fresh practice tree, private history and zoxide DB."""
        if name not in FIXTURES:
            raise ValueError(f"Unknown shell fixture: {name}")
        if self._home is None or self._root is None:
            raise ShellUnavailable(TEXT["not_started"])
        practice = self._home / "Practice"
        sub = name.partition("@")[2]
        start_dir = practice / sub if sub else practice
        expected_cwd = f"Practice/{sub}" if sub else "Practice"
        try:
            self._stop_shell()
            _reset_shell_fixture(self._home, self._root, self._zoxide)
            self._seed_zoxide()
            self._generation += 1
            events = self._root / f"events-{self._generation}.jsonl"
            pid = self._root / f"pid-{self._generation}"
            error = self._root / f"error-{self._generation}"
            events.touch(mode=0o600)
            script = self._root / "shell.sh"
            launcher = Path(__file__).with_name("landlock.py")
            env = {**self._env, "KT_EVENTS": str(events)}
            exports = " ".join(f"{key}={_sh(value)}" for key, value in sorted(env.items()))
            script.write_text(
                "#!/bin/sh\n"
                f"echo $$ > {_sh(pid)}\n"
                f'exec /usr/bin/env -i TERM="${{TERM:-xterm-256color}}" {exports} '
                f"{_sh(sys.executable)} -IS {_sh(launcher)} --root {_sh(self._root)} "
                f"--error-file {_sh(error)} -- "
                f"{_sh(self._binary)} --rcfile {_sh(self._root / 'bashrc')} -i\n",
                encoding="utf-8",
            )
            if self._events is not None:
                self._events.close()
            self._events = open(events, "rb")
            self._buffer.clear()
            self._queue.clear()
            self._state = ShellWorld(cwd=expected_cwd, seq=0)
            tab = self.herdr.world().tab
            result = self.herdr._call(
                "layout.apply", tab_id=tab.id, tab_label=TEXT["tab_label"], focus=True,
                root={"type": "pane", "command": ["/bin/sh", str(script)], "cwd": str(start_dir)},
            )
            pane = result["layout"]["root"]["pane_id"]
            self._wait_for(lambda state: state.cwd == expected_cwd and state.seq >= 1, pid, error)
            self.herdr._focus_and_paint(pane)
            self._queue.clear()
        except (OSError, KeyError, TypeError, HerdrUnavailable) as exc:
            raise ShellUnavailable(TEXT["no_start"].format(detail=exc)) from exc
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
            self.herdr.world(max_age=60)
            if error_file is not None and error_file.exists() and (detail := error_file.read_text().strip()):
                raise ShellUnavailable(TEXT["no_start"].format(detail=detail))
            if self._pid is None and pid_file.exists() and (text := pid_file.read_text().strip()):
                self._pid = int(text)
            if (state := self._pump()) is not None and ready(state):
                return state
            time.sleep(0.02)
        raise ShellUnavailable(TEXT["no_report"])

    def _relative(self, path: str) -> str:
        home = str(self._home).rstrip("/") + "/"
        return path[len(home):] if path.startswith(home) else path

    def _pump(self) -> ShellWorld | None:
        if self._events is None:
            return self._state
        chunk = self._events.read()
        if chunk:
            self._buffer.extend(chunk)
            while (newline := self._buffer.find(b"\n")) >= 0:
                line = bytes(self._buffer[:newline])
                del self._buffer[:newline + 1]
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    self._apply_event(event)
        return self._state

    def _apply_event(self, event: Mapping) -> None:
        state = self._state or ShellWorld()
        kind = event.get("kind")
        if kind == "prompt":
            cwd = self._relative(str(event.get("cwd", "")))
            cmd = str(event.get("cmd", ""))
            try:
                status = int(event.get("status", 0))
            except (TypeError, ValueError):
                status = None
            state = replace(state, cwd=cwd, last_cmd=cmd, last_status=status, seq=state.seq + 1)
        elif kind == "widget":
            widget = str(event.get("widget", ""))
            text = str(event.get("text", ""))
            if widget:
                state = replace(state, widgets=state.widgets + ((widget, text),), seq=state.seq + 1)
        elif kind == "zoxide":
            mode = str(event.get("mode", "query"))
            target = self._relative(str(event.get("target", "")))
            state = replace(state, zoxide=state.zoxide + ((mode, target),), seq=state.seq + 1)
        elif kind == "open":
            opener = str(event.get("opener", ""))
            target = self._relative(str(event.get("target", "")))
            if opener and target:
                state = replace(state, opened=state.opened + ((opener, target),), seq=state.seq + 1)
        elif kind == "tool":
            tool = str(event.get("tool", ""))
            raw_args = event.get("args", ())
            tool_args = tuple(str(item) for item in raw_args) if isinstance(raw_args, list) else ()
            raw = event.get("matches", ())
            matches = tuple(self._relative(str(item)) for item in raw if item) if isinstance(raw, list) else ()
            if tool:
                state = replace(state, tools=state.tools + ((tool, tool_args, matches),), seq=state.seq + 1)
        else:
            return
        self._state = state
        self._queue.append(state)

    def world(self) -> World:
        if self._home is None:
            raise ShellUnavailable(TEXT["not_started"])
        herdr = self.herdr.world(max_age=0.25)
        latest = self._pump()
        return World(herdr=herdr, shell=self._queue.popleft() if self._queue else latest)

    def typing(self) -> bool:
        # Shell sessions use F1/F2/F3 as trainer keys so all typed characters always go to bash.
        return False

    def chords(self, card: str) -> tuple[str, ...]:
        return self._bindings.get(_card_action(card), ())

    def play(self, card: str, *, index: int = 0, number: int | None = None) -> str:
        if number is not None:
            raise ValueError(f"{card} does not take a number.")
        action = _card_action(card)
        chords = self._bindings.get(action, ())
        if not chords:
            raise ValueError(f"No chord for {card} is bound in this private session.")
        chord = chords[index]
        if action in WIDGET_ACTIONS:
            self.press(chord)
        else:
            self.type_text(chord)
        return chord

    def press(self, chord: str) -> None:
        raw = kitty_keys(chord)
        if raw is None:
            raise ValueError(f"Cannot type the chord {chord!r} into private shell.")
        self._pace()
        self.herdr.feed(raw)
        self._last_key = time.monotonic()

    def type_text(self, text: str) -> None:
        if not text.isprintable():
            raise ValueError("Only printable text can be typed into private shell.")
        self._pace()
        for index, char in enumerate(text):
            if index:
                self._sleep(STEP_GAP)
            self.herdr.feed(char.encode())
        self._last_key = time.monotonic()

    def feed(self, raw: bytes) -> None:
        self.herdr.feed(raw)

    def _pace(self) -> None:
        self._sleep(self._last_key + CHORD_GAP - time.monotonic())

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            self.herdr.world(max_age=60)
            time.sleep(min(left, 0.02))

    def _stop_shell(self) -> None:
        pid, self._pid = self._pid, None
        if pid is None:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with suppress(ProcessLookupError):
                if _is_bash(pid):
                    os.kill(pid, sig)
            end = time.monotonic() + 1
            while time.monotonic() < end and _is_bash(pid):
                time.sleep(0.02)
            if not _is_bash(pid):
                return

    def close(self) -> None:
        try:
            with ExitStack() as cleanup:
                cleanup.callback(self._resources.close)
                cleanup.callback(self._stop_shell)
                cleanup.callback(self.herdr.close)
                if self._events is not None:
                    cleanup.callback(self._events.close)
        finally:
            self._resources = ExitStack()
            self._root = self._home = self._zoxide = self._env = self._events = None
            self._state = None
            self._queue.clear()
            self._bindings = {}
            self._buffer.clear()
            self._last_key = float("-inf")


def _is_bash(pid: int) -> bool:
    try:
        status = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    name, state = status[status.index("(") + 1:status.rindex(")")], status[status.rindex(")") + 2]
    return name == "bash" and state not in "ZX"
