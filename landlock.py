"""Linux Landlock write jail for the private Yazi process (stdlib ctypes only).

Restricts filesystem write-type operations to the private temp root (plus write/truncate on
`/dev/tty` and `/dev/null`). Reading and executing stay unrestricted. Fails closed: if Landlock
is unavailable or too old to block truncation (before Linux 6.2), or any syscall fails, the target
command is never executed.
"""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from collections.abc import Iterable


PR_SET_NO_NEW_PRIVS = 38
SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
MIN_ABI = 3  # the first that handles truncate (Linux 6.2)

# Filesystem write-type access rights (linux/landlock.h); read and execute stay unhandled.
FS_WRITE_FILE = 1 << 1
FS_REMOVE_DIR = 1 << 4
FS_REMOVE_FILE = 1 << 5
FS_MAKE_CHAR = 1 << 6
FS_MAKE_DIR = 1 << 7
FS_MAKE_REG = 1 << 8
FS_MAKE_SOCK = 1 << 9
FS_MAKE_FIFO = 1 << 10
FS_MAKE_BLOCK = 1 << 11
FS_MAKE_SYM = 1 << 12
FS_REFER = 1 << 13     # ABI >= 2
FS_TRUNCATE = 1 << 14  # ABI >= 3

WRITE_ABI1 = (
    FS_WRITE_FILE | FS_REMOVE_DIR | FS_REMOVE_FILE
    | FS_MAKE_CHAR | FS_MAKE_DIR | FS_MAKE_REG
    | FS_MAKE_SOCK | FS_MAKE_FIFO | FS_MAKE_BLOCK | FS_MAKE_SYM
)
O_PATH = getattr(os, "O_PATH", 0o10000000)


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.prctl.restype = ctypes.c_int
    return libc


def abi_version() -> int:
    """Return the kernel's Landlock ABI version (>= MIN_ABI), or raise OSError."""
    libc = _libc()
    version = libc.syscall(
        ctypes.c_long(SYS_LANDLOCK_CREATE_RULESET),
        ctypes.c_void_p(0),
        ctypes.c_size_t(0),
        ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION),
    )
    if version < 0:
        err = ctypes.get_errno() or errno.ENOSYS
        raise OSError(err, f"Landlock unavailable: {os.strerror(err)}")
    if version < MIN_ABI:
        raise OSError(errno.EOPNOTSUPP, "Landlock here cannot block truncating files: Linux 6.2 or newer is needed")
    return int(version)


def write_rights(abi: int) -> int:
    """Bitmask of every write-type filesystem right supported by `abi`."""
    if abi < 1:
        raise ValueError(f"Unsupported Landlock ABI {abi}")
    rights = WRITE_ABI1
    if abi >= 2:
        rights |= FS_REFER
    if abi >= 3:
        rights |= FS_TRUNCATE
    return rights


def _add_path_rule(libc, ruleset_fd: int, path: str | os.PathLike, allowed: int) -> None:
    fd = os.open(os.fspath(path), O_PATH | os.O_CLOEXEC)
    try:
        rule = _PathBeneathAttr(allowed_access=allowed, parent_fd=fd)
        rc = libc.syscall(
            ctypes.c_long(SYS_LANDLOCK_ADD_RULE),
            ctypes.c_int(ruleset_fd),
            ctypes.c_int(LANDLOCK_RULE_PATH_BENEATH),
            ctypes.byref(rule),
            ctypes.c_uint32(0),
        )
        if rc != 0:
            err = ctypes.get_errno() or errno.EINVAL
            raise OSError(err, f"landlock_add_rule({os.fspath(path)}): {os.strerror(err)}")
    finally:
        os.close(fd)


def restrict_writes(root: str | os.PathLike, *, extra_rw: Iterable[str | os.PathLike] = ("/dev/tty", "/dev/null")) -> int:
    """Confine this process (and its children) so filesystem writes are only allowed beneath `root`
    (plus write/truncate on the `extra_rw` files: the tty and `/dev/null`). Returns the Landlock ABI."""
    abi = abi_version()
    handled = write_rights(abi)
    dev_rights = FS_WRITE_FILE | FS_TRUNCATE
    libc = _libc()

    if libc.prctl(
        ctypes.c_int(PR_SET_NO_NEW_PRIVS),
        ctypes.c_ulong(1), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0),
    ) != 0:
        err = ctypes.get_errno() or errno.EPERM
        raise OSError(err, f"prctl(PR_SET_NO_NEW_PRIVS): {os.strerror(err)}")

    attr = _RulesetAttr(handled_access_fs=handled)
    ruleset_fd = int(libc.syscall(
        ctypes.c_long(SYS_LANDLOCK_CREATE_RULESET),
        ctypes.byref(attr),
        ctypes.c_size_t(ctypes.sizeof(attr)),
        ctypes.c_uint32(0),
    ))
    if ruleset_fd < 0:
        err = ctypes.get_errno() or errno.ENOSYS
        raise OSError(err, f"landlock_create_ruleset: {os.strerror(err)}")

    try:
        _add_path_rule(libc, ruleset_fd, root, handled)
        for path in extra_rw:
            _add_path_rule(libc, ruleset_fd, path, dev_rights)
        if libc.syscall(
            ctypes.c_long(SYS_LANDLOCK_RESTRICT_SELF),
            ctypes.c_int(ruleset_fd),
            ctypes.c_uint32(0),
        ) != 0:
            err = ctypes.get_errno() or errno.EPERM
            raise OSError(err, f"landlock_restrict_self: {os.strerror(err)}")
    finally:
        os.close(ruleset_fd)
    return abi


def _parse_args(argv: list[str]) -> tuple[str, str | None, list[str]]:
    root = error_file = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            cmd = argv[index + 1:]
            if not root or not cmd:
                raise ValueError("Usage: landlock.py --root DIR [--error-file PATH] -- CMD [ARGS...]")
            return root, error_file, cmd
        if arg == "--root" and index + 1 < len(argv):
            root = argv[index + 1]
            index += 2
        elif arg == "--error-file" and index + 1 < len(argv):
            error_file = argv[index + 1]
            index += 2
        else:
            raise ValueError(f"Unexpected argument: {arg}")
    raise ValueError("Usage: landlock.py --root DIR [--error-file PATH] -- CMD [ARGS...]")


def main(argv: list[str] | None = None) -> None:
    error_file: str | None = None
    try:
        root, error_file, cmd = _parse_args(sys.argv[1:] if argv is None else argv)
        restrict_writes(root)
        os.execv(cmd[0], cmd)
    except BaseException as exc:
        message = str(exc) or exc.__class__.__name__
        if error_file:
            try:
                with open(error_file, "w", encoding="utf-8") as handle:
                    handle.write(message + "\n")
            except OSError:
                pass
        sys.stderr.write(f"landlock: {message}\n")
        sys.exit(126)


if __name__ == "__main__":
    main()
