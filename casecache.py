#!/usr/bin/env python3
"""Race-free case-battery cache for the Pixel Buds widget.

    casecache.py put <pct>   store a 0-100 reading with the current time
    casecache.py get         print "<pct> <ts>" if a sane reading is stored

Every access goes through a held directory descriptor with O_NOFOLLOW (and
O_NONBLOCK on reads, so a planted FIFO cannot stall the shell) and
dir_fd-relative openat/renameat, and every check is made on the opened
descriptor (fstat), never on a pathname — so neither a parent nor the leaf
can be swapped between validation and use. Nothing here follows a symlink.
"""
import os
import stat
import sys
import time

MAX_BYTES = 64
NAME = "case"


DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _verify_dir(fd):
    """The descriptor must be a directory owned by us."""
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        os.close(fd)
        raise PermissionError("directory not ours")
    return st


def _step(parent_fd, name, create):
    """Open one path component relative to an already-verified directory
    descriptor, never following a symlink, creating it 0700 if allowed."""
    try:
        fd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        fd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
    _verify_dir(fd)
    return fd


def open_dir():
    """Descriptor for the private state directory, reached only through
    verified no-follow directory descriptors from a trusted base: an
    explicit absolute $XDG_STATE_HOME, else $HOME/.local/state. No full
    pathname is ever created or opened, so no ancestor can be swapped
    between verification and use."""
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        if not os.path.isabs(xdg):
            raise PermissionError("relative XDG_STATE_HOME")
        base = os.open(xdg, DIR_FLAGS)
        _verify_dir(base)
        components = []
    else:
        home = os.environ.get("HOME") or os.path.expanduser("~")
        base = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        _verify_dir(base)
        components = [".local", "state"]
    fd = base
    try:
        for name in components:
            nxt = _step(fd, name, create=True)
            os.close(fd)
            fd = nxt
        nxt = _step(fd, "omarchy-pixelbuds", create=True)
        os.close(fd)
        fd = nxt
    except BaseException:
        os.close(fd)
        raise
    if stat.S_IMODE(os.fstat(fd).st_mode) & 0o077:
        os.fchmod(fd, 0o700)
    return fd


def get(dfd):
    try:
        fd = os.open(NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > MAX_BYTES:
            return None
        data = os.read(fd, MAX_BYTES)
    finally:
        os.close(fd)
    parts = data.decode("ascii", "replace").split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    pct, ts = int(parts[0]), int(parts[1])
    if not 0 <= pct <= 100 or not 0 < ts <= int(time.time()):
        return None
    return pct, ts


def put(dfd, pct):
    if not 0 <= pct <= 100:
        return
    tmp = ".case.%d.%d" % (os.getpid(), int.from_bytes(os.urandom(4), "big"))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=dfd)
    try:
        os.write(fd, ("%d %d\n" % (pct, int(time.time()))).encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        # rename replaces the leaf atomically and never follows a symlink there.
        os.rename(tmp, NAME, src_dir_fd=dfd, dst_dir_fd=dfd)
    except OSError:
        os.unlink(tmp, dir_fd=dfd)
        raise


def main(argv):
    if len(argv) < 2 or argv[1] not in ("get", "put"):
        return 2
    try:
        dfd = open_dir()
    except OSError:
        return 1
    try:
        if argv[1] == "get":
            r = get(dfd)
            if r is None:
                return 1
            print("%d %d" % r)
            return 0
        if len(argv) != 3 or not argv[2].isdigit() or len(argv[2]) > 3:
            return 2
        put(dfd, int(argv[2]))
        return 0
    except OSError:
        return 1
    finally:
        os.close(dfd)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
