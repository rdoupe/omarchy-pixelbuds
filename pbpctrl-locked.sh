#!/usr/bin/python3 -I
"""Serialize pbpctrl behind a descriptor-safe XDG runtime lock.

Omarchy creates one bar-widget instance per monitor. This helper holds an
exclusive flock across those instances so BlueZ never receives two simultaneous
pbpctrl sessions for the same vendor UUID.

The lock is created and opened atomically with O_NOFOLLOW (O_EXCL on create,
non-truncating reopen) relative to a verified private runtime directory fd.
Owner, type, and link-count checks run on the opened file itself — a pathname
is never checked and then opened separately. After flock the same descriptor is
revalidated, then pbpctrl is exec'd with the lock fd held.
"""
from __future__ import annotations

import fcntl
import os
import signal
import stat
import sys

RUNTIME_SUBDIR = "omarchy-pixelbuds"
LOCK_NAME = "pbpctrl.lock"
LOCK_WAIT_SEC = 8
EX_TEMPFAIL = 75
DEFAULT_TRUSTED_PATH = "/usr/bin:/bin"
# Test stubs only. Production QML does not forward this variable.
TRUSTED_PATH_ENV = "PIXELBUDS_TRUSTED_PATH"
SESSION_ENV_KEYS = (
    "HOME", "USER", "LOGNAME",
    "LANG", "LC_ALL", "LC_CTYPE",
    "XDG_RUNTIME_DIR", "XDG_STATE_HOME",
    "DBUS_SYSTEM_BUS_ADDRESS", "DBUS_SESSION_BUS_ADDRESS",
    "WAYLAND_DISPLAY",
)

DIR_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
LOCK_CREATE_FLAGS = (
    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
)
LOCK_REOPEN_FLAGS = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class LockTimeout(Exception):
    """Exclusive flock wait exceeded LOCK_WAIT_SEC."""


def trusted_path_dirs():
    """Allowlisted absolute directories. Never consult ambient PATH."""
    extra = os.environ.get(TRUSTED_PATH_ENV, "")
    parts = []
    if extra:
        parts.extend(extra.split(os.pathsep))
    parts.extend(DEFAULT_TRUSTED_PATH.split(os.pathsep))
    dirs = []
    seen = set()
    for part in parts:
        if not part or not os.path.isabs(part) or ".." in part.split(os.sep):
            continue
        part = part.rstrip("/") or part
        if part not in seen:
            seen.add(part)
            dirs.append(part)
    return dirs or ["/usr/bin", "/bin"]


def _is_trusted_real(real, dirs):
    for directory in dirs:
        if real == directory or real.startswith(directory + os.sep):
            return True
    return False


def resolve_trusted_exec(name):
    """Return an allowlisted absolute executable. Never search ambient PATH."""
    if not name or name in (".", "..") or os.sep in name:
        raise RuntimeError("pbpctrl tool name is unsafe")
    dirs = trusted_path_dirs()
    for directory in dirs:
        candidate = os.path.join(directory, name)
        try:
            if not os.path.lexists(candidate):
                continue
            real = os.path.realpath(candidate)
            if not os.path.isfile(real) or not os.access(real, os.X_OK):
                continue
        except OSError:
            continue
        if _is_trusted_real(real, dirs):
            return real
    raise FileNotFoundError(name)


def closed_env():
    """Allowlist only. Drops PYTHON*, LD_*, and ambient PATH."""
    env = {
        "PATH": DEFAULT_TRUSTED_PATH,
        "LANG": os.environ.get("LANG") or "C.UTF-8",
    }
    for key in SESSION_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def require_nofollow_support():
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_EXCL"):
        raise RuntimeError("pbpctrl lock requires no-follow exclusive open support")


def runtime_path():
    runtime = os.environ.get("XDG_RUNTIME_DIR") or ("/run/user/%d" % os.geteuid())
    if not runtime or not os.path.isabs(runtime) or "\x00" in runtime:
        raise RuntimeError("pbpctrl lock runtime dir is invalid")
    return runtime.rstrip("/") or runtime


