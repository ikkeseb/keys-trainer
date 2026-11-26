"""Shell checks and needs for the terminal search, jump and open lessons, tool="shell".

All checks read structured state from `World.shell` (`ShellWorld`) populated by bash prompt/widget
hooks and recording wrappers/stubs, never the rendered screen.
"""

from __future__ import annotations

import re

from checks import _choice, _text, check, need
from world import ShellWorld, World


FOLDERS = {
    "Practice": "Practice",
    "Archive": "Practice/Archive",
    "Docs": "Practice/Docs",
    "Projects": "Practice/Projects",
}
_folder = _choice(*FOLDERS)


def _shell(world: World) -> ShellWorld:
    return world.shell if world.shell is not None else ShellWorld()


@check("shell_cwd_is", tool="shell", folder=_folder)
def shell_cwd_is(context, folder):
    """The private shell's working directory is this folder."""
    return _shell(context.now).cwd == FOLDERS[folder]


@check("widget_used", tool="shell", widget=_choice("history", "file", "cd"), contains=_text(None))
def widget_used(context, widget, contains):
    """Since `ref`, this readline fzf widget ran and selected non-empty text (containing `contains`)."""
    now, ref = _shell(context.now), _shell(context.ref)
    added = now.widgets[len(ref.widgets):]
    return any(
        kind == widget and bool(text.strip()) and (contains is None or contains in text)
        for kind, text in added
    )


@check("command_ran", tool="shell", contains=_text())
def command_ran(context, contains):
    """Since `ref`, a command containing `contains` (as a whole token) completed with exit status 0."""
    now, ref = _shell(context.now), _shell(context.ref)
    return (
        now.seq > ref.seq
        and now.last_status == 0
        and bool(re.search(rf"(?<!\w){re.escape(contains)}(?!\w)", now.last_cmd))
    )


@check("zoxide_jumped", tool="shell", folder=_folder)
def zoxide_jumped(context, folder):
    """Since `ref`, zoxide resolved a jump and the shell landed in `folder`."""
    now, ref = _shell(context.now), _shell(context.ref)
    target = FOLDERS[folder]
    added = now.zoxide[len(ref.zoxide):]
    return now.cwd == target and any(dest == target for _, dest in added)


@check("tool_matched", tool="shell", program=_choice("fd", "rg"), file=_text(), query=_text())
def tool_matched(context, program, file, query):
    """Since `ref`, `program` (fd or rg) ran with `query` and its matches included `file`."""
    now, ref = _shell(context.now), _shell(context.ref)
    added = now.tools[len(ref.tools):]
    needle = query.lower()
    return any(
        name == program
        and any(needle in arg.lower() for arg in args if arg and not arg.startswith("-"))
        and file in matches
        for name, args, matches in added
    )


@check("shell_opened", tool="shell", opener=_choice("default", "editor"), file=_text())
def shell_opened(context, opener, file):
    """Since `ref`, the recording opener stub ran with `opener` on `file`."""
    now, ref = _shell(context.now), _shell(context.ref)
    added = now.opened[len(ref.opened):]
    return (opener, file) in added


@need("shell_cwd_is", tool="shell", folder=_folder)
def need_shell_cwd_is(world, earlier, folder):
    return _shell(world).cwd == FOLDERS[folder]


@need("shell_clean", tool="shell")
def shell_clean(world, earlier):
    """No widgets, zoxide jumps, tool runs or stub opens have been recorded yet in this fixture."""
    shell = _shell(world)
    return not (shell.widgets or shell.zoxide or shell.opened or shell.tools)
