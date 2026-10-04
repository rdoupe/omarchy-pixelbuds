"""Protocol codec tests. Vectors marked (pbpctrl) are copied from the unit
tests in qzed/pbpctrl's libmaestro (MIT OR Apache-2.0, used here under MIT)."""
import os
import random
import struct
import unittest

from support import m

# (pbpctrl) libmaestro/src/hdlc/decoder.rs test_frame_decode
PBP_FRAME = bytes([0x7e, 0x06, 0x08, 0x09, 0x03, 0x05, 0x06, 0x07, 0x7d, 0x5d,
                   0x7d, 0x5e, 0x7f, 0xff, 0xe6, 0x2d, 0x17, 0xc6, 0x7e])
PBP_EXPECT = m.Frame(0x010203, 0x03, bytes([0x05, 0x06, 0x07, 0x7D, 0x7E, 0x7F, 0xFF]))


class Crc(unittest.TestCase):
    def test_vectors(self):  # (pbpctrl) crc.rs
        self.assertEqual(m.crc32(b"test test test"), 0x235b6a02)
        self.assertEqual(m.crc32(b"1234321"), 0xd981751c)


class Address(unittest.TestCase):
    def test_decode(self):  # (pbpctrl) varint.rs test_decode
        cases = [([0x01], 0, 1), ([0x00, 0x00, 0x00, 0x01], 0, 4), ([0x11, 0x00], 8, 1),
                 ([0x10, 0x21], 0x808, 2), ([0x03], 1, 1), ([0xff], 0x7f, 1), ([0x00, 0x03], 0x80, 2),
                 ([0xfe, 0xff], 0x3fff, 2), ([0xfe, 0xfe, 0xfe, 0xfe, 0x1f], 0xFFFFFFFF, 5)]
        for data, value, n in cases:
            self.assertEqual(m.decode_address(bytes(data)), (value, n))
        with self.assertRaises(ValueError):
            m.decode_address(b"\xfe")
        with self.assertRaises(ValueError):
            m.decode_address(bytes([0xFE, 0xFE, 0xFE, 0xFE, 0xFF]))

    def test_encode(self):  # (pbpctrl) varint.rs test_encode
        cases = {0x01234: [0x68, 0x49], 0x87654: [0xa8, 0xd8, 0x43], 0: [0x01], 1: [0x03],
                 0x7f: [0xff], 0x80: [0x00, 0x03], 0x3fff: [0xfe, 0xff], 0x4000: [0x00, 0x00, 0x03],
                 0xFFFFFFFF: [0xfe, 0xfe, 0xfe, 0xfe, 0x1f]}
        for value, data in cases.items():
            self.assertEqual(m.encode_address(value), bytes(data))

    def test_channels(self):
        self.assertEqual(m.address_for_channel(18), (10 << 6) | (2 << 10))
        self.assertEqual(m.address_for_channel(26), (13 << 6) | (4 << 10))
        with self.assertRaises(ValueError):
            m.address_for_channel(99)


class Hdlc(unittest.TestCase):
    def test_encode(self):  # (pbpctrl) encoder.rs test_encode
        self.assertEqual(m.hdlc_encode(0x010203, b""),
                         bytes([0x7e, 0x06, 0x08, 0x09, 0x03, 0x8b, 0x3b, 0xf7, 0x42, 0x7e]))
        self.assertEqual(m.hdlc_encode(0x010203, PBP_EXPECT.data), PBP_FRAME)

    def test_decode_and_trailing(self):
        d = m.HdlcDecoder()
        self.assertEqual(d.feed(PBP_FRAME + b"\x02\x01"), [PBP_EXPECT])
        self.assertEqual(d.feed(PBP_FRAME), [PBP_EXPECT])

    def test_split_feed_mid_escape(self):
        d = m.HdlcDecoder()
        self.assertEqual(d.feed(PBP_FRAME[:9]), [])
        self.assertTrue(d.escaped)
        self.assertEqual(d.feed(PBP_FRAME[9:]), [PBP_EXPECT])

    def test_back_to_back_shared_flag(self):
        d = m.HdlcDecoder()
        two = PBP_FRAME + PBP_FRAME[1:]          # closing flag opens the next frame
        self.assertEqual(d.feed(two), [PBP_EXPECT, PBP_EXPECT])

    def test_cut_off_frames(self):  # (pbpctrl) data-loss cases
        d = m.HdlcDecoder()
        self.assertEqual(d.feed(PBP_FRAME[:5] + PBP_FRAME), [PBP_EXPECT])
        self.assertEqual(d.feed(PBP_FRAME[:10] + PBP_FRAME), [PBP_EXPECT])
        self.assertGreaterEqual(d.errors, 2)

    def test_flag_after_escape(self):
        d = m.HdlcDecoder()
        bad = bytearray(PBP_FRAME[:10])
        bad[9] = 0x7E
        self.assertEqual(d.feed(bytes(bad) + PBP_FRAME), [PBP_EXPECT])

    def test_double_escape(self):
        d = m.HdlcDecoder()
        self.assertEqual(d.feed(b"\x7e\x06\x7d\x7d\x01" + PBP_FRAME), [PBP_EXPECT])

    def test_bad_crc(self):
        d = m.HdlcDecoder()
        bad = bytearray(PBP_FRAME)
        bad[5] ^= 1
        self.assertEqual(d.feed(bytes(bad)), [])
        self.assertEqual(d.errors, 1)

    def test_garbage_and_unframed_bytes(self):
        d = m.HdlcDecoder()
        self.assertEqual(d.feed(os.urandom(0) + b"garbage without flags" * 50), [])
        self.assertEqual(len(d.buf), 0)

    def test_oversized_frame_bounded(self):
        d = m.HdlcDecoder()
        huge = b"\x7e" + b"\x41" * (m.MAX_FRAME * 4)
        self.assertEqual(d.feed(huge), [])
        self.assertLessEqual(len(d.buf), m.MAX_FRAME)
        self.assertEqual(d.feed(b"\x7e" + PBP_FRAME[1:]), [PBP_EXPECT])
        self.assertEqual(d.errors, 1)

    def test_escape_roundtrip_all_bytes(self):
        payload = bytes(range(256)) * 4
        d = m.HdlcDecoder()
        self.assertEqual(d.feed(m.hdlc_encode(0x1234, payload)), [m.Frame(0x1234, 3, payload)])

    def test_random_noise_never_raises(self):
        rnd = random.Random(1234)
        d = m.HdlcDecoder()
        for _ in range(300):
            chunk = bytes(rnd.choice((0x7e, 0x7d, rnd.randrange(256))) for _ in range(rnd.randrange(64)))
            for frame in d.feed(chunk):
                m.decode_rpc_frame(frame)
            self.assertLessEqual(len(d.buf), m.MAX_FRAME)


