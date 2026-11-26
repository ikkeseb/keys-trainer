"""Yazi checks and needs for the Yazi lessons, registered in `checks` with tool="yazi".

They read `World.yazi`: a `YaziWorld` from the private Yazi's hook, and for the file
checks the `tree` on disk that `yazi_backend.YaziState` adds. `cwd` is relative to the
private home ("Practice", "Practice/Projects"); a folder parameter names its last part.
"""

from __future__ import annotations

from checks import _choice, _flag, _int, _Param, _text, check, need
from world import World, YaziWorld
from yazi_backend import PRACTICE_TREE


FOLDERS = {"Practice": "Practice", "Projects": "Practice/Projects"}
_folder = _choice(*FOLDERS)


def _names(default=...):
    return _Param(
        lambda value: isinstance(value, list) and 0 < len(value) <= 20
        and all(isinstance(name, str) and 0 < len(name) <= 40 for name in value),
        default, "list of file names",
    )


def _yazi(world: World) -> YaziWorld:
    return world.yazi if world.yazi is not None else YaziWorld()


def _in(yazi: YaziWorld, folder: str) -> bool:
    return yazi.cwd == FOLDERS[folder]


def _on_disk(yazi: YaziWorld, folder: str, file: str) -> bool | None:
    """Whether the file is on disk, or None when the world carries no tree."""
    tree = getattr(yazi, "tree", None)
    return None if not tree else f"{FOLDERS[folder]}/{file}" in tree


# Checks over the latest world (`context.now`), some relative to `context.ref`.

@check("cwd_is", tool="yazi", folder=_folder)
def cwd_is(context, folder):
    """Yazi shows this folder."""
    return _in(_yazi(context.now), folder)


@check("back_then_forward", tool="yazi", folder=_folder)
def back_then_forward(context, folder):
    """Since the start the user went back from this folder and then forward, and is in it again."""
    now, ref = _yazi(context.now), _yazi(context.ref)
    used = now.history.removeprefix(ref.history).split()
    return (_in(now, folder) and len(used) >= 2 and used[-2] == f"back:{FOLDERS[folder].rsplit('/', 1)[-1]}"
            and used[-1].startswith("forward:"))


@check("hovered_is", tool="yazi", entry=_text())
def hovered_is(context, entry):
    """The cursor is on this entry."""
    return _yazi(context.now).hovered == entry


def _hovered_at(context, index):
    now, ref = _yazi(context.now), _yazi(context.ref)
    return bool(now.entries) and now.cwd == ref.cwd and now.hovered == now.entries[index]


@check("hovered_last", tool="yazi")
def hovered_last(context):
    """The cursor is on the last entry, in the folder it was in."""
    return _hovered_at(context, -1)


@check("hovered_first", tool="yazi")
def hovered_first(context):
    """The cursor is on the first entry, in the folder it was in."""
    return _hovered_at(context, 0)


@check("hidden_shown", tool="yazi", on=_flag(True))
def hidden_shown(context, on):
    """Hidden files are shown (and a dot file is listed), or hidden (and none is)."""
    yazi = _yazi(context.now)
    return yazi.show_hidden == on and any(name.startswith(".") for name in yazi.entries) == on


@check("filter_is", tool="yazi", text=_text())
def filter_is(context, text):
    """The filter is confirmed with this text and only matching entries remain."""
    yazi = _yazi(context.now)
    return (
        yazi.filter == text and yazi.layer == "mgr" and bool(yazi.entries)
        and all(text.lower() in name.lower() for name in yazi.entries)
    )


@check("selection_is", tool="yazi", names=_names())
def selection_is(context, names):
    """Exactly these entries are marked."""
    return _yazi(context.now).selected == frozenset(names)


@check("visual_off", tool="yazi")
def visual_off(context):
    """Yazi is not in visual (range) mode."""
    return not _yazi(context.now).visual


@check("file_in", tool="yazi", file=_text(), folder=_folder)
def file_in(context, file, folder):
    """The file is in the folder on disk, and Yazi lists it there when that folder is open."""
    yazi = _yazi(context.now)
    return bool(_on_disk(yazi, folder, file)) and (not _in(yazi, folder) or file in yazi.entries)


@check("file_gone", tool="yazi", file=_text(), folder=_folder)
def file_gone(context, file, folder):
    """The file is not in the folder on disk, and Yazi does not list it there when that folder is open."""
    yazi = _yazi(context.now)
    return _on_disk(yazi, folder, file) is False and not (_in(yazi, folder) and file in yazi.entries)


@check("file_trashed", tool="yazi", file=_text(), folder=_folder)
def file_trashed(context, file, folder):
    """Exactly this file left the tree (nothing else appeared or changed) and it is in the private trash."""
    now, ref = _yazi(context.now), _yazi(context.ref)
    target = f"{FOLDERS[folder]}/{file}"
    ref_tree, now_tree = getattr(ref, "tree", None), getattr(now, "tree", None)
    if not ref_tree or target not in ref_tree or now_tree != ref_tree - {target}:
        return False
    if _in(now, folder) and file in now.entries:
        return False
    return file in getattr(now, "trashed", frozenset())


# Needs: can a scenario start in this world?

@need("in_folder", tool="yazi", folder=_folder)
def in_folder(world, earlier, folder):
    return _in(_yazi(world), folder)


@need("cursor_on", tool="yazi", entry=_text(), folder=_folder)
def cursor_on(world, earlier, entry, folder):
    yazi = _yazi(world)
    return _in(yazi, folder) and yazi.hovered == entry


@need("cursor_middle", tool="yazi")
def cursor_middle(world, earlier):
    """The cursor is neither on the first nor the last entry."""
    yazi = _yazi(world)
    return yazi.hovered in yazi.entries[1:-1]


@need("nothing_marked", tool="yazi")
def nothing_marked(world, earlier):
    yazi = _yazi(world)
    return not yazi.selected and not yazi.visual


@need("no_filter", tool="yazi")
def no_filter(world, earlier):
    """No filter is set and no prompt is open."""
    yazi = _yazi(world)
    return not yazi.filter and yazi.layer == "mgr"


@need("hidden_off", tool="yazi")
def hidden_off(world, earlier):
    return not _yazi(world).show_hidden


@need("clipboard_empty", tool="yazi")
def clipboard_empty(world, earlier):
    """Nothing is yanked or cut, so a paste would do nothing."""
    return _yazi(world).yanked == 0


@need("fresh_tree", tool="yazi")
def fresh_tree(world, earlier):
    """The practice tree on disk is as the fixture built it."""
    return getattr(_yazi(world), "tree", None) == frozenset(PRACTICE_TREE)


@check("copied", tool="yazi", what=_choice("path", "filename", "dirpath"), text=_text())
def copied(context, what, text):
    """The latest copy was of this kind and gave this text (paths from the practice home)."""
    return _yazi(context.now).copied == f"{what}:{text}"


@check("tabs", tool="yazi", count=_int(1, 9), position=_int(1, 9), folder=_choice(*FOLDERS, default=None))
def tabs(context, count, position, folder):
    """This many tabs are open and the one at `position` (from 1) is in front (optionally in `folder`)."""
    yazi = _yazi(context.now)
    return yazi.tab_count == count and yazi.tab_index == position - 1 and (folder is None or _in(yazi, folder))


@need("one_tab", tool="yazi")
def one_tab(world, earlier):
    return _yazi(world).tab_count == 1


@need("nothing_copied", tool="yazi")
def nothing_copied(world, earlier):
    return not _yazi(world).copied
