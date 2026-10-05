#!/usr/bin/python3 -I
"""Persistent Pixel Buds bridge for the Omarchy bar plugin.

Launched by Service.qml as `/usr/bin/python3 -I -B <this file>` with a closed
environment. It speaks line-delimited JSON (one object per line, ASCII,
bounded) on stdin/stdout and holds a single Maestro RFCOMM session to the
connected Pixel Buds for as long as they stay connected.

Lifecycle
  1. Find a connected BlueZ device that advertises the Maestro service UUID
     (never by name: a renamed pair must still be found). None -> "absent".
  2. Refuse to touch a device that drops Connected. ServicesResolved going
     false is the usual first sign that a device is leaving, so wait briefly
     and abort if Connected follows. Some Pixel Buds Pro 2 stay Connected
     with ServicesResolved false while they are already the audio device and
     the Maestro UUID is cached; those are connected, not left alone.
  3. Take the per-user runtime lock (one Maestro session per user, ever).
  4. Register a client-role BlueZ Profile1 for the Maestro UUID and ask BlueZ
     to ConnectProfile; BlueZ performs SDP and hands us the RFCOMM socket.
     This is exactly how pbpctrl connects.
  5. Resolve the Maestro channel, read the initial state, subscribe to the
     runtime-info and settings-change streams (push updates), then serve
     commands one at a time.
  6. Stand down immediately (shutdown the socket from the D-Bus thread, then
     exit) on: stdin EOF, SIGTERM/SIGINT/SIGHUP, the device's Connected or
     ServicesResolved turning false, BlueZ's RequestDisconnection/Release,
     the device object disappearing, or bluetoothd restarting.

Only the Python standard library and the stock PyGObject Gio/GLib bindings
are used. Nothing here spawns a process, uses a shell, or touches the network.
"""
from __future__ import annotations

import sys

# Never write bytecode next to the plugin, whatever the interpreter flags.
sys.dont_write_bytecode = True