class Ids(unittest.TestCase):
    def test_known_hashes(self):  # (pbpctrl) id.rs test_known_id_hashes
        self.assertEqual(m.hash65599("maestro_pw.Maestro"), 0x7ede71ea)
        self.assertEqual(m.hash65599("GetSoftwareInfo"), 0x7199fa44)
        self.assertEqual(m.hash65599("SubscribeToSettingsChanges"), 0x2821adf5)
        self.assertEqual(m.path_ids(m.M_SUB_SETTINGS), (0x7ede71ea, 0x2821adf5))


class Protobuf(unittest.TestCase):
    def test_varint_roundtrip(self):
        for v in (0, 1, 127, 128, 300, 2 ** 32 - 1, 2 ** 63):
            self.assertEqual(m.decode_varint(m.encode_varint(v), 0), (v, len(m.encode_varint(v))))

    def test_negative_int32(self):
        data = m.f_varint(1, -5)
        self.assertEqual(len(data), 11)
        self.assertEqual(m.get_int32(m.parse_fields(data), 1), -5)

    def test_varint_bounds(self):
        with self.assertRaises(m.DecodeError):
            m.decode_varint(b"\xff" * 11, 0)
        with self.assertRaises(m.DecodeError):
            m.decode_varint(b"\x80", 0)

    def test_fields(self):
        msg = m.f_varint(1, 7) + m.f_fixed32(3, 0xdeadbeef) + m.f_bytes(5, b"xy") + m.f_float(6, 1.5)
        f = m.parse_fields(msg)
        self.assertEqual(m.get_uint(f, 1), 7)
        self.assertEqual(m.get_fixed32(f, 3), 0xdeadbeef)
        self.assertEqual(m.get_bytes(f, 5), b"xy")
        self.assertEqual(m.get_float(f, 6), 1.5)
        self.assertIsNone(m.get_uint(f, 5))       # wrong wire type is not coerced

    def test_hostile_messages(self):
        for bad in (m.f_bytes(1, b"abc")[:-1], b"\x0b", b"\x00\x01", b"\x0d\x01\x02",
                    m.key(1, 2) + m.encode_varint(2 ** 40)):
            with self.assertRaises(m.DecodeError):
                m.parse_fields(bad)
        with self.assertRaises(m.DecodeError):
            m.parse_fields(m.f_varint(1, 1) * (m.MAX_FIELDS + 1))

    def test_rpc_packet_roundtrip(self):
        p = m.RpcPacket(m.PT_REQUEST, 19, 0x7ede71ea, 0x7199fa44, b"\x01\x02", 0, 7)
        q = m.RpcPacket.decode(p.encode())
        self.assertEqual((q.type, q.channel_id, q.service_id, q.method_id, q.payload, q.status, q.call_id),
                         (0, 19, 0x7ede71ea, 0x7199fa44, b"\x01\x02", 0, 7))
        frame = m.HdlcDecoder().feed(m.encode_rpc_frame(p))[0]
        self.assertEqual(frame.address, m.address_for_channel(19))
        self.assertEqual(m.decode_rpc_frame(frame).uid(), p.uid())

    def test_rpc_frame_filtering(self):
        self.assertIsNone(m.decode_rpc_frame(m.Frame(0, 0x13, b"")))      # not a UI frame
        self.assertIsNone(m.decode_rpc_frame(m.Frame(7, 3, struct.pack("<II", 1, 64))))  # poll port
        self.assertIsNone(m.decode_rpc_frame(m.Frame(0, 3, b"\x0b")))     # undecodable


