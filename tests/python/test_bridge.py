"""Bridge tests: command validation, session behaviour against a fake buds,
lifecycle (stdin EOF, disconnects), runtime lock and case cache hardening.
No Bluetooth, no system bus, no desktop."""
import io
import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest

from support import bridge, m, FakeBuds, FakeBluez, Collector, runtime_payload

MAESTRO = m.MAESTRO_UUID
FASTPAIR = "0000fe2c-0000-1000-8000-00805f9b34fb"


class EnvCase(unittest.TestCase):
    """Private XDG runtime/state dirs per test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = os.path.join(self.tmp.name, "runtime")
        self.state = os.path.join(self.tmp.name, "state")
        os.mkdir(self.runtime, 0o700)
        os.mkdir(self.state, 0o700)
        self.old_env = {k: os.environ.get(k) for k in ("XDG_RUNTIME_DIR", "XDG_STATE_HOME")}
        os.environ["XDG_RUNTIME_DIR"] = self.runtime
        os.environ["XDG_STATE_HOME"] = self.state

    def tearDown(self):
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()


def later(delay, fn):
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()
    return t


class Rig:
    """A Session wired to a FakeBuds over a socketpair, stdin over a pipe."""

    def __init__(self, cls=0x240404, **buds_kwargs):
        ours, theirs = socket.socketpair()
        self.buds = FakeBuds(theirs, **buds_kwargs)
        self.buds.start()
        ours.setblocking(False)
        self.sock = ours
        self.stdin_r, self.stdin_w = os.pipe()
        self.wake_r, self.wake_w = os.pipe2(os.O_NONBLOCK)
        self.link = bridge.Link(self.wake_w)
        self.link.attach(ours)
        self.reader = bridge.LineReader(self.stdin_r)
        self.terminate = threading.Event()
        self.hub = bridge.Hub(self.reader, self.wake_r, self.link, self.terminate)
        self.hub.sock = ours
        self.out = Collector()
        self.device = bridge.Device("/org/bluez/hci0/dev_AA", "AA:BB:CC:DD:EE:FF", "Pixel Buds Pro",
                                    cls, True, True)
        self.session = bridge.Session(bridge.Emitter(self.out), self.hub, self.device)

    def send(self, **cmd):
        os.write(self.stdin_w, (json.dumps(cmd) + "\n").encode())

    def close_stdin(self):
        if self.stdin_w is not None:
            os.close(self.stdin_w)
            self.stdin_w = None

    def run_until_stop(self):
        with self.assertRaisesStop() as stop:
            self.session.run()
        return stop

    def assertRaisesStop(self):
        return _StopCatcher()

    def results(self):
        return {e["id"]: e for e in self.out.of("result")}

    def last(self, kind):
        events = self.out.of(kind)
        return events[-1] if events else None

    def close(self):
        self.close_stdin()
        for fd in (self.stdin_r, self.wake_r, self.wake_w):
            os.close(fd)
        self.sock.close()


class _StopCatcher:
    def __enter__(self):
        self.reason = None
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is bridge.Stop:
            self.reason = exc.reason
            return True
        raise AssertionError("expected Stop, got %r" % (exc,))


class ParseCommand(unittest.TestCase):
    def ok(self, **obj):
        return bridge.parse_command(json.dumps(obj).encode())

    def bad(self, raw):
        with self.assertRaises(ValueError):
            bridge.parse_command(raw if isinstance(raw, bytes) else json.dumps(raw).encode())

    def test_valid(self):
        self.assertEqual(self.ok(id=1, cmd="refresh"), {"id": 1, "cmd": "refresh"})
        self.assertEqual(self.ok(id=2, cmd="set_anc", mode="aware")["mode"], "aware")
        self.assertEqual(self.ok(id=3, cmd="cycle_anc", direction="prev")["direction"], "prev")
        self.assertEqual(self.ok(id=4, cmd="set", key="mono", value=True)["value"], True)
        self.assertEqual(self.ok(id=5, cmd="set", key="balance", value=-35)["value"], -35)
        self.assertEqual(self.ok(id=6, cmd="set", key="eq", bands=[0, 1.5, -6, 6, 0.5])["bands"][1], 1.5)
        self.assertEqual(self.ok(id=7, cmd="set", key="gesture-control", left="anc", right="assistant")["right"],
                         "assistant")
        self.assertEqual(self.ok(id=8, cmd="set", key="anc-gesture-loop", modes=["off", "aware"])["modes"],
                         ["off", "aware"])

    def test_invalid(self):
        for raw in (b"not json", b"[1]", "é".encode(), {"cmd": "refresh"}, {"id": -1, "cmd": "refresh"},
                    {"id": True, "cmd": "refresh"}, {"id": 1, "cmd": "rm -rf"},
                    {"id": 1, "cmd": "refresh", "extra": 1},
                    {"id": 1, "cmd": "set_anc", "mode": "loud"},
                    {"id": 1, "cmd": "cycle_anc", "direction": "up"},
                    {"id": 1, "cmd": "set", "key": "mono", "value": "true"},
                    {"id": 1, "cmd": "set", "key": "mono", "value": 1},
                    {"id": 1, "cmd": "set", "key": "balance", "value": 101},
                    {"id": 1, "cmd": "set", "key": "balance", "value": 2.5},
                    {"id": 1, "cmd": "set", "key": "eq", "bands": [0, 0, 0, 0]},
                    {"id": 1, "cmd": "set", "key": "eq", "bands": [0, 0, 0, 0, 7]},
                    {"id": 1, "cmd": "set", "key": "eq", "bands": [0, 0, 0, 0, True]},
                    {"id": 1, "cmd": "set", "key": "gesture-control", "left": "play", "right": "anc"},
                    {"id": 1, "cmd": "set", "key": "anc-gesture-loop", "modes": ["off"]},
                    {"id": 1, "cmd": "set", "key": "anc-gesture-loop", "modes": ["off", "off"]},
                    {"id": 1, "cmd": "set", "key": "auto-ota", "value": True}):
            self.bad(raw)
        self.bad(b'{"id":1,"cmd":"set","key":"eq","bands":[NaN,0,0,0,0]}')


class LineReaderBounds(unittest.TestCase):
    def test_overlong_line_dropped_whole(self):
        r, w = os.pipe()
        reader = bridge.LineReader(r)
        os.write(w, b"x" * (bridge.MAX_IN_LINE * 3) + b"\n" + b'{"ok":1}\n')
        while not reader.lines or len(reader.lines) < 2:
            reader.read()
        self.assertEqual(list(reader.lines), [None, b'{"ok":1}'])
        self.assertLessEqual(len(reader.buf), bridge.MAX_IN_LINE)
        os.close(w)
        reader.read()
        self.assertTrue(reader.eof)
        os.close(r)


class PickDevice(unittest.TestCase):
    def obj(self, addr, name, uuids, connected=True, resolved=True):
        return {"org.bluez.Device1": {"Address": addr, "Alias": name, "UUIDs": uuids,
                                      "Connected": connected, "ServicesResolved": resolved,
                                      "Class": 0x244404}}

    def test_maestro_uuid_required(self):
        objs = {"/a": self.obj("11:22:33:44:55:66", "Pixel Buds Pro", [FASTPAIR])}
        self.assertIsNone(bridge.pick_device(objs))

    def test_renamed_buds_found_behind_other_device(self):
        objs = {"/a": self.obj("11:22:33:44:55:66", "Headset", [FASTPAIR]),
                "/b": self.obj("aa:bb:cc:dd:ee:ff", "Ryan's Earbuds\x07", [FASTPAIR, MAESTRO.upper()])}
        dev = bridge.pick_device(objs)
        self.assertEqual((dev.path, dev.addr, dev.name, dev.cls), ("/b", "AA:BB:CC:DD:EE:FF", "Ryan's Earbuds", 0x244404))

    def test_disconnected_and_malformed_skipped(self):
        objs = {"/a": self.obj("AA:BB:CC:DD:EE:FF", "Pixel Buds", [MAESTRO], connected=False),
                "/b": self.obj("not-an-address", "Pixel Buds", [MAESTRO]),
                "/c": {"org.bluez.Device1": "garbage"},
                "/d": self.obj("AA:BB:CC:DD:EE:00", "x" * 500, [MAESTRO], resolved=False)}
        dev = bridge.pick_device(objs)
        self.assertEqual(dev.path, "/d")
        self.assertFalse(dev.resolved)
        self.assertEqual(len(dev.name), bridge.MAX_NAME)


class SessionTests(EnvCase):
    def setUp(self):
        super().setUp()
        self.rig = Rig()
        self.addCleanup(self.rig.close)

    def test_start_reads_state_and_subscribes(self):
        s = self.rig.session
        s.start()
        self.assertEqual(s.client.channel, 19)
        st = self.rig.last("state")["status"]
        self.assertEqual(st["anc"], "active")
        self.assertEqual((st["left"], st["right"], st["case"]), (90, 85, 75))
        self.assertEqual((st["right_state"], st["left_in_case"], st["right_in_case"]), ("charging", 0, 1))
        self.assertEqual(st["adaptive_supported"], "0")
        self.assertEqual(self.rig.out.of("ready"), [{"type": "ready"}])
        deadline = time.monotonic() + 2
        while m.M_SUB_SETTINGS not in self.rig.buds.subs and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn(m.M_SUB_RUNTIME_INFO, self.rig.buds.subs)
        self.assertIn(m.M_SUB_SETTINGS, self.rig.buds.subs)

    def test_all_commands(self):
        r = self.rig
        r.session.start()
        r.send(id=1, cmd="controls")
        r.send(id=2, cmd="set_anc", mode="aware")
        r.send(id=3, cmd="set", key="balance", value=-30)
        r.send(id=4, cmd="set", key="eq", bands=[1, 0, 0, 0, -1.5])
        r.send(id=5, cmd="set", key="gesture-control", left="assistant", right="assistant")
        r.send(id=6, cmd="set", key="anc-gesture-loop", modes=["off", "active", "aware"])
        r.send(id=7, cmd="set", key="mono", value=True)
        r.send(id=8, cmd="set_anc", mode="adaptive")      # not supported by a Pro (class 0x240404)
        r.send(id=9, cmd="refresh")
        later(0.5, r.close_stdin)
        with _StopCatcher() as stop:
            r.session.run()
        self.assertEqual(stop.reason, "stdin_closed")
        res = r.results()
        for i in (1, 3, 4, 5, 6, 7, 9):
            self.assertTrue(res[i]["ok"], res[i])
        # 2 and 8 share the ANC slot; the later one supersedes the earlier.
        self.assertEqual(res[2]["error"], "superseded")
        self.assertFalse(res[8]["ok"])
        controls = r.last("controls")["controls"]
        self.assertEqual(controls, {
            "ctl_multipoint": "true", "ctl_ohd": "true", "ctl_speech_detection": "false",
            "ctl_volume_exposure_notifications": "true", "ctl_volume_eq": "false", "ctl_mono": "true",
            "ctl_gestures": "true", "ctl_gesture_left": "assistant", "ctl_gesture_right": "assistant",
            "ctl_anc_gesture_loop": "active,off,aware", "ctl_balance": -30,
            "ctl_eq": "1.00,0.00,0.00,0.00,-1.50"})

    def test_first_controls_match_status_sh_format(self):
        r = self.rig
        r.session.start()
        r.send(id=1, cmd="controls")
        later(0.3, r.close_stdin)
        with _StopCatcher():
            r.session.run()
        c = r.last("controls")["controls"]
        self.assertEqual((c["ctl_balance"], c["ctl_eq"], c["ctl_anc_gesture_loop"]),
                         (20, "0.00,1.50,-2.00,0.50,3.00", "active,aware"))
        self.assertEqual((c["ctl_gesture_left"], c["ctl_gesture_right"]), ("anc", "assistant"))

    def test_set_anc_and_cycle(self):
        r = self.rig
        r.session.start()
        r.send(id=1, cmd="cycle_anc", direction="next")
        later(0.3, r.close_stdin)
        with _StopCatcher():
            r.session.run()
        # loop [active, aware]: next after active skips off -> aware
        self.assertEqual(r.last("state")["status"]["anc"], "aware")
        self.assertEqual(r.buds.writes[-1], m.sv_anc("aware"))

    def test_adaptive_gated_by_class_or_device_evidence(self):
        rig2 = Rig(cls=bridge.PIXEL_BUDS_PRO2_CLASS)
        self.addCleanup(rig2.close)
        rig2.session.start()
        self.assertEqual(rig2.last("state")["status"]["adaptive_supported"], "1")
        from support import default_settings
        settings = default_settings()
        settings[m.S_ANC] = m.sv_anc("adaptive")
        rig3 = Rig(settings=settings)
        self.addCleanup(rig3.close)
        rig3.session.start()
        self.assertEqual(rig3.last("state")["status"]["adaptive_supported"], "1")

    def test_push_updates(self):
        r = self.rig
        r.session.start()
        later(0.2, lambda: r.buds.push_runtime(runtime_payload(case=None, left=(42, 2))))
        later(0.4, lambda: r.buds.push_setting(m.sv_anc("off")))
        later(0.4, lambda: r.buds.push_setting(m.sv_bool(m.S_MONO, True)))
        later(0.8, r.close_stdin)
        with _StopCatcher():
            r.session.run()
        st = r.last("state")["status"]
        self.assertEqual((st["left"], st["left_state"], st["case"], st["anc"]), (42, "charging", -1, "off"))
        # The case went silent, so the cached last reading is surfaced.
        self.assertEqual(st["case_last"], 75)
        self.assertGreaterEqual(st["case_last_age"], 0)
        self.assertEqual(r.last("controls")["controls"]["ctl_mono"], "true")

    def test_disconnect_mid_operation_stands_down_at_once(self):
        r = self.rig
        r.session.start()
        r.buds.silent_methods.add(m.M_READ_SETTING)
        r.send(id=1, cmd="refresh")
        later(0.3, lambda: r.link.set_gone("disconnected"))
        t0 = time.monotonic()
        with _StopCatcher() as stop:
            r.session.run()
        self.assertEqual(stop.reason, "disconnected")
        self.assertLess(time.monotonic() - t0, 2.0)      # well under the RPC timeout
        self.assertTrue(r.buds.stopped.wait(2.0))         # our socket was shut down

    def test_unanswered_read_times_out_and_cancels(self):
        r = self.rig
        r.session.start()
        r.buds.silent_methods.add(m.M_READ_SETTING)
        r.send(id=1, cmd="refresh")
        later(bridge.RPC_TIMEOUT + 0.5, r.close_stdin)
        with _StopCatcher():
            r.session.run()
        self.assertFalse(r.results()[1]["ok"])
        self.assertIn(m.M_READ_SETTING, r.buds.cancels)

    def test_hostile_device_bytes(self):
        r = self.rig
        r.session.start()
        junk = (b"\x00" * 100 + b"\x7e" + b"A" * (m.MAX_FRAME * 3) + b"\x7e"
                + m.hdlc_encode(m.address_for_channel(19), b"\x0b\x0b\x0b")
                + m.hdlc_encode(m.address_for_channel(19), m.RpcPacket(m.PT_SERVER_STREAM, 19, 1, 2, b"x", 0, 99).encode())
                + m.hdlc_encode(7, b"\x01\x00\x00\x00\x40\x00\x00\x00"))
        r.buds.send_raw(junk)
        r.buds.push_setting(m.f_varint(m.S_BALANCE, 5000))        # out-of-range value
        r.buds.push_setting(m.f_bytes(m.S_EQ, m.f_float(1, float("inf"))))
        r.send(id=1, cmd="controls")
        later(0.5, r.close_stdin)
        with _StopCatcher():
            r.session.run()
        self.assertTrue(r.results()[1]["ok"])
        self.assertEqual(r.last("controls")["controls"]["ctl_balance"], 20)
        self.assertLessEqual(len(r.session.client.decoder.buf), m.MAX_FRAME)

    def test_device_closing_link(self):
        r = self.rig
        r.session.start()
        later(0.2, lambda: r.buds.sock.shutdown(socket.SHUT_RDWR))
        with _StopCatcher() as stop:
            r.session.run()
        self.assertEqual(stop.reason, "link_lost")

    def test_bad_stdin_lines_reported_not_fatal(self):
        r = self.rig
        r.session.start()
        os.write(r.stdin_w, b"x" * 5000 + b"\n{bad\n")
        r.send(id=3, cmd="refresh")
        later(0.3, r.close_stdin)
        with _StopCatcher():
            r.session.run()
        self.assertEqual(len(r.out.of("error")), 2)
        self.assertTrue(r.results()[3]["ok"])

    def test_resolve_by_probing_when_not_announced(self):
        rig = Rig(channel=21, announce=False)
        self.addCleanup(rig.close)
        self.assertEqual(rig.session.client.resolve_channel(timeout=0.2, probe_timeout=0.2), 21)

    def test_output_lines_bounded(self):
        r = self.rig
        r.session.start()
        for line in "".join(r.out.lines).splitlines():
            self.assertLess(len(line), bridge.MAX_OUT_LINE)
            line.encode("ascii")


class MainLifecycle(EnvCase):
    def run_main(self, fake, script=(), close_after=None):
        r, w = os.pipe()
        out = Collector()
        for line in script:
            os.write(w, (json.dumps(line) + "\n").encode())
        if close_after is None:
            os.close(w)
        else:
            later(close_after, lambda: os.close(w))
        code = bridge.main(make_bluez=lambda: fake, stdin_fd=r, stdout=out)
        os.close(r)
        return code, out

    def test_absent(self):
        fake = FakeBluez()
        fake.device = None
        code, out = self.run_main(fake)
        self.assertEqual(code, 0)
        self.assertEqual([e["type"] for e in out.events()], ["hello", "bye"])
        self.assertEqual(out.events()[-1]["reason"], "absent")

    def test_unresolved_device_that_drops_is_not_connected(self):
        fake = FakeBluez(resolved=False)
        later(0.3, lambda: setattr(fake, "connected", False))
        _code, out = self.run_main(fake, close_after=2.0)
        self.assertEqual(out.events()[-1]["reason"], "disconnected")
        self.assertEqual(fake.connect_calls, 0)

    def test_unresolved_stable_device_is_connected(self):
        fake = FakeBluez(resolved=False)
        _code, out = self.run_main(fake, close_after=2.5)
        self.assertGreater(fake.connect_calls, 0)
        self.assertNotIn("not_ready", [e.get("reason") for e in out.events() if e["type"] == "bye"])

    def test_going_away_device_is_never_reconnected(self):
        fake = FakeBluez()
        fake.connected = False          # BlueZ flipped Connected after detection
        code, out = self.run_main(fake, close_after=1.0)
        self.assertEqual(out.events()[-1]["reason"], "disconnected")
        self.assertEqual(fake.connect_calls, 0)

    def test_full_session_then_stdin_eof(self):
        fake = FakeBluez()
        code, out = self.run_main(fake, script=[{"id": 1, "cmd": "refresh"}, {"id": 2, "cmd": "controls"}],
                                  close_after=1.0)
        self.assertEqual(code, 0)
        types = [e["type"] for e in out.events()]
        self.assertEqual(types[0], "hello")
        self.assertIn("ready", types)
        self.assertNotIn("bye", types)                  # EOF: silent exit
        self.assertTrue(fake.closed)
        self.assertTrue(fake.buds.stopped.wait(2.0))    # session socket released
        self.assertEqual(out.of("state")[0]["status"]["addr"], "AA:BB:CC:DD:EE:FF")

    def test_disconnect_event_ends_session(self):
        fake = FakeBluez()
        later(0.8, lambda: fake.link.set_gone("disconnected"))
        code, out = self.run_main(fake, close_after=5.0)
        self.assertEqual(out.events()[-1], {"type": "bye", "reason": "disconnected"})

    def test_connect_failure_retries_then_reports(self):
        fake = FakeBluez(fail_connect="br-connection-refused")
        code, out = self.run_main(fake, close_after=10.0)
        self.assertEqual(code, 1)
        self.assertEqual(fake.connect_calls, bridge.CONNECT_TRIES)
        self.assertEqual(out.events()[-1]["reason"], "connect_failed")
        self.assertEqual(out.events()[-1]["detail"], "br-connection-refused")

    def test_busy_when_another_session_holds_lock(self):
        held = bridge.acquire_lock(wait=0)
        self.addCleanup(os.close, held)
        old = bridge.LOCK_WAIT
        bridge.LOCK_WAIT = 0.3
        self.addCleanup(setattr, bridge, "LOCK_WAIT", old)
        fake = FakeBluez()
        code, out = self.run_main(fake, close_after=3.0)
        self.assertEqual(out.events()[-1]["reason"], "busy")
        self.assertEqual(fake.connect_calls, 0)


    def test_dbus_errors_become_bye_not_traceback(self):
        class Refusing(FakeBluez):
            def start(self, device, link):
                raise RuntimeError("org.bluez.Error.NotPermitted")
        fake = Refusing()
        code, out = self.run_main(fake, close_after=2.0)
        self.assertEqual(out.events()[-1]["reason"], "error")

        class Vanishing(FakeBluez):
            def device_state(self, path):
                raise RuntimeError("UnknownObject")
        fake = Vanishing()
        code, out = self.run_main(fake, close_after=2.0)
        self.assertEqual(out.events()[-1]["reason"], "disconnected")
        self.assertEqual(fake.connect_calls, 0)


class RuntimeLock(EnvCase):
    def lock_path(self):
        return os.path.join(self.runtime, "omarchy-pixelbuds", "maestro.lock")

    def test_happy_path_modes(self):
        fd = bridge.acquire_lock(wait=0)
        os.close(fd)
        d = os.path.join(self.runtime, "omarchy-pixelbuds")
        self.assertEqual(stat.S_IMODE(os.lstat(d).st_mode), 0o700)
        st = os.lstat(self.lock_path())
        self.assertTrue(stat.S_ISREG(st.st_mode))
        self.assertEqual((stat.S_IMODE(st.st_mode), st.st_nlink), (0o600, 1))

    def test_reopen_does_not_truncate(self):
        os.close(bridge.acquire_lock(wait=0))
        with open(self.lock_path(), "w") as f:
            f.write("keep-me")
        os.close(bridge.acquire_lock(wait=0))
        with open(self.lock_path()) as f:
            self.assertEqual(f.read(), "keep-me")

    def test_exclusive(self):
        fd = bridge.acquire_lock(wait=0)
        with self.assertRaises(bridge.LockBusy):
            bridge.acquire_lock(wait=0.2)
        os.close(fd)
        os.close(bridge.acquire_lock(wait=0))

    def test_unsafe_targets_refused(self):
        d = os.path.join(self.runtime, "omarchy-pixelbuds")
        os.mkdir(d, 0o700)
        victim = os.path.join(self.tmp.name, "victim")
        with open(victim, "w") as f:
            f.write("secret")
        os.symlink(victim, self.lock_path())
        with self.assertRaises(bridge.LockError):
            bridge.acquire_lock(wait=0)
        with open(victim) as f:
            self.assertEqual(f.read(), "secret")
        os.unlink(self.lock_path())
        # hard link
        with open(self.lock_path(), "w"):
            pass
        os.chmod(self.lock_path(), 0o600)
        os.link(self.lock_path(), os.path.join(self.tmp.name, "hard"))
        with self.assertRaises(bridge.LockError):
            bridge.acquire_lock(wait=0)
        os.unlink(os.path.join(self.tmp.name, "hard"))
        # group-accessible
        os.chmod(self.lock_path(), 0o660)
        with self.assertRaises(bridge.LockError):
            bridge.acquire_lock(wait=0)
        os.unlink(self.lock_path())
        # directory and FIFO planted as the lock name
        os.mkdir(self.lock_path())
        with self.assertRaises(bridge.LockError):
            bridge.acquire_lock(wait=0)
        os.rmdir(self.lock_path())
        os.mkfifo(self.lock_path(), 0o600)
        with self.assertRaises(bridge.LockError):
            bridge.acquire_lock(wait=0)

    def test_symlinked_directories_refused(self):
        evil = os.path.join(self.tmp.name, "evil")
        os.mkdir(evil, 0o700)
        os.symlink(evil, os.path.join(self.runtime, "omarchy-pixelbuds"))
        with self.assertRaises(bridge.LockError):
            bridge.acquire_lock(wait=0)
        self.assertEqual(os.listdir(evil), [])
        os.unlink(os.path.join(self.runtime, "omarchy-pixelbuds"))
        real = os.path.join(self.tmp.name, "real-runtime")
        os.mkdir(real, 0o700)
        os.rmdir(self.runtime)
        os.symlink(real, self.runtime)
        with self.assertRaises(bridge.LockError):
            bridge.acquire_lock(wait=0)
        self.assertEqual(os.listdir(real), [])


class CaseCacheTests(EnvCase):
    def test_put_get_and_hostile_leaf(self):
        cache = bridge.CaseCache()
        self.assertEqual(cache.update(80), {})
        later_cache = bridge.CaseCache(wall=lambda: time.time() + 600)
        got = later_cache.update(-1)
        self.assertEqual(got["case_last"], 80)
        self.assertTrue(599 <= got["case_last_age"] <= 602)
        leaf = os.path.join(self.state, "omarchy-pixelbuds", "case")
        os.unlink(leaf)
        target = os.path.join(self.tmp.name, "target")
        with open(target, "w") as f:
            f.write("50 999999\n")
        os.symlink(target, leaf)
        self.assertEqual(later_cache.update(-1), {})       # symlink leaf is not followed
        bridge.CaseCache().update(55)
        self.assertTrue(os.path.isfile(leaf) and not os.path.islink(leaf))
        with open(target) as f:
            self.assertEqual(f.read(), "50 999999\n")         # rename replaced the link only


if __name__ == "__main__":
    unittest.main()