import collections
import errno
import fcntl
import importlib.util
import json
import math
import os
import re
import select
import signal
import socket
import stat
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_sibling(name):
    """Import a module that ships next to this file. `python3 -I` drops the
    script directory from sys.path, so load it explicitly by absolute path."""
    spec = importlib.util.spec_from_file_location("pixelbuds_" + name, os.path.join(HERE, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


maestro = _load_sibling("maestro")
casecache = _load_sibling("casecache")

PROTOCOL_VERSION = 1
MAX_IN_LINE = 1024          # bytes per stdin command line
MAX_OUT_LINE = 4096         # bytes per stdout event line
MAX_QUEUE = 32              # pending commands
MAX_NAME = 100              # characters of the device name we pass on
MAX_RECV = 4096             # bytes per socket read
MAX_DEVICES = 256           # BlueZ objects scanned during detection
PIXEL_BUDS_PRO2_CLASS = 0x244404   # pbpctrl cli/src/bt.rs PIXEL_BUDS2_CLASS

RPC_TIMEOUT = 3.0
CONNECT_TIMEOUT = 12.0
CONNECT_TRIES = 3
# How long an unresolved-but-connected device may stay that way before we
# treat it as usable. A device that is actually leaving flips Connected
# inside this window; ConnectProfile is not called until the window passes.
UNRESOLVED_SETTLE = 1.0
LOCK_WAIT = 8.0
RUNTIME_STALE = 20.0        # refresh re-subscribes runtime info when older
CASE_WRITE_INTERVAL = 300.0

ADDR_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
ANC_MODES = ("off", "active", "aware", "adaptive")
LEGACY_ANC_MODES = ("off", "active", "aware")
HOLD_ACTIONS = ("anc", "assistant")

PROFILE_XML = """
<node>
  <interface name="org.bluez.Profile1">
    <method name="Release"/>
    <method name="NewConnection">
      <arg name="device" type="o" direction="in"/>
      <arg name="fd" type="h" direction="in"/>
      <arg name="properties" type="a{sv}" direction="in"/>
    </method>
    <method name="RequestDisconnection">
      <arg name="device" type="o" direction="in"/>
    </method>
  </interface>
</node>
"""


class Stop(Exception):
    """Leave the session. `reason` is reported in the final bye event."""

    def __init__(self, reason, detail=""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def clean_name(value):
    text = "".join(ch for ch in str(value) if ch.isprintable())
    return text.strip()[:MAX_NAME] or "Pixel Buds"


def format_eq(bands):
    return ",".join("%.2f" % b for b in bands)


class Emitter:
    """Writes bounded JSON lines. Only this object writes to stdout."""

    def __init__(self, stream):
        self.stream = stream
        self.lock = threading.Lock()

    def emit(self, obj):
        line = json.dumps(obj, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        if len(line) + 1 > MAX_OUT_LINE:
            line = json.dumps({"type": "error", "message": "event too large"})
        with self.lock:
            self.stream.write(line + "\n")
            self.stream.flush()


class LineReader:
    """Non-blocking, bounded line splitter for stdin."""

    def __init__(self, fd):
        self.fd = fd
        self.buf = bytearray()
        self.discarding = False
        self.eof = False
        self.lines = collections.deque()
        os.set_blocking(fd, False)

    def read(self):
        try:
            chunk = os.read(self.fd, 4096)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if not chunk:
            self.eof = True
            return
        for b in chunk:
            if b == 0x0A:
                if not self.discarding and len(self.lines) < MAX_QUEUE * 2:
                    self.lines.append(bytes(self.buf))
                self.buf.clear()
                self.discarding = False
            elif not self.discarding:
                if len(self.buf) >= MAX_IN_LINE:
                    self.buf.clear()
                    self.discarding = True     # overlong line: drop it whole
                    self.lines.append(None)
                else:
                    self.buf.append(b)


class Link:
    """State shared between the main thread and the D-Bus thread."""

    def __init__(self, wake_w):
        self.lock = threading.Lock()
        self.wake_w = wake_w
        self.gone = None
        self.sock = None
        self.fd = None
        self.connect_error = None

    def wake(self):
        try:
            os.write(self.wake_w, b"x")
        except OSError:
            pass

    def set_gone(self, reason):
        with self.lock:
            if self.gone is None:
                self.gone = reason
            sock = self.sock
        if sock is not None:
            # Release the RFCOMM channel right now, from whichever thread
            # noticed the disconnect, so we never hold the link open.
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.wake()

    def offer_fd(self, fd):
        with self.lock:
            accept = self.gone is None and self.fd is None and self.sock is None
            if accept:
                self.fd = fd
        if not accept:
            os.close(fd)
        self.wake()
        return accept

    def set_connect_error(self, message):
        with self.lock:
            self.connect_error = message
        self.wake()

    def attach(self, sock):
        with self.lock:
            self.sock = sock
            gone = self.gone
        if gone is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class Hub:
    """The only place the main thread blocks. Waits on stdin, the wake pipe
    and (once connected) the RFCOMM socket, with a deadline."""

    def __init__(self, reader, wake_r, link, terminate, clock=time.monotonic):
        self.reader = reader
        self.wake_r = wake_r
        self.link = link
        self.terminate = terminate
        self.clock = clock
        self.sock = None

    def check(self):
        if self.terminate.is_set():
            raise Stop("terminated")
        if self.reader.eof:
            raise Stop("stdin_closed")
        if self.link.gone is not None:
            raise Stop(self.link.gone)

    def wait(self, deadline):
        """Return bytes from the socket (possibly b"") or raise Stop."""
        self.check()
        rlist = [self.reader.fd, self.wake_r]
        if self.sock is not None:
            rlist.append(self.sock)
        timeout = max(0.0, deadline - self.clock())
        try:
            ready, _, _ = select.select(rlist, [], [], timeout)
        except InterruptedError:
            ready = []
        if self.wake_r in ready:
            try:
                os.read(self.wake_r, 512)
            except OSError:
                pass
        if self.reader.fd in ready:
            self.reader.read()
        self.check()
        if self.sock is not None and self.sock in ready:
            try:
                data = self.sock.recv(MAX_RECV)
            except (BlockingIOError, InterruptedError):
                return b""
            except OSError:
                raise Stop(self.link.gone or "link_lost")
            if not data:
                raise Stop(self.link.gone or "link_lost")
            return data
        return b""

    def send(self, data):
        self.check()
        if self.sock is None:
            raise Stop("link_lost")
        view = memoryview(data)
        deadline = self.clock() + RPC_TIMEOUT
        while view:
            if self.clock() >= deadline:
                raise Stop("link_lost", "send timed out")
            try:
                n = self.sock.send(view)
            except (BlockingIOError, InterruptedError):
                select.select([], [self.sock], [], 0.1)
                continue
            except OSError:
                raise Stop(self.link.gone or "link_lost")
            view = view[n:]

    def sleep(self, seconds):
        deadline = self.clock() + seconds
        while self.clock() < deadline:
            self.wait(deadline)


# --------------------------------------------------------------------------
# Descriptor-safe per-user runtime lock (from the former pbpctrl-locked.sh)
# --------------------------------------------------------------------------

RUNTIME_SUBDIR = "omarchy-pixelbuds"
LOCK_NAME = "maestro.lock"
DIR_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
LOCK_CREATE_FLAGS = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
LOCK_REOPEN_FLAGS = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class LockError(Exception):
    pass


class LockBusy(Exception):
    pass


def _runtime_path():
    runtime = os.environ.get("XDG_RUNTIME_DIR") or ("/run/user/%d" % os.geteuid())
    if not os.path.isabs(runtime) or "\x00" in runtime:
        raise LockError("runtime dir is invalid")
    return runtime.rstrip("/") or runtime


def _require_private_dir(fd):
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise LockError("runtime directory is not private")


def _require_private_lock(fd):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or info.st_mode & 0o177):
        raise LockError("lock file is unsafe")


def acquire_lock(wait=LOCK_WAIT, sleep=time.sleep, clock=time.monotonic):
    """Open, validate, flock and revalidate the session lock; returns the fd.

    The lock file is created/opened atomically with O_NOFOLLOW relative to a
    verified private directory descriptor; every check is fstat on the
    opened descriptor, never a pathname check followed by a separate open.
    """
    runtime_fd = private_fd = lock_fd = None
    try:
        try:
            runtime_fd = os.open(_runtime_path(), DIR_OPEN_FLAGS)
        except OSError as error:
            raise LockError("runtime dir is unavailable") from error
        _require_private_dir(runtime_fd)
        try:
            os.mkdir(RUNTIME_SUBDIR, 0o700, dir_fd=runtime_fd)
        except FileExistsError:
            pass
        except OSError as error:
            raise LockError("lock directory is unsafe") from error
        try:
            private_fd = os.open(RUNTIME_SUBDIR, DIR_OPEN_FLAGS, dir_fd=runtime_fd)
        except OSError as error:
            raise LockError("lock directory is unsafe") from error
        if stat.S_IMODE(os.fstat(private_fd).st_mode) & 0o077:
            os.fchmod(private_fd, 0o700)
        _require_private_dir(private_fd)
        try:
            lock_fd = os.open(LOCK_NAME, LOCK_CREATE_FLAGS, 0o600, dir_fd=private_fd)
        except FileExistsError:
            try:
                lock_fd = os.open(LOCK_NAME, LOCK_REOPEN_FLAGS, dir_fd=private_fd)
            except OSError as error:
                raise LockError("lock file is unsafe") from error
        except OSError as error:
            raise LockError("lock file is unsafe") from error
        _require_private_lock(lock_fd)
        deadline = clock() + wait
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if clock() >= deadline:
                    raise LockBusy("another Pixel Buds session is active")
                sleep(0.1)
        _require_private_lock(lock_fd)
        held, lock_fd = lock_fd, None
        return held
    finally:
        for fd in (lock_fd, private_fd, runtime_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


# --------------------------------------------------------------------------
# BlueZ over Gio D-Bus
# --------------------------------------------------------------------------

class Device:
    __slots__ = ("path", "addr", "name", "cls", "connected", "resolved")

    def __init__(self, path, addr, name, cls, connected, resolved):
        self.path, self.addr, self.name = path, addr, name
        self.cls, self.connected, self.resolved = cls, connected, resolved


def pick_device(objects):
    """Choose the connected Maestro device from a GetManagedObjects result
    (already unpacked to Python). Name matches are tried first only as a
    tie-break; the Maestro UUID is what qualifies a device."""
    found = []
    for i, (path, ifaces) in enumerate(objects.items()):
        if i >= MAX_DEVICES:
            break
        props = ifaces.get("org.bluez.Device1") if isinstance(ifaces, dict) else None
        if not isinstance(props, dict) or not isinstance(path, str):
            continue
        addr = str(props.get("Address", "")).upper()
        uuids = props.get("UUIDs", [])
        if not ADDR_RE.match(addr) or props.get("Connected") is not True:
            continue
        if not isinstance(uuids, (list, tuple)) or maestro.MAESTRO_UUID not in [str(u).lower() for u in uuids[:64]]:
            continue
        name = clean_name(props.get("Alias") or props.get("Name") or "Pixel Buds")
        cls = props.get("Class", 0)
        cls = cls if isinstance(cls, int) else 0
        dev = Device(path, addr, name, cls, True, props.get("ServicesResolved") is True)
        found.append((0 if "pixel buds" in name.lower() else 1, i, dev))
    found.sort(key=lambda t: (t[0], t[1]))
    return found[0][2] if found else None


class BluezGio:
    """Everything that touches D-Bus. A private GLib main loop runs in a
    daemon thread so BlueZ's Profile1 calls and property signals are served
    immediately, even while the main thread waits on the buds."""

    def __init__(self):
        import gi
        gi.require_version("Gio", "2.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self.ctx = GLib.MainContext.new()
        self.loop = GLib.MainLoop.new(self.ctx, False)
        self.profile_path = "/io/github/rdoupe/pixelbuds/maestro_%d" % os.getpid()
        self.link = None
        self.device = None
        self.bluez_owner = None
        self.registered = False
        self.thread = None

    def _call(self, path, iface, method, params, reply, timeout_ms=3000, dest="org.bluez"):
        GLib = self.GLib
        return self.bus.call_sync(dest, path, iface, method, params,
                                  GLib.VariantType.new(reply) if reply else None,
                                  self.Gio.DBusCallFlags.NONE, timeout_ms, None)

    def find_device(self):
        res = self._call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects",
                         None, "(a{oa{sa{sv}}})")
        return pick_device(res.unpack()[0])

    def device_state(self, path):
        res = self._call(path, "org.freedesktop.DBus.Properties", "GetAll",
                         self.GLib.Variant("(s)", ("org.bluez.Device1",)), "(a{sv})")
        props = res.unpack()[0]
        return props.get("Connected") is True, props.get("ServicesResolved") is True

    def _invoke(self, fn):
        source = self.GLib.idle_source_new()
        source.set_callback(lambda *_: (fn(), False)[1])
        source.attach(self.ctx)

    def start(self, device, link):
        """Export the profile object and watch the device, in the D-Bus thread."""
        self.device, self.link = device, link
        owner = self._call("/org/freedesktop/DBus", "org.freedesktop.DBus", "GetNameOwner",
                           self.GLib.Variant("(s)", ("org.bluez",)), "(s)",
                           dest="org.freedesktop.DBus")
        self.bluez_owner = owner.unpack()[0]
        ready = threading.Event()
        errors = []

        def run():
            self.ctx.push_thread_default()
            try:
                self._setup()
            except Exception as error:      # reported to the main thread
                errors.append(error)
                ready.set()
                return
            ready.set()
            self.loop.run()

        self.thread = threading.Thread(target=run, name="pixelbuds-dbus", daemon=True)
        self.thread.start()
        if not ready.wait(5.0) or errors:
            raise Stop("error", "D-Bus setup failed")
        self._call("/org/bluez", "org.bluez.ProfileManager1", "RegisterProfile",
                   self.GLib.Variant("(osa{sv})", (self.profile_path, maestro.MAESTRO_UUID, {
                       "Role": self.GLib.Variant("s", "client"),
                       "RequireAuthentication": self.GLib.Variant("b", False),
                       "RequireAuthorization": self.GLib.Variant("b", False),
                       "AutoConnect": self.GLib.Variant("b", False),
                   })), None)
        self.registered = True

    def _setup(self):
        Gio = self.Gio
        dev_path = self.device.path
        node = Gio.DBusNodeInfo.new_for_xml(PROFILE_XML)
        register = getattr(self.bus, "register_object_with_closures2", None) or self.bus.register_object
        register(self.profile_path, node.interfaces[0], self._on_method, None, None)

        def on_props(_conn, _sender, _path, _iface, _signal, params):
            iface, changed, _invalid = params.unpack()
            if iface != "org.bluez.Device1":
                return
            if changed.get("Connected") is False or changed.get("ServicesResolved") is False:
                self.link.set_gone("disconnected")

        def on_removed(_conn, _sender, _path, _iface, _signal, params):
            path, ifaces = params.unpack()
            if path == dev_path and "org.bluez.Device1" in ifaces:
                self.link.set_gone("disconnected")

        def on_owner(_conn, _sender, _path, _iface, _signal, params):
            name, _old, _new = params.unpack()
            if name == "org.bluez":
                self.link.set_gone("disconnected")

        flags = Gio.DBusSignalFlags.NONE
        self.bus.signal_subscribe("org.bluez", "org.freedesktop.DBus.Properties", "PropertiesChanged",
                                  dev_path, None, flags, on_props)
        self.bus.signal_subscribe("org.bluez", "org.freedesktop.DBus.ObjectManager", "InterfacesRemoved",
                                  "/", None, flags, on_removed)
        self.bus.signal_subscribe("org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged",
                                  "/org/freedesktop/DBus", "org.bluez", flags, on_owner)

    def _on_method(self, _conn, sender, _path, _iface, method, params, invocation):
        if sender != self.bluez_owner:
            invocation.return_dbus_error("org.bluez.Error.Rejected", "not BlueZ")
            return
        if method == "NewConnection":
            dev_path, index, _props = params.unpack()
            fdlist = invocation.get_message().get_unix_fd_list()
            fd = -1
            if fdlist is not None and 0 <= index < fdlist.get_length():
                try:
                    fd = fdlist.get(index)
                except Exception:
                    fd = -1
            if fd < 0 or dev_path != self.device.path:
                if fd >= 0:
                    os.close(fd)
                invocation.return_dbus_error("org.bluez.Error.Rejected", "unexpected connection")
                return
            if self.link.offer_fd(fd):
                invocation.return_value(None)
            else:
                invocation.return_dbus_error("org.bluez.Error.Rejected", "not accepting")
            return
        if method in ("RequestDisconnection", "Release"):
            self.link.set_gone("disconnected")
            invocation.return_value(None)
            return
        invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def connect_profile(self):
        def go():
            def done(bus, result):
                try:
                    bus.call_finish(result)
                except Exception as error:
                    msg = getattr(error, "message", "") or str(error)
                    self.link.set_connect_error(msg[:200])
            self.bus.call("org.bluez", self.device.path, "org.bluez.Device1", "ConnectProfile",
                          self.GLib.Variant("(s)", (maestro.MAESTRO_UUID,)), None,
                          self.Gio.DBusCallFlags.NONE, int(CONNECT_TIMEOUT * 1000), None, done)
        self._invoke(go)

    def close(self):
        if self.registered:
            self.registered = False
            try:
                self._call("/org/bluez", "org.bluez.ProfileManager1", "UnregisterProfile",
                           self.GLib.Variant("(o)", (self.profile_path,)), None, timeout_ms=1000)
            except Exception:
                pass
        if self.thread is not None:
            self._invoke(self.loop.quit)


# --------------------------------------------------------------------------
# Session: device state and command handling
# --------------------------------------------------------------------------

class Session:
    def __init__(self, emitter, hub, device, cache=None, clock=time.monotonic, wall=time.time):
        self.emitter = emitter
        self.hub = hub
        self.device = device
        self.cache = cache if cache is not None else CaseCache()
        self.clock = clock
        self.wall = wall
        self.client = maestro.RpcClient(hub, self.on_stream, clock=clock)
        self.runtime = {}
        self.runtime_at = None
        self.anc = "unknown"
        self.controls = {}
        self.loop_modes = None
        self.error = ""
        self.last_state = None
        self.queue = collections.deque()

    # ---- state ----
    @property
    def adaptive_supported(self):
        return (self.device.cls == PIXEL_BUDS_PRO2_CLASS or self.anc == "adaptive"
                or bool(self.loop_modes and "adaptive" in self.loop_modes))

    def status(self):
        st = {
            "connected": "1",
            "addr": self.device.addr,
            "name": self.device.name,
            "adaptive_supported": "1" if self.adaptive_supported else "0",
            "anc": self.anc,
        }
        for k in ("left", "right", "case"):
            if k in self.runtime:
                st[k] = self.runtime[k]
                st[k + "_state"] = self.runtime[k + "_state"]
        for k in ("left_in_case", "right_in_case"):
            if k in self.runtime:
                st[k] = self.runtime[k]
        if self.runtime:
            st.update(self.cache.update(self.runtime.get("case", -1)))
        if self.error:
            st["error"] = self.error
        return st

    def emit_state(self, force=False):
        st = self.status()
        if force or st != self.last_state:
            self.last_state = st
            self.emitter.emit({"type": "state", "status": st})

    def emit_controls(self):
        self.emitter.emit({"type": "controls", "controls": dict(self.controls)})

    def apply_setting(self, name, value):
        """Store one decoded setting; returns which view changed."""
        if name == "anc":
            self.anc = value if value in ANC_MODES else "unknown"
            return "state"
        if name in maestro.BOOL_SETTINGS:
            self.controls["ctl_" + name.replace("-", "_")] = "true" if value else "false"
            return "controls"
        if name == "gesture-control":
            for side in ("left", "right"):
                if value.get(side) in HOLD_ACTIONS:
                    self.controls["ctl_gesture_" + side] = value[side]
                else:
                    self.controls.pop("ctl_gesture_" + side, None)
            return "controls"
        if name == "anc-gesture-loop":
            self.loop_modes = list(value)
            if value:
                self.controls["ctl_anc_gesture_loop"] = ",".join(value)
            else:
                self.controls.pop("ctl_anc_gesture_loop", None)
            return "controls"
        if name == "balance":
            self.controls["ctl_balance"] = int(value)
            return "controls"
        if name == "eq":
            self.controls["ctl_eq"] = format_eq(value)
            return "controls"
        return None

    def on_stream(self, method, payload):
        try:
            if method == maestro.M_SUB_RUNTIME_INFO:
                self.runtime = maestro.decode_runtime_info(payload)
                self.runtime_at = self.clock()
                self.emit_state()
            elif method == maestro.M_SUB_SETTINGS:
                decoded = maestro.decode_setting_value(payload)
                if decoded is None:
                    return
                view = self.apply_setting(*decoded)
                if view == "state":
                    self.emit_state()
                elif view == "controls":
                    self.emit_controls()
                    self.emit_state()       # adaptive gating may have changed
        except maestro.DecodeError:
            pass

    # ---- device I/O ----
    def read_setting(self, sid):
        payload = self.client.call(maestro.M_READ_SETTING, maestro.read_setting_request(sid), RPC_TIMEOUT)
        decoded = maestro.decode_setting_value(payload)
        if decoded is None:
            raise maestro.RpcError("undecodable setting %d" % sid)
        self.apply_setting(*decoded)
        return decoded[1]

    def try_read(self, sid):
        try:
            return self.read_setting(sid)
        except maestro.RpcError:
            return None
        except maestro.DecodeError:
            return None

    def write(self, setting_value):
        self.client.call(maestro.M_WRITE_SETTING, maestro.write_setting_request(setting_value), RPC_TIMEOUT)

    def wait_runtime(self, timeout):
        since = self.runtime_at
        deadline = self.clock() + timeout
        while self.runtime_at == since and self.clock() < deadline:
            self.client.poll(deadline)

    def start(self):
        self.client.resolve_channel()
        self.try_read(maestro.S_ANC)
        self.client.subscribe(maestro.M_SUB_RUNTIME_INFO)
        self.wait_runtime(3.0)
        self.client.subscribe(maestro.M_SUB_SETTINGS)
        self.emit_state(force=True)
        self.emitter.emit({"type": "ready"})

    # ---- commands ----
    def enqueue(self, raw):
        if raw is None:
            self.emitter.emit({"type": "error", "message": "command line too long"})
            return
        try:
            cmd = parse_command(raw)
        except ValueError as error:
            self.emitter.emit({"type": "error", "message": str(error)[:200]})
            return
        kind = command_kind(cmd)
        for i, queued in enumerate(self.queue):
            if command_kind(queued) == kind:
                if cmd["cmd"] in ("refresh", "controls"):
                    self.result(cmd, True)       # identical poll already queued
                    return
                self.result(queued, False, "superseded")
                self.queue[i] = cmd              # last write wins
                return
        if len(self.queue) >= MAX_QUEUE:
            self.result(cmd, False, "busy")
            return
        self.queue.append(cmd)

    def result(self, cmd, ok, error=""):
        event = {"type": "result", "id": cmd["id"], "cmd": cmd["cmd"], "ok": bool(ok)}
        if error:
            event["error"] = error[:200]
        self.emitter.emit(event)

    def drain_stdin(self):
        while self.hub.reader.lines:
            self.enqueue(self.hub.reader.lines.popleft())

    def run(self):
        while True:
            self.drain_stdin()
            if self.queue:
                cmd = self.queue.popleft()
                try:
                    self.execute(cmd)
                    self.result(cmd, True)
                except maestro.RpcError as error:
                    self.result(cmd, False, str(error))
                except ValueError as error:
                    self.result(cmd, False, str(error))
                continue
            self.client.poll(self.clock() + 60.0)

    def execute(self, cmd):
        name = cmd["cmd"]
        if name == "refresh":
            self.read_setting(maestro.S_ANC)
            if self.runtime_at is None or self.clock() - self.runtime_at > RUNTIME_STALE:
                # The first stream item is a fresh snapshot (pbpctrl show runtime).
                self.client.subscribe(maestro.M_SUB_RUNTIME_INFO)
                self.wait_runtime(3.0)
            self.emit_state()
        elif name == "controls":
            for sid in (maestro.S_MULTIPOINT, maestro.S_OHD, maestro.S_SPEECH_DETECTION,
                        maestro.S_VOLUME_EXPOSURE, maestro.S_VOLUME_EQ, maestro.S_MONO,
                        maestro.S_GESTURES, maestro.S_GESTURE_CONTROL, maestro.S_ANC_GESTURE_LOOP,
                        maestro.S_BALANCE, maestro.S_EQ):
                self.try_read(sid)
            self.emit_controls()
            self.emit_state()
        elif name == "set_anc":
            mode = cmd["mode"]
            if mode == "adaptive" and not self.adaptive_supported:
                raise ValueError("adaptive is not supported by these buds")
            self.write(maestro.sv_anc(mode))
            self.read_setting(maestro.S_ANC)
            self.emit_state()
        elif name == "cycle_anc":
            loop = self.read_setting(maestro.S_ANC_GESTURE_LOOP)
            current = self.read_setting(maestro.S_ANC)
            if current not in maestro.ANC_CYCLE:
                raise ValueError("unknown ANC state")
            order = maestro.ANC_CYCLE
            idx = order.index(current)
            step = 1 if cmd["direction"] == "next" else -1
            for offs in range(1, len(order)):
                candidate = order[(idx + step * offs) % len(order)]
                if candidate in loop:
                    self.write(maestro.sv_anc(candidate))
                    break
            self.read_setting(maestro.S_ANC)
            self.emit_state()
        elif name == "set":
            key = cmd["key"]
            if key in maestro.BOOL_SETTINGS:
                sid = maestro.BOOL_SETTINGS[key]
                self.write(maestro.sv_bool(sid, cmd["value"]))
            elif key == "gesture-control":
                sid = maestro.S_GESTURE_CONTROL
                self.write(maestro.sv_gesture_control(cmd["left"], cmd["right"]))
            elif key == "anc-gesture-loop":
                modes = cmd["modes"]
                if "adaptive" in modes and not self.adaptive_supported:
                    raise ValueError("adaptive is not supported by these buds")
                sid = maestro.S_ANC_GESTURE_LOOP
                self.write(maestro.sv_anc_gesture_loop(modes))
            elif key == "balance":
                sid = maestro.S_BALANCE
                self.write(maestro.sv_balance(cmd["value"]))
            else:   # eq
                sid = maestro.S_EQ
                self.write(maestro.sv_eq(cmd["bands"]))
            # Re-read so the panel shows the device's truth, not our request.
            self.try_read(sid)
            self.emit_controls()
            self.emit_state()


def command_kind(cmd):
    if cmd["cmd"] == "set":
        return ("set", cmd["key"])
    if cmd["cmd"] in ("set_anc", "cycle_anc"):
        return ("anc",)
    return (cmd["cmd"],)


def _bool(obj, key):
    value = obj.get(key)
    if not isinstance(value, bool):
        raise ValueError("%s must be a boolean" % key)
    return value


def _number(obj, key, lo, hi):
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("%s must be a number" % key)
    if not lo <= value <= hi:
        raise ValueError("%s is out of range" % key)
    return value


def parse_command(raw):
    """Strictly validate one stdin command line. Raises ValueError."""
    try:
        text = raw.decode("ascii")
        obj = json.loads(text)
    except (UnicodeDecodeError, ValueError):
        raise ValueError("command is not ASCII JSON")
    if not isinstance(obj, dict):
        raise ValueError("command must be an object")
    cid = obj.get("id")
    if isinstance(cid, bool) or not isinstance(cid, int) or not 0 <= cid <= 0x7FFFFFFF:
        raise ValueError("id must be a non-negative integer")
    name = obj.get("cmd")
    allowed = {"id", "cmd"}
    out = {"id": cid, "cmd": name}
    if name in ("refresh", "controls"):
        pass
    elif name == "set_anc":
        allowed.add("mode")
        if obj.get("mode") not in ANC_MODES:
            raise ValueError("unknown ANC mode")
        out["mode"] = obj["mode"]
    elif name == "cycle_anc":
        allowed.add("direction")
        if obj.get("direction") not in ("next", "prev"):
            raise ValueError("direction must be next or prev")
        out["direction"] = obj["direction"]
    elif name == "set":
        key = obj.get("key")
        allowed.add("key")
        out["key"] = key
        if key in maestro.BOOL_SETTINGS:
            allowed.add("value")
            out["value"] = _bool(obj, "value")
        elif key == "gesture-control":
            allowed.update(("left", "right"))
            if obj.get("left") not in HOLD_ACTIONS or obj.get("right") not in HOLD_ACTIONS:
                raise ValueError("hold actions must be anc or assistant")
            out["left"], out["right"] = obj["left"], obj["right"]
        elif key == "anc-gesture-loop":
            allowed.add("modes")
            modes = obj.get("modes")
            if (not isinstance(modes, list) or len(modes) > 4
                    or any(m not in ANC_MODES for m in modes) or len(set(modes)) != len(modes)):
                raise ValueError("modes must be distinct ANC modes")
            if len(modes) < 2:
                raise ValueError("the hold gesture needs at least two modes")
            out["modes"] = list(modes)
        elif key == "balance":
            allowed.add("value")
            value = _number(obj, "value", -100, 100)
            if value != int(value):
                raise ValueError("balance must be an integer")
            out["value"] = int(value)
        elif key == "eq":
            allowed.add("bands")
            bands = obj.get("bands")
            if not isinstance(bands, list) or len(bands) != 5:
                raise ValueError("eq needs five bands")
            out["bands"] = [_number({"b": b}, "b", maestro.EQ_MIN, maestro.EQ_MAX) for b in bands]
        else:
            raise ValueError("unknown setting")
    else:
        raise ValueError("unknown command")
    if set(obj) - allowed:
        raise ValueError("unexpected fields")
    return out


class CaseCache:
    """The case reports only through a docked bud, so remember the last
    reading (as Android does) through the descriptor-safe casecache helper."""

    def __init__(self, wall=time.time):
        self.wall = wall
        self.last_put = None

    def update(self, case_pct):
        out = {}
        try:
            dfd = casecache.open_dir()
        except OSError:
            return out
        try:
            if 0 <= case_pct <= 100:
                now = self.wall()
                if self.last_put is None or self.last_put[0] != case_pct or now - self.last_put[1] > CASE_WRITE_INTERVAL:
                    casecache.put(dfd, case_pct)
                    self.last_put = (case_pct, now)
            else:
                got = casecache.get(dfd)
                if got is not None:
                    pct, ts = got
                    now = int(self.wall())
                    if 0 <= pct <= 100 and 0 < ts <= now:
                        out["case_last"] = pct
                        out["case_last_age"] = now - ts
        except OSError:
            pass
        finally:
            os.close(dfd)
        return out


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def confirm_still_connected(bluez, device, hub):
    """An unresolved device may be leaving, or it may be a Pixel Buds Pro 2
    that BlueZ never marks ServicesResolved while it is the active sink.
    Wait UNRESOLVED_SETTLE. Connected dropping means leave it alone.
    Staying connected means the Maestro UUID we already matched is enough."""
    deadline = time.monotonic() + UNRESOLVED_SETTLE
    while time.monotonic() < deadline:
        hub.sleep(0.2)
        try:
            connected, resolved = bluez.device_state(device.path)
        except Exception:
            raise Stop("disconnected")
        if not connected:
            raise Stop("disconnected")
        if resolved:
            device.resolved = True
            return


def connect(bluez, device, link, hub):
    """Open the Maestro RFCOMM socket through BlueZ. Re-verifies the link
    before every attempt so a leaving device is never reconnected."""
    last = "connect failed"
    for attempt in range(CONNECT_TRIES):
        hub.check()
        try:
            connected, _resolved = bluez.device_state(device.path)
        except Exception:
            raise Stop("disconnected")        # the device object is gone
        if not connected:
            raise Stop("disconnected")
        # ServicesResolved may stay false on a device that is in use.
        # Connected is the check that keeps a leaving device untouched.
        with link.lock:
            link.connect_error = None
        bluez.connect_profile()
        deadline = time.monotonic() + CONNECT_TIMEOUT
        while True:
            with link.lock:
                fd, err = link.fd, link.connect_error
            if fd is not None:
                sock = socket.socket(fileno=fd)
                sock.setblocking(False)
                link.attach(sock)
                return sock
            if err is not None:
                last = err
                break
            if time.monotonic() >= deadline:
                last = "connect timed out"
                break
            hub.wait(deadline)
        if attempt + 1 < CONNECT_TRIES:
            hub.sleep(1.0)
    raise Stop("connect_failed", last)


def main(argv=None, make_bluez=BluezGio, stdin_fd=0, stdout=None):
    stdout = stdout or sys.stdout
    emitter = Emitter(stdout)
    terminate = threading.Event()
    wake_r, wake_w = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
    link = Link(wake_w)

    def on_signal(_signum, _frame):
        # Only set the flag: the handler runs between bytecodes of the main
        # thread, which may be holding link.lock. set_wakeup_fd wakes the
        # hub, and Hub.check() turns the flag into an orderly shutdown.
        terminate.set()

    previous = {sig: signal.signal(sig, on_signal)
                for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    previous_wakeup = signal.set_wakeup_fd(wake_w, warn_on_full_buffer=False)

    reader = LineReader(stdin_fd)
    hub = Hub(reader, wake_r, link, terminate)
    bluez = None
    lock_fd = None
    sock = None
    reason, detail = "error", ""
    emitter.emit({"type": "hello", "v": PROTOCOL_VERSION})
    try:
        try:
            bluez = make_bluez()
            device = bluez.find_device()
        except Stop:
            raise
        except Exception:
            raise Stop("error", "BlueZ is unavailable")
        if device is None:
            raise Stop("absent")
        if not device.resolved:
            confirm_still_connected(bluez, device, hub)
        session = Session(emitter, hub, device)
        emitter.emit({"type": "state", "status": {
            "connected": "1", "addr": device.addr, "name": device.name,
            "adaptive_supported": "1" if session.adaptive_supported else "0", "anc": "unknown"}})
        try:
            lock_fd = acquire_lock(wait=LOCK_WAIT, sleep=lambda s: hub.sleep(s))
        except LockBusy:
            raise Stop("busy")
        except (LockError, OSError):
            raise Stop("error", "runtime lock is unsafe")
        try:
            bluez.start(device, link)
        except Stop:
            raise
        except Exception:
            raise Stop("error", "BlueZ refused the Maestro profile")
        sock = connect(bluez, device, link, hub)
        hub.sock = sock
        try:
            session.start()
            session.run()
        except maestro.RpcError as error:
            raise Stop("link_lost", str(error))
    except Stop as stop:
        reason, detail = stop.reason, stop.detail
    except Exception:
        reason, detail = "error", "internal error"
    finally:
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        if bluez is not None:
            bluez.close()
        if lock_fd is not None:
            os.close(lock_fd)
        signal.set_wakeup_fd(previous_wakeup)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        os.close(wake_r)
        os.close(wake_w)
    if reason in ("stdin_closed", "terminated"):
        return 0
    bye = {"type": "bye", "reason": reason}
    if detail:
        bye["detail"] = detail[:200]
    try:
        emitter.emit(bye)
    except (BrokenPipeError, OSError):
        pass
    return 0 if reason in ("absent", "not_ready", "disconnected") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0)