class Settings(unittest.TestCase):
    def rsp(self, sv):
        return m.decode_setting_value(m.f_bytes(4, sv))

    def test_bools_including_false(self):
        for name, sid in m.BOOL_SETTINGS.items():
            self.assertEqual(self.rsp(m.sv_bool(sid, True)), (name, True))
            self.assertEqual(self.rsp(m.sv_bool(sid, False)), (name, False))
        self.assertIsNone(self.rsp(m.f_varint(m.S_OHD, 7)))      # not a bool

    def test_anc(self):
        for mode in ("off", "active", "aware", "adaptive"):
            self.assertEqual(self.rsp(m.sv_anc(mode)), ("anc", mode))
        self.assertEqual(self.rsp(m.f_varint(m.S_ANC, 99)), ("anc", "unknown"))
        self.assertEqual(m.sv_anc("off"), b"\x68\x01")

    def test_gesture_control(self):
        self.assertEqual(self.rsp(m.sv_gesture_control("anc", "assistant")),
                         ("gesture-control", {"left": "anc", "right": "assistant"}))
        weird = m.f_bytes(7, m.f_bytes(1, m.f_bytes(4, m.f_varint(1, 3))))
        self.assertEqual(self.rsp(weird), ("gesture-control", {"left": "other", "right": "other"}))

    def test_loop(self):
        self.assertEqual(self.rsp(m.sv_anc_gesture_loop(["aware", "off", "adaptive"])),
                         ("anc-gesture-loop", ["off", "aware", "adaptive"]))
        self.assertEqual(m.sv_anc_gesture_loop(["active", "aware"]), m.f_bytes(12, b"\x08\x01\x18\x01"))

    def test_balance(self):  # (pbpctrl) settings.rs volume asymmetry round trip
        for raw in range(201):
            self.assertEqual(m.balance_to_raw(m.balance_from_raw(raw)), raw)
        self.assertEqual(m.balance_from_raw(0), 0)
        self.assertEqual(m.balance_from_raw(1), 1)
        self.assertEqual(m.balance_from_raw(2), -1)
        for v in (-100, -20, 0, 20, 100):
            self.assertEqual(self.rsp(m.sv_balance(v)), ("balance", v))
        self.assertIsNone(self.rsp(m.f_varint(m.S_BALANCE, 201)))
        self.assertIsNone(self.rsp(m.f_varint(m.S_BALANCE, -1)))

    def test_eq(self):
        self.assertEqual(self.rsp(m.sv_eq([0, 1.5, -2, 0.5, 3])), ("eq", [0.0, 1.5, -2.0, 0.5, 3.0]))
        self.assertEqual(self.rsp(m.sv_eq([9, -9, 0, 0, 0])), ("eq", [6.0, -6.0, 0.0, 0.0, 0.0]))
        nan = m.f_bytes(m.S_EQ, m.f_float(1, float("nan")))
        self.assertIsNone(self.rsp(nan))
        self.assertIsNone(self.rsp(m.f_bytes(m.S_EQ, m.f_float(2, 40.0))))

    def test_oneof_must_be_single(self):
        self.assertIsNone(self.rsp(m.sv_bool(2, True) + m.sv_bool(11, True)))
        self.assertIsNone(m.decode_setting_value(b""))
        self.assertEqual(self.rsp(m.f_varint(30, 1)), ("unsupported", 30))


class Runtime(unittest.TestCase):
    def test_runtime(self):
        from support import runtime_payload
        rt = m.decode_runtime_info(runtime_payload())
        self.assertEqual(rt, {"case": 75, "case_state": "not charging", "left": 90,
                              "left_state": "not charging", "right": 85, "right_state": "charging",
                              "left_in_case": 0, "right_in_case": 1})

    def test_missing_and_hostile(self):
        from support import runtime_payload
        rt = m.decode_runtime_info(runtime_payload(case=None, left=(250, 9), right=(-3, 2)))
        self.assertEqual((rt["case"], rt["case_state"]), (-1, "unknown"))
        self.assertEqual((rt["left"], rt["left_state"]), (-1, "unknown"))
        self.assertEqual(rt["right"], -1)
        zero = m.decode_runtime_info(m.f_bytes(6, m.f_bytes(2, b"")))
        self.assertEqual(zero["left"], 0)     # present, level omitted == 0%


if __name__ == "__main__":
    unittest.main()
