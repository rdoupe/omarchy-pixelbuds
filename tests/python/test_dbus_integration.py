"""End-to-end: the real bridge script, launched exactly as Service.qml does
(`/usr/bin/python3 -I -B`, closed environment), against a fake org.bluez on a
private dbus-daemon. Exercises the real Gio code: device detection, Profile1
registration, ConnectProfile -> NewConnection fd hand-over, the Maestro
session over that fd, and every stand-down path.

The private bus is reached through DBUS_SYSTEM_BUS_ADDRESS, the standard
libdbus/GDBus variable; nothing here talks to the real system bus, BlueZ or
any Bluetooth device. Skipped when dbus-daemon or PyGObject is missing.
"""
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from support import REPO

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(REPO, "bridge", "pixelbuds_bridge.py")
DBUS_DAEMON = "/usr/bin/dbus-daemon"

try:
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
    HAVE_GI = True
except (ImportError, ValueError):
    HAVE_GI = False

CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path=%s</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


@unittest.skipUnless(HAVE_GI and os.access(DBUS_DAEMON, os.X_OK), "needs dbus-daemon and PyGObject")
class PrivateBusCase(unittest.TestCase):
    """A private dbus-daemon with the fake org.bluez on it. No tests here."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        sock = os.path.join(self.tmp, "bus")
        cfg = os.path.join(self.tmp, "bus.conf")
        with open(cfg, "w") as f:
            f.write(CONFIG % sock)
        self.daemon = subprocess.Popen([DBUS_DAEMON, "--config-file=" + cfg, "--nofork", "--nopidfile"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={})
        self.addCleanup(self._kill, self.daemon)
        deadline = time.monotonic() + 5
        while not os.path.exists(sock):
            if time.monotonic() > deadline:
                self.fail("private dbus-daemon did not start")
            time.sleep(0.02)
        self.address = "unix:path=" + sock
        for d in ("runtime", "state", "home"):
            os.mkdir(os.path.join(self.tmp, d), 0o700)
        self.env = {
            "PATH": "/usr/bin:/bin",
            "HOME": os.path.join(self.tmp, "home"),
            "LANG": "C.UTF-8",
            "XDG_RUNTIME_DIR": os.path.join(self.tmp, "runtime"),
            "XDG_STATE_HOME": os.path.join(self.tmp, "state"),
            "DBUS_SYSTEM_BUS_ADDRESS": self.address,
        }
        self.fake = subprocess.Popen([sys.executable, "-B", os.path.join(HERE, "fake_bluez_service.py")],
                                     stdout=subprocess.PIPE, env=self.env, text=True)
        self.addCleanup(self._kill, self.fake)
        self.assertEqual(self.fake.stdout.readline().strip(), "ready")
        self.bus = Gio.DBusConnection.new_for_address_sync(
            self.address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)

    def _kill(self, proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()

    def control(self, method, reply=None):
        res = self.bus.call_sync("org.bluez", "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF", "test.Control",
                                 method, None, GLib.VariantType.new(reply) if reply else None,
                                 Gio.DBusCallFlags.NONE, 5000, None)
        return res.unpack()[0] if reply else None

    def control_args(self, method, args):
        self.bus.call_sync("org.bluez", "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF", "test.Control",
                           method, args, None, Gio.DBusCallFlags.NONE, 5000, None)


class BridgeOverPrivateBus(PrivateBusCase):
    def start_bridge(self):
        proc = subprocess.Popen(["/usr/bin/python3", "-I", "-B", BRIDGE], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        self.addCleanup(self._kill, proc)
        return proc

    def read_until(self, proc, kind, timeout=10.0):
        events = []
        deadline = time.monotonic() + timeout
        buf = b""
        while time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 0.1)
            if not ready:
                continue
            chunk = os.read(proc.stdout.fileno(), 4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                events.append(json.loads(line))
                if events[-1].get("type") == kind:
                    return events
        self.fail("no %r event; got %r; stderr=%r" % (kind, events, proc.stderr.read1(4096) if proc.poll() is not None else b""))

    def send(self, proc, **cmd):
        proc.stdin.write((json.dumps(cmd) + "\n").encode())
        proc.stdin.flush()

    def test_session_commands_then_stdin_eof(self):
        proc = self.start_bridge()
        events = self.read_until(proc, "ready")
        self.assertEqual(events[0], {"type": "hello", "v": 1})
        state = [e for e in events if e["type"] == "state"][-1]["status"]
        self.assertEqual((state["addr"], state["name"], state["adaptive_supported"]),
                         ("AA:BB:CC:DD:EE:FF", "Pixel Buds Pro 2", "1"))
        self.assertEqual((state["left"], state["anc"]), (90, "active"))
        self.send(proc, id=1, cmd="set_anc", mode="adaptive")
        events = self.read_until(proc, "result")
        self.assertTrue(events[-1]["ok"], events)
        self.assertEqual([e for e in events if e["type"] == "state"][-1]["status"]["anc"], "adaptive")
        t0 = time.monotonic()
        proc.stdin.close()
        self.assertEqual(proc.wait(5), 0)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(proc.stdout.read(), b"")          # EOF exit is silent
        stats = self.control("Stats", "(a{si})")
        self.assertEqual(stats["connects"], 1)
        self.assertEqual(stats["registered"], 0)           # profile unregistered on exit
        self.assertEqual(stats["buds_stopped"], 1)         # RFCOMM end released
        self.assertFalse(os.path.exists(os.path.join(REPO, "bridge", "__pycache__")))

    def test_user_disconnect_stands_down(self):
        proc = self.start_bridge()
        self.read_until(proc, "ready")
        self.control("Disconnect")
        events = self.read_until(proc, "bye", timeout=3.0)
        self.assertEqual(events[-1], {"type": "bye", "reason": "disconnected"})
        self.assertEqual(proc.wait(3), 0)
        self.assertEqual(self.control("Stats", "(a{si})")["connects"], 1)   # never reconnected

    def test_bluez_request_disconnection(self):
        proc = self.start_bridge()
        self.read_until(proc, "ready")
        t0 = time.monotonic()
        self.control("RequestDisconnection")
        self.assertLess(time.monotonic() - t0, 2.0)        # answered promptly
        events = self.read_until(proc, "bye", timeout=3.0)
        self.assertEqual(events[-1]["reason"], "disconnected")

    def test_sigterm_exits_quietly(self):
        proc = self.start_bridge()
        self.read_until(proc, "ready")
        proc.terminate()
        self.assertEqual(proc.wait(3), 0)

    def test_spoofed_new_connection_rejected(self):
        proc = self.start_bridge()
        self.read_until(proc, "ready")
        names = self.bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                   "ListNames", None, GLib.VariantType.new("(as)"),
                                   Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]
        ours = self.bus.get_unique_name()
        r, w = os.pipe()
        fds = Gio.UnixFDList.new()
        fds.append(r)
        os.close(r)
        os.close(w)
        rejected = 0
        for name in names:
            if not name.startswith(":") or name == ours:
                continue
            try:
                self.bus.call_with_unix_fd_list_sync(
                    name, "/io/github/rdoupe/pixelbuds/maestro_%d" % proc.pid, "org.bluez.Profile1",
                    "NewConnection", GLib.Variant("(oha{sv})", ("/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF", 0, {})),
                    None, Gio.DBusCallFlags.NONE, 2000, fds, None)
            except GLib.Error as error:
                if "Rejected" in error.message:
                    rejected += 1
        self.assertEqual(rejected, 1)
        self.send(proc, id=9, cmd="refresh")
        self.assertTrue(self.read_until(proc, "result")[-1]["ok"])  # real session unaffected


if __name__ == "__main__":
    unittest.main()