def _require_private_dir(fd, label):
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("%s is not a directory" % label)
    if info.st_uid != os.geteuid():
        raise RuntimeError("%s has an unexpected owner" % label)
    if info.st_mode & 0o077:
        raise RuntimeError("%s is accessible to others" % label)
    return info


def _require_private_lock(fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("pbpctrl lock file is not a regular file")
    if info.st_uid != os.geteuid():
        raise RuntimeError("pbpctrl lock file has an unexpected owner")
    if info.st_nlink != 1:
        raise RuntimeError("pbpctrl lock file has an unexpected link count")
    if info.st_mode & 0o177:
        raise RuntimeError("pbpctrl lock file is accessible to others")
    return info


def _close_quietly(*fds):
    for fd in fds:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _open_lock_fd(private_fd):
    try:
        fd = os.open(LOCK_NAME, LOCK_CREATE_FLAGS, 0o600, dir_fd=private_fd)
    except FileExistsError:
        try:
            fd = os.open(LOCK_NAME, LOCK_REOPEN_FLAGS, dir_fd=private_fd)
        except OSError as error:
            raise RuntimeError("pbpctrl lock file is unsafe") from error
    except OSError as error:
        raise RuntimeError("pbpctrl lock file is unsafe") from error
    try:
        _require_private_lock(fd)
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
    except Exception:
        os.close(fd)
        raise
    return fd


def _acquire_exclusive(fd, timeout=LOCK_WAIT_SEC):
    def _timeout(_signum, _frame):
        raise LockTimeout()

    prev = signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(timeout)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev)


def acquire_lock():
    """Open, validate, flock, and revalidate the pbpctrl lock. Returns a held fd."""
    require_nofollow_support()
    runtime = runtime_path()
    runtime_fd = private_fd = lock_fd = None
    try:
        try:
            runtime_fd = os.open(runtime, DIR_OPEN_FLAGS)
        except OSError as error:
            raise RuntimeError("pbpctrl lock runtime dir is unavailable") from error
        _require_private_dir(runtime_fd, "pbpctrl lock runtime dir")
        try:
            os.mkdir(RUNTIME_SUBDIR, 0o700, dir_fd=runtime_fd)
        except FileExistsError:
            pass
        except OSError as error:
            raise RuntimeError("pbpctrl lock directory is unsafe") from error
        try:
            private_fd = os.open(RUNTIME_SUBDIR, DIR_OPEN_FLAGS, dir_fd=runtime_fd)
        except OSError as error:
            raise RuntimeError("pbpctrl lock directory is unsafe") from error
        if stat.S_IMODE(os.fstat(private_fd).st_mode) & 0o077:
            os.fchmod(private_fd, 0o700)
        _require_private_dir(private_fd, "pbpctrl lock directory")
        lock_fd = _open_lock_fd(private_fd)
        _acquire_exclusive(lock_fd)
        _require_private_lock(lock_fd)
        os.set_inheritable(lock_fd, True)
        held = lock_fd
        lock_fd = None
        return held
    finally:
        _close_quietly(lock_fd, private_fd, runtime_fd)


def main(argv):
    try:
        pbpctrl = resolve_trusted_exec("pbpctrl")
    except FileNotFoundError:
        print("pbpctrl not found", file=sys.stderr)
        return 127
    except RuntimeError as error:
        print("pbpctrl lock is unsafe: %s" % error, file=sys.stderr)
        return EX_TEMPFAIL
    try:
        acquire_lock()
    except LockTimeout:
        print("timed out waiting for another Pixel Buds control operation", file=sys.stderr)
        return EX_TEMPFAIL
    except (OSError, RuntimeError) as error:
        print("pbpctrl lock is unsafe: %s" % error, file=sys.stderr)
        return EX_TEMPFAIL
    try:
        os.execve(pbpctrl, [pbpctrl] + argv[1:], closed_env())
    except OSError as error:
        print("pbpctrl exec failed: %s" % error, file=sys.stderr)
        return EX_TEMPFAIL
    return EX_TEMPFAIL


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
