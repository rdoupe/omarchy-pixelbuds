"""End-to-end: the real Service.qml in a headless shell, against the fake
org.bluez on a private dbus-daemon.

Connect/disconnect events reach Service.qml through the shell's own BlueZ
client (Quickshell.Bluetooth); the plugin parses no BlueZ output itself. This
checks that path with the real bridge: start-up with buds already connected,
a disconnect stopping the session, a reconnect starting it again, and a huge
Alias (which any local user can set) changing nothing.

Isolation: offscreen Qt platform; no Wayland, X11 or Hyprland variables;
session bus unreachable; system bus is the private test bus; HOME and every
XDG directory are temporary. Skipped when quickshell is not installed.
"""
import json
import os
import select
import subprocess
import time
import unittest

from support import REPO
from test_dbus_integration import PrivateBusCase

QUICKSHELL = "/usr/bin/quickshell"
HARNESS = os.path.join(REPO, "tests", "qml")

try:
    from gi.repository import GLib
except ImportError:
    GLib = None


@unittest.skipUnless(os.access(QUICKSHELL, os.X_OK), "needs quickshell")
class ServiceInHeadlessShell(PrivateBusCase):
    def start_shell(self, extra_env=None):
        for d in ("cfg", "cache", "data"):
            os.makedirs(os.path.join(self.tmp, d), exist_ok=True)
        env = dict(self.env)
        env.update({
            "QT_QPA_PLATFORM": "offscreen",
            "QT_QUICK_BACKEND": "software",
            "XDG_CONFIG_HOME": os.path.join(self.tmp, "cfg"),
            "XDG_CACHE_HOME": os.path.join(self.tmp, "cache"),
            "XDG_DATA_HOME": os.path.join(self.tmp, "data"),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + os.path.join(self.tmp, "no-session-bus"),
            "PIXELBUDS_TEST_SERVICE": "file://" + os.path.join(REPO, "Service.qml"),
            "NO_COLOR": "1",
        })
        env.update(extra_env or {})
        for name in ("WAYLAND_DISPLAY", "DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE"):
            env.pop(name, None)
        proc = subprocess.Popen([QUICKSHELL, "--no-color", "-p", HARNESS], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        self.addCleanup(self._kill, proc)
        self.log = b""
        return proc

    def wait_state(self, proc, want, timeout=15.0):
        """Waits until a logged state matches every key in `want`."""
        deadline = time.monotonic() + timeout
        buf = b""
        last = None
        while time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 0.1)
            if not ready:
                continue
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                break
            self.log += chunk
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                at = line.find(b"PBSTATE ")
                if at < 0:
                    continue
                last = json.loads(line[at + 8:])
                if all(last.get(k) == v for k, v in want.items()):
                    return last
        self.fail("state %r not reached; last %r\n%s" % (want, last, self.log.decode(errors="replace")[-3000:]))

    def test_startup_disconnect_reconnect(self):
        proc = self.start_shell()
        # Buds already connected when the shell starts.
        self.wait_state(proc, {"connected": True, "ready": True, "addr": "AA:BB:CC:DD:EE:FF"})
        # A user disconnect stops the session.
        self.control("Disconnect")
        self.wait_state(proc, {"connected": False, "ready": False})
        # Only a BlueZ connect event starts it again.
        self.control("Connect")
        self.wait_state(proc, {"connected": True, "ready": True})
        self.assertEqual(self.control("Stats", "(a{si})")["connects"], 2)

    def test_huge_alias_changes_nothing(self):
        proc = self.start_shell()
        self.wait_state(proc, {"connected": True, "ready": True})
        self.control_args("SetAlias", GLib.Variant("(s)", ("x" * 1_000_000,)))
        time.sleep(1.0)
        self.wait_state(proc, {"connected": True, "ready": True}, timeout=3.0)
        self.assertEqual(proc.poll(), None)
        self.assertEqual(self.control("Stats", "(a{si})")["connects"], 1)

    def test_connect_storm_is_rate_limited(self):
        # Another local UID may connect and disconnect the buds through
        # BlueZ's default policy; the shell must not relaunch the bridge for
        # every flap.
        proc = self.start_shell()
        self.wait_state(proc, {"connected": True, "ready": True})
        # Flaps just slower than the 400 ms debounce, for about 7 s.
        for _ in range(6):
            self.control("Disconnect")
            time.sleep(0.6)
            self.control("Connect")
            time.sleep(0.6)
        connects = self.control("Stats", "(a{si})")["connects"]
        self.assertLessEqual(connects, 4, "bridge connected %d times in 7 s of flapping" % connects)
        self.wait_state(proc, {"connected": True, "ready": True}, timeout=8.0)   # still recovers

    def test_reconnect_during_slow_shutdown_is_not_lost(self):
        # BlueZ answers UnregisterProfile slowly, so the old session is still
        # stopping when the buds come back; that connect must not be dropped.
        proc = self.start_shell()
        self.wait_state(proc, {"connected": True, "ready": True})
        self.control_args("SetUnregisterDelay", GLib.Variant("(u)", (900,)))
        self.control("Disconnect")
        time.sleep(0.1)
        self.control("Connect")
        self.wait_state(proc, {"connected": True, "ready": True}, timeout=10.0)

    def test_choosing_the_current_mode_settles(self):
        # The buds already report "active"; asking for it again changes
        # nothing, so no state event follows, only the command's result.
        proc = self.start_shell({"PIXELBUDS_TEST_SET_ANC": "active"})
        self.wait_state(proc, {"ancSent": True})
        self.wait_state(proc, {"ancSent": True, "pendingAnc": "", "ready": True}, timeout=5.0)


if __name__ == "__main__":
    unittest.main()
